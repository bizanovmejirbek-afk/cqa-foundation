import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config_schema import ChecklistConfig, CriterionConfig
from app.guardrails import (GuardrailError, build_output_model, build_prompt, judge_criterion,
                            parse_llm_output, verify_quote)

CFG = ChecklistConfig.model_validate(json.loads(
    (Path(__file__).parent.parent / "config/official_chat_v1.json").read_text()))
C = {c.id: c for c in CFG.criteria}

MSGS = [
    {"timestamp": "00:00", "speaker": "bot", "text": "Здравствуйте! Я бот Freedom Broker."},
    {"timestamp": "00:05", "speaker": "client", "text": "Сәлеметсіз бе, шотымдағы балансты білгім келеді"},
    {"timestamp": "00:40", "speaker": "operator", "text": "Сәлеметсіз бе! Менің атым  Айгерим. «Қалай көмектесе аламын?»"},
    {"timestamp": "01:10", "speaker": "operator", "text": "Please confirm your date of birth and IIN first."},
]


class FakeLLM:
    """Returns queued responses; records prompts so tests can assert on retries."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def complete_json(self, system, user, schema):
        self.calls.append(user)
        return self.responses.pop(0)


# ---------- schema ----------
def test_schema_enum_matches_config():
    s = build_output_model(C["c4_consultation"]).model_json_schema()
    assert s["properties"]["verdict"]["enum"] == ["20", "15", "10", "5", "0", "N/A"]
    assert build_output_model(C["c5_empathy"]).model_json_schema()["properties"]["verdict"]["enum"] == ["YES", "NO"]
    assert list(s["properties"]).index("verdict") > list(s["properties"]).index("evidence")


def test_parse_rejects_illegal_verdict_and_extra_fields():
    m = build_output_model(C["c4_consultation"])
    with pytest.raises(GuardrailError):
        parse_llm_output({"reasoning": "abc", "verdict": "12", "confidence": 0.9}, m)
    with pytest.raises(GuardrailError):
        parse_llm_output({"reasoning": "abc", "verdict": "20", "confidence": 0.9, "score": 100}, m)


def test_parse_strips_markdown_fences():
    m = build_output_model(C["c5_empathy"])
    out = parse_llm_output('```json\n{"reasoning":"polite","verdict":"YES","confidence":0.8}\n```', m)
    assert out.verdict == "YES"


def test_prompt_contains_config_and_is_stable():
    p1 = build_prompt(C["c3_identification"], MSGS)
    p2 = build_prompt(C["c3_identification"], MSGS)
    assert p1.prompt_hash == p2.prompt_hash
    assert "general question about tariffs" in p1.user and '"N/A": only if' in p1.user
    assert "#2 [00:40] OPERATOR:" in p1.user and "BOT (context only)" in p1.user


def test_prompt_voice_note_only_for_voice():
    assert "Do NOT judge speech speed" in build_prompt(C["c6_management"], MSGS, "voice").user
    assert "Do NOT judge speech speed" not in build_prompt(C["c6_management"], MSGS, "chat").user


# ---------- config validation ----------
def test_config_rejects_na_without_condition_and_weighted_violation():
    base = C["c2_needs"].model_dump(mode="json")
    with pytest.raises(ValidationError):
        CriterionConfig.model_validate({**base, "na_condition": None})
    with pytest.raises(ValidationError):
        CriterionConfig.model_validate({**C["c8_violations"].model_dump(mode="json"), "weight": 5})


# ---------- verifier ----------
def test_verify_exact_with_normalisation_and_offsets():
    v = verify_quote("менің атым Айгерим. Қалай көмектесе аламын?", 2, MSGS)
    assert v.status == "EXACT" and v.message_index == 2 and not v.index_corrected
    assert MSGS[2]["text"][v.char_start:v.char_end].startswith("Менің атым")


def test_verify_corrects_wrong_index():
    v = verify_quote("confirm your date of birth", 1, MSGS)
    assert v.status == "EXACT" and v.message_index == 3 and v.index_corrected


def test_verify_fuzzy_small_typo():
    v = verify_quote("Please confirm your date of brith and IIN first", 3, MSGS)
    assert v.status == "FUZZY" and v.message_index == 3


def test_verify_rejects_invented_quote():
    assert verify_quote("Your balance is 5000 USD", 3, MSGS).status == "UNVERIFIED"


def test_verify_wrong_speaker():
    v = verify_quote("Я бот Freedom Broker", 0, MSGS, require_speaker="operator")
    assert v.status == "UNVERIFIED" and v.reason == "WRONG_SPEAKER"


def test_verify_stitched_quote_must_be_in_order_in_one_message():
    assert verify_quote("Сәлеметсіз бе! ... Қалай көмектесе", 2, MSGS).status == "EXACT"
    assert verify_quote("Қалай көмектесе ... Сәлеметсіз бе!", 2, MSGS).status == "UNVERIFIED"


def test_short_quote_only_searched_in_cited_message():
    assert verify_quote("IIN", 3, MSGS).status == "EXACT"
    assert verify_quote("IIN", 2, MSGS).status == "UNVERIFIED"


# ---------- judge loop ----------
def test_subchecks_derive_verdict_and_override_llm():
    good_ev = [{"message_index": 2, "quote": "Сәлеметсіз бе!"}]
    resp = {"sub_checks": [
        {"check_id": "greeted", "answer": "YES", "evidence": good_ev},
        {"check_id": "introduced", "answer": "YES", "evidence": [{"message_index": 2, "quote": "Менің атым Айгерим"}]},
        {"check_id": "client_language", "answer": "NO", "evidence": []}],
        "evidence": good_ev, "reasoning": "Greeting and name, but language check failed",
        "verdict": "NO", "confidence": 0.9}
    r = judge_criterion(C["c1_contact"], MSGS, FakeLLM(resp))
    assert r.verdict == "NO" and r.derived_from_subchecks and r.issues == []


def test_retry_on_bot_evidence_then_success():
    bad = {"evidence": [{"message_index": 0, "quote": "Здравствуйте! Я бот Freedom Broker."}],
           "reasoning": "polite", "verdict": "YES", "confidence": 0.9}
    good = {"evidence": [{"message_index": 2, "quote": "Қалай көмектесе аламын?"}],
            "reasoning": "polite", "verdict": "YES", "confidence": 0.9}
    llm = FakeLLM(bad, good)
    r = judge_criterion(C["c5_empathy"], MSGS, llm)
    assert r.verdict == "YES" and r.attempts == 2 and not r.needs_review
    assert "needs at least one verified quote from the operator" in llm.calls[1]


def test_na_not_allowed_goes_to_review_after_retry():
    na = {"reasoning": "nothing", "verdict": "N/A", "confidence": 0.9, "na_reason": "x"}
    with pytest.raises(GuardrailError):  # "N/A" is not even in the enum for c5
        parse_llm_output(na, build_output_model(C["c5_empathy"]))
    with pytest.raises(GuardrailError):  # schema error on both attempts -> no invented verdict
        judge_criterion(C["c5_empathy"], MSGS, FakeLLM(na, na))


def test_unparseable_twice_raises_instead_of_guessing():
    with pytest.raises(GuardrailError):
        judge_criterion(C["c5_empathy"], MSGS, FakeLLM("not json", "still not json"))


def test_invented_quote_flags_needs_review():
    bad = {"evidence": [{"message_index": 3, "quote": "Your balance is 5000 USD"}],
           "reasoning": "verified", "verdict": "YES", "confidence": 0.95}
    r = judge_criterion(C["c3_identification"], MSGS, FakeLLM(bad, bad))
    assert r.needs_review and any("not found verbatim" in i for i in r.issues)
