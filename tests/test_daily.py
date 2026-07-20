"""Daily products: the once-per-work-date guard (failed/cancelled leave the day open)."""
from __future__ import annotations

import json
import uuid

import pytest

from app import bus, db
from app.institute import daily, workflows
from app.institute.prompts import work_date


async def _insert_run(workflow_id: str, status: str, wd: str) -> str:
    """Insert a workflow_runs row the way the engine stores it."""
    run_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    finished = None if status == "running" else now
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, status, variables, source, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, workflow_id, status,
         json.dumps({"WORK_DATE": wd}, ensure_ascii=False), "test", now, finished),
    )
    return run_id


@pytest.mark.parametrize(
    ("status", "blocks"),
    [("running", True), ("completed", True), ("failed", False), ("cancelled", False)],
)
async def test_ran_today_guard_by_status(status, blocks):
    """_ran_today direct: running/completed block the day, failed/cancelled leave it open."""
    wd = work_date()
    await _insert_run("briefing", status, wd)
    assert await daily._ran_today("briefing", wd) is blocks
    # a run for another work date never blocks today
    assert await daily._ran_today("briefing", "1970-01-01") is False


async def test_completed_run_blocks_same_day_rerun():
    await workflows.reconcile_from_disk()
    first = await daily.run_briefing()
    assert first is not None
    run = await workflows.get_run(first)
    assert run["status"] == "completed"

    assert await daily.run_briefing() is None


async def test_cancelled_run_allows_same_day_rerun():
    await workflows.reconcile_from_disk()
    await _insert_run("briefing", "cancelled", work_date())

    run_id = await daily.run_briefing()
    assert run_id is not None
    run = await workflows.get_run(run_id)
    assert run["status"] == "completed"


async def test_failed_run_allows_same_day_rerun():
    await workflows.reconcile_from_disk()
    await _insert_run("daily", "failed", work_date())

    run_id = await daily.run_daily()
    assert run_id is not None
    run = await workflows.get_run(run_id)
    assert run["status"] == "completed"
