"""Convert the 65-dialogue review spreadsheet into
   data/dev_conversations.json  (organiser API format: conversation_id, channel, language, transcript, audio_url)
   eval/gold.json               (reference verdicts per criterion, for accuracy measurement)

Usage:  python -m tools.import_xlsx path/to/conversations_quality_review_65.xlsx
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^\[(?P<ts>[^\]]+)\] (?P<speaker>client|operator|bot|system): ?(?P<text>.*)$")

# spreadsheet column -> criterion id in config/official_*_v1.json
CRITERIA = {
    "01 Контакт (10)": "c1_contact",
    "02 Потребность (15)": "c2_needs",
    "03 ID (20)": "c3_identification",
    "04 Консультация (0–20)": "c4_consultation",
    "05 Эмпатия (15)": "c5_empathy",
    "06 Диалог (0–10)": "c6_management",
    "07 Завершение (5)": "c7_conclusion",
    "08 Грубая ошибка": "c8_violations",
}
WORDS = {"да": "YES", "нет": "NO", "n/a": "N/A", "na": "N/A"}


def parse_transcript(text: str) -> list[dict]:
    messages: list[dict] = []
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        m = LINE.match(raw)
        if m:
            messages.append({"timestamp": m["ts"], "speaker": m["speaker"], "text": m["text"]})
        elif messages:  # continuation of a multi-line message
            messages[-1]["text"] += "\n" + raw
        else:
            raise ValueError(f"cannot parse first line: {raw[:80]!r}")
    return messages


def norm_verdict(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.0+)?", s):
        return str(int(float(s)))
    return WORDS.get(s.lower())  # anything else ("Требует проверки") -> None = unknown


def main(path: str) -> None:
    wb = openpyxl.load_workbook(path, data_only=True)

    ws = wb["conversations"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = rows[0]
    records = []
    for r in rows[1:]:
        d = dict(zip(hdr, r))
        if not d.get("conversation_id"):
            continue
        records.append({
            "conversation_id": str(d["conversation_id"]).strip(),
            "channel": str(d["channel"]).strip(),
            "language": str(d["language"]).strip().lower(),
            "transcript": parse_transcript(d["transcript"]),
            "audio_url": (d.get("audio_url") or "").strip(),
        })

    ws2 = wb["Оценки по критериям"]
    rows2 = list(ws2.iter_rows(values_only=True))
    hdr2 = rows2[0]
    gold = {}
    for r in rows2[1:]:
        d = dict(zip(hdr2, r))
        if not d.get("conversation_id"):
            continue
        gold[str(d["conversation_id"]).strip()] = {
            "verdicts": {cid: norm_verdict(d.get(col)) for col, cid in CRITERIA.items()},
            "final_score": None if d.get("Итог / 100") in (None, "") else float(d["Итог / 100"]),
            "status": d.get("Статус"),
        }

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "eval").mkdir(exist_ok=True)
    (ROOT / "data" / "dev_conversations.json").write_text(
        json.dumps({"isSuccess": True, "error": None, "data": {
            "dataset_group_id": "dev-65", "total_records": len(records), "records": records}},
            ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "eval" / "gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(records)} conversations -> data/dev_conversations.json")
    print(f"{len(gold)} reference evaluations -> eval/gold.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "conversations_quality_review_65.xlsx")
