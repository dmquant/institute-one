"""The execution core — one path for every model invocation.

``submit()`` awaits a hand run; ``spawn()`` is the fire-and-forget flavor for
autonomous loops. Every invocation is a row in the ``tasks`` table (the audit
spine). There is no queue service and no polling: dispatch is a function call
under semaphores, completion is a function return plus a bus event.

Concurrency: one global semaphore (settings.max_concurrent) plus one mutex per
hand (a CLI binary runs at most one task at a time).
Crash recovery: ``recover_orphans()`` marks non-terminal rows failed at boot;
domain loops re-drive themselves from their own durable pending rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..hands.base import OnChunk
from ..hands.registry import get_registry

log = logging.getLogger("institute.executor")

TERMINAL = {"completed", "failed", "rate_limited", "cancelled", "expired"}

_global_sem: asyncio.Semaphore | None = None
_hand_locks: dict[str, asyncio.Lock] = {}
_running: dict[str, asyncio.Task] = {}  # task_id -> asyncio task (for cancel)


def _sem() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(get_settings().max_concurrent)
    return _global_sem


def _hand_lock(name: str) -> asyncio.Lock:
    if name not in _hand_locks:
        _hand_locks[name] = asyncio.Lock()
    return _hand_locks[name]


@dataclass
class Task:
    id: str
    status: str
    hand: str | None
    requested_hand: str
    model: str | None
    prompt: str
    source: str
    session_id: str | None
    parent_run_id: str | None
    workspace_dir: str
    exit_code: int | None = None
    output: str = ""
    error: str | None = None
    artifacts: list[str] | None = None
    tried: list[str] | None = None

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Task":
        return cls(
            id=r["id"], status=r["status"], hand=r["hand"], requested_hand=r["requested_hand"],
            model=r["model"], prompt=r["prompt"], source=r["source"], session_id=r["session_id"],
            parent_run_id=r["parent_run_id"], workspace_dir=r["workspace_dir"] or "",
            exit_code=r["exit_code"], output=r["output"] or "", error=r["error"],
            artifacts=json.loads(r["artifacts"] or "[]"), tried=json.loads(r["tried"] or "[]"),
        )


def compact_error(text: str, cap: int = 1000) -> str:
    """Keep the most informative line first, cap total size."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    lines = [l for l in text.splitlines() if l.strip()]
    head = lines[-1] if lines else text[:200]
    return (head + "\n…\n" + text[:cap - len(head) - 5]).strip()[:cap]


async def _create_row(
    *, task_id: str, hand: str, prompt: str, source: str, model: str | None,
    session_id: str | None, parent_run_id: str | None, workspace: Path, timeout_s: int,
) -> None:
    await db.execute(
        """INSERT INTO tasks (id, session_id, requested_hand, model, prompt, status, source,
                              parent_run_id, workspace_dir, timeout_s, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, session_id, hand, model, prompt, "queued", source,
         parent_run_id, str(workspace), timeout_s, bus.now_iso()),
    )
    await bus.emit("task.queued", "task", task_id, {"hand": hand, "source": source})


async def _finish(task_id: str, status: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [bus.now_iso(), status, task_id]
    await db.execute(
        f"UPDATE tasks SET {sets}{', ' if sets else ''}finished_at = ?, status = ? WHERE id = ?",
        params,
    )
    await bus.emit(f"task.{status}", "task", task_id, {"status": status})


async def _execute(task_id: str, *, on_chunk: OnChunk | None = None, allow_fallback: bool = True) -> Task:
    settings = get_settings()
    registry = get_registry()
    row = await db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise ValueError(f"unknown task {task_id}")
    requested = row["requested_hand"]
    prompt: str = row["prompt"]
    model: str | None = row["model"]
    timeout_s: int = row["timeout_s"] or settings.default_timeout_s
    workspace = Path(row["workspace_dir"])

    hand, tried = registry.resolve(requested, allow_fallback=allow_fallback)
    await db.execute("UPDATE tasks SET tried = ? WHERE id = ?", (json.dumps(tried), task_id))
    if hand is None:
        await _finish(task_id, "rate_limited", error=f"no hand available (tried: {', '.join(tried)})")
        return await get_task(task_id)  # type: ignore[return-value]

    if hand.name != requested:
        model = None  # never carry an explicit model across a hand family boundary

    async with _sem(), _hand_lock(hand.name):
        claimed = await db.execute(
            "UPDATE tasks SET status='running', hand=?, model=?, started_at=? WHERE id=? AND status='queued'",
            (hand.name, model, bus.now_iso(), task_id),
        )
        if claimed == 0:  # cancelled while queued
            return await get_task(task_id)  # type: ignore[return-value]
        await bus.emit("task.running", "task", task_id, {"hand": hand.name})

        try:
            result = await asyncio.wait_for(
                hand.execute(prompt, workspace, model=model, timeout_s=timeout_s, on_chunk=on_chunk),
                timeout=timeout_s + 30,  # belt over the hand's own timeout
            )
        except asyncio.TimeoutError:
            registry.record_result(hand.name, ok=False)
            await _finish(task_id, "expired", error=f"timed out after {timeout_s}s", exit_code=-1)
            return await get_task(task_id)  # type: ignore[return-value]
        except asyncio.CancelledError:
            await _finish(task_id, "cancelled", error="cancelled by operator")
            raise
        except Exception as exc:  # noqa: BLE001 - a hand bug must not kill the loop
            log.exception("hand %s crashed", hand.name)
            registry.record_result(hand.name, ok=False)
            await _finish(task_id, "failed", error=compact_error(str(exc)), exit_code=-1)
            return await get_task(task_id)  # type: ignore[return-value]

    output = (result.output or "")[: settings.output_cap_bytes]

    if result.rate_limit is not None:
        registry.mark_rate_limited(hand.name, result.rate_limit)
        registry.record_result(hand.name, ok=False, rate_limited=True)
        # one automatic retry on the next hand in the chain
        if allow_fallback:
            nxt, _ = registry.resolve(requested, allow_fallback=True)
            if nxt is not None and nxt.name != hand.name:
                await db.execute(
                    "UPDATE tasks SET status='queued', hand=NULL, started_at=NULL WHERE id=? AND status='running'",
                    (task_id,),
                )
                # row may already be terminal if cancelled; only retry if requeue succeeded
                check = await db.query_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
                if check and check["status"] == "queued":
                    return await _execute(task_id, on_chunk=on_chunk, allow_fallback=allow_fallback)
        await _finish(
            task_id, "rate_limited",
            output=output, exit_code=result.exit_code,
            error=compact_error(result.rate_limit.raw or result.rate_limit.reason),
        )
        return await get_task(task_id)  # type: ignore[return-value]

    ok = result.exit_code == 0
    registry.record_result(hand.name, ok=ok)
    await _finish(
        task_id, "completed" if ok else "failed",
        output=output, exit_code=result.exit_code,
        artifacts=json.dumps(result.artifacts or []),
        error=None if ok else compact_error(output[-2000:] if output else "non-zero exit"),
    )
    return await get_task(task_id)  # type: ignore[return-value]


async def submit(
    hand: str,
    prompt: str,
    *,
    source: str = "api",
    model: str | None = None,
    session_id: str | None = None,
    parent_run_id: str | None = None,
    workspace: Path | None = None,
    timeout_s: int | None = None,
    fallback: bool = True,
    on_chunk: OnChunk | None = None,
) -> Task:
    """Run a hand and wait for the result. THE way to invoke a model."""
    settings = get_settings()
    task_id = uuid.uuid4().hex[:12]
    ws = workspace or (settings.workspaces_dir / "adhoc" / task_id)
    ws.mkdir(parents=True, exist_ok=True)
    await _create_row(
        task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
        session_id=session_id, parent_run_id=parent_run_id, workspace=ws,
        timeout_s=timeout_s or settings.default_timeout_s,
    )
    atask = asyncio.ensure_future(_execute(task_id, on_chunk=on_chunk, allow_fallback=fallback))
    _running[task_id] = atask
    try:
        return await atask
    finally:
        _running.pop(task_id, None)


async def spawn(hand: str, prompt: str, **kwargs: Any) -> str:
    """Fire-and-forget submit. Returns the task id immediately."""
    settings = get_settings()
    task_id = uuid.uuid4().hex[:12]
    ws = kwargs.pop("workspace", None) or (settings.workspaces_dir / "adhoc" / task_id)
    ws.mkdir(parents=True, exist_ok=True)
    on_chunk = kwargs.pop("on_chunk", None)
    fallback = kwargs.pop("fallback", True)
    timeout_s = kwargs.pop("timeout_s", None) or settings.default_timeout_s
    await _create_row(
        task_id=task_id, hand=hand, prompt=prompt,
        source=kwargs.pop("source", "api"), model=kwargs.pop("model", None),
        session_id=kwargs.pop("session_id", None), parent_run_id=kwargs.pop("parent_run_id", None),
        workspace=ws, timeout_s=timeout_s,
    )
    atask = asyncio.create_task(_execute(task_id, on_chunk=on_chunk, allow_fallback=fallback))
    _running[task_id] = atask
    atask.add_done_callback(lambda _t: _running.pop(task_id, None))
    return task_id


async def get_task(task_id: str) -> Task | None:
    row = await db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return Task.from_row(row) if row else None


async def list_tasks(
    *, status: str | None = None, hand: str | None = None, source: str | None = None,
    session_id: str | None = None, parent_run_id: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    where, params = [], []
    for col, val in (("status", status), ("hand", hand), ("source", source),
                     ("session_id", session_id), ("parent_run_id", parent_run_id)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT id, session_id, hand, requested_hand, model, status, source, exit_code, error, parent_run_id, created_at, started_at, finished_at FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(limit, 500))
    return await db.query(sql, params)


async def cancel(task_id: str) -> bool:
    atask = _running.get(task_id)
    n = await db.execute(
        "UPDATE tasks SET status='cancelled', finished_at=?, error='cancelled by operator' "
        "WHERE id=? AND status IN ('queued','running')",
        (bus.now_iso(), task_id),
    )
    if atask is not None and not atask.done():
        atask.cancel()
        return True
    return n > 0


async def recover_orphans() -> int:
    """Boot-time sweep: any non-terminal task row was orphaned by a restart."""
    n = await db.execute(
        "UPDATE tasks SET status='failed', error='orphaned by restart', finished_at=? "
        "WHERE status IN ('queued','running')",
        (bus.now_iso(),),
    )
    if n:
        log.warning("marked %d orphaned tasks failed", n)
    return n


async def queue_stats() -> dict[str, Any]:
    rows = await db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    by_status = {r["status"]: r["n"] for r in rows}
    return {"by_status": by_status, "running_now": len(_running)}
