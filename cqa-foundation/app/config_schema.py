"""Checklist / criterion configuration — the single source of truth.

Everything the LLM is asked, every value it may return, and every number the
scoring engine uses is derived from these models. The Criterion Studio UI
produces exactly this JSON; nothing about a criterion lives in code.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
NA = "N/A"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OptionConfig(_Strict):
    """One allowed verdict, e.g. YES / NO, or a scale level such as "15"."""
    value: str = Field(min_length=1, max_length=32)
    points: Decimal = Field(ge=0)
    definition: str = Field(min_length=1, max_length=1000)

    @field_validator("value")
    @classmethod
    def _value_not_na(cls, v: str) -> str:
        if v.upper() in {"N/A", "NA"}:
            raise ValueError("N/A is not an option; enable it with na_allowed")
        return v


class SubCheck(_Strict):
    """CheckEval-style binary question. Answers are combined by Python, not the LLM."""
    id: str
    question: str = Field(min_length=3, max_length=500)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("sub-check id must be snake_case, 2-64 chars")
        return v


class CriterionConfig(_Strict):
    id: str
    name: str = Field(min_length=2, max_length=120)
    definition: str = Field(min_length=3, max_length=3000)
    kind: Literal["binary", "scale", "violation"]
    weight: Decimal = Field(ge=0)
    options: list[OptionConfig] = Field(min_length=2)
    na_allowed: bool = False
    na_condition: Optional[str] = None
    subject: Literal["operator"] = "operator"  # whose behaviour is judged
    evidence_required: bool = True
    sub_checks: list[SubCheck] = Field(default_factory=list)
    sub_check_rule: Optional[Literal["all", "any"]] = None
    pass_value: Optional[str] = None  # verdict when sub-checks pass
    fail_value: Optional[str] = None  # verdict when sub-checks fail
    channel_notes: dict[Literal["chat", "voice"], str] = Field(default_factory=dict)
    section: Optional[str] = None
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("criterion id must be snake_case, 2-64 chars")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "CriterionConfig":
        values = [o.value for o in self.options]
        if len(set(values)) != len(values):
            raise ValueError("option values must be unique")
        if self.na_allowed and not self.na_condition:
            raise ValueError("na_condition is required when na_allowed is true")
        if self.kind == "violation":
            if self.weight != 0:
                raise ValueError("violation criteria carry no weight (reported separately)")
            if set(values) != {"YES", "NO"}:
                raise ValueError("violation criteria use exactly YES (found) / NO (not found)")
            if self.na_allowed:
                raise ValueError("violation criteria cannot be N/A")
        else:
            if self.max_points <= 0:
                raise ValueError("at least one option must award points > 0")
        if self.kind == "binary" and len(values) != 2:
            raise ValueError("binary criteria need exactly two options")
        ids = [s.id for s in self.sub_checks]
        if len(set(ids)) != len(ids):
            raise ValueError("sub-check ids must be unique")
        if self.sub_checks:
            if self.kind != "binary":
                raise ValueError("sub-checks are supported for binary criteria only")
            if not (self.sub_check_rule and self.pass_value and self.fail_value):
                raise ValueError("sub-checks need sub_check_rule, pass_value and fail_value")
            if {self.pass_value, self.fail_value} - set(values):
                raise ValueError("pass_value / fail_value must be option values")
        return self

    # ---- derived helpers (used by guardrails and scoring) ----
    @property
    def max_points(self) -> Decimal:
        return max(o.points for o in self.options)

    @property
    def allowed_verdicts(self) -> list[str]:
        vals = [o.value for o in self.options]
        return vals + ([NA] if self.na_allowed else [])

    def option(self, value: str) -> OptionConfig:
        for o in self.options:
            if o.value == value:
                return o
        raise KeyError(value)

    def is_full_marks(self, value: str) -> bool:
        return self.option(value).points == self.max_points

    def is_positive(self, value: str) -> bool:
        """'Something happened' verdicts that must be backed by operator evidence."""
        if self.kind == "violation":
            return value == "YES"
        return self.option(value).points > 0


class ChecklistConfig(_Strict):
    name: str = Field(min_length=2, max_length=120)
    channel: Literal["chat", "voice", "any"] = "any"
    criteria: list[CriterionConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ChecklistConfig":
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion ids must be unique within a checklist")
        if not any(c.enabled and c.kind != "violation" and c.weight > 0 for c in self.criteria):
            raise ValueError("checklist needs at least one enabled weighted criterion")
        return self

    @property
    def active(self) -> list[CriterionConfig]:
        return [c for c in self.criteria if c.enabled]

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()
