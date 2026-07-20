from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, StrictBool

from .. import db
from ..config import VERSION, get_settings
from ..hands.registry import get_registry
from ..institute import scheduler
from ..institute.prompts import now_sgt, work_date
from ..router import executor

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health():
    return {"ok": True, "version": VERSION, "time_sgt": now_sgt().isoformat(timespec="seconds")}


@router.get("/api/meta")
async def meta():
    settings = get_settings()
    return {
        "version": VERSION,
        "timezone": settings.timezone,
        "work_date": work_date(),
        "hands": get_registry().status_snapshot(),
        "vault_configured": settings.vault_dir is not None,
        "queue": await executor.queue_stats(),
        "limits": {
            "max_concurrent": settings.max_concurrent,
            "default_timeout_s": settings.default_timeout_s,
            "output_cap_bytes": settings.output_cap_bytes,
        },
    }


@router.get("/api/admin/state")
async def admin_state():
    rows = await db.query("SELECT key, value FROM admin_state")
    return {r["key"]: r["value"] for r in rows}


@router.get("/api/cron/health")
async def cron_health():
    """Per-job scheduler health: registry full set LEFT JOIN cron_metrics.

    The job set is scheduler.job_registry() (every @metered job, even ones
    that never fired) plus any job names only present in cron_metrics (renamed
    jobs inside the 30-day window). Registry fields: registered/gated/schedule/
    next_run_time (gated is None for metrics-only names). Metric fields keep
    the pre-S4 shape: ok_rate counts real executions only (maintenance skips
    are neither ok nor failed); avg_duration_ms likewise. last_status is
    'ok'|'failed'|'skipped', None for jobs that never fired.
    """
    aggregates = await db.query(
        """SELECT job,
                  COUNT(*) AS fires,
                  SUM(CASE WHEN skipped_by_maintenance = 1 THEN 1 ELSE 0 END) AS skipped,
                  SUM(CASE WHEN skipped_by_maintenance = 0 AND ok = 1 THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN skipped_by_maintenance = 0 AND ok = 0 THEN 1 ELSE 0 END) AS failed,
                  AVG(CASE WHEN skipped_by_maintenance = 0 THEN duration_ms END) AS avg_duration_ms,
                  MAX(fired_at) AS last_fired_at
           FROM cron_metrics GROUP BY job"""
    )
    last_rows = await db.query(
        "SELECT job, ok, skipped_by_maintenance FROM cron_metrics "
        "WHERE id IN (SELECT MAX(id) FROM cron_metrics GROUP BY job)"
    )
    last_status = {
        r["job"]: ("skipped" if r["skipped_by_maintenance"] else ("ok" if r["ok"] else "failed"))
        for r in last_rows
    }
    error_rows = await db.query(
        "SELECT job, fired_at, error FROM cron_metrics "
        "WHERE id IN (SELECT MAX(id) FROM cron_metrics "
        "             WHERE ok = 0 AND skipped_by_maintenance = 0 GROUP BY job)"
    )
    last_error = {r["job"]: {"fired_at": r["fired_at"], "error": r["error"]} for r in error_rows}

    metrics = {}
    for r in aggregates:
        executed = r["ok"] + r["failed"]
        metrics[r["job"]] = {
            "last_fired_at": r["last_fired_at"],
            "last_status": last_status.get(r["job"]),
            "fires": r["fires"],
            "ok": r["ok"],
            "failed": r["failed"],
            "skipped": r["skipped"],
            "ok_rate": round(r["ok"] / executed, 4) if executed else None,
            "avg_duration_ms": round(r["avg_duration_ms"]) if r["avg_duration_ms"] is not None else None,
            "last_error": last_error.get(r["job"]),
        }

    no_metrics = {
        "last_fired_at": None, "last_status": None, "fires": 0, "ok": 0,
        "failed": 0, "skipped": 0, "ok_rate": None, "avg_duration_ms": None,
        "last_error": None,
    }
    registry = {entry["name"]: entry for entry in scheduler.job_registry()}
    jobs = {}
    for name in sorted(set(registry) | set(metrics)):
        entry = registry.get(name)
        jobs[name] = {
            "registered": entry["registered"] if entry else False,
            "gated": entry["gated"] if entry else None,
            "schedule": entry["trigger"] if entry else None,
            "next_run_time": entry["next_run_time"] if entry else None,
            **metrics.get(name, no_metrics),
        }
    return {"window_days": 30, "jobs": jobs}


class MaintenanceBody(BaseModel):
    paused: StrictBool


@router.post("/api/admin/maintenance")
async def set_maintenance(body: MaintenanceBody):
    """Flip the maintenance switch: gated scheduler jobs skip while paused."""
    await scheduler.set_maintenance(body.paused)
    return {"paused": await scheduler.get_maintenance()}
