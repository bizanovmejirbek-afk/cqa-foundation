import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.config_schema import ChecklistConfig
from app.scoring import ScoringError, compute_score, resolve_verdicts

CFG = ChecklistConfig.model_validate(json.loads(
    (Path(__file__).parent.parent / "config/official_chat_v1.json").read_text()))

ALL = {"c1_contact": "YES", "c2_needs": "YES", "c3_identification": "YES", "c4_consultation": "20",
       "c5_empathy": "YES", "c6_management": "10", "c7_conclusion": "YES", "c8_violations": "NO"}


def test_perfect_score():
    r = compute_score(CFG, ALL)
    assert r.status == "OK" and r.final_score == Decimal("100.00")


def test_case_examples_and_na_redistribution():
    v = {**ALL, "c2_needs": "N/A", "c3_identification": "N/A", "c4_consultation": "15", "c6_management": "7"}
    r = compute_score(CFG, v)
    assert r.final_score == Decimal("85.38")           # 55.5 / 65 * 100
    rows = {x.criterion_id: x for x in r.rows}
    assert rows["c2_needs"].status == "NA" and rows["c2_needs"].effective_weight == Decimal("0.00")
    assert sum(x.effective_weight for x in r.rows) == Decimal("100.00")


def test_official_contribution_examples():
    # 15/20 of weight 20 -> 15 points; 7/10 of weight 15 -> 10.5 points (all criteria applicable)
    r = compute_score(CFG, {**ALL, "c4_consultation": "15", "c6_management": "7"})
    rows = {x.criterion_id: x for x in r.rows}
    assert rows["c4_consultation"].contribution == Decimal("15.00")
    assert rows["c6_management"].contribution == Decimal("10.50")
    assert r.final_score == Decimal("90.50")


def test_violation_reported_separately_never_in_score():
    r = compute_score(CFG, {**ALL, "c8_violations": "YES"})
    assert r.final_score == Decimal("100.00")
    assert r.violations == [{"criterion_id": "c8_violations", "name": "Serious violations"}]


def test_all_na_cannot_calculate():
    na = {k: ("N/A" if k in {"c2_needs", "c3_identification", "c4_consultation", "c7_conclusion"} else v)
          for k, v in ALL.items()}
    only_na = ChecklistConfig.model_validate({**CFG.model_dump(mode="json"), "criteria": [
        c for c in CFG.model_dump(mode="json")["criteria"] if c["na_allowed"] or c["kind"] == "violation"]})
    r = compute_score(only_na, {k: v for k, v in na.items() if k in {c.id for c in only_na.criteria}})
    assert r.status == "CANNOT_CALCULATE" and r.final_score is None


def test_missing_verdict_is_incomplete_not_zero():
    v = dict(ALL); v["c5_empathy"] = None
    r = compute_score(CFG, v)
    assert r.status == "INCOMPLETE" and r.final_score is None and r.missing == ["c5_empathy"]


@pytest.mark.parametrize("bad", [{"c4_consultation": "12"}, {"c1_contact": "N/A"}, {"c1_contact": "yes"}])
def test_illegal_verdicts_raise(bad):
    with pytest.raises(ScoringError):
        compute_score(CFG, {**ALL, **bad})


def _two_criteria(w_yes: int, w_no: int) -> ChecklistConfig:
    opt = [{"value": "YES", "points": 1, "definition": "done"}, {"value": "NO", "points": 0, "definition": "not"}]
    return ChecklistConfig.model_validate({"name": "test", "criteria": [
        {"id": "aa", "name": "Crit A", "definition": "a a", "kind": "binary", "weight": w_yes, "options": opt},
        {"id": "bb", "name": "Crit B", "definition": "b b", "kind": "binary", "weight": w_no, "options": opt}]})


def test_round_half_up_not_bankers():
    r = compute_score(_two_criteria(1, 799), {"aa": "YES", "bb": "NO"})   # exact value 0.125
    assert round(0.125, 2) == 0.12                                         # the float trap
    assert r.final_score == Decimal("0.13")


def test_float_representation_trap():
    r = compute_score(_two_criteria(107, 3893), {"aa": "YES", "bb": "NO"})  # exact value 2.675
    assert round(107 / 4000 * 100, 2) == 2.67                               # float gets it wrong
    assert r.final_score == Decimal("2.68")


def test_reviewer_override_latest_wins():
    ai = dict(ALL)
    reviews = [{"criterion_id": "c5_empathy", "decision": "change", "new_verdict": "NO", "created_at": 1},
               {"criterion_id": "c5_empathy", "decision": "change", "new_verdict": "YES", "created_at": 2},
               {"criterion_id": "c7_conclusion", "decision": "change", "new_verdict": "N/A", "created_at": 3}]
    final, src = resolve_verdicts(ai, reviews)
    assert final["c5_empathy"] == "YES" and src["c7_conclusion"] == "REVIEW"
    assert compute_score(CFG, final, src).final_score == Decimal("100.00")
