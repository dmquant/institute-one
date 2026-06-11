from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..institute import research

router = APIRouter(prefix="/api/research", tags=["research"])


class EnqueueBody(BaseModel):
    topic: str
    priority: int = 0


@router.get("/queue")
async def list_queue(status: str | None = None, limit: int = 100):
    return await research.list_queue(status=status, limit=limit)


@router.post("/queue")
async def enqueue(body: EnqueueBody):
    if not body.topic.strip():
        raise HTTPException(400, "topic must not be empty")
    return await research.enqueue(body.topic, priority=body.priority, source="api")


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
    # shield: a client disconnect must not cancel an in-flight research run
    processed = await asyncio.shield(research.tick())
    return {"processed": processed}


@router.get("/log")
async def recent_log(limit: int = 50):
    return await db.query(
        "SELECT * FROM research_log ORDER BY completed_at DESC, id DESC LIMIT ?",
        (min(max(limit, 1), 500),),
    )
