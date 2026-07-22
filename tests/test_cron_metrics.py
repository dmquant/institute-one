"""Cron observability: metered() writes one cron_metrics row per firing
(success / failure / maintenance skip), scheduler.job_registry() exposes the
full job set (S4-P0-03), /api/cron/health returns registry LEFT JOIN metrics,
and the janitor prunes rows older than 30 days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from app import db
from app.config import get_settings
from app.institute import scheduler

# The complete gate decision table — MUST stay in sync with
# tests/test_maintenance.py::test_job_gating_registry_matches_semantics
# (that test locks @metered reflection against this same table).
EXPECTED_GATES = {
    "briefing": True,
    "daily-report": True,
    "analyst-dailies": True,
    "memory-compact": True,
    "whiteboard-kickoff": True,
    "whiteboard-tick": True,
    "mailbox-sweep": True,
    "research-tick": True,
    "research-tree-tick": True,
    "rate-limit-revival": True,
    "factcheck-tick": True,
    "factcheck-outbox": False,  # drain-only, no model calls -> never gated
    "operator-selfimprove": False,  # deterministic derivation, zero model calls
    "portfolio-proposer": False,  # DB reads + PIT marks only, zero model calls
    "chain-tick": True,
    "committee": True,
    "operator-fast-route": True,
    "operator-deep-route": True,
    "janitor": False,
    "hand-scorecard": False,
    "market-refresh": False,
    "operator-vault-sweep": False,
    "paper-opener": False,
    "paper-mtm": False,
}


async def _rows(job: str) -> list[dict]:
    return await db.query("SELECT * FROM cron_metrics WHERE job = ? ORDER BY id", (job,))


# ---- metered() writes a row per firing ---------------------------------------

async def test_metered_success_records_ok_row():
    @scheduler.metered("probe-ok")
    async def job():
        pass

    await job()
    rows = await _rows("probe-ok")
    assert len(rows) == 1
    r = rows[0]
    assert r["ok"] == 1 and r["skipped_by_maintenance"] == 0 and r["error"] is None
    assert r["duration_ms"] >= 0
    assert r["fired_at"]  # bus.now_iso()

    await job()  # every firing appends one row
    assert len(await _rows("probe-ok")) == 2


async def test_metered_failure_records_error_and_never_raises():
    @scheduler.metered("probe-fail")
    async def job():
        raise RuntimeError("boom with details")

    await job()  # must not raise (scheduler jobs never raise)
    rows = await _rows("probe-fail")
    assert len(rows) == 1
    r = rows[0]
    assert r["ok"] == 0 and r["skipped_by_maintenance"] == 0
    assert "RuntimeError" in r["error"] and "boom with details" in r["error"]


async def test_metered_maintenance_skip_records_skipped_row():
    calls: list[str] = []

    @scheduler.metered("probe-gated", gated=True)
    async def job():
        calls.append("ran")

    await scheduler.set_maintenance(True)
    try:
        await job()
    finally:
        await scheduler.set_maintenance(False)

    assert calls == []  # gate held
    rows = await _rows("probe-gated")
    assert len(rows) == 1
    assert rows[0]["skipped_by_maintenance"] == 1

    # resumed: the next firing is a normal ok row
    await job()
    rows = await _rows("probe-gated")
    assert len(rows) == 2
    assert rows[1]["skipped_by_maintenance"] == 0 and rows[1]["ok"] == 1
    assert calls == ["ran"]


async def test_metric_write_failure_never_breaks_the_job(monkeypatch, caplog):
    """REVIEW-B1 L2: metrics are observability, not control flow — a failing
    cron_metrics INSERT must neither raise out of the wrapper nor stop the
    wrapped job from running (for both succeeding and failing jobs)."""
    import logging

    from app.institute import scheduler as sched_mod

    real_execute = db.execute

    async def broken_metrics_execute(sql, params=()):
        if isinstance(sql, str) and "INSERT INTO cron_metrics" in sql:
            raise RuntimeError("synthetic metrics outage")
        return await real_execute(sql, params)

    monkeypatch.setattr(sched_mod.db, "execute", broken_metrics_execute)

    ran: list[str] = []

    @scheduler.metered("probe-metrics-outage-ok")
    async def ok_job():
        ran.append("ok")

    @scheduler.metered("probe-metrics-outage-fail")
    async def failing_job():
        ran.append("fail")
        raise RuntimeError("job boom")

    with caplog.at_level(logging.ERROR, logger="institute.scheduler"):
        await ok_job()       # must not raise
        await failing_job()  # must not raise either

    assert ran == ["ok", "fail"]  # both jobs actually executed
    assert any("cron metric write failed" in r.getMessage() for r in caplog.records)
    # nothing landed while the outage held
    monkeypatch.undo()
    assert await _rows("probe-metrics-outage-ok") == []
    assert await _rows("probe-metrics-outage-fail") == []


# ---- scheduler.job_registry() -------------------------------------------------

async def test_job_registry_definition_surface_without_scheduler():
    """With the scheduler stopped (every test runs this way) the registry
    still exposes the full definition surface: 24 jobs, exact gate table,
    sorted by name, live fields all None/False."""
    reg = scheduler.job_registry()
    assert [r["name"] for r in reg] == sorted(EXPECTED_GATES)
    assert {r["name"]: r["gated"] for r in reg} == EXPECTED_GATES
    assert len(reg) == 24  # 9 cron + 15 interval (R1: factcheck-outbox, operator-selfimprove, portfolio-proposer)
    for r in reg:
        assert set(r) == {"name", "gated", "registered", "trigger", "next_run_time"}
        assert r["registered"] is False
        assert r["trigger"] is None and r["next_run_time"] is None


async def test_job_registry_live_scheduler_marks_all_jobs_registered(monkeypatch):
    """S4-P0-03 proof: with the scheduler actually started, all 24 jobs are
    registered with a real trigger and a computed next run time."""
    monkeypatch.setattr(get_settings(), "scorecard_time", "03:17")
    scheduler.start()
    try:
        reg = scheduler.job_registry()
        assert {r["name"]: r["gated"] for r in reg} == EXPECTED_GATES
        for r in reg:
            assert r["registered"] is True
            assert r["trigger"]  # e.g. "cron[hour='8', minute='30']" / "interval[0:01:00]"
            assert r["next_run_time"]  # ISO timestamp
        scorecard = next(r for r in reg if r["name"] == "hand-scorecard")
        assert "hour='3'" in scorecard["trigger"] and "minute='17'" in scorecard["trigger"]
    finally:
        scheduler.shutdown()


# ---- /api/cron/health ---------------------------------------------------------

async def test_cron_health_endpoint_shape():
    from app.main import create_app

    async def put(job: str, *, ok: int = 1, skipped: int = 0, duration: int = 0,
                  error: str | None = None, fired_at: str | None = None):
        await db.execute(
            "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, error, skipped_by_maintenance) "
            "VALUES (?,?,?,?,?,?)",
            (job, fired_at or "2026-07-20T10:00:00+00:00", duration, ok, error, skipped),
        )

    await put("briefing", duration=100, fired_at="2026-07-20T10:00:00+00:00")
    await put("briefing", ok=0, duration=300, error="RuntimeError: x",
              fired_at="2026-07-20T11:00:00+00:00")
    await put("briefing", skipped=1, fired_at="2026-07-20T12:00:00+00:00")
    await put("janitor", duration=50, fired_at="2026-07-20T09:00:00+00:00")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/cron/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 30

    briefing = body["jobs"]["briefing"]
    assert briefing["fires"] == 3
    assert briefing["ok"] == 1 and briefing["failed"] == 1 and briefing["skipped"] == 1
    assert briefing["ok_rate"] == 0.5              # skips excluded from the rate
    assert briefing["avg_duration_ms"] == 200       # (100+300)/2, skip excluded
    assert briefing["last_fired_at"] == "2026-07-20T12:00:00+00:00"
    assert briefing["last_status"] == "skipped"
    assert briefing["last_error"] == {"fired_at": "2026-07-20T11:00:00+00:00",
                                      "error": "RuntimeError: x"}
    # registry fields (scheduler not running under tests)
    assert briefing["gated"] is True and briefing["registered"] is False

    janitor = body["jobs"]["janitor"]
    assert janitor["ok_rate"] == 1.0
    assert janitor["last_status"] == "ok"
    assert janitor["last_error"] is None
    assert janitor["gated"] is False


async def test_cron_health_includes_never_fired_jobs():
    """S4-P0-03: the response is the registry full set LEFT JOIN metrics —
    with only one job having metrics, the other 19 still show up with zeroed
    metric fields and their gate, closing the 12/20 observability gap."""
    from app.main import create_app

    await db.execute(
        "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, skipped_by_maintenance) "
        "VALUES ('janitor', '2026-07-20T09:00:00+00:00', 5, 1, 0)"
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/cron/health")).json()

    assert set(body["jobs"]) == set(EXPECTED_GATES)  # full set, nothing lost
    assert {n: j["gated"] for n, j in body["jobs"].items()} == EXPECTED_GATES
    never_fired = body["jobs"]["committee"]
    assert never_fired["fires"] == 0 and never_fired["last_status"] is None
    assert never_fired["ok_rate"] is None and never_fired["last_error"] is None
    assert body["jobs"]["janitor"]["fires"] == 1  # the metrics side still joins


async def test_cron_health_keeps_metrics_only_job_names():
    """A job name present only in cron_metrics (renamed/removed job inside the
    30-day window) stays visible: registered=False, gated unknown (None)."""
    from app.main import create_app

    await db.execute(
        "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, skipped_by_maintenance) "
        "VALUES ('probe-renamed', '2026-07-20T09:00:00+00:00', 5, 1, 0)"
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/cron/health")).json()

    ghost = body["jobs"]["probe-renamed"]
    assert ghost["registered"] is False and ghost["gated"] is None
    assert ghost["schedule"] is None and ghost["fires"] == 1


async def test_cron_health_empty_table():
    """Empty cron_metrics no longer means an empty response: the registry
    definition surface is always present (that is the S4-P0-03 point)."""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/cron/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 30
    assert set(body["jobs"]) == set(EXPECTED_GATES)
    assert all(j["fires"] == 0 and j["last_fired_at"] is None for j in body["jobs"].values())


# ---- janitor prunes the 30-day window -----------------------------------------

async def test_janitor_prunes_metrics_older_than_30_days():
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    for fired_at in (old, fresh):
        await db.execute(
            "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, skipped_by_maintenance) "
            "VALUES ('probe-prune', ?, 0, 1, 0)",
            (fired_at,),
        )

    await scheduler._janitor()

    remaining = [r["fired_at"] for r in await _rows("probe-prune")]
    assert remaining == [fresh]
    # the janitor's own firing was recorded too (it is metered like the rest)
    assert len(await _rows("janitor")) == 1
