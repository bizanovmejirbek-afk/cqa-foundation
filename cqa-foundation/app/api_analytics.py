"""Read-only analytics API: per-dialogue recommendations and the company priority ranking."""
from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .analytics import EvaluationIn, ResultIn, extract_violations, load_catalog, operator_view, priority_table
from .api_main import effective, latest_evaluations
from .config_schema import ChecklistConfig
from .db import get_session
from .models import Conversation, Evaluation

router = APIRouter(prefix="/api/analytics")


def _to_input(s: Session, ev: Evaluation) -> EvaluationIn:
    conv = s.get(Conversation, ev.conversation_id)
    cfg = ChecklistConfig.model_validate(ev.checklist_version.config_json)
    results = []
    for r in ev.results:
        verdict, source = effective(r)
        results.append(ResultIn(
            criterion_id=r.criterion_id, verdict=verdict, source=source,
            sub_checks=r.sub_checks_json or [],
            evidence=[{"message_index": e.message_index, "quote": e.quote, "matched_text": e.matched_text,
                       "status": e.status.value} for e in r.evidence if e.sub_check_id is None],
            missing_after_index=r.missing_after_index, missing_description=r.missing_description,
            reasoning=r.reasoning))
    snap = ev.snapshots[-1] if ev.snapshots else None
    return EvaluationIn(conversation_id=ev.conversation_id, checklist=cfg, results=results,
                        final_score=snap.final_score if snap else None,
                        operator_id=getattr(conv, "operator_id", None) if conv else None,
                        channel=conv.channel if conv else None, language=conv.language if conv else None)


def _inputs(s: Session, channel: Optional[str]) -> list[EvaluationIn]:
    out = [_to_input(s, ev) for ev in latest_evaluations(s).values()]
    if channel:
        out = [e for e in out if channel.lower() in (e.channel or "").lower()]
    return out


@router.get("/company")
def company(channel: Optional[str] = None, s: Session = Depends(get_session)):
    """channel = 'chat' | 'voice' to filter; default all."""
    return priority_table(_inputs(s, channel), load_catalog())


@router.get("/operators")
def operators(channel: Optional[str] = None, s: Session = Depends(get_session)):
    return operator_view(_inputs(s, channel), load_catalog())


@router.get("/conversations/{conv_id}")
def conversation(conv_id: str, s: Session = Depends(get_session)):
    ev = latest_evaluations(s).get(conv_id)
    if ev is None:
        raise HTTPException(404, "conversation has no evaluation yet")
    item = _to_input(s, ev)
    scored, serious = extract_violations(item, load_catalog())
    return {"conversation_id": conv_id, "final_score": float(item.final_score) if item.final_score is not None else None,
            "violations": [v.to_dict() for v in scored], "serious": [v.to_dict() for v in serious]}


@router.get("/violations.csv")
def violations_csv(s: Session = Depends(get_session)):
    cat = load_catalog()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["conversation_id", "operator_id", "criterion_id", "violation_id", "violation", "lost_points",
                "criterion_lost_points", "split", "verdict", "confirmed", "source", "evidence", "reason",
                "recommendation"])
    for item in _inputs(s, None):
        sc, se = extract_violations(item, cat)
        for v in sc + se:
            w.writerow([v.conversation_id, v.operator_id or "", v.criterion_id, v.violation_id, v.violation,
                        v.lost_points, v.criterion_lost_points, v.split, v.verdict, v.confirmed, v.source,
                        " | ".join(f"#{q['message_index']} {q['quote']}" for q in v.evidence), v.reason,
                        v.recommendation])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=cqa_violations.csv"})
