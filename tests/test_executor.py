"""Executor core: submit, artifacts, cancel, orphan recovery, fallback chain,
per-hand queue-depth cap, lock-order starvation."""
from __future__ import annotations

import asyncio
import json

from app import bus, db
from app.config import get_settings
from app.hands import registry as registry_mod
from app.hands.base import Hand, HandResult, RateLimitInfo
from app.hands.registry import get_registry
from app.router import executor


async def test_submit_echo_completes(tmp_path):
    task = await executor.submit("echo", "hello institute", source="test", workspace=tmp_path)
    assert task.status == "completed"
    assert task.exit_code == 0
    assert task.hand == "echo"
    assert task.requested_hand == "echo"
    assert task.output.startswith("[echo] ")
    assert "hello institute" in task.output

    events = await bus.replay(0, types=["task."])
    types = [e.type for e in events if e.ref_id == task.id]
    assert types == ["task.queued", "task.running", "task.completed"]


async def test_write_file_artifacts_land_in_workspace(tmp_path):
    prompt = "please write the report\nWRITE_FILE: notes/report.md\nrest of prompt"
    task = await executor.submit("echo", prompt, source="test", workspace=tmp_path)
    assert task.status == "completed"

    target = tmp_path / "notes" / "report.md"
    assert target.is_file()
    assert "WRITE_FILE: notes/report.md" in target.read_text(encoding="utf-8")
    assert task.artifacts == ["notes/report.md"]


async def test_cancel_queued_task(tmp_path):
    task_id = "queuedcancel1"
    await executor._create_row(
        task_id=task_id, hand="echo", prompt="never runs", source="test", model=None,
        session_id=None, parent_run_id=None, workspace=tmp_path, timeout_s=60,
    )
    assert await executor.cancel(task_id) is True
    task = await executor.get_task(task_id)
    assert task.status == "cancelled"
    # cancelling an already-terminal task is a no-op
    assert await executor.cancel(task_id) is False


async def test_recover_orphans_marks_running_row_failed():
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("orphan0000001", "echo", "interrupted by restart", "running", "test", bus.now_iso()),
    )
    n = await executor.recover_orphans()
    assert n == 1
    task = await executor.get_task("orphan0000001")
    assert task.status == "failed"
    assert task.error == "orphaned by restart"


async def test_prepare_respawn_atomically_binds_exactly_one_canonical_child(tmp_path):
    """R5: the executor owns retry row construction. Source claim, reciprocal
    immutable binding, and born-queued child insert commit in one transaction;
    a second prepare converges on the same child across terminal status."""
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO tasks "
        "(id, requested_hand, prompt, status, source, workspace_dir, timeout_s, "
        " fallback_chain, error, created_at, finished_at) "
        "VALUES ('prepare-src','echo','retry me','rate_limited','test',?,60,"
        " '[\"echo\"]','quota',?,?)",
        (str(tmp_path), now, now),
    )
    source = await db.query_one("SELECT * FROM tasks WHERE id='prepare-src'")

    async with db.transaction() as conn:
        first = await executor.prepare_respawn_from_row(conn, source, max_attempts=5)
    source = await db.query_one("SELECT * FROM tasks WHERE id='prepare-src'")
    async with db.transaction() as conn:
        second = await executor.prepare_respawn_from_row(conn, source, max_attempts=5)

    assert first.created is True
    assert second.created is False
    assert second.task_id == first.task_id
    source = await db.query_one("SELECT * FROM tasks WHERE id='prepare-src'")
    child = await db.query_one("SELECT * FROM tasks WHERE id=?", (first.task_id,))
    assert source["revival_task_id"] == child["id"]
    assert child["revived_from_task_id"] == source["id"]
    assert child["status"] == "queued"
    assert source["revival_attempts"] == 1
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='prepare-src'"
    )) == 1


async def test_recover_orphans_requeues_and_drives_prepared_running_child(tmp_path):
    """R5: generic running tasks still fail at boot, but a canonical revival
    child is durable prepared work: running orphan -> queued -> same-id drive."""
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO tasks "
        "(id, requested_hand, prompt, status, source, workspace_dir, timeout_s, "
        " fallback_chain, error, created_at, finished_at) "
        "VALUES ('recover-src','echo','retry me','rate_limited','test',?,60,"
        " '[\"echo\"]','quota',?,?)",
        (str(tmp_path), now, now),
    )
    source = await db.query_one("SELECT * FROM tasks WHERE id='recover-src'")
    async with db.transaction() as conn:
        prepared = await executor.prepare_respawn_from_row(conn, source, max_attempts=5)
    await db.execute(
        "UPDATE tasks SET status='running', hand='echo', started_at=? "
        "WHERE id=? AND status='queued'",
        (bus.now_iso(), prepared.task_id),
    )

    assert await executor.recover_orphans() == 1
    running = list(executor._running.values())
    if running:
        await asyncio.gather(*running)

    child = await db.query_one("SELECT * FROM tasks WHERE id=?", (prepared.task_id,))
    assert child["status"] == "completed"
    assert child["revived_from_task_id"] == "recover-src"
    assert len(await db.query(
        "SELECT id FROM tasks WHERE revived_from_task_id='recover-src'"
    )) == 1


class AlwaysRateLimitedHand(Hand):
    name = "flaky"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        return HandResult(
            output="429 too many requests", exit_code=1,
            rate_limit=RateLimitInfo(reason="rate_limit", retry_after_s=120, raw="429 too many requests"),
        )


async def test_fallback_to_echo_and_cooldown(tmp_path, monkeypatch):
    registry = get_registry()
    registry.register(AlwaysRateLimitedHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "flaky", ["echo"])

    task = await executor.submit("flaky", "fall back please", source="test", workspace=tmp_path)
    assert task.status == "completed"
    assert task.requested_hand == "flaky"
    assert task.hand == "echo"
    assert "flaky" in (task.tried or [])
    assert "[echo]" in task.output

    # the rate-limited hand went on cooldown
    assert registry.cooling_until("flaky") is not None
    assert not registry.is_available("flaky")
    snap = {s["name"]: s for s in registry.status_snapshot()}
    assert snap["flaky"]["cooldown_reason"] == "rate_limit"

    registry.clear_cooldown("flaky")
    assert registry.cooling_until("flaky") is None
    assert registry.is_available("flaky")


# ---- M8-003: execution policy + retry lineage persist on the row -------------

async def test_submit_persists_fallback_chain_and_lineage_root(tmp_path):
    """The exact chain a caller confined resolution to lands on the row (the
    retry endpoint replays it later), as does the retry lineage root."""
    task = await executor.submit(
        "echo", "persist my policy", source="test", workspace=tmp_path,
        fallback_chain=["echo"], lineage_root="roottask00001",
    )
    assert task.status == "completed"
    row = await db.query_one(
        "SELECT fallback_chain, lineage_root FROM tasks WHERE id = ?", (task.id,)
    )
    assert json.loads(row["fallback_chain"]) == ["echo"]
    assert row["lineage_root"] == "roottask00001"
    # the Task dataclass surfaces both
    assert task.fallback_chain == ["echo"]
    assert task.lineage_root == "roottask00001"


async def test_submit_without_chain_persists_null(tmp_path):
    """No explicit chain -> NULL (registry-default fallback), NOT '[]': the
    retry endpoint distinguishes 'no confinement' from an explicit chain."""
    task = await executor.submit("echo", "default policy", source="test", workspace=tmp_path)
    row = await db.query_one(
        "SELECT fallback_chain, lineage_root FROM tasks WHERE id = ?", (task.id,)
    )
    assert row["fallback_chain"] is None
    assert row["lineage_root"] is None
    assert task.fallback_chain is None
    assert task.lineage_root is None


async def test_spawn_persists_fallback_chain_and_lineage_root(tmp_path):
    """spawn() (the retry endpoint's path) persists the same policy fields;
    a tuple chain (settings.research_hand_names is a tuple) stores as JSON."""
    task_id = await executor.spawn(
        "echo", "spawned with policy", source="test", workspace=tmp_path,
        fallback_chain=("echo",), lineage_root="rootspawn0001",
    )
    atask = executor._running.get(task_id)
    if atask is not None:
        await atask
    row = await db.query_one(
        "SELECT status, fallback_chain, lineage_root FROM tasks WHERE id = ?", (task_id,)
    )
    assert row["status"] == "completed"
    assert json.loads(row["fallback_chain"]) == ["echo"]
    assert row["lineage_root"] == "rootspawn0001"


# ---- ROADMAP Phase 2: per-hand queue-depth cap ('overcommitted', 0028) -------

async def _queue_backlog(hand: str, n: int) -> None:
    """Insert n inert queued rows for the hand — a backlog nobody executes."""
    for i in range(n):
        await db.execute(
            "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"backlog-{hand}-{i}", hand, "parked", "queued", "test", bus.now_iso()),
        )


async def test_submit_over_depth_cap_fast_fails_overcommitted(tmp_path, monkeypatch):
    """Beyond the cap, submit() sheds the task as a born-terminal
    'overcommitted' row: no hand assignment, no queued/running events,
    cancel refuses it."""
    assert get_settings().hand_queue_depth == 8  # documented default
    monkeypatch.setattr(get_settings(), "hand_queue_depth", 2)
    await _queue_backlog("echo", 3)              # 3 > 2: over the cap

    task = await executor.submit("echo", "shed me", source="test", workspace=tmp_path)
    assert task.status == "overcommitted"
    assert task.hand is None                     # never ran on any hand
    assert "3 queued" in (task.error or "") and "cap 2" in (task.error or "")
    row = await db.query_one(
        "SELECT created_at, finished_at FROM tasks WHERE id = ?", (task.id,)
    )
    assert row["finished_at"] == row["created_at"]  # born terminal

    events = await bus.replay(0, types=["task."])
    types = [e.type for e in events if e.ref_id == task.id]
    assert types == ["task.overcommitted"]       # never queued, never ran

    # terminal semantics: cancel is a no-op, exactly like other terminal rows
    assert await executor.cancel(task.id) is False


async def test_spawn_over_depth_cap_fast_fails_overcommitted(tmp_path, monkeypatch):
    """spawn() (fire-and-forget) applies the same admission check and never
    schedules an asyncio task for a shed row."""
    monkeypatch.setattr(get_settings(), "hand_queue_depth", 1)
    await _queue_backlog("echo", 2)              # 2 > 1: over the cap

    task_id = await executor.spawn("echo", "shed me too", source="test", workspace=tmp_path)
    assert task_id not in executor._running
    row = await db.query_one("SELECT status, hand, error FROM tasks WHERE id = ?", (task_id,))
    assert row["status"] == "overcommitted"
    assert row["hand"] is None
    assert "cap 1" in row["error"]


async def test_submit_at_or_under_depth_cap_runs_normally(tmp_path, monkeypatch):
    """A backlog of EXACTLY the cap is still admitted (the cap bounds runaway
    pileups, not normal fan-out bursts), and the count is per-hand — another
    hand's backlog never sheds this one."""
    monkeypatch.setattr(get_settings(), "hand_queue_depth", 3)
    await _queue_backlog("echo", 3)        # 3 > 3 is false: admitted
    await _queue_backlog("otherhand", 9)   # other hand's backlog is irrelevant

    task = await executor.submit("echo", "still admitted", source="test", workspace=tmp_path)
    assert task.status == "completed"
    assert task.hand == "echo"


async def test_recover_orphans_ignores_overcommitted_rows(tmp_path, monkeypatch):
    """'overcommitted' is terminal: the boot orphan sweep (queued/running only)
    must leave it untouched while the queued backlog rows ARE swept."""
    monkeypatch.setattr(get_settings(), "hand_queue_depth", 1)
    await _queue_backlog("echo", 2)
    task = await executor.submit("echo", "shed", source="test", workspace=tmp_path)
    assert task.status == "overcommitted"

    n = await executor.recover_orphans()
    assert n == 2  # only the queued backlog rows were orphan-marked

    after = await executor.get_task(task.id)
    assert after.status == "overcommitted"
    assert "orphaned" not in (after.error or "")


# ---- LOOP-P1: lock order — hand mutex FIRST, then the global semaphore -------

class BlockingHand(Hand):
    """Holds its per-hand mutex until released — a stand-in for a long CLI run."""

    name = "slowhand"
    hand_type = "cli"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.started.set()
        await self.release.wait()
        return HandResult(output="slow done", exit_code=0)


async def test_backlog_on_busy_hand_does_not_starve_idle_hand(tmp_path, monkeypatch):
    """Waiters parked behind a busy hand must NOT pin global semaphore slots.

    Regression for the old ``async with _sem(), _hand_lock(...)`` order: with
    max_concurrent=3, one running slowhand task plus two waiters used to hold
    all three global slots, so a submit to the idle echo hand hung until the
    slowhand backlog drained. Correct order (hand lock first) leaves waiters
    off the semaphore: the echo submit below must complete promptly while
    slowhand is still blocked.
    """
    # pin the width this scenario saturates (conftest nulls _global_sem per
    # test, so the first _sem() below builds the semaphore from this value)
    monkeypatch.setattr(get_settings(), "max_concurrent", 3)
    slow = BlockingHand()
    get_registry().register(slow)

    ids = [await executor.spawn(
        "slowhand", "occupy the hand", source="test", workspace=tmp_path, fallback=False,
    )]
    await asyncio.wait_for(slow.started.wait(), timeout=5)  # running: holds the hand lock
    for i in range(2):  # two waiters queue up behind the busy hand
        ids.append(await executor.spawn(
            "slowhand", f"wait behind it {i}", source="test", workspace=tmp_path, fallback=False,
        ))
    await asyncio.sleep(0.05)  # let both waiters reach their lock await

    try:
        task = await asyncio.wait_for(
            executor.submit(
                "echo", "idle hand must still run", source="test",
                workspace=tmp_path, fallback=False,
            ),
            timeout=4,
        )
        assert task.status == "completed"
        assert task.hand == "echo"
    finally:
        slow.release.set()  # drain the slowhand backlog either way
        for tid in ids:
            atask = executor._running.get(tid)
            if atask is not None:
                await atask

    for tid in ids:  # the parked waiters ran normally once the hand freed up
        row = await db.query_one("SELECT status FROM tasks WHERE id = ?", (tid,))
        assert row["status"] == "completed"
