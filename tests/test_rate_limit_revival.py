"""Scheduler revival for rate-limited tasks after their hand cooldown ends."""
from __future__ import annotations

import asyncio
import time

from app import bus, db
from app.hands.registry import get_registry
from app.institute import scheduler
from app.router import executor


async def _insert_rate_limited(
    task_id: str,
    *,
    workspace: str,
    source: str = "revival-test",
    fallback_chain: str | None = '["echo"]',
    lineage_root: str | None = None,
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO tasks "
        "(id, session_id, hand, requested_hand, model, prompt, status, source, error, "
        " workspace_dir, timeout_s, fallback_chain, lineage_root, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, "session-original", "echo", "echo", "echo-model", "revive me",
         "rate_limited", source, "quota exhausted", workspace, 60, fallback_chain,
         lineage_root, now, now),
    )


async def _insert_live(task_id: str, lineage_root: str, workspace: str) -> None:
    await db.execute(
        "INSERT INTO tasks "
        "(id, requested_hand, prompt, status, source, workspace_dir, timeout_s, "
        " lineage_root, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "echo", "already retrying", "queued", "revival-test", workspace,
         60, lineage_root, bus.now_iso()),
    )


async def _drain_executor() -> None:
    pending = list(executor._running.values())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_cooldown_not_expired_does_not_revive(tmp_path):
    await _insert_rate_limited("coolingtask01", workspace=str(tmp_path))
    get_registry()._cooldowns["echo"] = {
        "until": time.time() + 3600,
        "reason": "test",
        "marked_at": time.time(),
    }

    await scheduler._rate_limit_revival_job()

    assert await db.query("SELECT id FROM tasks WHERE lineage_root = 'coolingtask01'") == []
    old = await db.query_one("SELECT error FROM tasks WHERE id = 'coolingtask01'")
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER not in old["error"]


async def test_expired_cooldown_replays_chain_and_lineage_once(tmp_path):
    root = "originalroot1"
    await _insert_rate_limited(
        "limitedretry1",
        workspace=str(tmp_path),
        source="research",
        fallback_chain='["echo"]',
        lineage_root=root,
    )
    get_registry()._cooldowns["echo"] = {
        "until": time.time() - 1,
        "reason": "expired",
        "marked_at": time.time() - 120,
    }

    await scheduler._rate_limit_revival_job()
    await _drain_executor()

    rows = await db.query("SELECT id FROM tasks WHERE lineage_root = ?", (root,))
    assert len(rows) == 2  # terminal source generation + one revived generation
    new_id = next(r["id"] for r in rows if r["id"] != "limitedretry1")
    revived = await executor.get_task(new_id)
    assert revived is not None
    assert revived.status == "completed"
    assert revived.fallback_chain == ["echo"]
    assert revived.lineage_root == root
    assert revived.source == "research"
    assert revived.session_id == "session-original"

    old = await executor.get_task("limitedretry1")
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER in old.error

    # The child is now terminal, so only the durable source-row marker can
    # prevent a later tick from spawning this same generation again.
    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    assert len(await db.query("SELECT id FROM tasks WHERE lineage_root = ?", (root,))) == 2


async def test_existing_live_lineage_is_idempotently_skipped(tmp_path):
    root = "activeroot01"
    await _insert_rate_limited("activeorigin1", workspace=str(tmp_path), lineage_root=root)
    await _insert_live("activechild01", root, str(tmp_path))

    await scheduler._rate_limit_revival_job()

    rows = await db.query("SELECT id FROM tasks WHERE lineage_root = ? ORDER BY id", (root,))
    assert [r["id"] for r in rows] == ["activechild01", "activeorigin1"]
    old = await db.query_one("SELECT error FROM tasks WHERE id = 'activeorigin1'")
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER not in old["error"]


async def test_revival_tick_spawns_at_most_three(tmp_path):
    for i in range(4):
        await _insert_rate_limited(
            f"limit-task-{i}",
            workspace=str(tmp_path),
            source="revival-limit",
        )

    await scheduler._rate_limit_revival_job()
    await _drain_executor()

    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-limit' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == scheduler.RATE_LIMIT_REVIVAL_LIMIT == 3
    marked = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-limit' "
        "AND instr(COALESCE(error, ''), ?) > 0",
        (scheduler.RATE_LIMIT_REVIVAL_MARKER,),
    )
    assert len(marked) == 3

    # The next firing drains the one remaining row, rather than exceeding the
    # cap in the first firing or permanently starving it.
    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-limit' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == 4
