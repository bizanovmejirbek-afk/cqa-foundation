"""SQLAlchemy 2.0 models (SQLite).

Design rules:
- Checklist versions are immutable: edits create a new row (ORM guard + SQL trigger).
- AI results are never overwritten. Reviewer decisions and score snapshots are
  append-only, so "AI original -> human override -> final" is always reconstructable.
- Money and scores are stored as TEXT-encoded Decimal: SQLite has no decimal type
  and SQLAlchemy's Numeric would round-trip through float.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, DDL, Enum, ForeignKey,
                        Index, Integer, String, Text, TypeDecorator, UniqueConstraint, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, object_session, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DecimalText(TypeDecorator):
    """Exact Decimal storage for SQLite."""
    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, Decimal: DecimalText}


class EvalStatus(str, enum.Enum):
    RUNNING = "RUNNING"; COMPLETED = "COMPLETED"; FAILED = "FAILED"


class ScoreStatus(str, enum.Enum):
    OK = "OK"; CANNOT_CALCULATE = "CANNOT_CALCULATE"; INCOMPLETE = "INCOMPLETE"


class MatchStatus(str, enum.Enum):
    EXACT = "EXACT"; FUZZY = "FUZZY"; UNVERIFIED = "UNVERIFIED"


class AppealStatus(str, enum.Enum):
    OPEN = "OPEN"; IN_REVIEW = "IN_REVIEW"; RESOLVED = "RESOLVED"


class Decision(str, enum.Enum):
    CONFIRM = "CONFIRM"; CHANGE = "CHANGE"


class SnapshotTrigger(str, enum.Enum):
    AI = "AI"; REVIEW = "REVIEW"; RECALC = "RECALC"


# --------------------------------------------------------------------------- #
# Checklists
# --------------------------------------------------------------------------- #
class Checklist(Base):
    __tablename__ = "checklists"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(10), default="any")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    versions: Mapped[list["ChecklistVersion"]] = relationship(
        back_populates="checklist", order_by="ChecklistVersion.version")


class ChecklistVersion(Base):
    __tablename__ = "checklist_versions"
    # no unique on config_hash: reverting to an earlier config must create a new version
    __table_args__ = (UniqueConstraint("checklist_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    checklist_id: Mapped[int] = mapped_column(ForeignKey("checklists.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, CheckConstraint("version >= 1"))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)          # validated ChecklistConfig
    config_hash: Mapped[str] = mapped_column(String(64))               # sha256 of canonical JSON
    parent_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("checklist_versions.id"))
    change_note: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checklist: Mapped[Checklist] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)      # organiser conversation_id
    channel: Mapped[str] = mapped_column(String(20))                   # "agent chat" | "voice call"
    language: Mapped[Optional[str]] = mapped_column(String(8))
    audio_url: Mapped[Optional[str]] = mapped_column(Text)
    transcript_json: Mapped[list[Any]] = mapped_column(JSON)           # original messages, untouched
    transcript_hash: Mapped[str] = mapped_column(String(64))
    source_group_id: Mapped[Optional[str]] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- #
# Evaluations and AI results
# --------------------------------------------------------------------------- #
class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (Index("ix_eval_conv_created", "conversation_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="RESTRICT"))
    checklist_version_id: Mapped[int] = mapped_column(ForeignKey("checklist_versions.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    prompt_template_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[EvalStatus] = mapped_column(Enum(EvalStatus), default=EvalStatus.RUNNING)
    ai_final_score: Mapped[Optional[Decimal]] = mapped_column(DecimalText)   # snapshot, never updated after COMPLETED
    ai_score_status: Mapped[Optional[ScoreStatus]] = mapped_column(Enum(ScoreStatus))
    violations_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship()
    checklist_version: Mapped[ChecklistVersion] = relationship()
    results: Mapped[list["CriterionResult"]] = relationship(back_populates="evaluation",
                                                            cascade="all, delete-orphan")
    snapshots: Mapped[list["ScoreSnapshot"]] = relationship(back_populates="evaluation",
                                                            order_by="ScoreSnapshot.id")


class CriterionResult(Base):
    """The AI's result for one criterion. Immutable once written."""
    __tablename__ = "criterion_results"
    __table_args__ = (UniqueConstraint("evaluation_id", "criterion_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id", ondelete="CASCADE"))
    criterion_id: Mapped[str] = mapped_column(String(64))
    criterion_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(12))
    weight: Mapped[Decimal] = mapped_column(DecimalText)
    max_points: Mapped[Optional[Decimal]] = mapped_column(DecimalText)
    verdict: Mapped[Optional[str]] = mapped_column(String(32))          # None = judge failed
    points: Mapped[Optional[Decimal]] = mapped_column(DecimalText)
    llm_verdict: Mapped[Optional[str]] = mapped_column(String(32))      # before sub-check derivation
    derived_from_subchecks: Mapped[bool] = mapped_column(Boolean, default=False)
    sub_checks_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    na_reason: Mapped[Optional[str]] = mapped_column(Text)
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]]
    missing_after_index: Mapped[Optional[int]]                          # -1 = before first message
    missing_description: Mapped[Optional[str]] = mapped_column(Text)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    issues_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    raw_response_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    evaluation: Mapped[Evaluation] = relationship(back_populates="results")
    evidence: Mapped[list["EvidenceSpan"]] = relationship(back_populates="result",
                                                          cascade="all, delete-orphan")
    appeals: Mapped[list["Appeal"]] = relationship(back_populates="result")
    decisions: Mapped[list["ReviewDecision"]] = relationship(back_populates="result",
                                                             order_by="ReviewDecision.id")


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"
    id: Mapped[int] = mapped_column(primary_key=True)
    criterion_result_id: Mapped[int] = mapped_column(ForeignKey("criterion_results.id", ondelete="CASCADE"))
    sub_check_id: Mapped[Optional[str]] = mapped_column(String(64))     # None = main evidence
    cited_message_index: Mapped[int]
    message_index: Mapped[Optional[int]]                                # resolved index (may differ)
    char_start: Mapped[Optional[int]]
    char_end: Mapped[Optional[int]]
    quote: Mapped[str] = mapped_column(Text)                            # what the LLM claimed
    matched_text: Mapped[Optional[str]] = mapped_column(Text)           # what the transcript says
    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus))
    match_score: Mapped[float] = mapped_column(default=0.0)
    speaker: Mapped[Optional[str]] = mapped_column(String(16))
    index_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[Optional[str]] = mapped_column(String(40))
    result: Mapped[CriterionResult] = relationship(back_populates="evidence")


# --------------------------------------------------------------------------- #
# Human-in-the-loop audit trail (append-only)
# --------------------------------------------------------------------------- #
class Appeal(Base):
    __tablename__ = "appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    criterion_result_id: Mapped[int] = mapped_column(ForeignKey("criterion_results.id", ondelete="RESTRICT"))
    author: Mapped[str] = mapped_column(String(80))
    comment: Mapped[str] = mapped_column(Text, CheckConstraint("length(trim(comment)) > 0"))
    requested_verdict: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[AppealStatus] = mapped_column(Enum(AppealStatus), default=AppealStatus.OPEN)
    locked_by: Mapped[Optional[str]] = mapped_column(String(80))
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    result: Mapped[CriterionResult] = relationship(back_populates="appeals")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    criterion_result_id: Mapped[int] = mapped_column(ForeignKey("criterion_results.id", ondelete="RESTRICT"))
    appeal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("appeals.id", ondelete="RESTRICT"))
    reviewer: Mapped[str] = mapped_column(String(80))
    decision: Mapped[Decision] = mapped_column(Enum(Decision))
    old_verdict: Mapped[Optional[str]] = mapped_column(String(32))      # effective verdict before
    new_verdict: Mapped[Optional[str]] = mapped_column(String(32))      # effective verdict after
    old_points: Mapped[Optional[Decimal]] = mapped_column(DecimalText)
    new_points: Mapped[Optional[Decimal]] = mapped_column(DecimalText)
    reason: Mapped[str] = mapped_column(Text, CheckConstraint("length(trim(reason)) > 0"))
    blind_mode: Mapped[bool] = mapped_column(Boolean, default=False)    # AI verdict hidden while deciding
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result: Mapped[CriterionResult] = relationship(back_populates="decisions")


class ScoreSnapshot(Base):
    """Every (re)calculation of the final score. Consecutive rows = old vs new."""
    __tablename__ = "score_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id", ondelete="CASCADE"))
    trigger: Mapped[SnapshotTrigger] = mapped_column(Enum(SnapshotTrigger))
    review_decision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("review_decisions.id"))
    previous_snapshot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("score_snapshots.id"))
    final_score: Mapped[Optional[Decimal]] = mapped_column(DecimalText)
    score_status: Mapped[ScoreStatus] = mapped_column(Enum(ScoreStatus))
    ledger_json: Mapped[dict[str, Any]] = mapped_column(JSON)           # ScoreResult.to_dict()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluation: Mapped[Evaluation] = relationship(back_populates="snapshots")


# --------------------------------------------------------------------------- #
# Immutability guards: ORM level (clear errors) + SQL triggers (defence in depth)
# --------------------------------------------------------------------------- #
class ImmutableRecordError(RuntimeError):
    pass


def _forbid(op: str):
    def _guard(mapper, connection, target):
        if op == "update":
            # SQLAlchemy also calls before_update when only a relationship collection changed
            # (e.g. a ReviewDecision appended to result.decisions). Block real column edits only.
            sess = object_session(target)
            if sess is not None and not sess.is_modified(target, include_collections=False):
                return
        raise ImmutableRecordError(f"{type(target).__name__} rows are append-only ({op} blocked)")
    return _guard


for _cls in (ChecklistVersion, CriterionResult, EvidenceSpan, ReviewDecision, ScoreSnapshot):
    event.listen(_cls, "before_update", _forbid("update"))
for _cls in (ChecklistVersion, ReviewDecision, ScoreSnapshot):
    event.listen(_cls, "before_delete", _forbid("delete"))

for _table in ("checklist_versions", "review_decisions", "score_snapshots"):
    event.listen(Base.metadata, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS trg_{_table}_no_update BEFORE UPDATE ON {_table} "
        f"BEGIN SELECT RAISE(ABORT, '{_table} is append-only'); END;"))
    event.listen(Base.metadata, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS trg_{_table}_no_delete BEFORE DELETE ON {_table} "
        f"BEGIN SELECT RAISE(ABORT, '{_table} is append-only'); END;"))
