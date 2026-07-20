from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import db
from ..institute import research

router = APIRouter(prefix="/api/research", tags=["research"])


class EnqueueBody(BaseModel):
    topic: str
    priority: int = 0
    # structured fields (card M3-001) — omit them all for the old topic-only behavior
    thesis_id: str | None = None
    security_id: str | None = None
    question: str | None = Field(default=None, max_length=research.MAX_QUESTION_LEN)
    output_type: str | None = Field(default=None, max_length=research.MAX_ANNOTATION_LEN)
    priority_reason: str | None = Field(default=None, max_length=research.MAX_ANNOTATION_LEN)
    project_id: str | None = None    # 0021: 归属项目（可选；须为 active 项目）


class SeedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos 422, matching the domain's strictness

    action_codes: list[str] = ["deep_research_candidate"]
    # 0 = dry sweep (count matches, enqueue nothing); bounds mirror the domain
    cap: int = Field(default=10, ge=0, le=research.MAX_SEED_CAP)


@router.get("/queue")
async def list_queue(status: str | None = None, limit: int = 100):
    return await research.list_queue(status=status, limit=limit)


@router.post("/queue")
async def enqueue(body: EnqueueBody):
    if not body.topic.strip():
        raise HTTPException(400, "topic must not be empty")
    try:
        return await research.enqueue(
            body.topic, priority=body.priority, source="api",
            thesis_id=body.thesis_id, security_id=body.security_id,
            question=body.question, output_type=body.output_type,
            priority_reason=body.priority_reason,
            project_id=body.project_id,
        )
    except ValueError as exc:  # unknown thesis/security, missing anchor
        raise HTTPException(400, str(exc)) from exc


@router.post("/seed-from-theses")
async def seed_from_theses(body: SeedBody):
    """Seed structured candidates from imported theses carrying a matching
    practical.actionCode (idempotent: existing triples dedup, cooldowns refuse)."""
    try:
        return await research.seed_from_theses(action_codes=body.action_codes, cap=body.cap)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/queue/{item_id}")
async def get_item(item_id: str):
    item = await research.get_item(item_id)
    if item is None:
        raise HTTPException(404, "research item not found")
    return item


@router.post("/queue/{item_id}/cancel")
async def cancel_item(item_id: str):
    item = await research.cancel_item(item_id)
    if item is None:
        raise HTTPException(404, "research item not found")
    return item


@router.post("/tick")
async def tick():
    # shield: a client disconnect must not cancel an in-flight research run.
    # shielded_tick registers the task so the lifespan shutdown drain still
    # cancels it — shielded from the request, not from the process stopping.
    processed = await asyncio.shield(research.shielded_tick())
    return {"processed": processed}


@router.get("/log")
async def recent_log(limit: int = 50):
    return await db.query(
        "SELECT * FROM research_log ORDER BY completed_at DESC, id DESC LIMIT ?",
        (min(max(limit, 1), 500),),
    )
