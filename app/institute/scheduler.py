"""APScheduler wiring — every periodic loop in one place.

No persistent jobstore: jobs are pure kickers over durable DB state, so a
restart loses nothing. Domain modules are imported lazily inside each job so a
broken/missing module degrades that one job instead of breaking boot.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .. import bus, db
from ..config import get_settings
from .prompts import now_sgt, work_date

log = logging.getLogger("institute.scheduler")

_scheduler: AsyncIOScheduler | None = None


# ---- maintenance switch ----------------------------------------------------

async def get_maintenance() -> bool:
    row = await db.query_one("SELECT value FROM admin_state WHERE key = 'maintenance'")
    if row is None:
        return False
    try:
        return bool(json.loads(row["value"]).get("paused", False))
    except Exception:  # noqa: BLE001 - corrupt state means not paused
        return False


async def set_maintenance(paused: bool) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES ('maintenance', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"paused": paused}),),
    )
    log.info("maintenance %s", "paused" if paused else "resumed")


def metered(name: str, *, gated: bool = False) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[None]]]:
    """Wrap a scheduler job: logs duration, never raises; gated jobs skip under maintenance."""
    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[None]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> None:
            try:
                if gated and await get_maintenance():
                    log.debug("job %s skipped (maintenance paused)", name)
                    return
                t0 = time.monotonic()
                await fn(*args, **kwargs)
                dt = time.monotonic() - t0
                log.log(logging.INFO if dt >= 1.0 else logging.DEBUG, "job %s finished in %.1fs", name, dt)
            except Exception:  # noqa: BLE001 - scheduler jobs must never raise
                log.exception("job %s failed", name)
        return wrapper
    return deco


# ---- jobs (domain modules imported lazily inside) --------------------------

@metered("briefing")
async def _briefing_job() -> None:
    from . import daily
    await daily.run_briefing()


@metered("daily-report")
async def _daily_job() -> None:
    from . import daily
    await daily.run_daily()


@metered("analyst-dailies", gated=True)
async def _analyst_dailies_job() -> None:
    from . import analyst_daily
    await analyst_daily.run_all()


@metered("whiteboard-kickoff", gated=True)
async def _whiteboard_kickoff_job() -> None:
    from . import whiteboard
    await whiteboard.kickoff()


@metered("whiteboard-tick")
async def _whiteboard_tick_job() -> None:
    from . import whiteboard
    await whiteboard.tick()


@metered("mailbox-sweep")
async def _mailbox_sweep_job() -> None:
    from . import mailbox
    await mailbox.sweep()


@metered("research-tick", gated=True)
async def _research_tick_job() -> None:
    from . import research
    await research.tick()


@metered("janitor")
async def _janitor() -> None:
    settings = get_settings()
    now_utc = datetime.now(timezone.utc)

    # 1) workflow runs stuck 'running' >6h with no live task under them
    stuck_cutoff = (now_utc - timedelta(hours=6)).isoformat(timespec="seconds")
    for run in await db.query(
        "SELECT id FROM workflow_runs WHERE status = 'running' AND started_at < ?", (stuck_cutoff,)
    ):
        live = await db.query_one(
            "SELECT id FROM tasks WHERE parent_run_id = ? AND status IN ('queued','running') LIMIT 1",
            (run["id"],),
        )
        if live is None:
            n = await db.execute(
                "UPDATE workflow_runs SET status = 'failed', "
                "error = 'expired by janitor: stuck running >6h with no live task', finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (bus.now_iso(), run["id"]),
            )
            if n:
                log.warning("janitor expired stuck workflow run %s", run["id"])

    # 2) stale topic pool entries (>14 days pending)
    pool_cutoff = (now_utc - timedelta(days=14)).isoformat(timespec="seconds")
    n = await db.execute(
        "UPDATE topic_pool SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
        (pool_cutoff,),
    )
    if n:
        log.info("janitor expired %d stale pool topics", n)

    # 3) adhoc workspaces older than 7 days
    def _sweep_adhoc() -> int:
        adhoc = settings.workspaces_dir / "adhoc"
        if not adhoc.is_dir():
            return 0
        horizon = time.time() - 7 * 86400
        removed = 0
        for d in adhoc.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < horizon:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        return removed

    removed = await asyncio.to_thread(_sweep_adhoc)
    if removed:
        log.info("janitor removed %d old adhoc workspaces", removed)

    # 4) nightly DB backup during the 03:00-05:00 SGT window (once per date)
    if 3 <= now_sgt().hour < 5:
        target = settings.backups_dir / f"institute-{work_date()}.db"
        if not target.exists():
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            settings.backups_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, settings.db_path, target)
            log.info("janitor wrote backup %s", target.name)


# ---- lifecycle --------------------------------------------------------------

def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    sched = AsyncIOScheduler(timezone=settings.timezone)

    def cron(job: Callable, name: str, hhmm: str) -> None:
        hhmm = (hhmm or "").strip()
        if not hhmm:
            log.info("job %s disabled (empty time)", name)
            return
        try:
            h, m = hhmm.split(":")
            trigger = CronTrigger(hour=int(h), minute=int(m), timezone=settings.timezone)
        except (ValueError, TypeError):
            log.error("job %s: cannot parse time %r; disabled", name, hhmm)
            return
        sched.add_job(job, trigger, id=name, max_instances=1, coalesce=True, misfire_grace_time=3600)

    def every(job: Callable, name: str, *, minutes: int = 0, seconds: int = 0) -> None:
        if minutes <= 0 and seconds <= 0:
            log.info("job %s disabled (non-positive interval)", name)
            return
        sched.add_job(
            job, IntervalTrigger(minutes=minutes, seconds=seconds, timezone=settings.timezone),
            id=name, max_instances=1, coalesce=True, misfire_grace_time=60,
        )

    cron(_briefing_job, "briefing", settings.briefing_time)
    cron(_daily_job, "daily-report", settings.daily_time)
    cron(_analyst_dailies_job, "analyst-dailies", settings.analyst_daily_time)
    every(_whiteboard_kickoff_job, "whiteboard-kickoff", minutes=settings.whiteboard_kickoff_minutes)
    every(_whiteboard_tick_job, "whiteboard-tick", seconds=settings.whiteboard_tick_seconds)
    every(_mailbox_sweep_job, "mailbox-sweep", seconds=settings.mailbox_sweep_seconds)
    every(_research_tick_job, "research-tick", minutes=settings.research_tick_minutes)
    every(_janitor, "janitor", minutes=settings.janitor_minutes)

    sched.start()
    _scheduler = sched
    log.info("scheduler started: %d jobs (tz=%s)", len(sched.get_jobs()), settings.timezone)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")
