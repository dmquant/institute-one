"""Restart recovery at the LIFESPAN level (ROADMAP Phase 8).

Each domain's recovery function has a unit test already (test_executor /
test_research / test_factcheck); what was untested is the boot path itself:
``app.main.lifespan`` is what a real restart runs, and it must wire the sweeps
together — orphaned tasks marked failed, running research rows handed back to
pending, workflows reconciled, the scheduler started, and shutdown leaving the
process clean. Two gaps with no coverage at all are closed here too: the
janitor adopting stuck workflow runs, and the whiteboard tick failing
restart-orphaned cards and finishing the board.

Entering the real lifespan registers domain bus handlers; a fixture restores
the handler list afterwards so nothing leaks into later tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import bus, db
from app.institute import scheduler as scheduler_mod
from app.institute import whiteboard
from app.institute.prompts import work_date


@pytest.fixture(autouse=True)
async def _lifespan_isolation(app_runtime):
    """Snapshot the bus handler list around each test (the lifespan registers
    exporter/chain/factcheck/forecast/operator handlers globally) and make sure
    the DB connection is back for conftest's teardown after a lifespan closed it."""
    saved = list(bus._handlers)
    yield
    bus._handlers[:] = saved
    await db.init()  # lifespan exit closes the connection; teardown needs one


def _lifespan():
    from app.main import create_app, lifespan

    return lifespan(create_app())


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat(timespec="seconds")


async def _seed_dirty_state() -> None:
    now = bus.now_iso()
    # in-flight executor work from the "previous process"
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at) VALUES (?,?,?,?,?,?)",
        ("orphanqueued1", "echo", "排队中断", "queued", "test", now),
    )
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("orphanrunnin1", "echo", "运行中断", "running", "test", now, now),
    )
    # a running research row deadlocks _claim_next() forever unless recovered
    await db.execute(
        "INSERT INTO research_queue (id, topic, status, source, created_at, started_at) "
        "VALUES (?,?,?,?,?,?)",
        ("rq-running-1", "光模块", "running", "test", now, now),
    )
    await db.execute(
        "INSERT INTO research_queue (id, topic, status, source, created_at) VALUES (?,?,?,?,?)",
        ("rq-pending-1", "机器人", "pending", "test", now),
    )
    # crash mid-verification: one stale 'verifying' card (past the 60-min
    # staleness window) and one fresh claim that must NOT be stolen
    for card_id, claim, started in (
        ("fc-stale-1", "旧论断", _ago(hours=2)),
        ("fc-fresh-1", "新论断", now),
    ):
        await db.execute(
            "INSERT INTO fact_cards (id, source_kind, source_ref, claim, category, status, "
            "created_at, verify_started_at) VALUES (?,?,?,?,?,?,?,?)",
            (card_id, "research_report", f"src-{card_id}", claim, "other", "verifying", now, started),
        )


async def test_lifespan_boot_recovers_dirty_state_and_shuts_down_clean():
    await _seed_dirty_state()

    async with _lifespan():
        # executor sweep: every non-terminal task row is orphan-marked
        for task_id in ("orphanqueued1", "orphanrunnin1"):
            row = await db.query_one("SELECT status, error FROM tasks WHERE id = ?", (task_id,))
            assert row == {"status": "failed", "error": "orphaned by restart"}

        # research sweep: running -> pending (started_at cleared), pending untouched
        recovered = await db.query_one("SELECT * FROM research_queue WHERE id = 'rq-running-1'")
        assert recovered["status"] == "pending"
        assert recovered["started_at"] is None
        untouched = await db.query_one("SELECT status FROM research_queue WHERE id = 'rq-pending-1'")
        assert untouched["status"] == "pending"

        # boot also reconciled workflows from disk and started the scheduler
        wf_ids = {r["id"] for r in await db.query("SELECT id FROM workflows")}
        assert {"briefing", "daily", "research"} <= wf_ids
        assert scheduler_mod._scheduler is not None

        # fact-check recovery belongs to the first tick after boot, not the
        # lifespan: the stale sweep hands back stale 'verifying' cards while a
        # fresh in-flight claim keeps its slot
        from app.institute import factcheck

        await factcheck._recover_stale_running()
        stale = await db.query_one("SELECT status, verify_started_at FROM fact_cards WHERE id = 'fc-stale-1'")
        assert stale == {"status": "pending", "verify_started_at": None}
        fresh = await db.query_one("SELECT status FROM fact_cards WHERE id = 'fc-fresh-1'")
        assert fresh["status"] == "verifying"

    # shutdown left the process clean: scheduler stopped, connection closed
    assert scheduler_mod._scheduler is None
    assert db._conn is None


async def test_second_restart_is_idempotent():
    """Restarting twice must not re-orphan or double-recover anything."""
    await _seed_dirty_state()
    async with _lifespan():
        pass
    async with _lifespan():
        rows = await db.query("SELECT status FROM research_queue")
        assert {r["status"] for r in rows} == {"pending"}
        tasks = await db.query("SELECT status FROM tasks")
        assert {t["status"] for t in tasks} == {"failed"}
        # recovery is claim-based: a second boot finds nothing left to sweep
        from app.institute import research

        assert await research.recover_orphans() == 0


# ---- janitor adoption of stuck workflow runs (no prior coverage) ---------------

async def test_janitor_expires_stuck_workflow_runs_but_spares_live_ones():
    now = bus.now_iso()
    stuck_start = _ago(hours=7)
    for run_id, started in (("run-stuck", stuck_start), ("run-live", stuck_start), ("run-fresh", now)):
        await db.execute(
            "INSERT INTO workflow_runs (id, workflow_id, status, source, started_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, "briefing", "running", "test", started),
        )
    # run-live still has a live task under it -> the janitor must not touch it
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, parent_run_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("livetask00001", "echo", "长跑步骤", "queued", "workflow", "run-live", now),
    )

    await scheduler_mod._janitor()

    stuck = await db.query_one("SELECT status, error, finished_at FROM workflow_runs WHERE id = 'run-stuck'")
    assert stuck["status"] == "failed"
    assert "expired by janitor" in stuck["error"]
    assert stuck["finished_at"]
    assert (await db.query_one("SELECT status FROM workflow_runs WHERE id = 'run-live'"))["status"] == "running"
    assert (await db.query_one("SELECT status FROM workflow_runs WHERE id = 'run-fresh'"))["status"] == "running"


# ---- whiteboard: restart-orphaned running card (no prior coverage) ----------------

async def test_whiteboard_tick_fails_orphaned_card_and_finishes_board():
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, work_date, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("board-orph", "出口链复盘", "", "active", 1, work_date(), now, now),
    )
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("card-orph", "board-orph", 1, "chief-strategist", "running", now),
    )
    assert "card-orph" not in whiteboard._active_cards  # the restart wiped process state

    # first tick: the orphaned running card is failed (idx == max_cards -> no handoff)
    await whiteboard.tick()
    card = await db.query_one("SELECT status, finished_at FROM whiteboard_cards WHERE id = 'card-orph'")
    assert card["status"] == "failed"
    assert card["finished_at"]

    # second tick: nothing pending or running -> the board finalizes
    await whiteboard.tick()
    board = await db.query_one("SELECT status FROM whiteboard_boards WHERE id = 'board-orph'")
    assert board["status"] == "completed"
    events = await bus.replay(0, types=["whiteboard.board_completed"])
    assert [e.ref_id for e in events] == ["board-orph"]


# ---- research trees (round-4 D4 partition) -----------------------------------

async def test_research_tree_running_nodes_do_not_survive_restart():
    """Boot invariant for the BFS-tree partition: after a restart no
    research_tree_nodes row may still claim 'running' (drain state is
    process-local and dies with the process). Deterministic fixture through
    the domain functions (S4-P0-02 — the old reflection probe blind-inserted
    rows, always tripped a 0020 CHECK or FK, and skipped forever): claim real
    running roots, stop one tree mid-flight, then boot. recover_orphans()
    must requeue running nodes under live trees as pending and prune running
    nodes stranded under terminal trees."""
    from app.institute import research_tree

    live = await research_tree.create_tree("重启探针·存活树", max_depth=1, max_nodes=3)
    stopped = await research_tree.create_tree("重启探针·停止树", max_depth=1, max_nodes=3)
    assert "refused" not in live and "refused" not in stopped
    live_root, stopped_root = live["nodes"][0]["id"], stopped["nodes"][0]["id"]

    # legally claim both roots (pending -> running, the tick's claim path)
    concurrency = (await research_tree.get_limits())["node_concurrency"]
    claimed = set()
    for _ in range(2):
        node = await research_tree._claim_next_node(concurrency)
        assert node is not None and node["status"] == "running"
        claimed.add(node["id"])
    assert claimed == {live_root, stopped_root}

    # stop one tree while its root is mid-flight: stop_tree() prunes pending
    # nodes only, so the running root legally survives the stop — exactly the
    # crash shape recovery must prune (a terminal tree never re-runs work)
    await research_tree.stop_tree(stopped["id"])
    mid = await db.query_one(
        "SELECT status FROM research_tree_nodes WHERE id = ?", (stopped_root,)
    )
    assert mid["status"] == "running"

    async with _lifespan():
        left = await db.query(
            "SELECT id FROM research_tree_nodes WHERE status = 'running'"
        )
        assert left == []  # the invariant the old probe could never assert
        requeued = await db.query_one(
            "SELECT status, task_id FROM research_tree_nodes WHERE id = ?", (live_root,)
        )
        assert requeued == {"status": "pending", "task_id": None}
        pruned = await db.query_one(
            "SELECT status, finished_at FROM research_tree_nodes WHERE id = ?", (stopped_root,)
        )
        assert pruned["status"] == "pruned"
        assert pruned["finished_at"]
