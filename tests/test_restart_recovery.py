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

import asyncio
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


async def test_lifespan_boot_drives_bound_revival_child_instead_of_failing_it(tmp_path):
    """R5 boot integration: executor recovery recognizes a canonical revival
    child as durable prepared work. The exact queued id survives lifespan boot
    and completes; generic orphan semantics must not turn it failed first."""
    from app.router import executor

    now = bus.now_iso()
    await db.execute(
        "INSERT INTO tasks "
        "(id, requested_hand, prompt, status, source, workspace_dir, timeout_s, "
        " fallback_chain, error, created_at, finished_at) "
        "VALUES ('boot-revival-src','echo','boot retry','rate_limited','test',?,60,"
        " '[\"echo\"]','quota',?,?)",
        (str(tmp_path), now, now),
    )
    source = await db.query_one("SELECT * FROM tasks WHERE id='boot-revival-src'")
    async with db.transaction() as conn:
        prepared = await executor.prepare_respawn_from_row(conn, source, max_attempts=5)
    await db.execute(
        "UPDATE tasks SET error=error || '\n[rate-limit-revival:claimed]' "
        "WHERE id='boot-revival-src'"
    )

    async with _lifespan():
        for _ in range(100):
            child = await db.query_one(
                "SELECT status FROM tasks WHERE id=?", (prepared.task_id,)
            )
            if child["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert child["status"] == "completed"
        source = await db.query_one(
            "SELECT revival_task_id FROM tasks WHERE id='boot-revival-src'"
        )
        assert source["revival_task_id"] == prepared.task_id
        assert len(await db.query(
            "SELECT id FROM tasks WHERE revived_from_task_id='boot-revival-src'"
        )) == 1


async def test_paused_boot_reconciles_without_model_work_then_scheduler_resumes(tmp_path):
    """Maintenance covers the pre-scheduler boot window too.

    Durable revival/mailbox work is reconciled to queued, but neither echo
    driver may start until maintenance is explicitly resumed.  Pure cleanup
    (generic task failure and research requeue) still runs at boot; after the
    flip, the existing gated jobs adopt the exact same prepared task ids.
    """
    from app.institute import mailbox
    from app.router import executor

    now = bus.now_iso()

    await db.execute(
        "INSERT INTO tasks "
        "(id, requested_hand, prompt, status, source, workspace_dir, timeout_s, "
        " fallback_chain, error, created_at, finished_at) "
        "VALUES ('paused-revival-src','echo','paused retry','rate_limited','test',?,60,"
        " '[\"echo\"]','quota',?,?)",
        (str(tmp_path / "revival"), now, now),
    )
    source = await db.query_one("SELECT * FROM tasks WHERE id='paused-revival-src'")
    async with db.transaction() as conn:
        prepared = await executor.prepare_respawn_from_row(conn, source, max_attempts=5)
    assert prepared is not None
    await db.execute(
        "UPDATE tasks SET status='running', started_at=? WHERE id=?",
        (now, prepared.task_id),
    )

    await db.execute(
        "INSERT INTO mailbox_threads "
        "(id, subject, analyst_id, status, created_at, updated_at) "
        "VALUES ('paused-mailbox','暂停恢复','macro-analyst','open',?,?)",
        (now, now),
    )
    dispatch_id = await db.insert(
        "INSERT INTO mailbox_messages "
        "(thread_id, author, kind, body, status, created_at) "
        "VALUES ('paused-mailbox','macro-analyst','dispatch','','pending',?)",
        (now,),
    )
    _thread, analyst, hand, prompt = await mailbox._prepare_dispatch(
        "paused-mailbox", dispatch_id,
    )
    mailbox_task_id, _lease, reason = await mailbox._book_dispatch_task(
        "paused-mailbox", dispatch_id,
        hand=hand, model=analyst.model, prompt=prompt,
    )
    assert reason == "ok" and mailbox_task_id
    await db.execute(
        "UPDATE tasks SET status='running', started_at=? WHERE id=?",
        (now, mailbox_task_id),
    )

    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at) "
        "VALUES ('paused-generic','echo','cleanup only','queued','test',?)",
        (now,),
    )
    await db.execute(
        "INSERT INTO research_queue "
        "(id, topic, status, source, created_at, started_at) "
        "VALUES ('paused-research','纯状态对账','running','test',?,?)",
        (now, now),
    )
    await scheduler_mod.set_maintenance(True)

    async with _lifespan():
        revival_child = await db.query_one(
            "SELECT status FROM tasks WHERE id=?", (prepared.task_id,),
        )
        mailbox_task = await db.query_one(
            "SELECT status FROM tasks WHERE id=?", (mailbox_task_id,),
        )
        dispatch = await db.query_one(
            "SELECT status, leased_at FROM mailbox_messages WHERE id=?", (dispatch_id,),
        )
        assert revival_child["status"] == "queued"
        assert mailbox_task["status"] == "queued"
        assert dispatch == {"status": "pending", "leased_at": None}
        assert prepared.task_id not in executor._running
        assert mailbox_task_id not in executor._running

        generic = await db.query_one(
            "SELECT status, error FROM tasks WHERE id='paused-generic'"
        )
        assert generic == {"status": "failed", "error": "orphaned by restart"}
        research = await db.query_one(
            "SELECT status, started_at FROM research_queue WHERE id='paused-research'"
        )
        assert research == {"status": "pending", "started_at": None}

        await scheduler_mod.set_maintenance(False)
        await scheduler_mod._rate_limit_revival_job()
        await scheduler_mod._mailbox_sweep_job()
        for _ in range(200):
            revival_child = await db.query_one(
                "SELECT status FROM tasks WHERE id=?", (prepared.task_id,),
            )
            dispatch = await db.query_one(
                "SELECT status FROM mailbox_messages WHERE id=?", (dispatch_id,),
            )
            if revival_child["status"] == "completed" and dispatch["status"] == "done":
                break
            await asyncio.sleep(0.01)
        assert revival_child["status"] == "completed"
        assert dispatch["status"] == "done"
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE revived_from_task_id='paused-revival-src'"
        ))["n"] == 1
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE mailbox_dispatch_id=?",
            (dispatch_id,),
        ))["n"] == 1


async def test_lifespan_boot_prewarms_active_prompt_override():
    """A persisted active override is live on the first prompt after boot."""
    from app.institute import prompt_overrides, prompts
    from app.institute.analysts import get_analyst

    row = await prompt_overrides.create(
        "prompts.citation_mandate", "【重启后立即生效的引用规范】",
    )
    await prompt_overrides.activate(row["id"])
    prompt_overrides.invalidate_cache()  # model a fresh process
    assert prompt_overrides._cache is None

    async with _lifespan():
        prompt = prompts.build_analyst_prompt(get_analyst("macro-analyst"), "启动探针")
        assert "【重启后立即生效的引用规范】" in prompt
        assert prompt_overrides._cache == {
            "prompts.citation_mandate": "【重启后立即生效的引用规范】",
        }


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
