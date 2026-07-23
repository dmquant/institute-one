"""Mailbox — operator <-> analyst threads.

Each operator note triggers a *dispatch*: a pending dispatch row in
``mailbox_messages`` plus a background coroutine.  The coroutine atomically
binds one durable executor task before driving it, then atomically commits the
terminal dispatch, unique reply, thread timestamp, and durable event.  A
bounded task-aware ``sweep()`` re-drives or settles rows orphaned by a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..hands.registry import get_registry
from ..router import executor
from ..util import new_id
from .analysts import get_analyst

log = logging.getLogger("institute.mailbox")

HISTORY_LIMIT = 10
PER_MESSAGE_CAP = 1500
# Max dispatches one sweep() firing re-drives (P11h): a restart with a large
# orphan backlog trickles out over successive ticks instead of stampeding the
# executor in one; skipped rows (in-flight / still driven) don't consume it.
SWEEP_REDRIVE_LIMIT = 20
# Max candidate rows one sweep() firing reads (R3 P2). Rows still driven by a
# live task are filtered in SQL; the keyset cursor below advances the window
# across firings and wraps at the tail, so an in-flight head cannot starve
# orphans sorted behind it.
SWEEP_SCAN_LIMIT = 100
SWEEP_CURSOR_KEY = "mailbox_sweep_cursor"
# Dispatch lease TTL floor (R4 P1, 0040): a pending row whose lease is younger
# than this belongs to a live worker (or a crash younger than the horizon) and
# is not sweepable; older leases are reclaimed. The effective TTL is computed
# at every expiry check as max(this floor, settings.default_timeout_s + 300),
# so raising the executor timeout always widens the lease horizon with it —
# a slow-but-alive dispatch is never re-driven from under its worker.
DISPATCH_LEASE_TTL_S = 45 * 60
# Hard model-attempt ceiling (R5 P1): every task booking increments this in
# the SAME transaction as the durable task row and dispatch binding.
DISPATCH_MAX_ATTEMPTS = 3
# A completed task is valuable durable output.  Transient reply/event commit
# failures retry settlement without another model call, but a permanently
# corrupt row/trigger must not spin forever either.
DISPATCH_MAX_RECONCILE_ATTEMPTS = 5

# Dispatch message ids being driven by THIS process (sweep skips these).
_inflight: set[int] = set()
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro: Any) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


# ---- public API ------------------------------------------------------------

async def create_thread(subject: str, analyst_id: str, body: str) -> dict[str, Any]:
    if get_analyst(analyst_id) is None:
        raise ValueError(f"unknown analyst {analyst_id}")
    thread_id = new_id()
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO mailbox_threads (id, subject, analyst_id, status, created_at, updated_at) "
        "VALUES (?,?,?,'open',?,?)",
        (thread_id, subject, analyst_id, now, now),
    )
    await db.insert(
        "INSERT INTO mailbox_messages (thread_id, author, kind, body, status, created_at) "
        "VALUES (?,'operator','note',?,'done',?)",
        (thread_id, body, now),
    )
    await _dispatch(thread_id)
    return await get_thread(thread_id) or {"id": thread_id}


async def reply(thread_id: str, body: str) -> dict[str, Any]:
    thread = await db.query_one("SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,))
    if thread is None:
        raise ValueError(f"unknown thread {thread_id}")
    now = bus.now_iso()
    # an operator reply reopens a closed thread
    await db.execute(
        "UPDATE mailbox_threads SET status='open', updated_at=? WHERE id=?", (now, thread_id)
    )
    await db.insert(
        "INSERT INTO mailbox_messages (thread_id, author, kind, body, status, created_at) "
        "VALUES (?,'operator','note',?,'done',?)",
        (thread_id, body, now),
    )
    await _dispatch(thread_id)
    return await get_thread(thread_id) or {"id": thread_id}


async def list_threads(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT t.*, (SELECT COUNT(*) FROM mailbox_messages m WHERE m.thread_id = t.id) AS n_messages "
        "FROM mailbox_threads t"
    )
    params: list[Any] = []
    if status:
        sql += " WHERE t.status = ?"
        params.append(status)
    sql += " ORDER BY t.updated_at DESC LIMIT ?"
    params.append(min(limit, 200))
    return await db.query(sql, params)


async def get_thread(thread_id: str) -> dict[str, Any] | None:
    thread = await db.query_one("SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,))
    if thread is None:
        return None
    thread["messages"] = await db.query(
        "SELECT * FROM mailbox_messages WHERE thread_id = ? ORDER BY id", (thread_id,)
    )
    return thread


async def close_thread(thread_id: str) -> dict[str, Any]:
    thread = await db.query_one("SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,))
    if thread is None:
        raise ValueError(f"unknown thread {thread_id}")
    await db.execute(
        "UPDATE mailbox_threads SET status='closed', updated_at=? WHERE id=? AND status='open'",
        (bus.now_iso(), thread_id),
    )
    return await get_thread(thread_id) or thread


# ---- dispatch --------------------------------------------------------------

async def _dispatch(thread_id: str) -> None:
    """Insert the pending dispatch row and spawn the coroutine that drives it."""
    thread = await db.query_one("SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,))
    if thread is None:
        raise ValueError(f"unknown thread {thread_id}")
    message_id = await db.insert(
        "INSERT INTO mailbox_messages (thread_id, author, kind, body, status, created_at) "
        "VALUES (?,?,'dispatch','','pending',?)",
        (thread_id, thread["analyst_id"], bus.now_iso()),
    )
    _spawn_bg(_run_dispatch(thread_id, message_id))


def _stale_lease_cutoff() -> str:
    """Leases written strictly before this instant are reclaimable.

    TTL = max(DISPATCH_LEASE_TTL_S, settings.default_timeout_s + 300): the
    constant is only a floor, so a configured executor timeout longer than
    40 minutes still cannot be swept out from under its live worker.
    """
    ttl = max(DISPATCH_LEASE_TTL_S, get_settings().default_timeout_s + 300)
    return (
        datetime.fromisoformat(bus.now_iso()) - timedelta(seconds=ttl)
    ).isoformat(timespec="seconds")


class _DispatchBookingRefused(Exception):
    """Rollback control flow for the atomic dispatch/task booking."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _prepare_dispatch(
    thread_id: str, message_id: int,
) -> tuple[dict[str, Any], Any, str, str]:
    """Build the deterministic prompt before claiming a model attempt."""
    settings = get_settings()
    thread = await db.query_one("SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,))
    if thread is None:
        raise ValueError(f"unknown mailbox thread {thread_id}")
    analyst = get_analyst(thread["analyst_id"])
    if analyst is None:
        raise ValueError(f"unknown analyst {thread['analyst_id']}")
    history = await db.query(
        "SELECT * FROM mailbox_messages WHERE thread_id=? AND kind IN ('note','reply') AND id<? "
        "ORDER BY id DESC LIMIT ?",
        (thread_id, message_id, HISTORY_LIMIT),
    )
    history.reverse()
    lines = []
    for message in history:
        who = "操作员" if message["author"] == "operator" else analyst.name
        body = (message["body"] or "").strip()
        if len(body) > PER_MESSAGE_CAP:
            body = body[:PER_MESSAGE_CAP] + "…"
        lines.append(f"{who}：{body}")
    context = "## 对话记录（最近）\n\n" + "\n\n".join(lines) if lines else ""
    task_text = (
        f"这是你与操作员的邮件线程「{thread['subject']}」。\n"
        "请以你的分析师身份直接回复操作员的最新留言：简明、直接、先结论后论据；"
        "事实性论断给出来源。不要写文件，直接输出回复正文。"
    )
    from . import memory
    prompt = await memory.prompt_with_memory(
        analyst, task_text, context_blocks=[context] if context else None,
    )
    # opt-in weighted pick (settings.enable_hand_weights, default False): the
    # pool is the 'mailbox' scope's positive weight rows; an explicit
    # analyst.hand always wins and no pick keeps the default hand.
    hand = get_registry().pick_weighted("mailbox", explicit=analyst.hand) or (
        analyst.hand or settings.default_hand
    )
    return thread, analyst, hand, prompt


async def _book_dispatch_task(
    thread_id: str,
    message_id: int,
    *,
    hand: str,
    model: str | None,
    prompt: str,
) -> tuple[str | None, str | None, str]:
    """Atomically bind one bounded attempt to one born-queued task.

    The dispatch claim, attempts increment, task id, and executor row commit
    together.  Capacity pressure consumes nothing and a losing concurrent
    worker rolls back cleanly.
    """
    over = await executor._overcommit_depth(hand)  # noqa: SLF001 - same admission policy
    if over is not None:
        await db.execute(
            "UPDATE mailbox_messages SET dispatch_error=? "
            "WHERE id=? AND kind='dispatch' AND status='pending' AND task_id IS NULL",
            (f"hand '{hand}' has {over[0]} queued tasks (cap {over[1]}); deferred",
             message_id),
        )
        return None, None, "capacity"

    settings = get_settings()
    task_id = new_id()
    lease_id = uuid.uuid4().hex
    workspace = settings.workspaces_dir / "adhoc" / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    now = bus.now_iso()
    try:
        async with db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE mailbox_messages SET lease_id=?, leased_at=?, task_id=?, "
                "dispatch_attempts=dispatch_attempts+1, reconcile_attempts=0, "
                "dispatch_error=NULL "
                "WHERE id=? AND thread_id=? AND kind='dispatch' AND status='pending' "
                "AND task_id IS NULL AND dispatch_attempts < ? "
                "AND (lease_id IS NULL OR leased_at IS NULL OR leased_at < ?)",
                (lease_id, now, task_id, message_id, thread_id,
                 DISPATCH_MAX_ATTEMPTS, _stale_lease_cutoff()),
            )
            claimed = bool(cur.rowcount)
            await cur.close()
            if not claimed:
                raise _DispatchBookingRefused("lost")
            await executor.book_prepared(
                conn=conn, task_id=task_id, hand=hand, model=model, prompt=prompt,
                source="mailbox", workspace=workspace,
                timeout_s=settings.default_timeout_s, mailbox_dispatch_id=message_id,
            )
    except _DispatchBookingRefused as refusal:
        return None, None, refusal.reason

    try:
        await bus.emit("task.queued", "task", task_id, {"hand": hand, "source": "mailbox"})
    except Exception:  # noqa: BLE001 - observability cannot orphan prepared work
        log.exception("task.queued emit failed for mailbox dispatch %s", message_id)
    return task_id, lease_id, "ok"


async def _drive_bound_task(task_id: str) -> executor.Task:
    """Drive or join one already durable queued task through executor core."""
    return await executor.submit_prepared(task_id)


def _task_deadline_passed(task: dict[str, Any]) -> bool:
    """True only beyond the task's persisted execution deadline."""
    if not task.get("started_at"):
        return False
    try:
        started = datetime.fromisoformat(str(task["started_at"]))
        timeout_s = int(task.get("timeout_s") or get_settings().default_timeout_s)
        return datetime.fromisoformat(bus.now_iso()) > started + timedelta(seconds=timeout_s + 60)
    except (TypeError, ValueError):
        return False


def _task_matches_dispatch(task: dict[str, Any], dispatch: dict[str, Any]) -> bool:
    return (
        task.get("id") == dispatch.get("task_id")
        and task.get("mailbox_dispatch_id") == dispatch.get("id")
        and task.get("source") == "mailbox"
    )


async def _release_failed_attempt(
    dispatch: dict[str, Any], task: dict[str, Any], reason: str,
) -> bool:
    """Release one terminal failed task or stop at the hard attempt ceiling."""
    terminal = int(dispatch.get("dispatch_attempts") or 0) >= DISPATCH_MAX_ATTEMPTS
    next_status = "failed" if terminal else "pending"
    n = await db.execute(
        "UPDATE mailbox_messages SET status=?, "
        "task_id=CASE WHEN ?='pending' THEN NULL ELSE task_id END, "
        "lease_id=NULL, leased_at=NULL, reconcile_attempts=0, dispatch_error=? "
        "WHERE id=? AND kind='dispatch' AND status='pending' "
        "AND task_id=? AND lease_id IS ?",
        (next_status, next_status, reason[:500], dispatch["id"], task["id"],
         dispatch.get("lease_id")),
    )
    return bool(n)


async def _quarantine_binding(dispatch: dict[str, Any], reason: str) -> bool:
    """Fail closed when a dispatch/task binding cannot be proven."""
    n = await db.execute(
        "UPDATE mailbox_messages SET status='failed', lease_id=NULL, leased_at=NULL, "
        "dispatch_error=? WHERE id=? AND kind='dispatch' AND status='pending' "
        "AND task_id IS ? AND lease_id IS ?",
        (reason[:500], dispatch["id"], dispatch.get("task_id"), dispatch.get("lease_id")),
    )
    return bool(n)


async def _record_reconcile_failure(dispatch: dict[str, Any], error: str) -> None:
    """Bound retries for a poison completed-result settlement."""
    await db.execute(
        "UPDATE mailbox_messages SET reconcile_attempts=reconcile_attempts+1, "
        "status=CASE WHEN reconcile_attempts+1>=? THEN 'failed' ELSE status END, "
        "lease_id=CASE WHEN reconcile_attempts+1>=? THEN NULL ELSE lease_id END, "
        "leased_at=NULL, dispatch_error=? "
        "WHERE id=? AND kind='dispatch' AND status='pending' "
        "AND task_id IS ? AND lease_id IS ? AND reconcile_attempts<?",
        (DISPATCH_MAX_RECONCILE_ATTEMPTS, DISPATCH_MAX_RECONCILE_ATTEMPTS,
         error[:500], dispatch["id"], dispatch.get("task_id"),
         dispatch.get("lease_id"), DISPATCH_MAX_RECONCILE_ATTEMPTS),
    )


async def _record_unbound_failure(message_id: int, error: str) -> None:
    """Bound deterministic failures that happen before a task can be booked."""
    await db.execute(
        "UPDATE mailbox_messages SET dispatch_attempts=dispatch_attempts+1, "
        "status=CASE WHEN dispatch_attempts+1>=? THEN 'failed' ELSE status END, "
        "lease_id=NULL, leased_at=NULL, dispatch_error=? "
        "WHERE id=? AND kind='dispatch' AND status='pending' AND task_id IS NULL "
        "AND dispatch_attempts<?",
        (DISPATCH_MAX_ATTEMPTS, error[:500], message_id, DISPATCH_MAX_ATTEMPTS),
    )


async def _settle_completed_dispatch(
    dispatch: dict[str, Any], task: dict[str, Any],
) -> bool:
    """Commit dispatch + unique reply + thread timestamp + event atomically."""
    thread = await db.query_one(
        "SELECT * FROM mailbox_threads WHERE id=?", (dispatch["thread_id"],)
    )
    if thread is None:
        return await _quarantine_binding(dispatch, "mailbox thread is missing")
    analyst = get_analyst(thread["analyst_id"])
    if analyst is None:
        return await _quarantine_binding(dispatch, f"unknown analyst {thread['analyst_id']}")
    body = _clean_reply(str(task.get("output") or ""))
    if not body:
        return await _release_failed_attempt(dispatch, task, "completed task returned empty output")

    now = bus.now_iso()
    event_id: int | None = None
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE mailbox_messages SET status='done', dispatch_error=NULL "
            "WHERE id=? AND kind='dispatch' AND status='pending' "
            "AND task_id=? AND lease_id IS ?",
            (dispatch["id"], task["id"], dispatch.get("lease_id")),
        )
        claimed = bool(cur.rowcount)
        await cur.close()
        if not claimed:
            return False
        cur = await conn.execute(
            "INSERT INTO mailbox_messages "
            "(thread_id, author, kind, body, task_id, status, created_at, dispatch_id) "
            "VALUES (?,?,'reply',?,?,'done',?,?)",
            (dispatch["thread_id"], analyst.id, body, task["id"], now, dispatch["id"]),
        )
        reply_id = int(cur.lastrowid or 0)
        await cur.close()
        await conn.execute(
            "UPDATE mailbox_threads SET updated_at=? WHERE id=?",
            (now, dispatch["thread_id"]),
        )
        payload = {
            "analyst_id": analyst.id,
            "dispatch_id": dispatch["id"],
            "reply_id": reply_id,
            "task_id": task["id"],
        }
        cur = await conn.execute(
            "INSERT INTO events (type, ref_kind, ref_id, payload, created_at) "
            "VALUES ('mailbox.reply','thread',?,?,?)",
            (dispatch["thread_id"], json.dumps(payload, ensure_ascii=False), now),
        )
        event_id = int(cur.lastrowid or 0)
        await cur.close()
        await conn.execute(
            "UPDATE mailbox_messages SET reply_event_id=?, lease_id=NULL, leased_at=NULL "
            "WHERE id=? AND status='done' AND task_id=?",
            (event_id, dispatch["id"], task["id"]),
        )
    if event_id:
        try:
            await bus.publish_durable(event_id)
        except Exception:  # noqa: BLE001 - durable cursor row already committed
            log.exception("could not live-publish durable mailbox event %s", event_id)
    return True


async def _reconcile_bound_dispatch(
    dispatch: dict[str, Any], *, drive_queued: bool = True,
) -> None:
    """Converge one bound task without ever creating another generation."""
    if not dispatch.get("lease_id"):
        await _quarantine_binding(dispatch, "bound mailbox task has no lease")
        return
    task = await db.query_one("SELECT * FROM tasks WHERE id=?", (dispatch["task_id"],))
    if task is None or not _task_matches_dispatch(task, dispatch):
        await _quarantine_binding(dispatch, "missing or mismatched mailbox task binding")
        return
    status = str(task["status"])
    if status == "completed":
        try:
            await _settle_completed_dispatch(dispatch, task)
        except Exception as exc:  # noqa: BLE001 - keep completed output recoverable
            log.exception("mailbox dispatch %s settlement failed", dispatch["id"])
            await _record_reconcile_failure(dispatch, str(exc))
        return
    if status in {"failed", "rate_limited", "cancelled", "expired", "overcommitted"}:
        await _release_failed_attempt(
            dispatch, task, f"task {task['id']} ended {status}: {task.get('error') or ''}",
        )
        return
    active = executor._running.get(task["id"])  # noqa: SLF001
    if status == "running" and (active is None or active.done()):
        if _task_deadline_passed(task):
            moved = await db.execute(
                "UPDATE tasks SET status='failed', error='orphaned mailbox task past deadline', "
                "finished_at=? WHERE id=? AND status='running' AND mailbox_dispatch_id=?",
                (bus.now_iso(), task["id"], dispatch["id"]),
            )
            if moved:
                task["status"] = "failed"
                task["error"] = "orphaned mailbox task past deadline"
                await _release_failed_attempt(dispatch, task, task["error"])
        return
    if not drive_queued and active is None:
        return
    if status == "queued" or (active is not None and not active.done()):
        try:
            await _drive_bound_task(task["id"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - durable task remains for sweep/boot
            log.exception("driving prepared mailbox task %s failed", task["id"])
            return
        fresh = await db.query_one(
            "SELECT * FROM mailbox_messages WHERE id=? AND kind='dispatch'",
            (dispatch["id"],),
        )
        if fresh and fresh["status"] == "pending" and fresh["task_id"] == task["id"]:
            await _reconcile_bound_dispatch(fresh, drive_queued=False)


async def _run_dispatch(thread_id: str, message_id: int) -> None:
    """Book/drive/settle one dispatch. Durable DB claims are the arbiter."""
    _inflight.add(message_id)  # telemetry only; never gates stale recovery
    try:
        dispatch = await db.query_one(
            "SELECT * FROM mailbox_messages WHERE id=? AND thread_id=? AND kind='dispatch'",
            (message_id, thread_id),
        )
        if dispatch is None or dispatch["status"] != "pending":
            return
        if dispatch.get("task_id"):
            await _reconcile_bound_dispatch(dispatch)
            return
        if int(dispatch.get("dispatch_attempts") or 0) >= DISPATCH_MAX_ATTEMPTS:
            await db.execute(
                "UPDATE mailbox_messages SET status='failed', lease_id=NULL, leased_at=NULL, "
                "dispatch_error=COALESCE(dispatch_error,'retry limit reached') "
                "WHERE id=? AND kind='dispatch' AND status='pending' AND task_id IS NULL",
                (message_id,),
            )
            return
        try:
            _thread, analyst, hand, prompt = await _prepare_dispatch(thread_id, message_id)
        except Exception as exc:  # noqa: BLE001 - deterministic poison is bounded
            log.exception("preparing mailbox dispatch %s failed", message_id)
            await _record_unbound_failure(message_id, str(exc))
            return
        task_id, _lease_id, reason = await _book_dispatch_task(
            thread_id, message_id, hand=hand, model=analyst.model, prompt=prompt,
        )
        if task_id is None:
            if reason not in {"lost", "capacity"}:
                await _record_unbound_failure(message_id, reason)
            return
        dispatch = await db.query_one(
            "SELECT * FROM mailbox_messages WHERE id=? AND kind='dispatch'", (message_id,)
        )
        if dispatch is not None and dispatch["status"] == "pending":
            await _reconcile_bound_dispatch(dispatch)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - runs as a bare background task
        log.exception("dispatch %s on thread %s crashed; durable row retained", message_id, thread_id)
    finally:
        _inflight.discard(message_id)


# ---- maintenance ------------------------------------------------------------

async def _sweep_cursor() -> int | None:
    """Persisted keyset position of the sweep scan (None = head of id order).

    One admin_state row (no migration); missing/corrupt state degrades to a
    head scan — the same fail-open posture the feature switches use.
    """
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (SWEEP_CURSOR_KEY,)
    )
    if row is None:
        return None
    try:
        value = json.loads(row["value"])
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    except Exception:  # noqa: BLE001 - corrupt cursor means scan from the head
        pass
    return None


async def _set_sweep_cursor(cursor: int | None) -> None:
    if cursor is None:
        await db.execute("DELETE FROM admin_state WHERE key = ?", (SWEEP_CURSOR_KEY,))
        return
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SWEEP_CURSOR_KEY, json.dumps(cursor)),
    )


async def sweep() -> None:
    """Re-drive dispatch rows orphaned by a restart. Never raises.

    One firing reads at most SWEEP_SCAN_LIMIT candidates and spawns at most
    SWEEP_REDRIVE_LIMIT reconcilers.  An unbound row needs a stale/no lease;
    a bound row is eligible immediately when its task is terminal/missing,
    or after the lease ages while queued/running.  Re-entry always uses the
    SAME task id and its queued->running claim, so lease age can never create
    a second model call.  ``_inflight`` is deliberately ignored: DB state is
    the liveness arbiter and a hung local coroutine must not veto recovery.
    """
    try:
        cursor = await _sweep_cursor()
        sql = (
            "SELECT m.id, m.thread_id FROM mailbox_messages m "
            "WHERE m.kind = 'dispatch' AND m.status = 'pending' "
            "AND ((m.task_id IS NULL "
            "      AND (m.lease_id IS NULL OR m.leased_at IS NULL OR m.leased_at < ?)) "
            "  OR (m.task_id IS NOT NULL "
            "      AND ((m.lease_id IS NULL OR m.leased_at IS NULL OR m.leased_at < ?) "
            "           OR NOT EXISTS (SELECT 1 FROM tasks t WHERE t.id = m.task_id "
            "                          AND t.status IN ('queued','running')))))"
        )
        cutoff = _stale_lease_cutoff()
        params: list[Any] = [cutoff, cutoff]
        if cursor is not None:
            sql += " AND m.id > ?"
            params.append(cursor)
        sql += " ORDER BY m.id LIMIT ?"
        params.append(SWEEP_SCAN_LIMIT)
        rows = await db.query(sql, params)

        # Full window -> resume after it; short window = tail -> wrap. A
        # cap-break overrides with the last row actually scheduled, so the
        # unscanned remainder of this window comes up next firing.
        next_cursor = rows[-1]["id"] if len(rows) == SWEEP_SCAN_LIMIT else None
        redriven = 0
        for i, r in enumerate(rows):
            if redriven >= SWEEP_REDRIVE_LIMIT:
                next_cursor = rows[i - 1]["id"]
                log.info(
                    "mailbox sweep re-drive cap (%d) reached; %d row(s) deferred to the next tick",
                    SWEEP_REDRIVE_LIMIT, len(rows) - i,
                )
                break
            log.info("sweep re-driving dispatch %s on thread %s", r["id"], r["thread_id"])
            _spawn_bg(_run_dispatch(r["thread_id"], r["id"]))
            redriven += 1
        await _set_sweep_cursor(next_cursor)
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("mailbox sweep failed")


async def recover_orphans(*, redrive: bool = True) -> int:
    """Boot hook: invalidate process-owned leases, then adopt prepared work.

    ``executor.recover_orphans()`` preserves/requeues only reciprocal pending
    mailbox tasks.  Once the old process is gone its lease cannot be live, so
    clear it and normally let the bounded sweep attach drivers or settle
    terminal output immediately. ``redrive=False`` stops after invalidating
    the dead lease so maintenance-paused boot cannot start model work; the
    existing gated mailbox sweep adopts it after resume.
    """
    reset = await db.execute(
        "UPDATE mailbox_messages SET leased_at=NULL "
        "WHERE kind='dispatch' AND status='pending' AND lease_id IS NOT NULL"
    )
    if redrive:
        await sweep()
    return reset


# ---- helpers -----------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NOISE_RE = re.compile(r"^(\[[^\]]*\]|DONE:.*|⏺.*|>+\s*)$")


def _clean_reply(text: str) -> str:
    """Strip ANSI codes and leading CLI noise lines from a hand's output."""
    text = _ANSI_RE.sub("", text or "").strip()
    lines = text.splitlines()
    while lines and (not lines[0].strip() or _NOISE_RE.match(lines[0].strip())):
        lines.pop(0)
    cleaned = "\n".join(lines).strip()
    return cleaned or text
