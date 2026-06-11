from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..institute import mailbox

router = APIRouter(prefix="/api/mailbox", tags=["mailbox"])


class ThreadBody(BaseModel):
    subject: str
    analyst_id: str
    body: str


class ReplyBody(BaseModel):
    body: str


@router.get("/threads")
async def list_threads(status: str | None = None, limit: int = 50):
    return await mailbox.list_threads(status=status, limit=limit)


@router.post("/threads")
async def create_thread(body: ThreadBody):
    try:
        return await mailbox.create_thread(body.subject, body.analyst_id, body.body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    thread = await mailbox.get_thread(thread_id)
    if thread is None:
        raise HTTPException(404, "thread not found")
    return thread


@router.post("/threads/{thread_id}/reply")
async def reply(thread_id: str, body: ReplyBody):
    try:
        return await mailbox.reply(thread_id, body.body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/threads/{thread_id}/close")
async def close_thread(thread_id: str):
    try:
        return await mailbox.close_thread(thread_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/sweep")
async def sweep():
    await mailbox.sweep()
    return {"ok": True}
