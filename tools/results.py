"""Write results/dev/results.json and results/prod/results.json in the organiser format 1.0.

  python -m tools.results dev                     # export the 65 DEV dialogues already evaluated
  python -m tools.results dev --evaluate-missing  # evaluate DEV dialogues that have no evaluation yet
  python -m tools.results prod                    # control hour: fetch from API -> evaluate -> export -> check
  python -m tools.results prod --ids a,b,c        # export/evaluate these IDs (if the set came another way)
  python -m tools.results prod --file control.json   # control set given as a JSON file
  python -m tools.results check results/prod/results.json --expect 10

Team name: TEAM_NAME in .env or --team. Model name is taken from the evaluations themselves.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _repo_root() -> Path:
    """results/ must sit in the ROOT OF THE GIT REPOSITORY (the project may be one folder deeper)."""
    for d in (ROOT, *ROOT.parents):
        if (d / ".git").exists():
            return d
    return ROOT


OUT = _repo_root() / "results"

from app.db import SessionLocal, init_db  # noqa: E402
from app.results_export import build_document, dumps, entry_for, validate  # noqa: E402


def _dev_ids() -> list[str]:
    body = json.loads((ROOT / "data" / "dev_conversations.json").read_text(encoding="utf-8"))
    recs = body.get("data", body)
    recs = recs.get("records", recs) if isinstance(recs, dict) else recs
    return [str(r["conversation_id"]) for r in recs]


def _evaluate(s, ids: list[str], force: bool) -> None:
    from app.api_main import latest_evaluations, run_one
    have = latest_evaluations(s)
    todo = [i for i in ids if force or i not in have]
    for n, cid in enumerate(todo, 1):
        for attempt in (1, 2):
            try:
                ev = run_one(s, cid, None)
                print(f"  [{n}/{len(todo)}] {cid}: {ev.snapshots[-1].final_score}")
                break
            except Exception as e:  # keep going: a failed dialogue is reported as status=error
                s.rollback()
                print(f"  [{n}/{len(todo)}] {cid}: ошибка (попытка {attempt}): {getattr(e, 'detail', e)}")


def _export(s, ids: list[str], out: Path, team: str, expect: int | None) -> int:
    from app.api_main import latest_evaluations
    latest = latest_evaluations(s)
    from app.models import Conversation

    def fallback(cid: str) -> list[int]:
        """Client's request (first client turn) and the operator's first reply, 1-based."""
        conv = s.get(Conversation, cid)
        msgs = conv.transcript_json if conv else []
        sp = [str(m.get("speaker", "")).lower() for m in msgs]
        turns = [next((i + 1 for i, x in enumerate(sp) if x == "client"), None),
                 next((i + 1 for i, x in enumerate(sp) if x in ("operator", "agent")), None)]
        return sorted({t for t in turns if t}) or [1]

    entries = [entry_for(cid, latest.get(cid), fallback(cid)) for cid in ids]
    models = sorted({latest[c].model for c in ids if c in latest and latest[c].model})
    mode = "local" if all("mock" in m or "ollama" in m.lower() for m in models) and models else "external_api"
    doc = build_document(entries, team, ", ".join(models) or "unknown", mode)
    problems = validate(doc, expected_count=expect, expected_ids=ids)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(doc), encoding="utf-8")
    ok = sum(1 for e in entries if e["status"] == "ok")
    print(f"\nЗаписано: {out} · записей {len(entries)} · ok {ok} · error {len(entries) - ok}")
    if any("mock" in m for m in models):
        print("ВНИМАНИЕ: оценки сделаны mock-судьёй, это не настоящие результаты.")
    if problems:
        print("ПРОБЛЕМЫ:\n  - " + "\n  - ".join(problems))
        return 1
    print("Проверка формата: OK. Теперь commit + push.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set", choices=["dev", "prod", "check"])
    ap.add_argument("path", nargs="?", help="for check: path to results.json")
    ap.add_argument("--team", default=os.getenv("TEAM_NAME"))
    ap.add_argument("--ids", help="comma-separated conversation_id list")
    ap.add_argument("--file", help="control set as a JSON file (same shape as the API answer)")
    ap.add_argument("--group", help="group_id for the organiser API (default CQA_GROUP_ID)")
    ap.add_argument("--evaluate-missing", action="store_true", help="dev: evaluate dialogues without an evaluation")
    ap.add_argument("--reevaluate", action="store_true", help="evaluate again even if an evaluation exists")
    ap.add_argument("--expect", type=int, help="check: expected number of records")
    a = ap.parse_args(argv)

    if a.set == "check":
        doc = json.loads(Path(a.path).read_text(encoding="utf-8"))
        problems = validate(doc, expected_count=a.expect)
        print("OK" if not problems else "ПРОБЛЕМЫ:\n  - " + "\n  - ".join(problems))
        return 0 if not problems else 1

    if not a.team:
        print("Укажите название команды: --team \"...\" или TEAM_NAME=... в .env")
        return 2
    init_db()
    with SessionLocal() as s:
        if a.set == "dev":
            ids = _dev_ids()
            if a.evaluate_missing or a.reevaluate:
                _evaluate(s, ids, force=a.reevaluate)
            return _export(s, ids, OUT / "dev" / "results.json", a.team, len(ids))

        # prod: get the control set, store it, evaluate, export
        from app.source_api import fetch_records, import_records, load_file
        if a.ids:
            ids = [x.strip() for x in a.ids.split(",") if x.strip()]
        else:
            records = load_file(a.file) if a.file else fetch_records(a.group)
            print(f"Получено диалогов: {len(records)}")
            print(import_records(s, records, a.group or os.getenv("CQA_GROUP_ID") or "prod"))
            ids = [str(r["conversation_id"]) for r in records if "conversation_id" in r]
        print(f"Оцениваем {len(ids)} диалогов…")
        _evaluate(s, ids, force=a.reevaluate or not a.ids)
        return _export(s, ids, OUT / "prod" / "results.json", a.team, 10)


if __name__ == "__main__":
    sys.exit(main())
