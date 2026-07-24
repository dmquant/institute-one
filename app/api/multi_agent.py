"""HTTP face of the multi-agent primitives (Phase 7) + durable groups/runs (M8-012).

POST /api/multi-agent/run fans a prompt out to ≤5 named analysts and waits
for the joined verdict — but only up to a total wall-clock budget (``wait_s``,
default 900s, max 1800s). On budget exhaustion the response is 202 with every
task id: the tasks are NOT cancelled, they keep running to completion and
land in the tasks table (same disconnect semantics as ask/stream), so the
caller can poll ``GET /api/tasks/{id}`` and re-derive the verdict later.

M8-012 additions: every run lands a durable ``multi_agent_runs`` row (the
response carries ``run_id``), so a disconnected caller reconnects through
``GET /api/multi-agent/runs/{run_id}`` (settle-on-read: once every task is
terminal the structured verdict is claimed and persisted). Named groups
(member analysts + routing strategy) get CRUD under ``/groups`` and a
``/groups/{id}/run`` endpoint that runs the panel with its stored strategy.

Business-rule violations (unknown analyst, cap, blank prompt, bad mode,
bad timeouts) are 400s; malformed JSON / wrong types are still FastAPI's
standard 422 (pydantic runs before the handler).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..institute import multi_agent
from ..institute.analysts import get_analyst

log = logging.getLogger("institute.api.multi_agent")

router = APIRouter(prefix="/api/multi-agent", tags=["multi-agent"])

MAX_AGENTS = multi_agent.MAX_AGENTS
MAX_TIMEOUT_S = 3600        # per-task executor timeout cap
DEFAULT_WAIT_S = 900.0      # total wall-clock budget for the synchronous wait
MAX_WAIT_S = 1800.0


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunBody(StrictBody):
    agents: list[str]
    prompt: str
    mode: str = "all"                  # all | first_success | majority_vote | best_effort
    hand: str | None = None            # overrides every agent's preferred hand
    timeout_s: int | None = None       # per-task; None -> start_run's default (1800s)
    wait_s: float = DEFAULT_WAIT_S     # total request budget; capped at MAX_WAIT_S


def _check_budgets(timeout_s: int | None, wait_s: float) -> None:
    if timeout_s is not None and not (0 < timeout_s <= MAX_TIMEOUT_S):
        raise HTTPException(400, f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")
    if not (0 < wait_s <= MAX_WAIT_S):
        raise HTTPException(400, f"wait_s must be in (0, {MAX_WAIT_S:.0f}]")


async def _run_and_wait(
    *, agents: list[str], prompt: str, mode: str, hand: str | None,
    timeout_s: int | None, wait_s: float, group_id: str | None = None,
):
    """Durable fan-out + bounded synchronous wait (shared by /run and
    /groups/{id}/run).

    200: every task reached a terminal state within the budget — the joined
    verdict (with per-agent outputs) is the body, and the run row is settled.
    202: the budget elapsed first — body carries ``task_ids`` (in agents
    order) and ``run_id``; the tasks keep running, reconnect via
    GET /api/multi-agent/runs/{run_id} (or poll GET /api/tasks/{id}).
    503: executor spawning failed — the durable run is already ``failed``;
    body carries ``run_id`` and every task id that did spawn (those tasks keep
    running), so the failure is inspectable instead of becoming an opaque 500.
    """
    kwargs = {"hand": hand, "group_id": group_id}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    try:
        run = await multi_agent.start_run(agents, prompt, mode=mode, **kwargs)
    except multi_agent.RunSpawnError as exc:
        log.exception(
            "multi-agent run %s failed while spawning tasks (%d of %d spawned)",
            exc.run_id, len(exc.task_ids), exc.total_agents,
        )
        return JSONResponse(status_code=503, content={
            "detail": "task spawn failed; any returned task_ids keep running",
            "status": "failed",
            "error": exc.error,
            "mode": mode,
            "agents": list(agents),
            "task_ids": exc.task_ids,
            "run_id": exc.run_id,
        })
    task_ids: list[str] = run["task_ids"]
    try:
        tasks = await multi_agent.wait_fan_out(task_ids, timeout_s=wait_s)
    except asyncio.TimeoutError:
        log.info(
            "multi-agent run %s exceeded wait_s=%.0fs; returning 202 with %d task ids",
            run["id"], wait_s, len(task_ids),
        )
        return JSONResponse(status_code=202, content={
            "detail": f"wait budget of {wait_s:.0f}s elapsed; tasks keep running",
            "mode": mode,
            "agents": list(agents),
            "task_ids": task_ids,          # agents order; poll GET /api/tasks/{id}
            "run_id": run["id"],           # reconnect: GET /api/multi-agent/runs/{run_id}
        })
    result = multi_agent.join(tasks, mode)
    # annotate each per-task projection with the agent it belongs to
    # (join keeps fan-out order, which is the request's agents order)
    for agent_id, item in zip(agents, result["outputs"]):
        item["agent"] = agent_id
    result["run_id"] = run["id"]
    await multi_agent.settle_run(run["id"])   # persist the structured verdict
    return result


@router.post("/run")
async def run(body: RunBody):
    """Fan out, wait up to ``wait_s`` seconds total, join (see _run_and_wait)."""
    if not body.agents:
        raise HTTPException(400, "agents must not be empty")
    if len(body.agents) > MAX_AGENTS:
        raise HTTPException(400, f"at most {MAX_AGENTS} agents per run (got {len(body.agents)})")
    if len(set(body.agents)) != len(body.agents):
        raise HTTPException(400, "duplicate agent ids in run")
    unknown = [a for a in body.agents if get_analyst(a) is None]
    if unknown:
        raise HTTPException(400, f"unknown analysts: {', '.join(unknown)}")
    if not body.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    if body.mode not in multi_agent.JOIN_MODES:
        raise HTTPException(
            400, f"unknown mode {body.mode!r} (expected one of: {', '.join(multi_agent.JOIN_MODES)})"
        )
    _check_budgets(body.timeout_s, body.wait_s)
    return await _run_and_wait(
        agents=body.agents, prompt=body.prompt, mode=body.mode, hand=body.hand,
        timeout_s=body.timeout_s, wait_s=body.wait_s,
    )


# ---- groups (M8-012) ----------------------------------------------------------

class GroupBody(StrictBody):
    name: str
    agents: list[str]
    description: str = ""
    mode: str = "all"
    hand: str | None = None


class GroupPatch(StrictBody):
    name: str | None = None
    agents: list[str] | None = None
    description: str | None = None
    mode: str | None = None
    hand: str | None = None


class GroupRunBody(StrictBody):
    prompt: str
    timeout_s: int | None = None
    wait_s: float = DEFAULT_WAIT_S


@router.post("/groups")
async def create_group(body: GroupBody):
    try:
        return await multi_agent.create_group(
            body.name, body.agents,
            description=body.description, mode=body.mode, hand=body.hand,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/groups")
async def list_groups(limit: int = 100):
    return await multi_agent.list_groups(limit=limit)


@router.get("/groups/{group_id}")
async def get_group(group_id: str):
    group = await multi_agent.get_group(group_id)
    if group is None:
        raise HTTPException(404, "group not found")
    return group


@router.put("/groups/{group_id}")
async def update_group(group_id: str, body: GroupPatch):
    try:
        group = await multi_agent.update_group(group_id, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if group is None:
        raise HTTPException(404, "group not found")
    return group


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str):
    if not await multi_agent.delete_group(group_id):
        raise HTTPException(404, "group not found")
    return Response(status_code=204)


@router.post("/groups/{group_id}/run")
async def run_group(group_id: str, body: GroupRunBody):
    """Run the panel with its stored routing strategy (agents/mode/hand come
    from the group; the request supplies the prompt and budgets)."""
    group = await multi_agent.get_group(group_id)
    if group is None:
        raise HTTPException(404, "group not found")
    if not body.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    _check_budgets(body.timeout_s, body.wait_s)
    return await _run_and_wait(
        agents=list(group["agents"]), prompt=body.prompt, mode=group["mode"],
        hand=group["hand"], timeout_s=body.timeout_s, wait_s=body.wait_s,
        group_id=group["id"],
    )


# ---- run history & reconnect (M8-012) -------------------------------------------

@router.get("/runs")
async def run_history(group_id: str | None = None, status: str | None = None, limit: int = 50):
    """Persisted run history, newest first (rows as stored — the single-run
    read settles; this list stays a cheap projection)."""
    try:
        return await multi_agent.list_run_records(group_id=group_id, status=status, limit=limit)
    except ValueError as exc:  # unknown status filter
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """The reconnect read: settle-on-read (a finished-but-unsettled run gets
    its structured verdict claimed now), plus live per-task ``outputs`` with
    full text re-read from the tasks rows."""
    record = await multi_agent.get_run_record(run_id)
    if record is None:
        raise HTTPException(404, "run not found")
    record["outputs"] = await multi_agent.run_outputs(record)
    return record
