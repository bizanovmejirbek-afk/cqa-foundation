"""Criterion Studio endpoints. The browser previews instantly on its own; this
endpoint returns the authoritative prompt + schema built by the same code the
judge uses, so what you see is exactly what the model receives."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config_schema import ChecklistConfig, CriterionConfig
from .db import get_session
from .guardrails import build_prompt, render_criterion_block
from .models import Checklist, ChecklistVersion
from .services import save_checklist_version

router = APIRouter(prefix="/api/studio", tags=["studio"])

SAMPLE = [
    {"timestamp": "00:00", "speaker": "client", "text": "Здравствуйте, как вывести деньги?"},
    {"timestamp": "00:20", "speaker": "operator", "text": "Здравствуйте! Меня зовут Айгерим, сейчас помогу."},
]


class PreviewIn(BaseModel):
    criterion: dict
    channel: str = "chat"
    messages: list[dict] | None = None


def _errors(e: ValidationError) -> list[dict]:
    return [{"field": ".".join(map(str, er["loc"])), "message": er["msg"]} for er in e.errors()]


@router.post("/preview")
def preview(body: PreviewIn):
    try:
        c = CriterionConfig.model_validate(body.criterion)
    except ValidationError as e:
        return {"valid": False, "errors": _errors(e)}
    p = build_prompt(c, body.messages or SAMPLE, body.channel)
    return {"valid": True, "errors": [], "criterion_block": render_criterion_block(c, body.channel),
            "system": p.system, "user": p.user, "schema": p.schema, "prompt_hash": p.prompt_hash}


@router.get("/checklists/{slug}/latest")
def latest_version(slug: str, s: Session = Depends(get_session)):
    v = s.scalar(select(ChecklistVersion).join(Checklist).where(Checklist.slug == slug)
                 .order_by(ChecklistVersion.version.desc()).limit(1))
    if v is None:
        raise HTTPException(404, "checklist not found")
    return {"version": v.version, "config": v.config_json}


class SaveIn(BaseModel):
    checklist: dict
    change_note: str | None = None
    author: str | None = None


@router.post("/checklists/{slug}/versions")
def save_version(slug: str, body: SaveIn, s: Session = Depends(get_session)):
    try:
        cfg = ChecklistConfig.model_validate(body.checklist)
    except ValidationError as e:
        raise HTTPException(422, detail=_errors(e))
    v = save_checklist_version(s, slug, cfg, body.change_note, body.author)
    return {"checklist_version_id": v.id, "version": v.version, "config_hash": v.config_hash}
