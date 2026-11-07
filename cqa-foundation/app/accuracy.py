"""Compare AI evaluations with reference labels (eval/gold.json).

Measures the AI's own verdicts (before any human review), because that is what
the organisers compare with Ground Truth on the control set.
"""
from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

GOLD_PATH = Path(__file__).resolve().parent.parent / "eval" / "gold.json"


@lru_cache(maxsize=1)
def _load(mtime: float) -> dict:
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


def load_gold() -> dict:
    if not GOLD_PATH.exists():
        return {}
    return _load(GOLD_PATH.stat().st_mtime)


def reference_for(conv_id: str) -> Optional[dict]:
    return load_gold().get(conv_id)


def _pct(a: int, b: int) -> Optional[float]:
    return round(100 * a / b, 1) if b else None


def compute_accuracy(evals: list[dict]) -> dict:
    """`evals`: [{conversation_id, language, channel, final_score, verdicts: {criterion_id: ai_verdict}}]"""
    gold = load_gold()
    per_crit = defaultdict(lambda: {"n": 0, "match": 0})
    per_lang = defaultdict(lambda: {"n": 0, "match": 0})
    na = {"tp": 0, "fp": 0, "fn": 0}
    viol = {"tp": 0, "fp": 0, "fn": 0}
    score_err: list[float] = []
    mismatches, conv_rows = [], []

    for ev in evals:
        g = gold.get(ev["conversation_id"])
        if not g:
            continue
        matched = compared = 0
        for cid, ref in g["verdicts"].items():
            pred = ev["verdicts"].get(cid)
            if ref is None or cid not in ev["verdicts"]:
                continue  # unknown reference label, or criterion not in this checklist
            compared += 1
            ok = pred == ref
            matched += ok
            per_crit[cid]["n"] += 1; per_crit[cid]["match"] += ok
            lang = (ev.get("language") or "?").upper()
            per_lang[lang]["n"] += 1; per_lang[lang]["match"] += ok
            if ref == "N/A" or pred == "N/A":
                na["tp" if ref == pred == "N/A" else ("fp" if pred == "N/A" else "fn")] += 1
            if cid == "c8_violations":
                if ref == "YES" or pred == "YES":
                    viol["tp" if ref == pred == "YES" else ("fp" if pred == "YES" else "fn")] += 1
            if not ok:
                mismatches.append({"conversation_id": ev["conversation_id"], "criterion_id": cid,
                                   "reference": ref, "ai": pred, "language": lang})
        if ev.get("final_score") is not None and g.get("final_score") is not None:
            score_err.append(abs(float(ev["final_score"]) - g["final_score"]))
        conv_rows.append({"conversation_id": ev["conversation_id"], "matched": matched, "compared": compared,
                          "ai_score": ev.get("final_score"), "reference_score": g.get("final_score")})

    total_n = sum(v["n"] for v in per_crit.values())
    total_m = sum(v["match"] for v in per_crit.values())
    return {
        "reference_available": bool(gold),
        "conversations_compared": len(conv_rows),
        "overall_accuracy": _pct(total_m, total_n),
        "criteria": {k: {**v, "accuracy": _pct(v["match"], v["n"])} for k, v in sorted(per_crit.items())},
        "languages": {k: {**v, "accuracy": _pct(v["match"], v["n"])} for k, v in sorted(per_lang.items())},
        "na": {**na, "precision": _pct(na["tp"], na["tp"] + na["fp"]), "recall": _pct(na["tp"], na["tp"] + na["fn"])},
        "violations": {**viol, "precision": _pct(viol["tp"], viol["tp"] + viol["fp"]),
                       "recall": _pct(viol["tp"], viol["tp"] + viol["fn"])},
        "score_mae": round(sum(score_err) / len(score_err), 2) if score_err else None,
        "score_within_5": _pct(sum(e <= 5 for e in score_err), len(score_err)),
        "conversations": sorted(conv_rows, key=lambda r: (r["matched"] - r["compared"], r["conversation_id"])),
        "mismatches": mismatches,
    }
