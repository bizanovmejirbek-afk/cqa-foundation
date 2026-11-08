"""Conversations, evaluations, appeals and review API."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .accuracy import compute_accuracy, reference_for
from .config_schema import ChecklistConfig
from .db import get_session
from .llm_client import LLMError, make_client
from .models import (Appeal, AppealStatus, Checklist, ChecklistVersion, Conversation, CriterionResult,
                     Decision, Evaluation)
from .services import ServiceError, apply_review, evaluate_conversation, file_appeal, lock_appeal
from .source_api import SourceError, fetch_records, import_records, load_file

router = APIRouter(prefix="/api")
ROOT = Path(__file__).parent.parent


def _s(x):
    return None if x is None else str(x)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def latest_version(s: Session, slug: str) -> ChecklistVersion:
    v = s.scalar(select(ChecklistVersion).join(Checklist).where(Checklist.slug == slug)
                 .order_by(ChecklistVersion.version.desc()).limit(1))
    if v is None:
        raise HTTPException(404, f"checklist '{slug}' not found")
    return v


def default_slug(conv: Conversation) -> str:
    return "official-voice" if "voice" in (conv.channel or "").lower() else "official-chat"


def effective(r: CriterionResult) -> tuple[Optional[str], str]:
    if r.decisions:
        return r.decisions[-1].new_verdict, "REVIEW"
    return r.verdict, "AI"


def result_dict(r: CriterionResult) -> dict:
    verdict, source = effective(r)
    return {
        "id": r.id, "criterion_id": r.criterion_id, "name": r.criterion_name, "kind": r.kind,
        "weight": _s(r.weight), "max_points": _s(r.max_points),
        "ai_verdict": r.verdict, "verdict": verdict, "source": source, "points": _s(r.points),
        "llm_verdict": r.llm_verdict, "derived_from_subchecks": r.derived_from_subchecks,
        "sub_checks": r.sub_checks_json, "na_reason": r.na_reason, "reasoning": r.reasoning,
        "confidence": r.confidence, "needs_review": r.needs_review, "issues": r.issues_json,
        "missing": ({"after_message_index": r.missing_after_index, "description": r.missing_description}
                    if r.missing_after_index is not None or r.missing_description else None),
        "evidence": [{"message_index": e.message_index, "cited_index": e.cited_message_index,
                      "char_start": e.char_start, "char_end": e.char_end, "quote": e.quote,
                      "matched_text": e.matched_text, "status": e.status.value, "score": e.match_score,
                      "speaker": e.speaker, "index_corrected": e.index_corrected, "reason": e.reason}
                     for e in r.evidence],
        "appeals": [{"id": a.id, "author": a.author, "comment": a.comment, "status": a.status.value,
                     "requested_verdict": a.requested_verdict, "locked_by": a.locked_by,
                     "created_at": a.created_at.isoformat()} for a in r.appeals],
        "decisions": [{"id": d.id, "reviewer": d.reviewer, "decision": d.decision.value,
                       "old_verdict": d.old_verdict, "new_verdict": d.new_verdict,
                       "reason": d.reason, "blind_mode": d.blind_mode,
                       "created_at": d.created_at.isoformat()} for d in r.decisions],
    }


def eval_dict(ev: Evaluation) -> dict:
    snap = ev.snapshots[-1] if ev.snapshots else None
    cv = ev.checklist_version
    cfg = ChecklistConfig.model_validate(cv.config_json)
    allowed = {c.id: c.allowed_verdicts for c in cfg.criteria}
    return {
        "id": ev.id, "conversation_id": ev.conversation_id, "status": ev.status.value,
        "checklist": {"slug": cv.checklist.slug, "name": cv.checklist.name, "version": cv.version},
        "provider": ev.provider, "model": ev.model, "tokens_in": ev.tokens_in, "tokens_out": ev.tokens_out,
        "cost_usd": _s(ev.cost_usd), "duration_ms": ev.duration_ms, "created_at": ev.created_at.isoformat(),
        "ai_final_score": _s(ev.ai_final_score),
        "final_score": _s(snap.final_score) if snap else None,
        "score_status": snap.score_status.value if snap else None,
        "ledger": snap.ledger_json if snap else None,
        "history": [{"id": x.id, "trigger": x.trigger.value, "final_score": _s(x.final_score),
                     "review_decision_id": x.review_decision_id, "created_at": x.created_at.isoformat()}
                    for x in ev.snapshots],
        "results": [{**result_dict(r), "allowed": allowed.get(r.criterion_id, [])} for r in ev.results],
    }


def latest_evaluations(s: Session) -> dict[str, Evaluation]:
    out: dict[str, Evaluation] = {}
    for ev in s.scalars(select(Evaluation).order_by(Evaluation.id)):
        out[ev.conversation_id] = ev
    return out


def run_one(s: Session, conv_id: str, slug: Optional[str]) -> Evaluation:
    conv = s.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, f"conversation '{conv_id}' not found")
    version = latest_version(s, slug or default_slug(conv))
    client = make_client()  # one client per evaluation -> its own token count and cost
    ev = evaluate_conversation(s, conv.id, version, client, client.provider, client.model)
    ev.tokens_in, ev.tokens_out, ev.cost_usd = client.tokens_in, client.tokens_out, client.cost_usd
    s.commit()
    return ev


# --------------------------------------------------------------------------- #
# Status and data import
# --------------------------------------------------------------------------- #
@router.get("/status")
def status(s: Session = Depends(get_session)):
    c = make_client()
    return {"llm_provider": c.provider, "llm_model": c.model,
            "source_api_configured": bool(os.getenv("CQA_API_URL")),
            "conversations": len(s.scalars(select(Conversation.id)).all())}


@router.get("/llm/test")
def llm_test():
    """One tiny call to the configured LLM; shows the exact error if it fails."""
    c = make_client()
    info = {"provider": c.provider, "model": c.model,
            "base_url": getattr(c, "url", None), "key_set": bool(os.getenv("LLM_API_KEY"))}
    if c.provider == "mock":
        return {**info, "ok": False, "error": "Mock mode: .env not loaded or LLM settings empty "
                "(need LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL; restart the server after editing .env)."}
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "language": {"type": "string"}},
              "required": ["ok", "language"]}
    try:
        raw = c.complete_json("Reply only with JSON.",
                              'Which language is this: "Сәлеметсіз бе, менің атым Айгерим"? '
                              'Return {"ok": true, "language": "<name>"}.', schema)
        return {**info, "ok": True, "answer": raw, "tokens_in": c.tokens_in, "tokens_out": c.tokens_out}
    except LLMError as e:
        return {**info, "ok": False, "error": str(e)}


class SyncIn(BaseModel):
    group_id: Optional[str] = None


@router.post("/sync")
def sync(body: SyncIn, s: Session = Depends(get_session)):
    records = fetch_records(body.group_id)
    return {"received": len(records), **import_records(s, records, body.group_id or os.getenv("CQA_GROUP_ID"))}


@router.post("/import-sample")
def import_sample(s: Session = Depends(get_session)):
    return import_records(s, load_file(ROOT / "data" / "sample_conversations.json"), "demo")


@router.post("/import-dev")
def import_dev(s: Session = Depends(get_session)):
    path = ROOT / "data" / "dev_conversations.json"
    if not path.exists():
        raise HTTPException(404, "data/dev_conversations.json not found (run: python -m tools.import_xlsx <file.xlsx>)")
    return import_records(s, load_file(path), "dev-65")


def _ai_match(ev: Evaluation) -> tuple[int, int]:
    ref = reference_for(ev.conversation_id)
    if not ref:
        return 0, 0
    pairs = [(r.verdict, ref["verdicts"].get(r.criterion_id)) for r in ev.results]
    pairs = [(a, b) for a, b in pairs if b is not None]
    return sum(a == b for a, b in pairs), len(pairs)


@router.get("/accuracy/export.csv")
def accuracy_export(only_mismatches: bool = True, s: Session = Depends(get_session)):
    """CSV of AI vs reference per criterion, with the AI's reasoning and evidence, for error analysis."""
    import csv
    import io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["conversation_id", "language", "channel", "criterion_id", "reference", "ai_verdict",
                "llm_verdict", "confidence", "na_reason", "missing", "evidence", "reasoning", "issues"])
    for conv_id, ev in sorted(latest_evaluations(s).items()):
        ref = reference_for(conv_id)
        if not ref:
            continue
        conv = s.get(Conversation, conv_id)
        for r in ev.results:
            gold = ref["verdicts"].get(r.criterion_id)
            if gold is None or (only_mismatches and gold == r.verdict):
                continue
            ev_txt = " | ".join(f"#{e.message_index}[{e.status.value}] {e.matched_text or e.quote}" for e in r.evidence)
            miss = f"after #{r.missing_after_index}: {r.missing_description}" if r.missing_description else ""
            w.writerow([conv_id, conv.language if conv else "", conv.channel if conv else "", r.criterion_id,
                        gold, r.verdict, r.llm_verdict, r.confidence, r.na_reason or "", miss, ev_txt,
                        r.reasoning or "", "; ".join(r.issues_json or [])])
    data = "\ufeff" + buf.getvalue()  # BOM so Excel opens Cyrillic/Kazakh correctly
    return Response(content=data.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=cqa_mismatches.csv"})


@router.get("/accuracy")
def accuracy(s: Session = Depends(get_session)):
    rows = []
    for conv_id, ev in latest_evaluations(s).items():
        conv = s.get(Conversation, conv_id)
        rows.append({"conversation_id": conv_id, "language": conv.language if conv else None,
                     "channel": conv.channel if conv else None,
                     "final_score": float(ev.ai_final_score) if ev.ai_final_score is not None else None,
                     "verdicts": {r.criterion_id: r.verdict for r in ev.results},
                     "cost_usd": float(ev.cost_usd or 0)})
    out = compute_accuracy(rows)
    costs = [r["cost_usd"] for r in rows if r["cost_usd"]]
    out["avg_cost_usd"] = round(sum(costs) / len(costs), 6) if costs else None
    return out


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
@router.get("/conversations")
def list_conversations(s: Session = Depends(get_session)):
    latest = latest_evaluations(s)
    rows = []
    for c in s.scalars(select(Conversation).order_by(Conversation.id)):
        ev = latest.get(c.id)
        item = {"id": c.id, "channel": c.channel, "language": c.language,
                "messages": len(c.transcript_json or []), "has_audio": bool(c.audio_url), "evaluation": None}
        if ev:
            snap = ev.snapshots[-1] if ev.snapshots else None
            item["evaluation"] = {
                "id": ev.id, "final_score": _s(snap.final_score) if snap else None,
                "score_status": snap.score_status.value if snap else None,
                "verdicts": {r.criterion_id: effective(r)[0] for r in ev.results},
                "criteria": [{"id": r.criterion_id, "name": r.criterion_name, "kind": r.kind,
                              "max": _s(r.max_points)} for r in ev.results],
                "needs_review": sum(1 for r in ev.results if r.needs_review and not r.decisions),
                "violations": (snap.ledger_json or {}).get("violations", []) if snap else [],
                "match": dict(zip(("matched", "compared"), _ai_match(ev))),
            }
        ref = reference_for(c.id)
        item["reference_score"] = ref.get("final_score") if ref else None
        rows.append(item)
    return rows


@router.get("/conversations/{conv_id}")
def get_conversation(conv_id: str, s: Session = Depends(get_session)):
    c = s.get(Conversation, conv_id)
    if c is None:
        raise HTTPException(404, "conversation not found")
    ev = latest_evaluations(s).get(c.id)
    return {"id": c.id, "channel": c.channel, "language": c.language, "audio_url": c.audio_url,
            "transcript": c.transcript_json, "evaluation": eval_dict(ev) if ev else None,
            "reference": reference_for(c.id)}


# --------------------------------------------------------------------------- #
# Evaluations
# --------------------------------------------------------------------------- #
class EvalIn(BaseModel):
    conversation_id: str
    checklist_slug: Optional[str] = None


@router.post("/evaluations")
def create_evaluation(body: EvalIn, s: Session = Depends(get_session)):
    return eval_dict(run_one(s, body.conversation_id, body.checklist_slug))


class BatchIn(BaseModel):
    conversation_ids: list[str]
    checklist_slug: Optional[str] = None


@router.post("/evaluations/batch")
def batch_evaluations(body: BatchIn, s: Session = Depends(get_session)):
    out = []
    for cid in body.conversation_ids:
        try:
            ev = run_one(s, cid, body.checklist_slug)
            snap = ev.snapshots[-1]
            out.append({"conversation_id": cid, "evaluation_id": ev.id, "final_score": _s(snap.final_score),
                        "score_status": snap.score_status.value, "cost_usd": _s(ev.cost_usd)})
        except Exception as e:  # one bad conversation must not stop the control run
            s.rollback()
            out.append({"conversation_id": cid, "error": str(getattr(e, "detail", e))})
    return out


@router.get("/evaluations/{eval_id}")
def get_evaluation(eval_id: int, s: Session = Depends(get_session)):
    ev = s.get(Evaluation, eval_id)
    if ev is None:
        raise HTTPException(404, "evaluation not found")
    return eval_dict(ev)


# --------------------------------------------------------------------------- #
# Appeals and review
# --------------------------------------------------------------------------- #
class AppealIn(BaseModel):
    author: str = "user"
    comment: str
    requested_verdict: Optional[str] = None


@router.post("/results/{result_id}/appeals")
def create_appeal(result_id: int, body: AppealIn, s: Session = Depends(get_session)):
    a = file_appeal(s, result_id, body.author, body.comment, body.requested_verdict)
    return {"id": a.id, "status": a.status.value}


class LockIn(BaseModel):
    reviewer: str = "reviewer"


@router.post("/appeals/{appeal_id}/lock")
def lock(appeal_id: int, body: LockIn, s: Session = Depends(get_session)):
    a = lock_appeal(s, appeal_id, body.reviewer)
    return {"id": a.id, "status": a.status.value, "locked_by": a.locked_by,
            "locked_until": a.locked_until.isoformat() if a.locked_until else None}


class ReviewIn(BaseModel):
    reviewer: str = "reviewer"
    decision: Decision
    reason: str
    new_verdict: Optional[str] = None
    appeal_id: Optional[int] = None
    blind_mode: bool = False


@router.post("/results/{result_id}/reviews")
def review(result_id: int, body: ReviewIn, s: Session = Depends(get_session)):
    old = s.get(CriterionResult, result_id)
    if old is None:
        raise HTTPException(404, "result not found")
    prev = old.evaluation.snapshots[-1] if old.evaluation.snapshots else None
    snap = apply_review(s, result_id, body.reviewer, body.decision, body.reason, body.new_verdict,
                        body.appeal_id, body.blind_mode)
    return {"old_final_score": _s(prev.final_score) if prev else None,
            "new_final_score": _s(snap.final_score), "score_status": snap.score_status.value,
            "ledger": snap.ledger_json}


@router.get("/review-queue")
def review_queue(s: Session = Depends(get_session)):
    items = []
    for a in s.scalars(select(Appeal).where(Appeal.status != AppealStatus.RESOLVED).order_by(Appeal.id)):
        r = a.result
        items.append({"type": "APPEAL", "appeal_id": a.id, "result_id": r.id, "criterion": r.criterion_name,
                      "conversation_id": r.evaluation.conversation_id, "evaluation_id": r.evaluation_id,
                      "verdict": effective(r)[0], "comment": a.comment, "status": a.status.value,
                      "locked_by": a.locked_by})
    for ev in latest_evaluations(s).values():
        for r in ev.results:
            if r.needs_review and not r.decisions and not any(
                    a.status != AppealStatus.RESOLVED for a in r.appeals):
                items.append({"type": "AUTO_FLAG", "result_id": r.id, "criterion": r.criterion_name,
                              "conversation_id": ev.conversation_id, "evaluation_id": ev.id,
                              "verdict": r.verdict, "issues": r.issues_json, "confidence": r.confidence})
    return items
