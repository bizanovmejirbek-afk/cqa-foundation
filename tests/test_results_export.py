from decimal import Decimal
from types import SimpleNamespace as NS

from app.results_export import CODES, build_document, entry_for, to_value, validate

ST = lambda v: NS(value=v)  # noqa: E731


def result(cid, verdict, idx=(3,), missing=None):
    return NS(criterion_id=cid, verdict=verdict, decisions=[], reasoning="Оператор поздоровался.", na_reason=None,
              evidence=[NS(message_index=i, status=ST("EXACT")) for i in idx], sub_checks_json=[],
              missing_after_index=missing)


def evaluation(verdicts, score="85.38"):
    return NS(results=[result(c, v) for c, v in verdicts.items()], model="claude-haiku-5-5",
              snapshots=[NS(final_score=Decimal(score), score_status=ST("OK"))])


GOOD = {"c1_contact": "YES", "c2_needs": "N/A", "c3_identification": "N/A", "c4_consultation": "15",
        "c5_empathy": "YES", "c6_management": "7", "c7_conclusion": "YES", "c8_violations": "NO"}


def test_mapping_matches_organiser_format():
    e = entry_for("demo-001", evaluation(GOOD))
    vals = {c["criterion_id"]: c["value"] for c in e["criteria"]}
    assert vals == {"contact": True, "discovery": None, "identification": None, "consultation": 15,
                    "empathy": True, "dialogue": 7, "closure": True, "critical": False}
    assert e["criteria"][0]["evidence_turns"] == [4]          # 0-based #3 -> turn 4
    assert e["score"] == {"final_score": 85.38, "critical_flag": False}
    assert validate(build_document([e], "Team", "claude-haiku-5-5"), expected_count=1) == []


def test_failed_judge_becomes_error_record_not_zero():
    bad = dict(GOOD, c5_empathy=None)
    e = entry_for("x", evaluation(bad))
    assert e["status"] == "error" and e["criteria"] == [] and e["score"] is None and "empathy" in e["error"]
    assert entry_for("y", None)["status"] == "error"
    assert validate(build_document([e], "T", "m")) == []


def test_validator_catches_wrong_types_duplicates_and_count():
    e = entry_for("a", evaluation(GOOD))
    e["criteria"][0]["value"] = 1            # int instead of bool
    doc = build_document([e, entry_for("a", evaluation(GOOD))], "T", "m")
    p = validate(doc, expected_count=10)
    assert any("contact" in x for x in p) and any("повторяющиеся" in x for x in p) and any("ожидалось 10" in x for x in p)


def test_all_codes_present():
    assert [c for c, _, _ in CODES.values()] == ["contact", "discovery", "identification", "consultation",
                                                 "empathy", "dialogue", "closure", "critical"]
    assert to_value("c6_management", "10") == 10 and to_value("c8_violations", "YES") is True
