"""Mailbox: create_thread dispatch produces an analyst reply on echo; clean sweep."""
from __future__ import annotations

import asyncio

from app import bus, db
from app.institute import mailbox


async def _wait_for_reply(thread_id: str, timeout_s: float = 5.0) -> dict:
    """Poll get_thread until the dispatch lands its reply."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        thread = await mailbox.get_thread(thread_id)
        replies = [m for m in thread["messages"] if m["kind"] == "reply"]
        if replies:
            return thread
        await asyncio.sleep(0.02)
    raise AssertionError(f"no reply on thread {thread_id} after {timeout_s}s")


async def test_create_thread_produces_reply_on_echo():
    thread = await mailbox.create_thread("利率展望", "macro-analyst", "请给出下周美债收益率的判断。")
    assert thread["status"] == "open"
    assert thread["analyst_id"] == "macro-analyst"
    kinds = [m["kind"] for m in thread["messages"]]
    assert kinds[0] == "note"        # operator note
    assert "dispatch" in kinds       # pending dispatch row spawned

    thread = await _wait_for_reply(thread["id"])
    replies = [m for m in thread["messages"] if m["kind"] == "reply"]
    assert len(replies) == 1
    assert replies[0]["author"] == "macro-analyst"
    assert replies[0]["body"].strip()

    dispatches = [m for m in thread["messages"] if m["kind"] == "dispatch"]
    assert all(m["status"] == "done" for m in dispatches)
    assert all(m["task_id"] for m in dispatches)

    events = await bus.replay(0, types=["mailbox.reply"])
    assert any(e.ref_id == thread["id"] for e in events)


async def test_sweep_is_noop_when_clean():
    thread = await mailbox.create_thread("收盘点评", "equity-analyst", "今天 A 股怎么看？")
    await _wait_for_reply(thread["id"])

    before = await db.query("SELECT id, status FROM mailbox_messages ORDER BY id")
    await mailbox.sweep()
    await asyncio.sleep(0.05)
    if mailbox._bg_tasks:  # sweep must not have spawned anything
        await asyncio.gather(*list(mailbox._bg_tasks), return_exceptions=True)

    after = await db.query("SELECT id, status FROM mailbox_messages ORDER BY id")
    assert after == before
    pending = await db.query(
        "SELECT id FROM mailbox_messages WHERE kind='dispatch' AND status='pending'"
    )
    assert pending == []
