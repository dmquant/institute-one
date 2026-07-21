"""Maintenance switch: API round-trip, gated jobs skip while paused, gating
registry; per-job feature switches (M8-006): job:<name> off skips ANY metered
job (gated or not) with a cron_metrics skip row, missing switch = enabled."""
from __future__ import annotations

import inspect
import json

from httpx import ASGITransport, AsyncClient

from app import db
from app.institute import scheduler, workflows


async def _store_switches(switches: dict[str, bool]) -> None:
    """Write the versioned envelope exactly as the CAS PUT stores it."""
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES ('feature_switches', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"version": 1, "switches": switches}),),
    )


# ---- API round-trip ---------------------------------------------------------

async def test_maintenance_api_roundtrip():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # fresh institute: no maintenance row, not paused
        r = await client.get("/api/admin/state")
        assert r.status_code == 200
        assert "maintenance" not in r.json()
        assert await scheduler.get_maintenance() is False

        r = await client.post("/api/admin/maintenance", json={"paused": True})
        assert r.status_code == 200
        assert r.json() == {"paused": True}
        assert await scheduler.get_maintenance() is True

        # the switch is visible in the read-only admin state
        r = await client.get("/api/admin/state")
        assert json.loads(r.json()["maintenance"]) == {"paused": True}

        r = await client.post("/api/admin/maintenance", json={"paused": False})
        assert r.status_code == 200
        assert r.json() == {"paused": False}
        assert await scheduler.get_maintenance() is False

        # body must carry a bool
        r = await client.post("/api/admin/maintenance", json={})
        assert r.status_code == 422


# ---- the metered() gate -----------------------------------------------------

async def test_gated_job_skips_while_paused_ungated_runs():
    calls: list[str] = []

    @scheduler.metered("probe-gated", gated=True)
    async def probe_gated():
        calls.append("gated")

    @scheduler.metered("probe-ungated")
    async def probe_ungated():
        calls.append("ungated")

    await scheduler.set_maintenance(True)
    await probe_gated()
    await probe_ungated()
    assert calls == ["ungated"]  # gated skipped, ungated still ran

    await scheduler.set_maintenance(False)
    await probe_gated()
    assert calls == ["ungated", "gated"]


# ---- per-job feature switches (M8-006 enforcement) ---------------------------

async def test_job_switch_off_skips_gated_and_ungated_with_metric():
    """job:<name> = false skips the job — gated OR ungated (the switch is an
    explicit per-job opt-out, unlike maintenance which only pauses quota
    spenders) — and the firing lands in cron_metrics as a skip row whose
    error marks the cause."""
    calls: list[str] = []

    @scheduler.metered("probe-sw-gated", gated=True)
    async def probe_gated():
        calls.append("gated")

    @scheduler.metered("probe-sw-ungated")
    async def probe_ungated():
        calls.append("ungated")

    await _store_switches({"job:probe-sw-gated": False, "job:probe-sw-ungated": False})
    await probe_gated()
    await probe_ungated()
    assert calls == []

    rows = await db.query(
        "SELECT * FROM cron_metrics WHERE job LIKE 'probe-sw-%' ORDER BY id"
    )
    assert len(rows) == 2
    for r in rows:
        assert r["skipped_by_maintenance"] == 1  # the one skip flag (shared with maintenance)
        assert r["ok"] == 1                      # a skip is not a failure
        assert "feature switch" in r["error"]    # cause disambiguated per row

    # flipped back on -> both run, normal ok rows (error NULL)
    await _store_switches({"job:probe-sw-gated": True, "job:probe-sw-ungated": True})
    await probe_gated()
    await probe_ungated()
    assert sorted(calls) == ["gated", "ungated"]
    ok_rows = await db.query(
        "SELECT * FROM cron_metrics WHERE job LIKE 'probe-sw-%' AND skipped_by_maintenance = 0"
    )
    assert len(ok_rows) == 2 and all(r["ok"] == 1 and r["error"] is None for r in ok_rows)


async def test_job_switch_default_is_enabled():
    """Missing row / missing key / corrupt value all mean ENABLED (backward
    compatible: pre-switch deployments and unlisted jobs keep running);
    the legacy flat {name: bool} value shape is still consumed."""
    calls: list[int] = []

    @scheduler.metered("probe-sw-default")
    async def probe():
        calls.append(1)

    await probe()  # 1) no feature_switches row at all
    await _store_switches({"job:someone-else": False})
    await probe()  # 2) row exists, this job's switch missing
    await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = 'feature_switches'",
        (json.dumps({"job:probe-sw-default": False}),),  # 3) legacy flat map: OFF
    )
    await probe()
    await db.execute(
        "UPDATE admin_state SET value = '{not json' WHERE key = 'feature_switches'"
    )
    await probe()  # 4) corrupt value fails open
    assert calls == [1, 1, 1]  # ran for 1/2/4, skipped only for the legacy OFF

    skips = await db.query(
        "SELECT * FROM cron_metrics WHERE job = 'probe-sw-default' AND skipped_by_maintenance = 1"
    )
    assert len(skips) == 1 and "feature switch" in skips[0]["error"]


async def test_briefing_job_skips_when_switch_off_end_to_end():
    """Real job, real skip: no workflow run opens (no quota), metric recorded."""
    await workflows.reconcile_from_disk()
    await _store_switches({"job:briefing": False})
    await scheduler._briefing_job()
    assert await db.query("SELECT id FROM workflow_runs") == []
    row = await db.query_one("SELECT * FROM cron_metrics WHERE job = 'briefing'")
    assert row["skipped_by_maintenance"] == 1 and "feature switch" in row["error"]

    await _store_switches({"job:briefing": True})
    await scheduler._briefing_job()
    assert len(await db.query("SELECT id FROM workflow_runs WHERE workflow_id = 'briefing'")) == 1


async def test_briefing_job_skips_under_maintenance_and_runs_after_resume():
    """End to end on the real job: paused -> zero runs (no quota), resumed -> one run."""
    await workflows.reconcile_from_disk()

    await scheduler.set_maintenance(True)
    await scheduler._briefing_job()
    assert await db.query("SELECT id FROM workflow_runs") == []

    await scheduler.set_maintenance(False)
    await scheduler._briefing_job()
    runs = await db.query("SELECT * FROM workflow_runs WHERE workflow_id = 'briefing'")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"


def test_scheduler_source_never_calls_raw_datetime_now():
    """Hard rule 7 (R3 P3): storage timestamps come from bus.now_iso() and
    work-date logic from now_sgt()/work_date() — scheduler.py must not call
    datetime.now() directly (the janitor's UTC cutoff base had regressed)."""
    assert "datetime.now(" not in inspect.getsource(scheduler)


def test_job_gating_registry_matches_semantics():
    """Everything that submits new model calls is gated; ungated jobs never
    spend quota. FULL-SET assertion (F3 P3-3): reflect over every @metered
    function in the scheduler module and compare against the complete decision
    table — a new job cannot ship without being classified here, and a
    silently flipped gate fails loudly."""
    expected = {
        # gated: the job's domain path reaches executor.submit
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
        "chain-tick": True,
        "committee": True,
        "operator-fast-route": True,
        "operator-deep-route": True,
        # ungated: pure DB/PIT/HTTP upkeep — never spends model quota
        "factcheck-outbox": False,  # drain-only delivery, no model calls
        "operator-selfimprove": False,  # deterministic observe/propose/measure, zero model calls
        "portfolio-proposer": False,  # DB reads + PIT marks, zero model calls
        "janitor": False,
        "hand-scorecard": False,
        "market-refresh": False,
        "operator-vault-sweep": False,
        "paper-opener": False,
        "paper-mtm": False,
    }
    found = {
        fn.job_name: fn.gated
        for _, fn in inspect.getmembers(scheduler, callable)
        if hasattr(fn, "job_name")
    }
    assert found == expected  # full set: nothing missing, nothing extra, gates exact
    assert len(found) == 24   # 9 cron + 15 interval (R1 additions)
