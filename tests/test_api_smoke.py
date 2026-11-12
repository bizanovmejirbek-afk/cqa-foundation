"""End-to-end API test with the offline mock judge and a temporary database.
Skipped automatically where FastAPI is not installed."""
import os
import tempfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

_tmp = tempfile.mkdtemp()
os.environ["CQA_DB_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["LLM_PROVIDER"] = "mock"          # never call a paid API from tests
os.environ.pop("LLM_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:   # runs startup: create tables, seed checklists
        yield c


def test_full_flow(client):
    assert client.get("/api/status").json()["llm_provider"] == "mock"
    r = client.post("/api/import-sample").json()
    assert r["added"] == 3

    rows = client.get("/api/conversations").json()
    assert {x["id"] for x in rows} >= {"demo-ru-withdrawal", "demo-kz-tariff", "demo-en-violation"}

    ev = client.post("/api/evaluations", json={"conversation_id": "demo-ru-withdrawal"}).json()
    assert ev["final_score"] == "100.00" and len(ev["results"]) == 8
    assert ev["checklist"]["slug"] == "official-chat"
    c5 = next(r for r in ev["results"] if r["criterion_id"] == "c5_empathy")
    assert c5["allowed"] == ["YES", "NO"] and c5["evidence"][0]["status"] == "EXACT"

    # appeal -> review change -> recalculated score, history kept
    ap = client.post(f"/api/results/{c5['id']}/appeals", json={"comment": "Operator ignored frustration"}).json()
    out = client.post(f"/api/results/{c5['id']}/reviews", json={
        "decision": "CHANGE", "new_verdict": "NO", "reason": "Agree with appeal", "appeal_id": ap["id"]}).json()
    assert out["old_final_score"] == "100.00" and out["new_final_score"] == "85.00"

    detail = client.get(f"/api/evaluations/{ev['id']}").json()
    c5d = next(r for r in detail["results"] if r["criterion_id"] == "c5_empathy")
    assert c5d["ai_verdict"] == "YES" and c5d["verdict"] == "NO" and c5d["source"] == "REVIEW"
    assert [h["trigger"] for h in detail["history"]] == ["AI", "REVIEW"]
    assert detail["ai_final_score"] == "100.00" and detail["final_score"] == "85.00"

    # bad review is rejected with 400, not 500
    bad = client.post(f"/api/results/{c5['id']}/reviews", json={"decision": "CHANGE", "new_verdict": "12", "reason": "x"})
    assert bad.status_code == 400

    q = client.get("/api/review-queue").json()
    assert all(i["result_id"] != c5["id"] for i in q)  # resolved appeal left the queue


def test_studio_preview_and_versioning(client):
    latest = client.get("/api/studio/checklists/official-chat/latest").json()
    cfg = latest["config"]
    crit = cfg["criteria"][1]
    p = client.post("/api/studio/preview", json={"criterion": crit}).json()
    assert p["valid"] and crit["name"] in p["user"]

    cfg["criteria"][1]["definition"] += " Replaced during the drill."
    v = client.post("/api/studio/checklists/official-chat/versions", json={"checklist": cfg}).json()
    assert v["version"] == latest["version"] + 1
    same = client.post("/api/studio/checklists/official-chat/versions", json={"checklist": cfg}).json()
    assert same["version"] == v["version"]     # unchanged content -> no new version


def test_dev_import_and_accuracy(client):
    r = client.post("/api/import-dev").json()
    assert r["added"] + r["skipped"] + r["updated"] == 65
    ev = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001"}).json()
    assert ev["checklist"]["slug"] == "official-chat"
    conv = client.get("/api/conversations/p1-ru-001").json()
    assert conv["reference"]["verdicts"]["c3_identification"] == "N/A"
    acc = client.get("/api/accuracy").json()
    assert acc["reference_available"] and acc["conversations_compared"] >= 1
    voice = next(x for x in client.get("/api/conversations").json() if "voice" in x["channel"])
    assert voice["has_audio"]
