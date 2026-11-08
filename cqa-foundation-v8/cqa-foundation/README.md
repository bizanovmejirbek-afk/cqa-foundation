# Freedom Broker CQA — core foundation

Config-driven conversation evaluation: the checklist is data, the judge prompt and
output schema are generated from it, quotes are verified against the transcript,
and the score is computed by deterministic Python.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                                   # 30 tests: scoring + guardrails
uvicorn app.main:app --reload               # http://localhost:8000/index.html
```

Copy `.env.example` to `.env`. With no LLM key the app runs an offline **mock judge**
(clearly labelled in the UI) so the whole flow can be demonstrated; it is not an evaluator.

Screens: `index.html` (conversations, batch evaluation) · `conversation.html?id=…`
(transcript, evidence links, appeal, review, score ledger) · `studio.html` (checklist editor).

On startup the official checklist (`config/official_chat_v1.json`) is seeded as
version 1 of `official-chat` and `official-voice` (idempotent: an unchanged file creates no new version).

## Development data and accuracy

`data/dev_conversations.json` holds the 65 development dialogues and `eval/gold.json`
the team's reference labels, both generated from the review spreadsheet:

```bash
python -m tools.import_xlsx conversations_quality_review_65.xlsx
```

In the UI: **Load 65 dev dialogues** → select → **Evaluate selected** → open **Accuracy**.
Baseline to beat: a judge that always gives full marks already matches 76.9% of reference verdicts.

## Layout

| File | Responsibility |
|---|---|
| `app/config_schema.py` | `CriterionConfig` / `ChecklistConfig` — validation of everything the Studio saves |
| `app/guardrails.py` | Prompt builder, dynamic Pydantic output model, quote verifier, consistency rules, judge loop with one corrective retry |
| `app/scoring.py` | Deterministic engine: Decimal, ROUND_HALF_UP, N/A redistribution, violations separate, `CANNOT_CALCULATE` / `INCOMPLETE` |
| `app/models.py` | SQLAlchemy models: immutable checklist versions, evaluations, criterion results, evidence spans, appeals, review decisions, score snapshots |
| `app/services.py` | Versioning, persisting evaluations, appeals, locking, review + recalculation |
| `app/api_studio.py` | `POST /api/studio/preview`, `GET/POST /api/studio/checklists/{slug}/…` |
| `app/source_api.py` | Organiser GET API adapter + local JSON import |
| `app/llm_client.py` | OpenAI-compatible, Anthropic and mock clients with token/cost counting |
| `app/api_main.py` | Conversations, evaluations (single + batch), appeals, review, review queue |
| `static/index.html`, `conversation.html` | Conversation list and detail screens |
| `static/studio.*` | Criterion Studio (vanilla JS) |

## Plugging in a model

`judge_criterion()` takes any object with
`complete_json(system: str, user: str, schema: dict) -> str | dict`.
Pass `schema` to your provider's structured-output option. Record token usage in
your client and write it to `Evaluation.tokens_in/out` and `cost_usd`
(cost = Σ over all calls, including retries).

## Design rules

- Never store a criterion in code; never let the LLM compute a score.
- AI results, review decisions and score snapshots are append-only (ORM guard + SQLite triggers).
- Scores are stored as TEXT-encoded Decimal (SQLite has no decimal type).
- A judge that fails twice raises; the service stores the criterion as `JUDGE_FAILED`,
  `needs_review = true`, and the score status becomes `INCOMPLETE` — never a silent 0.
