"""Deep research queue: dedup, full tick on echo, daily cap."""
from __future__ import annotations

from app import bus, db
from app.config import get_settings
from app.institute import research, workflows
from app.institute.prompts import work_date


async def test_enqueue_dedups_same_topic_pending():
    first = await research.enqueue("NVDA", source="test")
    assert first["status"] == "pending"
    assert "deduped" not in first

    second = await research.enqueue("NVDA", source="test")
    assert second.get("deduped") is True
    assert second["id"] == first["id"]

    rows = await research.list_queue(status="pending")
    assert len([r for r in rows if r["topic"] == "NVDA"]) == 1


async def test_tick_runs_research_workflow_to_completed():
    await workflows.reconcile_from_disk()
    item = await research.enqueue("AAPL", source="test")

    item_id = await research.tick()
    assert item_id == item["id"]

    done = await research.get_item(item_id)
    assert done["status"] == "completed"
    assert done["run_id"]
    assert done["run"]["status"] == "completed"
    assert len(done["run"]["results"]) == 7  # 6 research steps + follow-ups
    assert all(r["status"] == "completed" for r in done["run"]["results"])

    log_rows = await db.query("SELECT * FROM research_log WHERE topic = ?", ("AAPL",))
    assert len(log_rows) == 1
    assert log_rows[0]["run_id"] == done["run_id"]

    events = await bus.replay(0, types=["research.completed"])
    mine = [e for e in events if e.ref_id == item_id]
    assert len(mine) == 1
    assert mine[0].payload["topic"] == "AAPL"

    # nothing left to do: another tick is a no-op
    assert await research.tick() is None


async def test_daily_cap_respected(monkeypatch):
    await workflows.reconcile_from_disk()
    monkeypatch.setattr(get_settings(), "research_daily_cap", 1)

    # one research already completed today (work_date is the SGT calendar date)
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at) VALUES (?,?,?,?)",
        ("ALREADY-DONE", "run0", "done earlier today", f"{work_date()}T00:00:00+00:00"),
    )

    item = await research.enqueue("TSLA", source="test")
    assert item["status"] == "pending"

    assert await research.tick() is None  # cap reached: nothing claimed
    after = await research.get_item(item["id"])
    assert after["status"] == "pending"
