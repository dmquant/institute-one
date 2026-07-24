"""Janitor nightly backup (loop-fix P9): VACUUM INTO produces a consistent
snapshot (valid SQLite, same rows) during the 03:00-05:00 SGT window, once per
date; a crashed attempt's temp file is cleaned up on the next firing; and a
backup failure is isolated — the janitor's other steps and its own cron metric
stay healthy."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.config import get_settings
from app.institute import scheduler
from app.institute.prompts import work_date


def _fresh_backups_dir() -> Path:
    """backups_dir survives across tests (one INSTITUTE_HOME per session)."""
    d = get_settings().backups_dir
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _in_window() -> datetime:
    return datetime(2026, 7, 20, 3, 30)  # only .hour / .date() are read


def _outside_window() -> datetime:
    return datetime(2026, 7, 20, 12, 0)


def _snapshot_admin_state_count(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return con.execute("SELECT COUNT(*) FROM admin_state").fetchone()[0]
    finally:
        con.close()


async def test_backup_is_valid_consistent_snapshot(monkeypatch):
    backups = _fresh_backups_dir()
    monkeypatch.setattr(scheduler, "now_sgt", _in_window)
    for i in range(5):
        await db.execute(
            "INSERT INTO admin_state (key, value) VALUES (?, 'x')", (f"probe:backup:{i}",)
        )

    await scheduler._janitor()

    target = backups / f"institute-{work_date()}.db"
    assert target.exists()
    assert list(backups.glob("*.tmp")) == []  # no temp residue after success
    live = (await db.query_one("SELECT COUNT(*) AS n FROM admin_state"))["n"]
    assert _snapshot_admin_state_count(target) == live


async def test_backup_skipped_outside_window(monkeypatch):
    backups = _fresh_backups_dir()
    monkeypatch.setattr(scheduler, "now_sgt", _outside_window)
    await scheduler._janitor()
    assert list(backups.iterdir()) == []


async def test_backup_written_once_per_date(monkeypatch):
    backups = _fresh_backups_dir()
    monkeypatch.setattr(scheduler, "now_sgt", _in_window)
    target = backups / f"institute-{work_date()}.db"
    target.write_bytes(b"already-made")

    await scheduler._janitor()

    assert target.read_bytes() == b"already-made"  # existing backup untouched


async def test_backup_recovers_from_crashed_tmp(monkeypatch):
    """A crash mid-snapshot leaves institute-<date>.db.tmp behind; the next
    firing must clean it and still land a valid backup (VACUUM INTO refuses
    to write over an existing non-empty file)."""
    backups = _fresh_backups_dir()
    monkeypatch.setattr(scheduler, "now_sgt", _in_window)
    target = backups / f"institute-{work_date()}.db"
    tmp = backups / (target.name + ".tmp")
    tmp.write_bytes(b"half-written garbage")

    await scheduler._janitor()

    assert target.exists()
    assert not tmp.exists()
    assert _snapshot_admin_state_count(target) >= 0  # valid sqlite, integrity ok


async def test_backup_failure_never_poisons_janitor(monkeypatch):
    """P9 isolation: a failing backup is logged and swallowed — the janitor's
    other steps still run and its own cron_metrics row stays ok."""
    _fresh_backups_dir()
    monkeypatch.setattr(scheduler, "now_sgt", _in_window)

    async def boom() -> None:
        raise RuntimeError("synthetic backup failure")

    monkeypatch.setattr(scheduler, "_nightly_backup", boom)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, skipped_by_maintenance) "
        "VALUES ('probe-backup-isolation', ?, 0, 1, 0)",
        (old,),
    )

    await scheduler._janitor()

    # the metrics-prune step still ran ...
    assert await db.query(
        "SELECT id FROM cron_metrics WHERE job = 'probe-backup-isolation'"
    ) == []
    # ... and the janitor firing itself is recorded healthy, not failed
    row = await db.query_one("SELECT ok, error FROM cron_metrics WHERE job = 'janitor'")
    assert row["ok"] == 1 and row["error"] is None
