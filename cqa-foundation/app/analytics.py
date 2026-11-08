"""AI Recommendations + Violation Priority Analytics.

Read-only layer on top of finished evaluations: it never changes verdicts, weights or the
final score. Everything here is deterministic Python.

Definitions (see README):
  violation type   = a failed sub-check of a criterion ("c4_consultation.unanswered"), or the
                     criterion itself when no sub-check explains the lost points
  Lost Points      = weight x (max option points - earned option points) / max option points,
                     i.e. points lost BEFORE N/A redistribution; split equally when several
                     sub-checks of one criterion failed in the same dialogue
  Frequency        = dialogues with the type / dialogues where the criterion is applicable x 100
  Priority Score   = average Lost Points x Frequency
  Serious violations (weight 0) are listed separately and never enter the formula.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from .config_schema import NA, ChecklistConfig

CATALOG_PATH = Path(__file__).parent.parent / "config" / "violation_catalog.json"
SYSTEMIC_MIN_DIALOGUES = 3      # a type seen in >= 3 dialogues is reported as recurring
TOP_N = 5
Q2 = Decimal("0.01")


def _r(x: Decimal) -> Decimal:
    return x.quantize(Q2, rounding=ROUND_HALF_UP)


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("types", {})
    except FileNotFoundError:
        return {}


@dataclass
class ResultIn:
    """One criterion result of one evaluation, already resolved to the effective verdict."""
    criterion_id: str
    verdict: Optional[str]                  # None = judge failed
    source: str = "AI"                      # "AI" | "REVIEW"
    sub_checks: list[dict] = field(default_factory=list)   # [{check_id, answer, evidence:[...]}]
    evidence: list[dict] = field(default_factory=list)     # [{message_index, quote, matched_text, status}]
    missing_after_index: Optional[int] = None
    missing_description: Optional[str] = None
    reasoning: Optional[str] = None


@dataclass
class EvaluationIn:
    conversation_id: str
    checklist: ChecklistConfig
    results: list[ResultIn]
    final_score: Optional[Decimal] = None
    operator_id: Optional[str] = None
    channel: Optional[str] = None
    language: Optional[str] = None


@dataclass
class Violation:
    conversation_id: str
    operator_id: Optional[str]
    criterion_id: str
    criterion_name: str
    violation_id: str
    violation: str
    lost_points: Decimal                    # share attributed to this type
    criterion_lost_points: Decimal          # total lost on the criterion in this dialogue
    split: bool                             # True if the criterion loss was shared between types
    verdict: str
    reason: str
    evidence: list[dict]
    confirmed: bool                         # backed by a verified quote, a missing-step location or a reviewer
    source: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lost_points"] = float(self.lost_points)
        d["criterion_lost_points"] = float(self.criterion_lost_points)
        return d


def _verified(spans: list[dict]) -> list[dict]:
    return [e for e in spans or [] if e.get("status") in ("EXACT", "FUZZY")]


def _quote_view(e: dict) -> dict:
    return {"message_index": e.get("message_index"), "quote": e.get("matched_text") or e.get("quote")}


def extract_violations(ev: EvaluationIn, catalog: dict[str, dict]) -> tuple[list[Violation], list[Violation]]:
    """Return (scored violations, serious violations) for one evaluation."""
    by_id = {r.criterion_id: r for r in ev.results}
    scored: list[Violation] = []
    serious: list[Violation] = []
    for c in ev.checklist.active:
        r = by_id.get(c.id)
        if r is None or r.verdict is None or r.verdict == NA:
            continue
        missing = None
        if r.missing_description or r.missing_after_index is not None:
            where = f"после #{r.missing_after_index}: " if r.missing_after_index is not None else ""
            missing = where + (r.missing_description or "")
        reason = missing or (r.reasoning or "").strip()[:400]

        if c.kind == "violation":
            if r.verdict == "YES":
                info = catalog.get(c.id, {})
                quotes = [_quote_view(e) for e in _verified(r.evidence)]
                serious.append(Violation(
                    ev.conversation_id, ev.operator_id, c.id, c.name, c.id, info.get("name", c.name),
                    Decimal(0), Decimal(0), False, r.verdict, reason, quotes,
                    bool(quotes) or r.source == "REVIEW", r.source, info.get("operator", "")))
            continue

        earned = c.option(r.verdict).points
        mx = c.max_points
        if mx <= 0 or earned >= mx:
            continue
        lost = _r(c.weight * (mx - earned) / mx)

        answers = {s.get("check_id"): s for s in r.sub_checks or []}
        failed = [s for s in c.sub_checks if s.on_fail == "fail" and s.id in answers
                  and answers[s.id].get("answer") != s.pass_answer]
        types = [(f"{c.id}.{s.id}", answers[s.id]) for s in failed] or [(c.id, None)]
        share = _r(lost / len(types))
        for vid, ans in types:
            info = catalog.get(vid) or catalog.get(c.id, {})
            spans = _verified((ans or {}).get("evidence", [])) or _verified(r.evidence)
            quotes = [_quote_view(e) for e in spans]
            confirmed = bool(quotes) or missing is not None or r.source == "REVIEW"
            scored.append(Violation(
                ev.conversation_id, ev.operator_id, c.id, c.name, vid, info.get("name", c.name),
                share, lost, len(types) > 1, r.verdict, reason, quotes, confirmed, r.source,
                info.get("operator", "")))
    return scored, serious


def _applicable(evals: list[EvaluationIn]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ev in evals:
        for r in ev.results:
            if r.verdict is not None and r.verdict != NA:
                out[r.criterion_id] = out.get(r.criterion_id, 0) + 1
    return out


def priority_table(evals: list[EvaluationIn], catalog: dict[str, dict]) -> dict[str, Any]:
    all_scored: list[Violation] = []
    all_serious: list[Violation] = []
    for ev in evals:
        sc, se = extract_violations(ev, catalog)
        all_scored += sc
        all_serious += se
    confirmed = [v for v in all_scored if v.confirmed]
    applicable = _applicable(evals)

    groups: dict[str, list[Violation]] = {}
    for v in confirmed:
        groups.setdefault(v.violation_id, []).append(v)

    rows = []
    for vid, vs in groups.items():
        per_dialogue: dict[str, Decimal] = {}
        for v in vs:                                   # one dialogue counted once per type
            per_dialogue[v.conversation_id] = per_dialogue.get(v.conversation_id, Decimal(0)) + v.lost_points
        count = len(per_dialogue)
        avg_lost = _r(sum(per_dialogue.values(), Decimal(0)) / count)
        denom = applicable.get(vs[0].criterion_id, 0)
        freq = _r(Decimal(count) * 100 / denom) if denom else Decimal(0)
        operators = {v.operator_id for v in vs if v.operator_id}
        if count < SYSTEMIC_MIN_DIALOGUES:
            pattern = "единичная"
        elif operators and len(operators) == 1:
            pattern = "повторяется у одного оператора"
        else:
            pattern = "повторяющаяся"
        info = catalog.get(vid) or catalog.get(vs[0].criterion_id, {})
        rows.append({
            "violation_id": vid, "violation": vs[0].violation,
            "criterion_id": vs[0].criterion_id, "criterion": vs[0].criterion_name,
            "count": count, "applicable": denom,
            "lost_points": float(avg_lost), "frequency": float(freq),
            "priority_score": float(_r(avg_lost * freq)),
            "split_share": any(v.split for v in vs),
            "pattern": pattern, "operators": len(operators) or None,
            "recommendation": info.get("operator", ""),
            "company_actions": info.get("company", []),
            "dialogues": sorted(per_dialogue),
        })
    rows.sort(key=lambda x: (-x["priority_score"], -x["count"], x["violation_id"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    serious_dialogues = sorted({v.conversation_id for v in all_serious})
    return {
        "analysed_dialogues": len(evals),
        "violations_total": len(confirmed) + len(all_serious),
        "dialogues_with_violations": len({v.conversation_id for v in confirmed} | set(serious_dialogues)),
        "unconfirmed_excluded": len(all_scored) - len(confirmed),
        "ranking": rows,
        "top3": rows[:3],
        "top5": rows[:TOP_N],
        "serious": [v.to_dict() for v in all_serious],
        "serious_dialogues": serious_dialogues,
        "management_recommendations": [
            {"rank": r["rank"], "violation": r["violation"], "pattern": r["pattern"],
             "actions": r["company_actions"] + ["Повторить оценку тех же типов диалогов после внедрения и сравнить Frequency."]}
            for r in rows[:TOP_N]],
        "method": {
            "lost_points": "weight × (max − earned) / max, до перераспределения N/A; при нескольких причинах в одном критерии делится поровну",
            "frequency": "диалоги с нарушением / диалоги, где критерий применим (без N/A) × 100",
            "priority_score": "средние Lost Points × Frequency",
            "confirmed": "есть проверенная цитата, указано место пропущенного действия или решение проверяющего",
            "pattern": f"повторяющаяся — не менее {SYSTEMIC_MIN_DIALOGUES} диалогов",
        },
    }


def operator_view(evals: list[EvaluationIn], catalog: dict[str, dict]) -> dict[str, Any]:
    """Per dialogue cards, plus per-operator repeated errors when operator_id exists."""
    dialogues = []
    by_op: dict[str, dict[str, int]] = {}
    for ev in evals:
        sc, se = extract_violations(ev, catalog)
        dialogues.append({
            "conversation_id": ev.conversation_id, "operator_id": ev.operator_id,
            "channel": ev.channel, "language": ev.language,
            "final_score": float(ev.final_score) if ev.final_score is not None else None,
            "lost_points": float(sum((v.lost_points for v in sc if v.confirmed), Decimal(0))),
            "violations": [v.to_dict() for v in sc],
            "serious": [v.to_dict() for v in se],
        })
        if ev.operator_id:
            counts = by_op.setdefault(ev.operator_id, {})
            for vid in {v.violation_id for v in sc if v.confirmed}:
                counts[vid] = counts.get(vid, 0) + 1
    operators = [{"operator_id": op, "dialogues": sum(1 for d in dialogues if d["operator_id"] == op),
                  "repeated": sorted(({"violation_id": k, "violation": (catalog.get(k) or {}).get("name", k), "count": n}
                                      for k, n in cnt.items()), key=lambda x: -x["count"])}
                 for op, cnt in sorted(by_op.items())]
    return {"dialogues": dialogues, "operators": operators,
            "operator_ids_available": bool(operators)}
