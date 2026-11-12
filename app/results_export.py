"""Organiser results format 1.0 (CQA Student Guide, appendix "Как сдать результаты").

results/dev/results.json  — the 65 DEV dialogues
results/prod/results.json — the 10 control dialogues

Mapping from our criterion ids to the organiser codes; values are the scale value BEFORE weighting
(true/false/null, or the number); evidence_turns are 1-based indices into the original transcript.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Iterable, Optional

CODES = {  # our id -> (organiser code, kind, allowed values)
    "c1_contact": ("contact", "bool", {True, False}),
    "c2_needs": ("discovery", "bool", {True, False, None}),
    "c3_identification": ("identification", "bool", {True, False, None}),
    "c4_consultation": ("consultation", "int", {0, 5, 10, 15, 20, None}),
    "c5_empathy": ("empathy", "bool", {True, False}),
    "c6_management": ("dialogue", "int", {0, 4, 7, 10}),
    "c7_conclusion": ("closure", "bool", {True, False, None}),
    "c8_violations": ("critical", "bool", {True, False}),
}
ORDER = [v[0] for v in CODES.values()]
RATIONALE_MAX = 400


def to_value(cid: str, verdict: Optional[str]) -> Any:
    code, kind, _ = CODES[cid]
    if verdict is None:
        raise ValueError(f"{cid}: no verdict")
    if verdict == "N/A":
        return None
    if kind == "bool":
        if verdict not in ("YES", "NO"):
            raise ValueError(f"{cid}: unexpected verdict {verdict!r}")
        return verdict == "YES"
    return int(Decimal(verdict))


def _turns(result, verdict: Optional[str] = None) -> list[int]:
    """1-based transcript turns: verified quotes (criterion + sub-checks), else the missing-step anchor."""
    idx: set[int] = set()
    for e in result.evidence:
        if e.message_index is not None and e.status.value in ("EXACT", "FUZZY"):
            idx.add(e.message_index + 1)
    for sc in result.sub_checks_json or []:
        for e in sc.get("evidence", []):
            if e.get("message_index") is not None and e.get("status") in ("EXACT", "FUZZY"):
                idx.add(int(e["message_index"]) + 1)
    if not idx and result.missing_after_index is not None and result.missing_after_index >= 0:
        idx.add(result.missing_after_index + 1)
    return sorted(idx)


def _rationale(result, verdict: Optional[str]) -> str:
    text = (result.reasoning or "").strip()
    if verdict == "N/A" and result.na_reason:
        text = result.na_reason.strip() if not text else text
    if result.decisions:
        d = result.decisions[-1]
        text = f"Решение проверяющего: {d.reason}".strip()
    text = " ".join(text.split())
    return text if len(text) <= RATIONALE_MAX else text[: RATIONALE_MAX - 1].rstrip() + "…"


def entry_for(conv_id: str, ev, fallback_turns: Optional[list[int]] = None) -> dict:
    """One record. `ev` is the latest Evaluation (ORM) or None. `fallback_turns` (1-based) is used when a
    criterion has no verified quote, e.g. an N/A verdict: the guide asks for supporting turns in every row."""
    if ev is None:
        return {"conversation_id": conv_id, "status": "error", "criteria": [], "score": None,
                "error": "Диалог не был оценён."}
    by_id = {r.criterion_id: r for r in ev.results}
    missing = [cid for cid in CODES if cid not in by_id]
    failed = [cid for cid, r in by_id.items() if cid in CODES and _effective(r) is None]
    snap = ev.snapshots[-1] if ev.snapshots else None
    if missing or failed or snap is None or snap.final_score is None:
        why = []
        if missing:
            why.append("нет критериев: " + ", ".join(CODES[c][0] for c in missing))
        if failed:
            why.append("модель не вернула ответ по: " + ", ".join(CODES[c][0] for c in failed))
        if snap is None or snap.final_score is None:
            why.append(f"итог не рассчитан ({snap.score_status.value if snap else 'нет снимка'})")
        return {"conversation_id": conv_id, "status": "error", "criteria": [], "score": None,
                "error": "; ".join(why)}
    criteria = []
    for cid, (code, _, _) in CODES.items():
        r = by_id[cid]
        verdict = _effective(r)
        criteria.append({"criterion_id": code, "value": to_value(cid, verdict),
                         "rationale": _rationale(r, verdict),
                         "evidence_turns": _turns(r, verdict) or list(fallback_turns or [])})
    critical = next(c["value"] for c in criteria if c["criterion_id"] == "critical")
    return {"conversation_id": conv_id, "status": "ok", "criteria": criteria,
            "score": {"final_score": float(Decimal(snap.final_score).quantize(Decimal("0.01"))),
                      "critical_flag": bool(critical)}}


def _effective(r) -> Optional[str]:
    return r.decisions[-1].new_verdict if r.decisions else r.verdict


def build_document(entries: Iterable[dict], team_name: str, model_name: str, mode: str = "external_api") -> dict:
    return {"schema_version": "1.0", "team_name": team_name,
            "model": {"mode": mode, "name": model_name}, "results": list(entries)}


def validate(doc: dict, expected_count: Optional[int] = None, expected_ids: Optional[Iterable[str]] = None) -> list[str]:
    """Problems found; empty list = ready to push."""
    p: list[str] = []
    if doc.get("schema_version") != "1.0":
        p.append("schema_version должен быть \"1.0\"")
    if not str(doc.get("team_name", "")).strip():
        p.append("team_name пустой")
    m = doc.get("model") or {}
    if m.get("mode") not in ("local", "external_api", "hybrid") or not str(m.get("name", "")).strip():
        p.append("model.mode / model.name заполнены неверно")
    res = doc.get("results")
    if not isinstance(res, list):
        return p + ["results должен быть массивом"]
    ids = [r.get("conversation_id") for r in res]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        p.append("повторяющиеся conversation_id: " + ", ".join(map(str, dup)))
    if expected_count is not None and len(res) != expected_count:
        p.append(f"записей {len(res)}, ожидалось {expected_count}")
    if expected_ids is not None:
        exp = set(expected_ids)
        lost = sorted(exp - set(ids))
        extra = sorted(set(ids) - exp)
        if lost:
            p.append("нет записей для: " + ", ".join(lost))
        if extra:
            p.append("лишние ID: " + ", ".join(map(str, extra)))
    allowed = {code: vals for code, _, vals in CODES.values()}
    for r in res:
        cid = r.get("conversation_id")
        if r.get("status") == "error":
            if r.get("criteria") != [] or r.get("score") is not None or not r.get("error"):
                p.append(f"{cid}: для status=error нужны criteria=[], score=null и текст error")
            continue
        if r.get("status") != "ok":
            p.append(f"{cid}: status должен быть ok или error")
            continue
        codes = [c.get("criterion_id") for c in r.get("criteria", [])]
        if sorted(codes) != sorted(ORDER):
            p.append(f"{cid}: нужны все 8 критериев ровно по одному разу")
            continue
        for c in r["criteria"]:
            v = c.get("value")
            vals = allowed[c["criterion_id"]]
            if v is None:
                ok = None in vals
            elif isinstance(v, bool):
                ok = any(a is v for a in vals)
            else:
                ok = type(v) is int and v in {a for a in vals if type(a) is int}
            if not ok:
                p.append(f"{cid}: {c['criterion_id']} = {v!r} недопустимо")
            if not isinstance(c.get("rationale"), str):
                p.append(f"{cid}: {c['criterion_id']} без rationale")
            t = c.get("evidence_turns")
            if not isinstance(t, list) or not all(type(x) is int and x >= 1 for x in t):
                p.append(f"{cid}: {c['criterion_id']} evidence_turns должны быть номерами с 1")
        sc = r.get("score") or {}
        fs = sc.get("final_score")
        if not isinstance(fs, (int, float)) or not 0 <= fs <= 100 or round(fs, 2) != fs:
            p.append(f"{cid}: final_score должен быть числом 0–100 с двумя знаками")
        crit = next((c["value"] for c in r["criteria"] if c["criterion_id"] == "critical"), None)
        if sc.get("critical_flag") is not crit:
            p.append(f"{cid}: critical_flag не совпадает с critical")
    return p


def dumps(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
