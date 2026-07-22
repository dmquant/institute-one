"""Scheduler revival for rate-limited tasks after their hand cooldown ends."""
from __future__ import annotations

import asyncio
import sqlite3
import time

from app import bus, db
from app.hands.base import Hand, HandResult
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
    hand: str | None = "echo",
    requested_hand: str | None = "echo",
    finished_at: str | None = None,
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO tasks "
        "(id, session_id, hand, requested_hand, model, prompt, status, source, error, "
        " workspace_dir, timeout_s, fallback_chain, lineage_root, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, "session-original", hand, requested_hand, "echo-model", "revive me",
         "rate_limited", source, "quota exhausted", workspace, 60, fallback_chain,
         lineage_root, finished_at or now, finished_at or now),
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
    await scheduler._rate_limit_revival_job()  # completed child -> consume source

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

    # The next firing drains the one remaining row, rather than exceeding the
    # cap in the first firing or permanently starving it.
    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    await scheduler._rate_limit_revival_job()  # reconcile all completed children
    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-limit' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == 4
    marked = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-limit' "
        "AND instr(COALESCE(error, ''), ?) > 0",
        (scheduler.RATE_LIMIT_REVIVAL_MARKER,),
    )
    assert len(marked) == 4


# ---- loop-fix P11g: the candidate scan itself is bounded ---------------------

async def test_revival_candidate_scan_carries_limit(tmp_path, monkeypatch):
    """The rate_limited scan must never pull the whole table: the SQL carries
    LIMIT bound to RATE_LIMIT_REVIVAL_SCAN_LIMIT (with headroom over the
    per-firing revival cap so skippable rows don't starve eligible ones)."""
    assert scheduler.RATE_LIMIT_REVIVAL_SCAN_LIMIT >= scheduler.RATE_LIMIT_REVIVAL_LIMIT

    await _insert_rate_limited("scanprobe0001", workspace=str(tmp_path))
    captured: list[tuple[str, tuple]] = []
    real_query = db.query

    async def spy(sql, params=()):
        captured.append((sql, tuple(params)))
        return await real_query(sql, params)

    monkeypatch.setattr(scheduler.db, "query", spy)
    await scheduler._rate_limit_revival_job()
    await _drain_executor()

    scan = next((sql, p) for sql, p in captured if "status = 'rate_limited'" in sql)
    assert "LIMIT" in scan[0]
    assert scheduler.RATE_LIMIT_REVIVAL_SCAN_LIMIT in scan[1]


async def test_revival_scan_limit_bounds_fetch_without_starvation(tmp_path, monkeypatch):
    """With the scan clamped to 1, one firing revives exactly one row even
    though the revival cap allows three — and the next firing reaches the
    remaining row (claimed rows leave the scan window), so nothing starves."""
    monkeypatch.setattr(scheduler, "RATE_LIMIT_REVIVAL_SCAN_LIMIT", 1)
    for i in range(2):
        await _insert_rate_limited(
            f"scancap-{i}", workspace=str(tmp_path), source="revival-scancap"
        )

    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-scancap' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == 1

    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-scancap' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == 2


# ---- R3 P1: bounded window must rotate, hand=NULL must not squat -------------

async def test_hand_null_row_revives_via_requested_hand(tmp_path):
    """hand=NULL is not a temporary state (a task can end rate_limited before
    any hand bound): revival falls back to requested_hand instead of skipping
    the row on every firing forever."""
    await _insert_rate_limited(
        "nullhand00001", workspace=str(tmp_path), source="revival-nullhand", hand=None,
    )

    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    await scheduler._rate_limit_revival_job()  # completed child -> consume source

    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-nullhand' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == 1
    marked = await db.query_one("SELECT error FROM tasks WHERE id = 'nullhand00001'")
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER in marked["error"]


async def test_revival_rotates_past_permanently_skipped_head(tmp_path):
    """A full window of rows that can never be revived (hand AND
    requested_hand both NULL) must not starve an eligible row behind it: the
    persisted cursor advances the window, so the next firing reaches row 51."""
    old = "2026-07-19T00:00:00+00:00"
    for i in range(scheduler.RATE_LIMIT_REVIVAL_SCAN_LIMIT):
        await _insert_rate_limited(
            f"poison-{i:04d}", workspace=str(tmp_path), source="revival-poison",
            hand=None, requested_hand=None, finished_at=old,
        )
    await _insert_rate_limited(
        "eligible00001", workspace=str(tmp_path), source="revival-eligible"
    )

    await scheduler._rate_limit_revival_job()  # window = the 50 poison head rows
    await scheduler._rate_limit_revival_job()  # cursor rotated past them
    await _drain_executor()
    await scheduler._rate_limit_revival_job()  # wrap scans poison head again
    await scheduler._rate_limit_revival_job()  # completed child -> consume source

    revived = await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-eligible' AND lineage_root IS NOT NULL"
    )
    assert len(revived) == 1
    marked = await db.query_one("SELECT error FROM tasks WHERE id = 'eligible00001'")
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER in marked["error"]
    # the poison rows were never claimed (nothing can revive them)
    assert await db.query(
        "SELECT id FROM tasks WHERE source = 'revival-poison' "
        "AND instr(COALESCE(error, ''), ?) > 0",
        (scheduler.RATE_LIMIT_REVIVAL_MARKER,),
    ) == []


# ---- R5: durable source -> canonical retry protocol --------------------------

async def _prepare_only(source_id: str):
    source = await db.query_one("SELECT * FROM tasks WHERE id = ?", (source_id,))
    async with db.transaction() as conn:
        return await executor.prepare_respawn_from_row(
            conn, source, max_attempts=scheduler.RATE_LIMIT_REVIVAL_MAX_ATTEMPTS,
        )


async def _canonical(source_id: str) -> tuple[dict, dict]:
    source = await db.query_one("SELECT * FROM tasks WHERE id = ?", (source_id,))
    assert source["revival_task_id"]
    child = await db.query_one(
        "SELECT * FROM tasks WHERE id = ?", (source["revival_task_id"],)
    )
    assert child["revived_from_task_id"] == source_id
    return source, child


async def test_marker_then_queued_child_survives_restart_and_runs_same_generation(tmp_path):
    """R5 P1: a bound queued child is durable work even if the compatibility
    marker already landed. Boot must drive THIS child, not fail it and never
    create a replacement generation."""
    await _insert_rate_limited(
        "restartsrc01", workspace=str(tmp_path), source="revival-restart"
    )
    prepared = await _prepare_only("restartsrc01")
    assert prepared.created is True
    await db.execute(
        "UPDATE tasks SET error=? WHERE id='restartsrc01'",
        (scheduler._revival_error("quota exhausted"),),
    )

    assert await executor.recover_orphans() == 0
    await _drain_executor()

    source, child = await _canonical("restartsrc01")
    assert source["revival_task_id"] == prepared.task_id
    assert child["status"] == "completed"
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='restartsrc01'"
    )) == 1


async def test_completed_canonical_is_reconciled_without_second_generation(tmp_path):
    """R5 P1: child completes, then the process dies before source marker.
    The next tick reconciles through revival_task_id and consumes the source;
    terminal child uniqueness prevents a second real model invocation."""
    await _insert_rate_limited(
        "completesrc1", workspace=str(tmp_path), source="revival-complete-window"
    )
    prepared = await _prepare_only("completesrc1")
    assert await executor.drive_prepared(prepared.task_id) is True
    await _drain_executor()

    source, child = await _canonical("completesrc1")
    assert child["status"] == "completed"
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER not in source["error"]

    await scheduler._rate_limit_revival_job()  # post-crash reconciliation
    source, _ = await _canonical("completesrc1")
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER in source["error"]
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='completesrc1'"
    )) == 1


async def test_queue_overcommit_defers_without_binding_or_consuming_source(tmp_path):
    """R5 P1: admission pressure performs zero model calls and must create no
    born-terminal canonical child, no source binding, no marker, no attempt.
    Once capacity clears, the same source can prepare normally."""
    await _insert_rate_limited(
        "oversource01", workspace=str(tmp_path), source="revival-overcommit"
    )
    cap = scheduler.get_settings().hand_queue_depth
    for i in range(cap + 1):
        await _insert_live(f"overback-{i:02d}", f"other-root-{i}", str(tmp_path))

    await scheduler._rate_limit_revival_job()
    source = await db.query_one("SELECT * FROM tasks WHERE id='oversource01'")
    assert source["revival_task_id"] is None
    assert source["revival_attempts"] == 0
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER not in source["error"]
    assert await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='oversource01'"
    ) == []

    await db.execute(
        "UPDATE tasks SET status='failed', finished_at=? WHERE id LIKE 'overback-%'",
        (bus.now_iso(),),
    )
    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    source, child = await _canonical("oversource01")
    assert child["status"] == "completed"
    assert source["revival_attempts"] == 1


async def test_task_queued_emit_failure_never_strands_or_duplicates_child(tmp_path, monkeypatch):
    """R5 P2: task row + source binding commit before event/driver creation.
    A task.queued emit failure may lose observability, but it cannot lose work:
    this tick or the next drives the same canonical child, never a new one."""
    await _insert_rate_limited(
        "emitsource01", workspace=str(tmp_path), source="revival-emit"
    )
    real_emit = bus.emit
    failed_once = False

    async def flaky_emit(event_type, *args, **kwargs):
        nonlocal failed_once
        if event_type == "task.queued" and not failed_once:
            failed_once = True
            raise RuntimeError("synthetic task.queued outage")
        return await real_emit(event_type, *args, **kwargs)

    monkeypatch.setattr(executor.bus, "emit", flaky_emit)
    await scheduler._rate_limit_revival_job()
    source, child = await _canonical("emitsource01")
    canonical_id = child["id"]

    await scheduler._rate_limit_revival_job()
    await _drain_executor()
    source, child = await _canonical("emitsource01")
    assert source["revival_task_id"] == canonical_id
    assert child["status"] == "completed"
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='emitsource01'"
    )) == 1


async def test_unrelated_integrity_error_without_canonical_never_consumes_source(
    tmp_path, monkeypatch,
):
    """R5 P2: a CHECK/id collision is not the lineage winner. Only a real,
    reciprocal canonical binding may converge; otherwise source stays
    unbound and unconsumed."""
    await _insert_rate_limited(
        "integritysrc", workspace=str(tmp_path), source="revival-integrity"
    )

    async def unrelated_integrity(*args, **kwargs):
        raise sqlite3.IntegrityError("CHECK constraint failed: unrelated")

    monkeypatch.setattr(executor, "prepare_respawn_from_row", unrelated_integrity)
    await scheduler._rate_limit_revival_job()

    source = await db.query_one("SELECT * FROM tasks WHERE id='integritysrc'")
    assert source["revival_task_id"] is None
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER not in source["error"]
    assert await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='integritysrc'"
    ) == []


class _CountingHand(Hand):
    name = "revival-counting"
    hand_type = "test"

    def __init__(self):
        self.calls = 0

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.calls += 1
        await asyncio.sleep(0.02)
        return HandResult(output="counted", exit_code=0)


class _FailingHand(Hand):
    name = "revival-failing"
    hand_type = "test"

    def __init__(self):
        self.calls = 0

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.calls += 1
        return HandResult(output="synthetic failure", exit_code=1)


async def test_failed_canonical_retries_same_id_with_bounded_attempts(tmp_path):
    """R5 terminal policy: retryable failures transition the SAME canonical
    child back to queued. Source attempts bound real executions; no terminal
    child can fall out of a partial index and permit a second generation."""
    hand = _FailingHand()
    get_registry().register(hand)
    await _insert_rate_limited(
        "failsource01", workspace=str(tmp_path), source="revival-failed",
        hand=hand.name, requested_hand=hand.name,
        fallback_chain=f'["{hand.name}"]',
    )

    for _ in range(scheduler.RATE_LIMIT_REVIVAL_MAX_ATTEMPTS + 2):
        await scheduler._rate_limit_revival_job()
        await _drain_executor()

    source, child = await _canonical("failsource01")
    assert source["revival_attempts"] == scheduler.RATE_LIMIT_REVIVAL_MAX_ATTEMPTS
    assert scheduler.RATE_LIMIT_REVIVAL_MARKER not in source["error"]
    assert child["status"] == "failed"
    assert hand.calls == scheduler.RATE_LIMIT_REVIVAL_MAX_ATTEMPTS
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='failsource01'"
    )) == 1


async def test_boot_and_tick_race_executes_prepared_child_once(tmp_path):
    """R5 P1: boot recovery and the current scheduler tick may both attach a
    driver to one durable queued child. queued->running is the DB arbiter:
    exactly one worker reaches Hand.execute, and no second child is inserted."""
    hand = _CountingHand()
    get_registry().register(hand)
    await _insert_rate_limited(
        "racesource01", workspace=str(tmp_path), source="revival-race",
        hand=hand.name, requested_hand=hand.name,
        fallback_chain=f'["{hand.name}"]',
    )
    prepared = await _prepare_only("racesource01")

    await asyncio.gather(
        executor.recover_orphans(),
        scheduler._rate_limit_revival_job(),
    )
    await _drain_executor()

    _, child = await _canonical("racesource01")
    assert child["id"] == prepared.task_id
    assert child["status"] == "completed"
    assert hand.calls == 1
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='racesource01'"
    )) == 1
