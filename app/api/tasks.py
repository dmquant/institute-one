from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import get_settings
from ..hands.registry import DEFAULT_FALLBACK_CHAINS, get_registry
from ..institute import memory
from ..institute.analysts import get_analyst
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
    """Cancel one task (ROADMAP Phase 0 cancel protocol).

    queued → the row is conditionally flipped to 'cancelled' (a submit parked
    on the hand mutex is woken and exits without running); running → the
    executor cancels the in-flight asyncio task, which kills the CLI process
    group and persists 'cancelled' (the shutdown drain's mechanism, applied
    to one task). Idempotence: terminal tasks answer 409, unknown ids 404 —
    repeating a cancel never flips state twice.
    """
    task = await executor.get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task.status in executor.TERMINAL:
        raise HTTPException(409, f"task already terminal (status: {task.status})")
    ok = await executor.cancel(task_id)
    if not ok:  # reached a terminal state between the check and the cancel
        raise HTTPException(409, "task already terminal")
    return {"cancelled": True}


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    """Requeue a failed task (incl. 'orphaned by restart') as a NEW row.

    The new row references the original prompt/hand/session/workspace and
    replays the original row's PERSISTED fallback_chain (0024) — policy
    fidelity holds across process restarts and settings changes; rows with a
    NULL stored chain fall back to the executor's legacy source derivation.

    lineage_root points every generation of a retry chain at the original
    task (a retry of a retry keeps the same root — one-lookup audit), and the
    0024 partial unique index allows at most ONE live task per lineage: while
    a previous retry is still queued/running, another retry answers 409. The
    idempotency window is the DATABASE's, not process memory, so it holds
    across restarts and concurrent processes; the pre-check below only makes
    the common case friendly — the unique index is the arbiter.
    """
    row = await db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(404, "task not found")
    if row["status"] != "failed":
        raise HTTPException(409, f"only failed tasks can be retried (status: {row['status']})")
    lineage_root = row["lineage_root"] or task_id
    live = await db.query_one(
        "SELECT id FROM tasks WHERE lineage_root = ? AND status IN ('queued','running')",
        (lineage_root,),
    )
    if live is not None:
        raise HTTPException(
            409, f"a retry for this lineage is already live (task {live['id']})",
        )
    try:
        new_id, lineage_root = await executor.respawn_from_row(row)
    except sqlite3.IntegrityError:
        # lost the pre-check race (same instant, other process): the 0024
        # unique index arbitrated — exactly one live retry per lineage
        raise HTTPException(409, "a retry for this lineage is already live") from None
    return {"task_id": new_id, "retried_from": task_id, "lineage_root": lineage_root}


class AskBody(BaseModel):
    prompt: str
    analyst_id: str | None = None
    hand: str | None = None
    model: str | None = None
    timeout_s: int | None = None


def _prefer_idle_hand(hand: str) -> str:
    """Interactive asks prefer an IDLE hand over queueing behind a busy one.

    The per-hand mutex means an ask on a busy hand waits for the running
    task (up to its full timeout, ~30 min for a workflow step). For a hand
    the caller did NOT pin explicitly, answering NOW on a sibling hand beats
    answering later on the preferred one — so: if the resolved hand's mutex
    is held, walk its fallback chain (the same chain the executor would use
    on unavailability) and take the first hand that is both idle and
    available (installed, not cooling, not degraded). Everything busy or
    unavailable → keep the original hand and queue as before.
    """
    if not executor.hand_busy(hand):
        return hand
    registry = get_registry()
    for cand in DEFAULT_FALLBACK_CHAINS.get(hand, []):
        if not executor.hand_busy(cand) and registry.is_available(cand):
            return cand
    return hand


async def prepare_ask(body: AskBody) -> tuple[str, str]:
    """Shared ``/api/ask`` + ``/api/ask/stream`` preprocessing → (hand, prompt).

    Persona wrap via ``memory.prompt_with_memory`` (standing-memory block
    included; unknown analyst ⇒ 404), hand precedence body > analyst > default — then
    the interactive idle-hand preference (交互优先空闲手, ROADMAP Phase 0):
    asks are ``source="api"`` interactive traffic, so when the caller did not
    explicitly pin a hand (``body.hand``) or a model (``body.model`` is
    hand-family-specific, so it counts as pinning too), a busy resolved hand
    is swapped for the first idle+available hand in its fallback chain; if
    the whole chain is busy the ask queues on the original hand as before.
    An explicit ``body.hand`` is never rerouted, busy or not.
    """
    settings = get_settings()
    prompt = body.prompt
    hand = body.hand or settings.default_hand
    if body.analyst_id:
        analyst = get_analyst(body.analyst_id)
        if analyst is None:
            raise HTTPException(404, f"unknown analyst {body.analyst_id}")
        prompt = await memory.prompt_with_memory(analyst, body.prompt)
        hand = body.hand or analyst.hand or settings.default_hand
    if body.hand is None and body.model is None:
        hand = _prefer_idle_hand(hand)
    return hand, prompt


@router.post("/ask")
async def ask(body: AskBody):
    """Synchronous one-shot: run a prompt (optionally as an analyst persona) and wait.

    Preprocessing (persona wrap, 404, idle-hand preference for interactive
    asks) lives in ``prepare_ask`` — shared verbatim with ``/api/ask/stream``.
    """
    hand, prompt = await prepare_ask(body)
    task = await executor.submit(
        hand, prompt, source="api", model=body.model, timeout_s=body.timeout_s,
    )
    return task


# alias kept for muscle memory / external scripts
@router.post("/execute", include_in_schema=False)
async def execute_alias(body: AskBody):
    return await ask(body)
