"""HTTP face of the multi-agent primitives (Phase 7).

One endpoint: POST /api/multi-agent/run fans a prompt out to ≤5 named
analysts and waits for the joined verdict — but only up to a total
wall-clock budget (``wait_s``, default 900s, max 1800s). On budget
exhaustion the response is 202 with every task id: the tasks are NOT
cancelled, they keep running to completion and land in the tasks table
(same disconnect semantics as ask/stream), so the caller can poll
``GET /api/tasks/{id}`` and re-derive the verdict later.

Business-rule violations (unknown analyst, cap, blank prompt, bad mode,
bad timeouts) are 400s; malformed JSON / wrong types are still FastAPI's
standard 422 (pydantic runs before the handler).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..institute import multi_agent
from ..institute.analysts import get_analyst

log = logging.getLogger("institute.api.multi_agent")

router = APIRouter(prefix="/api/multi-agent", tags=["multi-agent"])

MAX_AGENTS = 5
MAX_TIMEOUT_S = 3600        # per-task executor timeout cap
DEFAULT_WAIT_S = 900.0      # total wall-clock budget for the synchronous wait
MAX_WAIT_S = 1800.0


class RunBody(BaseModel):
    agents: list[str]
    prompt: str
    mode: str = "all"                  # all | first_success | majority_vote | best_effort
    hand: str | None = None            # overrides every agent's preferred hand
    timeout_s: int | None = None       # per-task; None -> spawn_fan_out's default (1800s)
    wait_s: float = DEFAULT_WAIT_S     # total request budget; capped at MAX_WAIT_S


@router.post("/run")
async def run(body: RunBody):
    """Fan out, wait up to ``wait_s`` seconds total, join.

    200: every task reached a terminal state within the budget — the joined
    verdict (with per-agent outputs) is the body.
    202: the budget elapsed first — body carries ``task_ids`` (in agents
    order); the tasks keep running and are queryable via GET /api/tasks/{id}.
    """
    if not body.agents:
        raise HTTPException(400, "agents must not be empty")
    if len(body.agents) > MAX_AGENTS:
        raise HTTPException(400, f"at most {MAX_AGENTS} agents per run (got {len(body.agents)})")
    unknown = [a for a in body.agents if get_analyst(a) is None]
    if unknown:
        raise HTTPException(400, f"unknown analysts: {', '.join(unknown)}")
    if not body.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    if body.mode not in multi_agent.JOIN_MODES:
        raise HTTPException(
            400, f"unknown mode {body.mode!r} (expected one of: {', '.join(multi_agent.JOIN_MODES)})"
        )
    if body.timeout_s is not None and not (0 < body.timeout_s <= MAX_TIMEOUT_S):
        raise HTTPException(400, f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")
    if not (0 < body.wait_s <= MAX_WAIT_S):
        raise HTTPException(400, f"wait_s must be in (0, {MAX_WAIT_S:.0f}]")

    kwargs = {"hand": body.hand}
    if body.timeout_s is not None:
        kwargs["timeout_s"] = body.timeout_s
    task_ids = await multi_agent.spawn_fan_out(body.agents, body.prompt, **kwargs)
    try:
        tasks = await multi_agent.wait_fan_out(task_ids, timeout_s=body.wait_s)
    except asyncio.TimeoutError:
        log.info(
            "multi-agent run exceeded wait_s=%.0fs; returning 202 with %d task ids",
            body.wait_s, len(task_ids),
        )
        return JSONResponse(status_code=202, content={
            "detail": f"wait budget of {body.wait_s:.0f}s elapsed; tasks keep running",
            "mode": body.mode,
            "agents": list(body.agents),
            "task_ids": task_ids,          # agents order; poll GET /api/tasks/{id}
        })
    result = multi_agent.join(tasks, body.mode)
    # annotate each per-task projection with the agent it belongs to
    # (join keeps fan-out order, which is the request's agents order)
    for agent_id, item in zip(body.agents, result["outputs"]):
        item["agent"] = agent_id
    return result
