"""Adapter for the organisers' GET API and for local JSON files (demo / control set)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from .models import Conversation

REQUIRED = ("conversation_id", "channel", "transcript")


class SourceError(RuntimeError):
    pass


def _extract_records(body: dict) -> list[dict]:
    """The case lists error / isSuccess / data / dataset_group_id / total_records / records
    but not their nesting, so accept records either inside `data` or at the top level."""
    if body.get("isSuccess") is False or body.get("error"):
        raise SourceError(f"API returned an error: {body.get('error')}")
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"]
    if isinstance(body.get("records"), list):
        return body["records"]
    if isinstance(data, list):
        return data
    raise SourceError(f"No records found in API response (keys: {sorted(body)})")


def fetch_records(group_id: str | None = None) -> list[dict]:
    url = os.getenv("CQA_API_URL")
    if not url:
        raise SourceError("CQA_API_URL is not set in .env")
    gid = group_id or os.getenv("CQA_GROUP_ID")
    if not gid:
        raise SourceError("group_id is missing (set CQA_GROUP_ID in .env or pass it)")
    r = httpx.get(url, params={"group_id": gid}, timeout=60)
    r.raise_for_status()
    return _extract_records(r.json())


def load_file(path: str | Path) -> list[dict]:
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    return body if isinstance(body, list) else _extract_records(body)


def import_records(s: Session, records: list[dict], group_id: str | None = None) -> dict:
    """Store records exactly as received. Same id + same transcript = skipped;
    same id + changed transcript = updated (organisers may fix data)."""
    added = updated = skipped = 0
    errors: list[str] = []
    for rec in records:
        missing = [k for k in REQUIRED if k not in rec]
        if missing:
            errors.append(f"{rec.get('conversation_id', '?')}: missing {missing}")
            continue
        transcript = rec["transcript"]
        h = hashlib.sha256(json.dumps(transcript, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cid = str(rec["conversation_id"])
        op = rec.get("operator_id") or rec.get("agent_id")
        op = str(op) if op not in (None, "") else None
        conv = s.get(Conversation, cid)
        if conv is not None and op and conv.operator_id != op:
            conv.operator_id = op
        if conv is None:
            s.add(Conversation(id=cid, channel=rec["channel"], language=rec.get("language"),
                               audio_url=rec.get("audio_url") or None, transcript_json=transcript,
                               transcript_hash=h, source_group_id=group_id, operator_id=op))
            added += 1
        elif conv.transcript_hash != h:
            conv.transcript_json, conv.transcript_hash = transcript, h
            conv.channel, conv.language = rec["channel"], rec.get("language")
            conv.audio_url = rec.get("audio_url") or None
            updated += 1
        else:
            skipped += 1
    s.commit()
    return {"added": added, "updated": updated, "skipped": skipped, "errors": errors}
