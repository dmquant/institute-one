"""Mailbox — operator <-> analyst threads.

Each operator note triggers a *dispatch*: a pending dispatch row in
``mailbox_messages`` plus a background coroutine that awaits the executor
directly and writes the analyst's reply back into the thread. ``sweep()``
re-drives dispatch rows orphaned by a restart.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..router.executor import TERMINAL
from .analysts import get_analyst
from .prompts import build_analyst_prompt

log = logging.getLogger("institute.mailbox")

HISTORY_LIMIT = 10
PER_MESSAGE_CAP = 1500

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
    thread_id = uuid.uuid4().hex[:12]
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


async def _run_dispatch(thread_id: str, message_id: int) -> None:
    """Await the executor directly and write the reply. Never raises."""
    if message_id in _inflight:
        return
    _inflight.add(message_id)
    try:
        settings = get_settings()
        thread = await db.query_one("SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,))
        msg = await db.query_one("SELECT * FROM mailbox_messages WHERE id = ?", (message_id,))
        if thread is None or msg is None or msg["status"] != "pending":
            return
        analyst = get_analyst(thread["analyst_id"])
        if analyst is None:
            await db.execute(
                "UPDATE mailbox_messages SET status='failed' WHERE id=? AND status='pending'",
                (message_id,),
            )
            return

        history = await db.query(
            "SELECT * FROM mailbox_messages WHERE thread_id=? AND kind IN ('note','reply') AND id<? "
            "ORDER BY id DESC LIMIT ?",
            (thread_id, message_id, HISTORY_LIMIT),
        )
        history.reverse()
        lines = []
        for m in history:
            who = "操作员" if m["author"] == "operator" else analyst.name
            body = (m["body"] or "").strip()
            if len(body) > PER_MESSAGE_CAP:
                body = body[:PER_MESSAGE_CAP] + "…"
            lines.append(f"{who}：{body}")
        context = "## 对话记录（最近）\n\n" + "\n\n".join(lines) if lines else ""

        task_text = (
            f"这是你与操作员的邮件线程「{thread['subject']}」。\n"
            "请以你的分析师身份直接回复操作员的最新留言：简明、直接、先结论后论据；"
            "事实性论断给出来源。不要写文件，直接输出回复正文。"
        )
        prompt = build_analyst_prompt(
            analyst, task_text, context_blocks=[context] if context else None
        )
        task = await executor.submit(
            analyst.hand or settings.default_hand, prompt,
            source="mailbox", model=analyst.model, session_id=None,
        )
        await db.execute(
            "UPDATE mailbox_messages SET task_id=? WHERE id=?", (task.id, message_id)
        )

        if task.status == "completed" and (task.output or "").strip():
            claimed = await db.execute(
                "UPDATE mailbox_messages SET status='done' WHERE id=? AND status='pending'",
                (message_id,),
            )
            if claimed:
                now = bus.now_iso()
                await db.insert(
                    "INSERT INTO mailbox_messages (thread_id, author, kind, body, status, created_at) "
                    "VALUES (?,?,'reply',?,'done',?)",
                    (thread_id, analyst.id, _clean_reply(task.output), now),
                )
                await db.execute(
                    "UPDATE mailbox_threads SET updated_at=? WHERE id=?", (now, thread_id)
                )
                await bus.emit("mailbox.reply", "thread", thread_id, {"analyst_id": analyst.id})
        else:
            await db.execute(
                "UPDATE mailbox_messages SET status='failed' WHERE id=? AND status='pending'",
                (message_id,),
            )
    except Exception:  # noqa: BLE001 - runs as a bare asyncio task
        log.exception("dispatch %s on thread %s crashed", message_id, thread_id)
        try:
            await db.execute(
                "UPDATE mailbox_messages SET status='failed' WHERE id=? AND status='pending'",
                (message_id,),
            )
        except Exception:  # noqa: BLE001
            log.exception("could not mark dispatch %s failed", message_id)
    finally:
        _inflight.discard(message_id)


# ---- maintenance ------------------------------------------------------------

async def sweep() -> None:
    """Re-drive dispatch rows orphaned by a restart. Never raises."""
    try:
        rows = await db.query(
            "SELECT id, thread_id, task_id FROM mailbox_messages "
            "WHERE kind='dispatch' AND status='pending' ORDER BY id"
        )
        for r in rows:
            if r["id"] in _inflight:
                continue
            if r["task_id"]:
                task = await executor.get_task(r["task_id"])
                if task is not None and task.status not in TERMINAL:
                    continue  # executor is still driving it
            log.info("sweep re-driving dispatch %s on thread %s", r["id"], r["thread_id"])
            _spawn_bg(_run_dispatch(r["thread_id"], r["id"]))
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("mailbox sweep failed")


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
