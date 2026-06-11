from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..institute.analysts import get_analyst
from ..institute.prompts import build_analyst_prompt
from ..router import executor

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/tasks")
async def list_tasks(
    status: str | None = None, hand: str | None = None, source: str | None = None,
    session_id: str | None = None, run_id: str | None = None, limit: int = 100,
):
    return await executor.list_tasks(
        status=status, hand=hand, source=source, session_id=session_id,
        parent_run_id=run_id, limit=limit,
    )


@router.get("/tasks/queue")
async def queue():
    return await executor.queue_stats()


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = await executor.get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    ok = await executor.cancel(task_id)
    return {"cancelled": ok}


class AskBody(BaseModel):
    prompt: str
    analyst_id: str | None = None
    hand: str | None = None
    model: str | None = None
    timeout_s: int | None = None


@router.post("/ask")
async def ask(body: AskBody):
    """Synchronous one-shot: run a prompt (optionally as an analyst persona) and wait."""
    settings = get_settings()
    prompt = body.prompt
    hand = body.hand or settings.default_hand
    if body.analyst_id:
        analyst = get_analyst(body.analyst_id)
        if analyst is None:
            raise HTTPException(404, f"unknown analyst {body.analyst_id}")
        prompt = build_analyst_prompt(analyst, body.prompt)
        hand = body.hand or analyst.hand or settings.default_hand
    task = await executor.submit(
        hand, prompt, source="api", model=body.model, timeout_s=body.timeout_s,
    )
    return task


# alias kept for muscle memory / external scripts
@router.post("/execute", include_in_schema=False)
async def execute_alias(body: AskBody):
    return await ask(body)
