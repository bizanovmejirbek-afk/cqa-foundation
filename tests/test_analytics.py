from decimal import Decimal
from pathlib import Path

from app.analytics import EvaluationIn, ResultIn, extract_violations, load_catalog, priority_table
from app.config_schema import ChecklistConfig

CFG = ChecklistConfig.model_validate_json(
    (Path(__file__).parent.parent / "config" / "official_chat_v1.json").read_text(encoding="utf-8"))
CAT = load_catalog()
OK = {"status": "EXACT", "message_index": 9, "quote": "q", "matched_text": "q"}


def full(**over):
    """All criteria at full marks / not violated, with optional overrides {cid: ResultIn}."""
    base = {"c1_contact": "YES", "c2_needs": "YES", "c3_identification": "N/A", "c4_consultation": "20",
            "c5_empathy": "YES", "c6_management": "10", "c7_conclusion": "YES", "c8_violations": "NO"}
    rs = [over.get(cid) or ResultIn(cid, v, evidence=[OK]) for cid, v in base.items()]
    return rs


def ev(cid, results):
    return EvaluationIn(conversation_id=cid, checklist=CFG, results=results)


def test_lost_points_scale_and_binary():
    r6 = ResultIn("c6_management", "7", sub_checks=[{"check_id": "unclear", "answer": "YES", "evidence": [OK]},
                                                    {"check_id": "unwarned_wait", "answer": "NO", "evidence": []},
                                                    {"check_id": "incoherent", "answer": "NO", "evidence": []}])
    r7 = ResultIn("c7_conclusion", "NO", sub_checks=[{"check_id": "goodbye", "answer": "NO", "evidence": []}],
                  missing_after_index=12, missing_description="no goodbye")
    sc, se = extract_violations(ev("d1", full(c6_management=r6, c7_conclusion=r7)), CAT)
    got = {v.violation_id: v.lost_points for v in sc}
    assert got == {"c6_management.unclear": Decimal("4.50"),      # 15 x (10-7)/10
                   "c7_conclusion.goodbye": Decimal("5.00")}      # binary NO loses the full weight
    assert all(v.confirmed for v in sc) and se == []


def test_split_between_two_failed_subchecks_is_not_double_counted():
    r4 = ResultIn("c4_consultation", "10", evidence=[OK],
                  sub_checks=[{"check_id": "unanswered", "answer": "YES", "evidence": [OK]},
                              {"check_id": "incorrect", "answer": "YES", "evidence": [OK]}])
    sc, _ = extract_violations(ev("d1", full(c4_consultation=r4)), CAT)
    assert [v.lost_points for v in sc] == [Decimal("5.00"), Decimal("5.00")]   # 10 lost, shared
    assert all(v.split for v in sc)


def test_na_and_full_marks_are_not_violations_and_serious_is_separate():
    r8 = ResultIn("c8_violations", "YES", evidence=[OK])
    sc, se = extract_violations(ev("d1", full(c8_violations=r8)), CAT)
    assert sc == [] and len(se) == 1 and se[0].lost_points == 0


def test_unconfirmed_violation_is_excluded_from_ranking():
    r5 = ResultIn("c5_empathy", "NO")          # no verified quote, no missing location, AI source
    t = priority_table([ev("d1", full(c5_empathy=r5))], CAT)
    assert t["ranking"] == [] and t["unconfirmed_excluded"] == 1


def test_frequency_excludes_na_and_priority_formula():
    na = ResultIn("c2_needs", "N/A")
    bad = ResultIn("c2_needs", "NO", sub_checks=[{"check_id": "ignored_detail", "answer": "YES", "evidence": [OK]}])
    evals = [ev("d1", full(c2_needs=bad)), ev("d2", full()), ev("d3", full(c2_needs=na)), ev("d4", full())]
    t = priority_table(evals, CAT)
    row = t["ranking"][0]
    assert row["violation_id"] == "c2_needs.ignored_detail"
    assert row["applicable"] == 3 and row["count"] == 1           # d3 is N/A -> not in denominator
    assert row["frequency"] == 33.33 and row["lost_points"] == 15.0
    assert row["priority_score"] == round(15.0 * 33.33, 2)
    assert row["pattern"] == "единичная"
