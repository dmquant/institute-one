"""The execution core — one path for every model invocation.

``submit()`` awaits a hand run; ``spawn()`` is the fire-and-forget flavor for
autonomous loops. Every invocation is a row in the ``tasks`` table (the audit
spine). There is no queue service and no polling: dispatch is a function call
under semaphores, completion is a function return plus a bus event.
Rows that must pre-exist inside a caller's own transaction (pre-booked work:
factcheck verification bookings, mailbox dispatches, canonical revival
children) are INSERTed through ``book_prepared()`` — one canonical column
list — then driven by ``submit_prepared()``, the same execution core and
``_running`` registration entered one layer in.

Concurrency: one global semaphore (settings.max_concurrent) plus one mutex per
hand (a CLI binary runs at most one task at a time), acquired hand-mutex first
so a busy hand's waiters never pin global slots. Admission: a per-hand
queued-depth cap (settings.hand_queue_depth) sheds excess submits/spawns as
born-terminal 'overcommitted' rows instead of queueing without bound.
Crash recovery: ``recover_orphans()`` marks non-terminal rows failed at boot;
domain loops re-drive themselves from their own durable pending rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import bus, db
from ..config import get_settings
from ..hands.base import OnChunk
from ..hands.registry import get_registry
from ..util import new_id

log = logging.getLogger("institute.executor")

TERMINAL = {"completed", "failed", "rate_limited", "cancelled", "expired", "overcommitted"}
# Non-terminal tasks statuses; TERMINAL | ACTIVE is the full CHECK enum
# (0001 as rebuilt by 0028, which added 'overcommitted').
# Canonical import point for API surfaces (e.g. /api/contract) — do not restate.
ACTIVE = ("queued", "running")

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


def hand_busy(name: str) -> bool:
    """True while the per-hand mutex is held (a task is running on that hand).

    Locks are created lazily by ``_hand_lock()``: a hand that never ran has no
    lock and is not busy. Waiters do not hold the lock, so "busy" means one
    task is executing on the hand or is about to (it may hold the mutex while
    waiting for a global slot — the hand-lock-first order); it says nothing
    about queue depth behind it.
    Read-only — callers (interactive ask routing, ROADMAP Phase 0) use it to
    prefer an idle hand instead of queueing behind a long workflow step.
    """
    lock = _hand_locks.get(name)
    return lock is not None and lock.locked()


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
    fallback_chain: list[str] | None = None  # persisted policy (0024); None = registry default
    lineage_root: str | None = None          # original task of a retry chain (0024)

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Task":
        return cls(
            id=r["id"], status=r["status"], hand=r["hand"], requested_hand=r["requested_hand"],
            model=r["model"], prompt=r["prompt"], source=r["source"], session_id=r["session_id"],
            parent_run_id=r["parent_run_id"], workspace_dir=r["workspace_dir"] or "",
            exit_code=r["exit_code"], output=r["output"] or "", error=r["error"],
            artifacts=json.loads(r["artifacts"] or "[]"), tried=json.loads(r["tried"] or "[]"),
            fallback_chain=json.loads(r["fallback_chain"]) if r["fallback_chain"] else None,
            lineage_root=r["lineage_root"],
        )


@dataclass(frozen=True)
class PreparedRespawn:
    """One durable source -> canonical retry binding.

    ``created`` distinguishes the transaction winner from a caller that
    converged on the already-bound canonical child. Both callers receive the
    same immutable task id.
    """

    task_id: str
    lineage_root: str
    created: bool


@dataclass(frozen=True)
class _RespawnSpec:
    task_id: str
    source_task_id: str
    lineage_root: str
    hand: str
    prompt: str
    source: str
    model: str | None
    session_id: str | None
    parent_run_id: str | None
    workspace: Path
    timeout_s: int
    fallback_chain: tuple[str, ...] | None


TRUNCATION_MARKER = "\n…[truncated]"


def truncate_output(text: str, cap_bytes: int) -> str:
    """Byte-aware cap on tasks.output (settings.output_cap_bytes).

    The old char-based slice under-capped multi-byte (CJK) output by up to 4x
    and truncated silently. This cuts on UTF-8 code-point boundaries and ends
    with an explicit marker so readers know the output is partial.
    """
    if not text:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= cap_bytes:
        return text
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    if cap_bytes <= marker_bytes:
        # cap smaller than the marker itself: the marker would blow the cap,
        # so degrade to a bare head slice (content beats an all-marker output)
        return raw[: max(cap_bytes, 0)].decode("utf-8", errors="ignore")
    keep = cap_bytes - marker_bytes
    # errors="ignore" drops the tail bytes of a code point split by the cut
    return raw[:keep].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def compact_error(text: str, cap: int = 1000) -> str:
    """Keep the head and tail (first + last lines), cap total size.

    The old form promoted the last line to the front and appended a head
    slice — reading order was inverted, and an overlong last line made the
    budget negative, silently dropping the first line entirely.
    """
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    head_budget = max(1, cap * 3 // 5)
    tail_budget = max(1, cap - head_budget - 3)  # 3 = len("\n…\n")
    return f"{text[:head_budget].rstrip()}\n…\n{text[-tail_budget:].lstrip()}"[:cap]


def _prepared_row_statement(
    *, task_id: str, hand: str, prompt: str, source: str, model: str | None,
    session_id: str | None, parent_run_id: str | None, workspace: Path, timeout_s: int,
    fallback_chain: Sequence[str] | None = None, lineage_root: str | None = None,
    revived_from_task_id: str | None = None, mailbox_dispatch_id: int | None = None,
    status: str = "queued", error: str | None = None,
    now: str | None = None, finished_at: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    # THE canonical tasks INSERT: ONE column list for every pre-booked row
    # (submit/spawn, factcheck bookings, mailbox dispatches, canonical revival
    # children, born-terminal overcommitted rows). Per-site extra columns are
    # optional parameters, never a forked column list — a new tasks column is
    # mirrored here once. fallback_chain/lineage_root persist the caller's
    # actual execution policy and retry ancestry (0024): the retry endpoint
    # replays the STORED chain instead of re-deriving it from live settings,
    # and the 0024 partial unique index on lineage_root arbitrates duplicate
    # live retries — this INSERT raises IntegrityError when the lineage
    # already has a live task.
    created = now or bus.now_iso()
    return (
        """INSERT INTO tasks (id, session_id, requested_hand, model, prompt, status, source,
                              parent_run_id, workspace_dir, timeout_s, fallback_chain,
                              lineage_root, revived_from_task_id, mailbox_dispatch_id,
                              error, created_at, finished_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, session_id, hand, model, prompt, status, source,
         parent_run_id, str(workspace), timeout_s,
         None if fallback_chain is None else json.dumps(list(fallback_chain)),
         lineage_root, revived_from_task_id, mailbox_dispatch_id, error,
         created, finished_at),
    )


async def book_prepared(
    *, task_id: str, hand: str, prompt: str, source: str, model: str | None = None,
    session_id: str | None = None, parent_run_id: str | None = None,
    workspace: Path, timeout_s: int,
    fallback_chain: Sequence[str] | None = None, lineage_root: str | None = None,
    revived_from_task_id: str | None = None, mailbox_dispatch_id: int | None = None,
    conn: Any | None = None,
) -> None:
    """INSERT one born-'queued' task row — the public pre-booking entry point.

    Every pre-booked row shares the canonical column list
    (``_prepared_row_statement``); per-site extras (``revived_from_task_id``
    for canonical revival children, ``mailbox_dispatch_id`` for mailbox
    dispatches) are optional parameters. With ``conn`` the INSERT joins the
    caller's transaction (domain claim + task row commit together) and the
    caller emits task.queued AFTER commit — bus.emit under the transaction's
    write lock would deadlock; without it the row is written and the event
    emitted here. Booking never drives the row: attach the in-process driver
    with ``submit_prepared()``.
    """
    sql, params = _prepared_row_statement(
        task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
        session_id=session_id, parent_run_id=parent_run_id, workspace=workspace,
        timeout_s=timeout_s, fallback_chain=fallback_chain,
        lineage_root=lineage_root, revived_from_task_id=revived_from_task_id,
        mailbox_dispatch_id=mailbox_dispatch_id,
    )
    if conn is not None:
        await _conn_execute(conn, sql, params)
        return
    await db.execute(sql, params)
    await bus.emit("task.queued", "task", task_id, {"hand": hand, "source": source})


async def _create_row(
    *, task_id: str, hand: str, prompt: str, source: str, model: str | None,
    session_id: str | None, parent_run_id: str | None, workspace: Path, timeout_s: int,
    fallback_chain: Sequence[str] | None = None, lineage_root: str | None = None,
    revived_from_task_id: str | None = None,
) -> None:
    await book_prepared(
        task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
        session_id=session_id, parent_run_id=parent_run_id, workspace=workspace,
        timeout_s=timeout_s, fallback_chain=fallback_chain,
        lineage_root=lineage_root, revived_from_task_id=revived_from_task_id,
    )


async def _queued_depth(hand: str) -> int:
    """Queued rows already waiting on this hand (by requested_hand: ``hand``
    stays NULL until the running claim, and fallback must not let a busy
    hand's backlog hide behind the hand that eventually picks a task up)."""
    row = await db.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE requested_hand = ? AND status = 'queued'",
        (hand,),
    )
    return int(row["n"]) if row else 0


async def _overcommit_depth(hand: str) -> tuple[int, int] | None:
    """(depth, cap) when the hand's queued backlog EXCEEDS the cap, else None.

    The cap is the tolerated backlog: a submit finding exactly cap queued
    rows is still admitted, one finding more is shed. Strictly-greater
    matters: a legitimate burst (e.g. the analyst-daily sweep fanning the
    whole roster onto one hand) parks up to roster-size rows in 'queued'
    while the per-hand mutex drains them one by one — the cap bounds runaway
    pileups without shedding the tail of a normal burst. Best-effort: the
    count and the later INSERT are separate statements, so concurrent
    submits can overshoot by a few rows (no invariant rides on the exact
    number). cap <= 0 disables the check entirely.
    """
    cap = get_settings().hand_queue_depth
    if cap <= 0:
        return None
    depth = await _queued_depth(hand)
    return (depth, cap) if depth > cap else None


async def _create_overcommitted_row(
    *, task_id: str, hand: str, prompt: str, source: str, model: str | None,
    session_id: str | None, parent_run_id: str | None, workspace: Path, timeout_s: int,
    fallback_chain: Sequence[str] | None, lineage_root: str | None,
    depth: int, cap: int,
) -> None:
    """Persist a fast-fail as a born-terminal 'overcommitted' row.

    The row IS the audit trail (every model request = one tasks row), but it
    never enters the queue: hand stays NULL, finished_at == created_at, and
    the only event is task.overcommitted — no task.queued, so nothing ever
    treats it as pending work.
    """
    now = bus.now_iso()
    sql, params = _prepared_row_statement(
        task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
        session_id=session_id, parent_run_id=parent_run_id, workspace=workspace,
        timeout_s=timeout_s, fallback_chain=fallback_chain, lineage_root=lineage_root,
        status="overcommitted",
        error=f"hand '{hand}' already has {depth} queued tasks (cap {cap})",
        now=now, finished_at=now,
    )
    await db.execute(sql, params)
    await bus.emit(
        "task.overcommitted", "task", task_id,
        {"hand": hand, "source": source, "status": "overcommitted"},
    )


async def _finish(task_id: str, status: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [bus.now_iso(), status, task_id]
    await db.execute(
        f"UPDATE tasks SET {sets}{', ' if sets else ''}finished_at = ?, status = ? WHERE id = ?",
        params,
    )
    await bus.emit(f"task.{status}", "task", task_id, {"status": status})


def _fallback_candidates(requested: str, fallback_chain: Sequence[str] | None) -> list[str] | None:
    if fallback_chain is None:
        return None
    return [requested, *(h for h in fallback_chain if h != requested)]


async def _execute(
    task_id: str,
    *,
    on_chunk: OnChunk | None = None,
    allow_fallback: bool = True,
    fallback_chain: Sequence[str] | None = None,
) -> Task:
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

    candidates = _fallback_candidates(requested, fallback_chain) if allow_fallback else None
    if candidates is None:
        hand, tried = registry.resolve(requested, allow_fallback=allow_fallback)
    else:
        hand, tried = registry.resolve_chain(candidates)
    await db.execute("UPDATE tasks SET tried = ? WHERE id = ?", (json.dumps(tried), task_id))
    if hand is None:
        await _finish(task_id, "rate_limited", error=f"no hand available (tried: {', '.join(tried)})")
        return await get_task(task_id)  # type: ignore[return-value]

    if hand.name != requested:
        model = None  # never carry an explicit model across a hand family boundary

    # Lock order (LOOP-P1): hand mutex FIRST, global semaphore second — and
    # nowhere else acquires both, so the order is process-wide and deadlock-free.
    # The old order (semaphore first) let tasks parked behind ONE busy hand pin
    # global slots while waiting for its mutex, starving every idle hand once
    # max_concurrent waiters piled up. Waiting on a hand lock now costs nothing
    # globally; a semaphore slot is only held while a task actually runs.
    async with _hand_lock(hand.name), _sem():
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

    output = truncate_output(result.output or "", settings.output_cap_bytes)

    if result.rate_limit is not None:
        registry.mark_rate_limited(hand.name, result.rate_limit)
        registry.record_result(hand.name, ok=False, rate_limited=True)
        # one automatic retry on the next hand in the chain
        if allow_fallback:
            if candidates is None:
                nxt, _ = registry.resolve(requested, allow_fallback=True)
            else:
                nxt, _ = registry.resolve_chain(candidates)
            if nxt is not None and nxt.name != hand.name:
                await db.execute(
                    "UPDATE tasks SET status='queued', hand=NULL, started_at=NULL WHERE id=? AND status='running'",
                    (task_id,),
                )
                # row may already be terminal if cancelled; only retry if requeue succeeded
                check = await db.query_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
                if check and check["status"] == "queued":
                    return await _execute(
                        task_id,
                        on_chunk=on_chunk,
                        allow_fallback=allow_fallback,
                        fallback_chain=fallback_chain,
                    )
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
    fallback_chain: Sequence[str] | None = None,
    lineage_root: str | None = None,
    on_chunk: OnChunk | None = None,
) -> Task:
    """Run a hand and wait for the result. THE way to invoke a model."""
    settings = get_settings()
    task_id = new_id()
    ws = workspace or (settings.workspaces_dir / "adhoc" / task_id)
    ws.mkdir(parents=True, exist_ok=True)
    over = await _overcommit_depth(hand)
    if over is not None:
        await _create_overcommitted_row(
            task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
            session_id=session_id, parent_run_id=parent_run_id, workspace=ws,
            timeout_s=timeout_s or settings.default_timeout_s,
            fallback_chain=fallback_chain, lineage_root=lineage_root,
            depth=over[0], cap=over[1],
        )
        return await get_task(task_id)  # type: ignore[return-value]
    await _create_row(
        task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
        session_id=session_id, parent_run_id=parent_run_id, workspace=ws,
        timeout_s=timeout_s or settings.default_timeout_s,
        fallback_chain=fallback_chain, lineage_root=lineage_root,
    )
    atask = asyncio.ensure_future(
        _execute(task_id, on_chunk=on_chunk, allow_fallback=fallback, fallback_chain=fallback_chain)
    )
    _running[task_id] = atask
    try:
        return await atask
    finally:
        _running.pop(task_id, None)


async def spawn(hand: str, prompt: str, **kwargs: Any) -> str:
    """Fire-and-forget submit. Returns the task id immediately."""
    settings = get_settings()
    task_id = new_id()
    ws = kwargs.pop("workspace", None) or (settings.workspaces_dir / "adhoc" / task_id)
    ws.mkdir(parents=True, exist_ok=True)
    on_chunk = kwargs.pop("on_chunk", None)
    fallback = kwargs.pop("fallback", True)
    fallback_chain = kwargs.pop("fallback_chain", None)
    lineage_root = kwargs.pop("lineage_root", None)
    timeout_s = kwargs.pop("timeout_s", None) or settings.default_timeout_s
    source = kwargs.pop("source", "api")
    model = kwargs.pop("model", None)
    session_id = kwargs.pop("session_id", None)
    parent_run_id = kwargs.pop("parent_run_id", None)
    over = await _overcommit_depth(hand)
    if over is not None:
        await _create_overcommitted_row(
            task_id=task_id, hand=hand, prompt=prompt, source=source, model=model,
            session_id=session_id, parent_run_id=parent_run_id, workspace=ws,
            timeout_s=timeout_s, fallback_chain=fallback_chain, lineage_root=lineage_root,
            depth=over[0], cap=over[1],
        )
        return task_id
    await _create_row(
        task_id=task_id, hand=hand, prompt=prompt,
        source=source, model=model,
        session_id=session_id, parent_run_id=parent_run_id,
        workspace=ws, timeout_s=timeout_s,
        fallback_chain=fallback_chain, lineage_root=lineage_root,
    )
    atask = asyncio.create_task(
        _execute(task_id, on_chunk=on_chunk, allow_fallback=fallback, fallback_chain=fallback_chain)
    )
    _register_driver(task_id, atask)
    return task_id


async def submit_prepared(task_id: str) -> Task:
    """Drive one pre-booked 'queued' row to a terminal state. THE way to run
    a task whose row was booked ahead of time (``book_prepared()``).

    The same driver ``submit()`` attaches, entered one layer in because the
    row already exists: ``_execute``'s conditional queued→running claim, hand
    mutex + global semaphore, fallback, terminal settle — plus the
    ``_running`` registration that keeps operator cancel and the shutdown
    drain working on it. An already-live in-process driver for the same id
    is joined (shielded, so the joining caller's cancel never kills the
    owner) instead of doubled; ``_execute``'s conditional claim stays the
    final arbiter.
    """
    active = _running.get(task_id)
    if active is not None and not active.done():
        return await asyncio.shield(active)
    atask = asyncio.ensure_future(_execute(task_id))
    _register_driver(task_id, atask)
    try:
        return await atask
    finally:
        if _running.get(task_id) is atask:
            _running.pop(task_id, None)


def _legacy_retry_policy(source: str, requested_hand: str) -> tuple[str, dict[str, Any]]:
    """Rebuild policy only for rows whose persisted fallback_chain is NULL."""
    if source == "research":
        hands = get_settings().research_hand_names
        hand = requested_hand if requested_hand in hands else hands[0]
        return hand, {"fallback_chain": hands}
    return requested_hand, {}


def _respawn_spec(row: dict[str, Any], *, task_id: str | None = None) -> _RespawnSpec:
    """Reconstruct the exact persisted retry policy in one place.

    Manual retry (``respawn_from_row``) and the scheduler's transactional
    prepare path both consume this spec; scheduler.py never duplicates task
    row construction or fallback-policy rules.
    """
    task_id = task_id or new_id()
    requested = row["requested_hand"]
    if not requested:
        raise ValueError(f"task {row['id']} has no requested hand")
    lineage_root = row["lineage_root"] or row["id"]
    if row["fallback_chain"] is not None:
        hand = requested
        fallback_chain = tuple(json.loads(row["fallback_chain"]))
    else:
        hand, policy = _legacy_retry_policy(row["source"], requested)
        chain = policy.get("fallback_chain")
        fallback_chain = tuple(chain) if chain is not None else None
    # An explicit model never crosses a hand family boundary.
    model = row["model"] if hand == requested else None
    workspace = (
        Path(row["workspace_dir"])
        if row["workspace_dir"]
        else get_settings().workspaces_dir / "adhoc" / task_id
    )
    return _RespawnSpec(
        task_id=task_id,
        source_task_id=row["id"],
        lineage_root=lineage_root,
        hand=hand,
        prompt=row["prompt"],
        source=row["source"],
        model=model,
        session_id=row["session_id"],
        parent_run_id=row["parent_run_id"],
        workspace=workspace,
        timeout_s=row["timeout_s"] or get_settings().default_timeout_s,
        fallback_chain=fallback_chain,
    )


def _stored_chain(raw: str | None) -> tuple[str, ...] | None:
    return tuple(json.loads(raw)) if raw is not None else None


def prepared_respawn_matches(source: dict[str, Any], child: dict[str, Any]) -> bool:
    """Prove that ``child`` is the canonical retry built from ``source``.

    Used after an IntegrityError: only a reciprocal winner with the exact
    expected retry policy may converge. An unrelated CHECK/id/schema failure
    can never consume the source.
    """
    if not source.get("revival_task_id") or source["revival_task_id"] != child.get("id"):
        return False
    try:
        spec = _respawn_spec(source, task_id=child["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        child.get("revived_from_task_id") == source["id"]
        and child.get("lineage_root") == spec.lineage_root
        and child.get("requested_hand") == spec.hand
        and child.get("prompt") == spec.prompt
        and child.get("source") == spec.source
        and child.get("model") == spec.model
        and child.get("session_id") == spec.session_id
        and child.get("parent_run_id") == spec.parent_run_id
        and child.get("workspace_dir") == str(spec.workspace)
        and child.get("timeout_s") == spec.timeout_s
        and _stored_chain(child.get("fallback_chain")) == spec.fallback_chain
    )


async def get_canonical_respawn(source_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    source = await db.query_one("SELECT * FROM tasks WHERE id = ?", (source_id,))
    if source is None or not source["revival_task_id"]:
        return None
    child = await db.query_one(
        "SELECT * FROM tasks WHERE id = ?", (source["revival_task_id"],)
    )
    if child is None or not prepared_respawn_matches(source, child):
        return None
    return source, child


async def _conn_query_one(conn: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row is not None else None


async def _conn_execute(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    cur = await conn.execute(sql, params)
    rowcount = cur.rowcount
    await cur.close()
    return rowcount


async def prepare_respawn_from_row(
    conn: Any,
    row: dict[str, Any],
    *,
    max_attempts: int,
) -> PreparedRespawn | None:
    """Atomically bind a rate-limited source to one born-queued child.

    The caller owns ``conn``'s surrounding transaction. A capacity rejection
    returns None without changing source/attempt state. The source claim and
    child INSERT commit together; a losing caller converges on the reciprocal
    canonical child instead of creating another generation.
    """
    if row.get("revival_task_id"):
        child = await _conn_query_one(
            conn, "SELECT * FROM tasks WHERE id = ?", (row["revival_task_id"],)
        )
        if child is None or not prepared_respawn_matches(row, child):
            raise RuntimeError(f"invalid canonical revival binding for source {row['id']}")
        return PreparedRespawn(
            task_id=child["id"],
            lineage_root=child["lineage_root"],
            created=False,
        )

    spec = _respawn_spec(row)
    cap = get_settings().hand_queue_depth
    if cap > 0:
        depth = await _conn_query_one(
            conn,
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE requested_hand = ? AND status = 'queued'",
            (spec.hand,),
        )
        if depth is not None and int(depth["n"]) > cap:
            return None

    spec.workspace.mkdir(parents=True, exist_ok=True)
    claimed = await _conn_execute(
        conn,
        "UPDATE tasks SET revival_task_id = ?, "
        "revival_attempts = revival_attempts + 1, "
        "revival_lease_id = NULL, revival_leased_at = NULL "
        "WHERE id = ? AND status = 'rate_limited' "
        "AND revival_task_id IS NULL AND revived_from_task_id IS NULL "
        "AND revival_attempts < ?",
        (spec.task_id, row["id"], max_attempts),
    )
    if not claimed:
        winner = await _conn_query_one(
            conn, "SELECT * FROM tasks WHERE id = ?", (row["id"],)
        )
        if winner is None or not winner["revival_task_id"]:
            return None
        child = await _conn_query_one(
            conn, "SELECT * FROM tasks WHERE id = ?", (winner["revival_task_id"],)
        )
        if child is None or not prepared_respawn_matches(winner, child):
            raise RuntimeError(f"invalid canonical revival winner for source {row['id']}")
        return PreparedRespawn(
            task_id=child["id"],
            lineage_root=child["lineage_root"],
            created=False,
        )

    await book_prepared(
        conn=conn,
        task_id=spec.task_id,
        hand=spec.hand,
        prompt=spec.prompt,
        source=spec.source,
        model=spec.model,
        session_id=spec.session_id,
        parent_run_id=spec.parent_run_id,
        workspace=spec.workspace,
        timeout_s=spec.timeout_s,
        fallback_chain=spec.fallback_chain,
        lineage_root=spec.lineage_root,
        revived_from_task_id=row["id"],
    )
    return PreparedRespawn(
        task_id=spec.task_id,
        lineage_root=spec.lineage_root,
        created=True,
    )


def _register_driver(task_id: str, atask: asyncio.Task) -> None:
    _running[task_id] = atask

    def _done(done: asyncio.Task) -> None:
        if _running.get(task_id) is done:
            _running.pop(task_id, None)

    atask.add_done_callback(_done)


async def drive_prepared(task_id: str) -> bool:
    """Attach an in-process driver to one durable canonical queued child.

    Multiple callers may race (boot + scheduler): ``_execute``'s conditional
    queued->running UPDATE is the final arbiter, so only one reaches the hand.
    A missing task.queued event never strands the durable row.
    """
    active = _running.get(task_id)
    if active is not None and not active.done():
        return False
    row = await db.query_one(
        "SELECT child.* FROM tasks child "
        "JOIN tasks source ON source.id = child.revived_from_task_id "
        "AND source.revival_task_id = child.id "
        "WHERE child.id = ?",
        (task_id,),
    )
    if row is None:
        return False
    if row["status"] == "running":
        # No in-memory driver owns it in this process: this is restart residue
        # or a driver that died before settling the row. Requeue the same id.
        requeued = await db.execute(
            "UPDATE tasks SET status='queued', hand=NULL, started_at=NULL, "
            "finished_at=NULL, error='recovered orphaned prepared revival' "
            "WHERE id=? AND status='running' AND revived_from_task_id IS NOT NULL",
            (task_id,),
        )
        if not requeued:
            return False
        row["status"] = "queued"
    if row["status"] != "queued":
        return False

    try:
        await bus.emit(
            "task.queued", "task", task_id,
            {"hand": row["requested_hand"], "source": row["source"]},
        )
    except Exception:  # noqa: BLE001 - observability cannot orphan durable work
        log.exception("task.queued emit failed for prepared revival %s", task_id)

    fallback_chain = (
        json.loads(row["fallback_chain"]) if row["fallback_chain"] is not None else None
    )
    coro = _execute(task_id, allow_fallback=True, fallback_chain=fallback_chain)
    try:
        atask = asyncio.create_task(coro)
    except BaseException:
        coro.close()
        raise
    _register_driver(task_id, atask)
    return True


class _PreparedRequeueLost(RuntimeError):
    pass


async def requeue_prepared(
    source_id: str,
    task_id: str,
    *,
    max_attempts: int,
) -> bool:
    """Retry one terminal canonical child in place, bounded by source attempts.

    The immutable source<->child binding survives every attempt. Queue
    pressure is checked before the transaction and consumes no attempt.
    """
    pair = await get_canonical_respawn(source_id)
    if pair is None or pair[1]["id"] != task_id:
        return False
    source, child = pair
    if child["status"] not in {"failed", "rate_limited", "expired", "overcommitted"}:
        return False
    spec = _respawn_spec(source, task_id=task_id)
    over = await _overcommit_depth(spec.hand)
    if over is not None:
        return False

    try:
        async with db.transaction() as conn:
            claimed = await _conn_execute(
                conn,
                "UPDATE tasks SET revival_attempts = revival_attempts + 1 "
                "WHERE id=? AND status='rate_limited' AND revival_task_id=? "
                "AND revival_attempts < ?",
                (source_id, task_id, max_attempts),
            )
            if not claimed:
                return False
            moved = await _conn_execute(
                conn,
                "UPDATE tasks SET status='queued', hand=NULL, requested_hand=?, model=?, "
                "tried=NULL, output=NULL, error=NULL, artifacts=NULL, exit_code=NULL, "
                "started_at=NULL, finished_at=NULL, fallback_chain=? "
                "WHERE id=? AND revived_from_task_id=? "
                "AND status IN ('failed','rate_limited','expired','overcommitted')",
                (
                    spec.hand,
                    spec.model,
                    None if spec.fallback_chain is None else json.dumps(list(spec.fallback_chain)),
                    task_id,
                    source_id,
                ),
            )
            if not moved:
                raise _PreparedRequeueLost(task_id)
    except _PreparedRequeueLost:
        return False
    return True


async def respawn_from_row(row: dict[str, Any]) -> tuple[str, str]:
    """Spawn a new generation while preserving one task row's stored policy.

    Status eligibility and live-lineage checks belong to the caller. Keeping
    only the reconstruction here lets the manual retry endpoint and automatic
    rate-limit revival replay the exact same hand/model/chain semantics.
    """
    spec = _respawn_spec(row)
    new_id = await spawn(
        spec.hand,
        spec.prompt,
        source=spec.source,
        model=spec.model,
        session_id=spec.session_id,
        parent_run_id=spec.parent_run_id,
        workspace=Path(row["workspace_dir"]) if row["workspace_dir"] else None,
        timeout_s=spec.timeout_s,
        lineage_root=spec.lineage_root,
        fallback_chain=spec.fallback_chain,
    )
    return new_id, spec.lineage_root


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
    """Cancel ONE task — the per-task version of the shutdown drain's path.

    Order matters (ROADMAP Phase 0 cancel protocol):

    1. queued → conditional-claim the row straight to 'cancelled' FIRST,
       then wake any submit/spawn parked on the semaphore / per-hand mutex.
       The row flip must come first: cancelling the asyncio task alone would
       surface CancelledError at the lock await — outside the executor's
       CancelledError→_finish handler — leaving the row 'queued' forever.
       With the row already terminal, the woken task (or, without a wake,
       its eventual queued→running claim) has nothing left to persist.
    2. running → cancel the in-flight asyncio task from ``_running``: the
       hand kills its CLI process group (hands/base.run_subprocess) and the
       executor's CancelledError handler persists status='cancelled' — the
       exact mechanism the shutdown drain relies on, applied to one task.
    3. running row with no live asyncio task (shouldn't happen; restart
       residue is swept by recover_orphans) → flip the row directly so an
       operator is never stuck on a phantom.

    Returns False for terminal/unknown tasks (the API maps that to 409/404).
    """
    n = await db.execute(
        "UPDATE tasks SET status='cancelled', finished_at=?, error='cancelled while queued' "
        "WHERE id=? AND status='queued'",
        (bus.now_iso(), task_id),
    )
    if n:
        await bus.emit("task.cancelled", "task", task_id, {"status": "cancelled"})
        # a submit/spawn parked on the semaphore/mutex would otherwise sit
        # there until the lock frees, only to fail its claim; wake it now.
        # Safe: the row is already terminal, so the CancelledError surfacing
        # at the lock await has nothing left to persist.
        atask = _running.get(task_id)
        if atask is not None and not atask.done():
            atask.cancel()
        return True
    atask = _running.get(task_id)
    if atask is not None and not atask.done():
        row = await db.query_one(
            "SELECT 1 FROM tasks WHERE id = ? AND status = 'running'", (task_id,)
        )
        if row:  # a terminal row with a not-yet-reaped asyncio task is left alone
            atask.cancel()
            return True
        return False
    n = await db.execute(
        "UPDATE tasks SET status='cancelled', finished_at=?, error='cancelled by operator' "
        "WHERE id=? AND status='running'",
        (bus.now_iso(), task_id),
    )
    if n:
        await bus.emit("task.cancelled", "task", task_id, {"status": "cancelled"})
    return n > 0


async def recover_prepared(*, drive: bool = True) -> int:
    """Boot-time adoption of durable canonical revival children.

    A queued child is already prepared work and only needs an in-process
    driver. A running child lost that driver with the process, so conditionally
    requeue the SAME immutable id before driving it. The reciprocal binding is
    required; corrupt one-sided rows fall through to generic orphan failure.
    ``drive=False`` performs only the durable running-to-queued reconciliation;
    the gated revival scheduler can attach the driver after maintenance ends.
    """
    requeued = await db.execute(
        "UPDATE tasks SET status='queued', hand=NULL, started_at=NULL, "
        "finished_at=NULL, error='recovered orphaned prepared revival' "
        "WHERE status='running' AND revived_from_task_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM tasks source "
        "            WHERE source.id = tasks.revived_from_task_id "
        "            AND source.revival_task_id = tasks.id)"
    )
    if drive:
        rows = await db.query(
            "SELECT child.id FROM tasks child "
            "JOIN tasks source ON source.id = child.revived_from_task_id "
            "AND source.revival_task_id = child.id "
            "WHERE child.status='queued' ORDER BY child.created_at, child.id"
        )
        for row in rows:
            try:
                await drive_prepared(row["id"])
            except Exception:  # noqa: BLE001 - next scheduler tick retries same id
                log.exception("could not drive recovered prepared revival %s", row["id"])
    if requeued:
        log.warning("requeued %d orphaned prepared revival task(s)", requeued)
    return requeued


async def recover_orphans(*, drive_prepared: bool = True) -> int:
    """Boot-time sweep with durable revival and mailbox partitions.

    Generic non-terminal tasks are failed as before. Canonical revival
    children are outbox-like prepared work: preserve queued rows, requeue
    running rows, and normally attach drivers to the same ids. Mailbox tasks
    whose reciprocal pending dispatch still names them follow the same
    preserve/requeue rule; ``mailbox.recover_orphans()`` attaches their driver
    immediately after this generic executor pass.  ``drive_prepared=False``
    keeps every database transition but defers model execution to the gated
    scheduler, which is required when booting under maintenance.

    Offline twin: app/cli.py's check_orphans() counts this same queued/running
    residue (plus research_queue's) over a read-only connection — a
    status-vocabulary change must land in both.
    """
    recovered = await recover_prepared(drive=drive_prepared)
    mailbox_requeued = await db.execute(
        "UPDATE tasks SET status='queued', hand=NULL, started_at=NULL, "
        "finished_at=NULL, error='recovered orphaned prepared mailbox task' "
        "WHERE status='running' AND mailbox_dispatch_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM mailbox_messages m "
        "            WHERE m.id=tasks.mailbox_dispatch_id "
        "            AND m.kind='dispatch' AND m.status='pending' "
        "            AND m.task_id=tasks.id)"
    )
    failed = await db.execute(
        "UPDATE tasks SET status='failed', error='orphaned by restart', finished_at=? "
        "WHERE status IN ('queued','running') "
        "AND NOT EXISTS (SELECT 1 FROM tasks source "
        "                WHERE source.id = tasks.revived_from_task_id "
        "                AND source.revival_task_id = tasks.id) "
        "AND NOT EXISTS (SELECT 1 FROM mailbox_messages m "
        "                WHERE m.id=tasks.mailbox_dispatch_id "
        "                AND m.kind='dispatch' AND m.status='pending' "
        "                AND m.task_id=tasks.id)",
        (bus.now_iso(),),
    )
    if mailbox_requeued:
        log.warning("requeued %d orphaned prepared mailbox task(s)", mailbox_requeued)
    if failed:
        log.warning("marked %d orphaned tasks failed", failed)
    return recovered + mailbox_requeued + failed


async def queue_stats() -> dict[str, Any]:
    rows = await db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    by_status = {r["status"]: r["n"] for r in rows}
    return {"by_status": by_status, "running_now": len(_running)}
