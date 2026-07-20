"""Daily products: 晨会简报 (briefing) and 每日日报 (daily report).

Once-per-work-date guard: every run stores {"WORK_DATE": <SGT date>} in its
variables JSON, so a LIKE match on that key is the idempotency check — it
survives restarts and covers manual run-now triggers too.
"""
from __future__ import annotations

import logging

from .. import db
from . import workflows
from .prompts import work_date

log = logging.getLogger("institute.daily")


async def _ran_today(workflow_id: str, wd: str) -> bool:
    # variables are stored via json.dumps default separators -> '"WORK_DATE": "<date>"'
    # failed and cancelled runs both leave the day open for a rerun
    row = await db.query_one(
        "SELECT id FROM workflow_runs WHERE workflow_id = ? AND status NOT IN ('failed','cancelled') AND variables LIKE ? LIMIT 1",
        (workflow_id, f'%"WORK_DATE": "{wd}"%'),
    )
    return row is not None


async def _run_once(workflow_id: str) -> str | None:
    wd = work_date()
    if await _ran_today(workflow_id, wd):
        log.info("%s already has a running/completed run for %s; skipping", workflow_id, wd)
        return None
    run = await workflows.run_workflow_and_wait(
        workflow_id, variables={"WORK_DATE": wd}, source="daily",
    )
    log.info("%s run %s finished with status %s", workflow_id, run["id"], run["status"])
    return run["id"]


async def run_briefing() -> str | None:
    return await _run_once("briefing")


async def run_daily() -> str | None:
    return await _run_once("daily")
