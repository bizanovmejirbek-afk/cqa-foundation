from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")  # before anything reads os.environ

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles

from .api_main import router as main_router  # noqa: E402
from .api_studio import router as studio_router  # noqa: E402
from .api_analytics import router as analytics_router  # noqa: E402
from .services import ServiceError  # noqa: E402
from .source_api import SourceError  # noqa: E402
from .llm_client import LLMError  # noqa: E402
from .config_schema import ChecklistConfig  # noqa: E402
from .db import SessionLocal, init_db  # noqa: E402
from .services import save_checklist_version  # noqa: E402
from .models import ChecklistVersion  # noqa: E402
from sqlalchemy import select  # noqa: E402

app = FastAPI(title="Freedom Broker CQA")
app.include_router(studio_router)
app.include_router(main_router)
app.include_router(analytics_router)


@app.exception_handler(LLMError)
async def _llm_error(_: Request, exc: Exception):
    return JSONResponse(status_code=502, content={"detail": f"LLM error: {exc}"})


@app.exception_handler(Exception)
async def _unexpected(_: Request, exc: Exception):  # never show the user an empty "{}"
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.exception_handler(ServiceError)
@app.exception_handler(SourceError)
async def _domain_error(_: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed = Path(__file__).parent.parent / "config" / "official_chat_v1.json"
    with SessionLocal() as s:  # idempotent: same content hash -> no new version
        for slug, fname in (("official-chat", "official_chat_v1.json"), ("official-voice", "official_voice_v1.json")):
            cfg = ChecklistConfig.model_validate_json((seed.parent / fname).read_text(encoding="utf-8"))
            note = f"seed {cfg.content_hash()[:12]}"
            # seed only when this file content was never seeded: edits saved in the Studio are not overwritten
            if s.scalar(select(ChecklistVersion.id).where(ChecklistVersion.change_note == note)) is None:
                save_checklist_version(s, slug, cfg, change_note=note, author="system")


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")
