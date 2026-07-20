"""Multi-agent primitives: fan_out / join (Phase 7).

``fan_out`` runs ONE prompt across several analysts in parallel — each agent
gets the standard persona sandwich (``prompts.build_analyst_prompt``) and one
executor task, so every invocation stays on THE execution path (hard rule 1)
and is audited as a normal ``tasks`` row. ``join`` folds the finished tasks
into a single verdict under one of four modes (all / first_success /
majority_vote / best_effort).

These are primitives, not a workflow: no session, no steps, no vault export —
callers (the API, future committee glue) decide what to do with the joined
result.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from ..config import get_settings
from ..router import executor
from .analysts import Analyst, get_analyst
from .prompts import build_analyst_prompt

log = logging.getLogger("institute.multi_agent")

JOIN_MODES = ("all", "first_success", "majority_vote", "best_effort")


async def spawn_fan_out(
    agents: Sequence[str],
    prompt: str,
    *,
    hand: str | None = None,
    timeout_s: int = 1800,
) -> list[str]:
    """Spawn one executor task per agent; return the task ids immediately.

    Every agent id must exist in the roster (ValueError BEFORE anything is
    spawned — no chief-strategist fallback here: the caller named the agents
    explicitly). Each agent becomes one ``executor.spawn()`` with the prompt
    wrapped in that analyst's persona sandwich; ids come back in agents order.

    ``hand`` overrides every agent's preferred hand; None falls back per agent
    to ``analyst.hand`` or ``settings.default_hand``.

    Error boundary: a spawn-layer failure (task-row insert etc.) propagates —
    agents already spawned at that point keep running; nothing rolls back.
    Hand/model failures never propagate: the executor folds them into the
    task row (status failed/expired/rate_limited).
    """
    if not agents:
        raise ValueError("agents must not be empty")
    roster: list[Analyst] = []
    for aid in agents:
        analyst = get_analyst(aid)
        if analyst is None:
            raise ValueError(f"unknown analyst {aid!r}")
        roster.append(analyst)
    settings = get_settings()
    return [
        await executor.spawn(
            hand or analyst.hand or settings.default_hand,
            build_analyst_prompt(analyst, prompt),
            source="multi_agent",
            model=analyst.model,
            timeout_s=timeout_s,
        )
        for analyst in roster
    ]


async def wait_fan_out(
    task_ids: Sequence[str], *, timeout_s: float | None = None,
) -> list[executor.Task]:
    """Wait until every spawned task leaves the executor, then return the rows.

    ``timeout_s`` is a WALL-CLOCK budget on the wait, not on the tasks: when
    it elapses, ``asyncio.TimeoutError`` is raised and the tasks KEEP RUNNING
    to completion in the background (``asyncio.wait`` never cancels pending
    awaitables) — the caller can re-read them later by id. Rows are returned
    in task_ids order.

    Truth lives in the tasks table: if a driver crashed without writing a
    terminal state (infrastructure bug), the returned row stays 'queued' /
    'running' — join() then counts it as not-completed instead of raising.
    """
    # executor._running holds the in-flight _execute futures (same registry
    # the test teardown drains); a missing id means the task already left.
    live = [f for tid in task_ids if (f := executor._running.get(tid)) is not None]
    if live:
        # asyncio.wait (not gather): a timeout must NOT cancel the tasks, and
        # a driver exception must stay in its future (row state is the truth)
        done, pending = await asyncio.wait(live, timeout=timeout_s)
        for f in done:  # retrieve driver exceptions so they don't log as unretrieved
            if not f.cancelled() and f.exception() is not None:
                log.warning("fan-out driver raised %r; the task row state is the truth", f.exception())
        if pending:
            raise asyncio.TimeoutError(f"{len(pending)} of {len(task_ids)} tasks still running")
    out: list[executor.Task] = []
    for tid in task_ids:
        task = await executor.get_task(tid)
        if task is None:  # unreachable: spawn persisted the row
            raise RuntimeError(f"task row {tid} disappeared")
        out.append(task)
    return out


async def fan_out(
    agents: Sequence[str],
    prompt: str,
    *,
    hand: str | None = None,
    timeout_s: int = 1800,
) -> list[executor.Task]:
    """Run one prompt across several analysts in parallel; wait for them all.

    ``spawn_fan_out`` + unbounded ``wait_fan_out`` (callers wanting a wall
    clock budget use the two halves directly, like the API does). Real
    concurrency is bounded by the executor's global semaphore and per-hand
    mutex (one CLI = one task at a time): tasks on the same hand serialize,
    distinct hands genuinely overlap. Returns finished Task rows in agents
    order.
    """
    return await wait_fan_out(await spawn_fan_out(agents, prompt, hand=hand, timeout_s=timeout_s))


def join(tasks: Sequence[executor.Task], mode: str) -> dict[str, Any]:
    """Fold finished fan_out tasks into one verdict dict.

    Modes:

    - ``all``: ok iff EVERY task completed; no single output is elected.
    - ``first_success``: output = the first completed task in fan-out order
      (fan_out already awaited everything, so "first" means submission order,
      not wall-clock finish order).
    - ``majority_vote``: ballots are the stripped output texts and only EXACT
      string equality counts as the same vote; ok iff one ballot takes a
      strict majority (> half) of ALL tasks — failures count against the
      quorum. Limitation: free-form model prose virtually never matches
      byte-for-byte, so this mode is only meaningful for constrained outputs
      (single-token verdicts, canonical JSON, enum answers); do not expect it
      to reconcile essays — ties and split votes yield ok=False.
    - ``best_effort``: never fails the join on individual errors; ok iff at
      least one task completed, and everything usable is in ``outputs``.

    Returns ``{"mode", "ok", "output", "outputs"[, "votes"]}`` where
    ``outputs`` is the per-task projection in fan-out order and ``output`` is
    the elected text (None when the mode elects nothing or nothing qualifies).
    """
    if mode not in JOIN_MODES:
        raise ValueError(f"unknown join mode {mode!r} (expected one of: {', '.join(JOIN_MODES)})")
    outputs = [
        {"task_id": t.id, "status": t.status, "output": t.output or "", "error": t.error}
        for t in tasks
    ]
    completed = [t for t in tasks if t.status == "completed"]
    result: dict[str, Any] = {"mode": mode, "ok": False, "output": None, "outputs": outputs}

    if mode == "all":
        result["ok"] = bool(tasks) and len(completed) == len(tasks)
    elif mode == "first_success":
        if completed:
            result["ok"] = True
            result["output"] = completed[0].output or ""
    elif mode == "majority_vote":
        tally: dict[str, int] = {}
        for t in completed:
            ballot = (t.output or "").strip()
            tally[ballot] = tally.get(ballot, 0) + 1
        result["votes"] = 0
        if tally:
            winner, votes = max(tally.items(), key=lambda kv: kv[1])
            result["votes"] = votes
            # a tie can never clear the strict-majority bar, so max() picking
            # the first-seen ballot on ties is safe (ok stays False)
            if votes * 2 > len(tasks):
                result["ok"] = True
                result["output"] = winner
    else:  # best_effort
        result["ok"] = bool(completed)

    return result
