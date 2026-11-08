"""Deterministic scoring engine. Pure Python, no LLM, no I/O.

Final = Σ(points_i / max_i × w_i) ÷ Σ w_i × 100 over applicable criteria,
computed in Decimal and rounded ROUND_HALF_UP once, at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import Literal, Optional

from .config_schema import NA, ChecklistConfig, CriterionConfig

getcontext().prec = 28
CENT = Decimal("0.01")
HUNDRED = Decimal(100)


class ScoringError(ValueError):
    pass


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LedgerRow:
    criterion_id: str
    name: str
    verdict: Optional[str]
    source: Literal["AI", "REVIEW", "MISSING"]
    status: Literal["SCORED", "NA", "VIOLATION", "MISSING", "ZERO_WEIGHT"]
    points: Optional[Decimal]
    max_points: Optional[Decimal]
    weight: Decimal                    # configured weight
    effective_weight: Decimal          # after N/A redistribution (sums to 100), display-rounded
    contribution: Decimal              # display-rounded share of the final score


@dataclass(frozen=True)
class ScoreResult:
    status: Literal["OK", "CANNOT_CALCULATE", "INCOMPLETE"]
    final_score: Optional[Decimal]
    applicable_weight: Decimal
    rows: list[LedgerRow]
    violations: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        def s(x):
            return None if x is None else str(x)
        return {
            "status": self.status, "final_score": s(self.final_score),
            "applicable_weight": str(self.applicable_weight), "note": self.note,
            "violations": self.violations, "missing": self.missing,
            "rows": [{**r.__dict__, "points": s(r.points), "max_points": s(r.max_points),
                      "weight": str(r.weight), "effective_weight": str(r.effective_weight),
                      "contribution": str(r.contribution)} for r in self.rows],
        }


def compute_score(checklist: ChecklistConfig, verdicts: dict[str, Optional[str]],
                  sources: Optional[dict[str, str]] = None) -> ScoreResult:
    """`verdicts`: criterion_id -> verdict value ("YES", "15", "N/A") or None if the
    judge failed. Unknown values raise ScoringError — never silently score 0."""
    sources = sources or {}
    crit = checklist.active
    unknown = set(verdicts) - {c.id for c in checklist.criteria}
    if unknown:
        raise ScoringError(f"verdicts for unknown criteria: {sorted(unknown)}")

    scored: list[tuple[CriterionConfig, str, Decimal]] = []
    pre_rows: list[tuple[CriterionConfig, Optional[str], str]] = []
    violations: list[dict] = []
    missing: list[str] = []

    for c in crit:
        v = verdicts.get(c.id)
        if v is None:
            missing.append(c.id)
            pre_rows.append((c, None, "MISSING"))
            continue
        if v not in c.allowed_verdicts:
            raise ScoringError(f"{c.id}: verdict {v!r} not in allowed {c.allowed_verdicts}")
        if c.kind == "violation":
            if v == "YES":
                violations.append({"criterion_id": c.id, "name": c.name})
            pre_rows.append((c, v, "VIOLATION"))
        elif v == NA:
            pre_rows.append((c, v, "NA"))
        elif c.weight == 0:
            pre_rows.append((c, v, "ZERO_WEIGHT"))
        else:
            fraction = c.option(v).points / c.max_points
            scored.append((c, v, fraction))
            pre_rows.append((c, v, "SCORED"))

    applicable_weight = sum((c.weight for c, _, _ in scored), Decimal(0))
    if missing:
        status, final, note = "INCOMPLETE", None, "Some criteria have no verdict yet."
    elif applicable_weight == 0:
        status, final, note = "CANNOT_CALCULATE", None, "No applicable weighted criteria remain."
    else:
        raw = sum((f * c.weight for c, _, f in scored), Decimal(0)) / applicable_weight * HUNDRED
        status, final, note = "OK", q2(raw), (
            "Contributions are rounded for display; the final score is computed from exact values.")

    fractions = {c.id: f for c, _, f in scored}
    rows: list[LedgerRow] = []
    for c, v, st in pre_rows:
        eff = (c.weight / applicable_weight * HUNDRED) if (st == "SCORED" and applicable_weight) else Decimal(0)
        f = fractions.get(c.id)
        rows.append(LedgerRow(
            criterion_id=c.id, name=c.name, verdict=v,
            source=("MISSING" if v is None else sources.get(c.id, "AI")),  # type: ignore[arg-type]
            status=st,  # type: ignore[arg-type]
            points=(c.option(v).points if v not in (None, NA) and c.kind != "violation" else None),
            max_points=(c.max_points if c.kind != "violation" else None),
            weight=c.weight, effective_weight=q2(eff),
            contribution=q2(eff * f) if f is not None else Decimal("0.00"),
        ))
    return ScoreResult(status, final, applicable_weight, rows, violations, missing, note)  # type: ignore[arg-type]


def resolve_verdicts(ai: dict[str, Optional[str]], reviews: list[dict]) -> tuple[dict, dict]:
    """Apply reviewer decisions on top of AI verdicts. `reviews` are append-only rows
    {criterion_id, decision: confirm|change, new_verdict, created_at}; latest wins."""
    final = dict(ai)
    sources = {k: "AI" for k in ai}
    for r in sorted(reviews, key=lambda r: r["created_at"]):
        if r["decision"] == "change":
            final[r["criterion_id"]] = r["new_verdict"]
        sources[r["criterion_id"]] = "REVIEW"
    return final, sources
