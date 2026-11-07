"""Glue between config, guardrails, scoring and the database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config_schema import NA, ChecklistConfig
from .guardrails import CriterionVerdict
from .models import (Appeal, AppealStatus, Checklist, ChecklistVersion, CriterionResult, Decision,
                     EvalStatus, Evaluation, EvidenceSpan, MatchStatus, ReviewDecision, ScoreSnapshot,
                     ScoreStatus, SnapshotTrigger)
from .scoring import compute_score, resolve_verdicts


class ServiceError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Checklist versioning
# --------------------------------------------------------------------------- #
def save_checklist_version(s: Session, slug: str, config: ChecklistConfig,
                           change_note: str | None = None, author: str | None = None) -> ChecklistVersion:
    """Create a new immutable version, or return the latest one if nothing changed."""
    cl = s.scalar(select(Checklist).where(Checklist.slug == slug))
    if cl is None:
        cl = Checklist(slug=slug, name=config.name, channel=config.channel)
        s.add(cl); s.flush()
    latest = s.scalar(select(ChecklistVersion).where(ChecklistVersion.checklist_id == cl.id)
                      .order_by(ChecklistVersion.version.desc()).limit(1))
    h = config.content_hash()
    if latest and latest.config_hash == h:
        return latest
    v = ChecklistVersion(checklist_id=cl.id, version=(latest.version + 1 if latest else 1),
                         config_json=config.model_dump(mode="json"), config_hash=h,
                         parent_version_id=latest.id if latest else None,
                         change_note=change_note, created_by=author)
    s.add(v); s.commit()
    return v


def load_config(v: ChecklistVersion) -> ChecklistConfig:
    return ChecklistConfig.model_validate(v.config_json)


# --------------------------------------------------------------------------- #
# Persisting an AI evaluation
# --------------------------------------------------------------------------- #
def persist_evaluation(s: Session, ev: Evaluation, cfg: ChecklistConfig,
                       verdicts: dict[str, Optional[CriterionVerdict]]) -> Evaluation:
    """`verdicts[cid] = None` means the judge failed for that criterion."""
    for c in cfg.active:
        cv = verdicts.get(c.id)
        r = CriterionResult(
            criterion_id=c.id, criterion_name=c.name, kind=c.kind, weight=c.weight,
            max_points=c.max_points if c.kind != "violation" else None,
            prompt_hash=(cv.prompt_hash if cv else ""),
            verdict=cv.verdict if cv else None,
            points=(c.option(cv.verdict).points if cv and cv.verdict != NA and c.kind != "violation" else None),
            llm_verdict=cv.llm_verdict if cv else None,
            derived_from_subchecks=bool(cv and cv.derived_from_subchecks),
            sub_checks_json=cv.sub_checks if cv else [],
            na_reason=cv.na_reason if cv else None,
            reasoning=cv.reasoning if cv else "Judge failed to return valid output.",
            confidence=cv.confidence if cv else None,
            missing_after_index=(cv.missing_location or {}).get("after_message_index") if cv else None,
            missing_description=(cv.missing_location or {}).get("description") if cv else None,
            needs_review=(cv.needs_review if cv else True),
            issues_json=cv.issues if cv else ["JUDGE_FAILED"],
            attempts=cv.attempts if cv else 0,
            raw_response_json=({"raw": cv.raw_response} if cv else None),
        )
        for v in (cv.evidence if cv else []):
            r.evidence.append(EvidenceSpan(
                cited_message_index=v.cited_index, message_index=v.message_index,
                char_start=v.char_start, char_end=v.char_end, quote=v.quote,
                matched_text=v.matched_text, status=MatchStatus(v.status), match_score=v.score,
                speaker=v.speaker, index_corrected=v.index_corrected, reason=v.reason))
        ev.results.append(r)

    score = compute_score(cfg, {c.id: (verdicts[c.id].verdict if verdicts.get(c.id) else None)
                                for c in cfg.active})
    ev.ai_final_score = score.final_score
    ev.ai_score_status = ScoreStatus(score.status)
    ev.violations_json = score.violations
    ev.status = EvalStatus.COMPLETED
    s.add(ev); s.flush()
    ev.snapshots.append(ScoreSnapshot(trigger=SnapshotTrigger.AI, final_score=score.final_score,
                                      score_status=ScoreStatus(score.status), ledger_json=score.to_dict()))
    s.commit()
    return ev


# --------------------------------------------------------------------------- #
# Appeals and review
# --------------------------------------------------------------------------- #
LOCK_MINUTES = 10


def file_appeal(s: Session, result_id: int, author: str, comment: str,
                requested_verdict: str | None = None) -> Appeal:
    if not comment.strip():
        raise ServiceError("An appeal needs a comment.")
    a = Appeal(criterion_result_id=result_id, author=author, comment=comment.strip(),
               requested_verdict=requested_verdict)
    s.add(a); s.commit()
    return a


def lock_appeal(s: Session, appeal_id: int, reviewer: str) -> Appeal:
    a = s.get(Appeal, appeal_id)
    now = datetime.now(timezone.utc)
    if a is None or a.status == AppealStatus.RESOLVED:
        raise ServiceError("Appeal not found or already resolved.")
    if a.locked_by and a.locked_by != reviewer and a.locked_until and a.locked_until > now:
        raise ServiceError(f"Appeal is being reviewed by {a.locked_by}.")
    a.locked_by, a.locked_until, a.status = reviewer, now + timedelta(minutes=LOCK_MINUTES), AppealStatus.IN_REVIEW
    s.commit()
    return a


def _effective_verdicts(ev: Evaluation) -> tuple[dict, dict]:
    ai = {r.criterion_id: r.verdict for r in ev.results}
    reviews = [{"criterion_id": r.criterion_id,
                "decision": "change" if d.decision == Decision.CHANGE else "confirm",
                "new_verdict": d.new_verdict, "created_at": (d.created_at, d.id)}
               for r in ev.results for d in r.decisions]
    return resolve_verdicts(ai, reviews)


def apply_review(s: Session, result_id: int, reviewer: str, decision: Decision, reason: str,
                 new_verdict: str | None = None, appeal_id: int | None = None,
                 blind_mode: bool = False) -> ScoreSnapshot:
    """Append a decision, recalculate, and append a new score snapshot (old vs new)."""
    if not reason.strip():
        raise ServiceError("A review decision needs a reason.")
    r = s.get(CriterionResult, result_id)
    if r is None:
        raise ServiceError("Criterion result not found.")
    ev = r.evaluation
    cfg = load_config(ev.checklist_version)  # always the version the evaluation used
    crit = next(c for c in cfg.criteria if c.id == r.criterion_id)

    current, _ = _effective_verdicts(ev)
    old = current.get(r.criterion_id)
    if decision == Decision.CHANGE:
        if new_verdict not in crit.allowed_verdicts:
            raise ServiceError(f"Verdict must be one of {crit.allowed_verdicts}.")
        if new_verdict == old:
            raise ServiceError("New verdict equals the current one; use CONFIRM.")
    else:
        new_verdict = old

    def pts(v):
        return crit.option(v).points if v not in (None, NA) and crit.kind != "violation" else None

    d = ReviewDecision(criterion_result_id=r.id, appeal_id=appeal_id, reviewer=reviewer,
                       decision=decision, old_verdict=old, new_verdict=new_verdict,
                       old_points=pts(old), new_points=pts(new_verdict), reason=reason.strip(),
                       blind_mode=blind_mode)
    r.decisions.append(d)      # keeps the in-memory graph current for recalculation
    s.flush()

    verdicts, sources = _effective_verdicts(ev)
    score = compute_score(cfg, verdicts, sources)
    prev = ev.snapshots[-1] if ev.snapshots else None
    snap = ScoreSnapshot(evaluation_id=ev.id, trigger=SnapshotTrigger.REVIEW, review_decision_id=d.id,
                         previous_snapshot_id=prev.id if prev else None, final_score=score.final_score,
                         score_status=ScoreStatus(score.status), ledger_json=score.to_dict())
    ev.snapshots.append(snap)
    if appeal_id:
        a = s.get(Appeal, appeal_id)
        if a and a.criterion_result_id == r.id:
            a.status, a.resolved_at, a.locked_by = AppealStatus.RESOLVED, datetime.now(timezone.utc), None
    s.commit()
    return snap


# --------------------------------------------------------------------------- #
# Running an evaluation
# --------------------------------------------------------------------------- #
def evaluate_conversation(s: Session, conversation_id: str, version: ChecklistVersion, client,
                          provider: str, model: str, max_workers: int | None = None) -> Evaluation:
    """Judge every enabled criterion in parallel; a judge failure becomes
    JUDGE_FAILED + needs_review instead of an invented verdict."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from .guardrails import PROMPT_TEMPLATE_VERSION, GuardrailError, judge_criterion
    from .models import Conversation

    conv = s.get(Conversation, conversation_id)
    if conv is None:
        raise ServiceError("Conversation not found.")
    cfg = load_config(version)
    channel = "voice" if "voice" in conv.channel.lower() else "chat"
    t0 = time.perf_counter()

    def run(c):
        try:
            return c.id, judge_criterion(c, conv.transcript_json, client, channel)
        except GuardrailError:
            return c.id, None

    import os
    workers = max_workers or int(os.getenv("LLM_MAX_WORKERS", "8"))  # local models: 1-2
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        verdicts = dict(pool.map(run, cfg.active))

    ev = Evaluation(conversation_id=conv.id, checklist_version_id=version.id, provider=provider,
                    model=model, prompt_template_version=PROMPT_TEMPLATE_VERSION,
                    duration_ms=int((time.perf_counter() - t0) * 1000))
    return persist_evaluation(s, ev, cfg, verdicts)
