"""Janitor retention for durable events and research-tree daily counters."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import bus, db
from app.config import get_settings
from app.institute import scheduler
from app.institute.prompts import now_sgt


async def _insert_event(event_type: str, created_at: str) -> int:
    return await db.insert(
        "INSERT INTO events (type, ref_kind, ref_id, payload, created_at) "
        "VALUES (?, 'test', ?, '{}', ?)",
        (event_type, event_type, created_at),
    )


async def test_events_retention_deletes_old_keeps_fresh_and_cursor_continues(monkeypatch):
    monkeypatch.setattr(get_settings(), "events_retention_days", 10)
    now = datetime.now(timezone.utc)
    old_id = await _insert_event(
        "retention.old", (now - timedelta(days=11)).isoformat(timespec="seconds")
    )
    fresh_id = await _insert_event(
        "retention.fresh", (now - timedelta(days=9)).isoformat(timespec="seconds")
    )

    await scheduler._janitor()

    assert await db.query_one("SELECT id FROM events WHERE id = ?", (old_id,)) is None
    assert await db.query_one("SELECT id FROM events WHERE id = ?", (fresh_id,)) is not None
    assert [e.id for e in await bus.replay(old_id)] == [fresh_id]

    emitted = await bus.emit("retention.after", "test", "after")
    assert emitted.id > fresh_id  # AUTOINCREMENT never reuses the deleted cursor
    replayed = await bus.replay(fresh_id)
    assert [(e.id, e.type) for e in replayed] == [(emitted.id, "retention.after")]


async def test_janitor_removes_only_booked_counters_older_than_30_days():
    today = now_sgt().date()
    prefix = scheduler.RESEARCH_TREE_BOOKED_PREFIX
    old_key = prefix + (today - timedelta(days=31)).isoformat()
    boundary_key = prefix + (today - timedelta(days=30)).isoformat()
    fresh_key = prefix + (today - timedelta(days=1)).isoformat()
    malformed_key = prefix + "not-a-date"
    for key in (old_key, boundary_key, fresh_key, malformed_key):
        await db.execute(
            "INSERT INTO admin_state (key, value) VALUES (?, '1')",
            (key,),
        )

    await scheduler._janitor()

    keys = {r["key"] for r in await db.query(
        "SELECT key FROM admin_state WHERE key LIKE 'research_tree_booked:%'"
    )}
    assert old_key not in keys
    assert {boundary_key, fresh_key, malformed_key} <= keys


async def test_events_retention_delete_is_batched():
    created_at = (
        datetime.now(timezone.utc) - timedelta(days=365)
    ).isoformat(timespec="seconds")
    total = scheduler.JANITOR_DELETE_LIMIT + 2
    rows = [
        (f"retention.batch.{i}", "test", str(i), "{}", created_at)
        for i in range(total)
    ]
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO events (type, ref_kind, ref_id, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            rows,
        )

    await scheduler._janitor()

    remaining = await db.query_one(
        "SELECT COUNT(*) AS n FROM events WHERE type LIKE 'retention.batch.%'"
    )
    assert remaining["n"] == 2
