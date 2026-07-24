"""Mailbox: create_thread dispatch produces an analyst reply on echo; clean sweep."""
from __future__ import annotations

import asyncio
from datetime import datetime

from app import bus, db
from app.config import get_settings
from app.institute import mailbox


async def _wait_for_reply(thread_id: str, timeout_s: float = 5.0) -> dict:
    """Poll get_thread until the dispatch lands its reply."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        thread = await mailbox.get_thread(thread_id)
        replies = [m for m in thread["messages"] if m["kind"] == "reply"]
        if replies:
            return thread
        await asyncio.sleep(0.02)
    raise AssertionError(f"no reply on thread {thread_id} after {timeout_s}s")


async def test_create_thread_produces_reply_on_echo():
    thread = await mailbox.create_thread("利率展望", "macro-analyst", "请给出下周美债收益率的判断。")
    assert thread["status"] == "open"
    assert thread["analyst_id"] == "macro-analyst"
    kinds = [m["kind"] for m in thread["messages"]]
    assert kinds[0] == "note"        # operator note
    assert "dispatch" in kinds       # pending dispatch row spawned

    thread = await _wait_for_reply(thread["id"])
    replies = [m for m in thread["messages"] if m["kind"] == "reply"]
    assert len(replies) == 1
    assert replies[0]["author"] == "macro-analyst"
    assert replies[0]["body"].strip()

    dispatches = [m for m in thread["messages"] if m["kind"] == "dispatch"]
    assert all(m["status"] == "done" for m in dispatches)
    assert all(m["task_id"] for m in dispatches)

    events = await bus.replay(0, types=["mailbox.reply"])
    assert any(e.ref_id == thread["id"] for e in events)


async def test_sweep_is_noop_when_clean():
    thread = await mailbox.create_thread("收盘点评", "equity-analyst", "今天 A 股怎么看？")
    await _wait_for_reply(thread["id"])

    before = await db.query("SELECT id, status FROM mailbox_messages ORDER BY id")
    await mailbox.sweep()
    await asyncio.sleep(0.05)
    if mailbox._bg_tasks:  # sweep must not have spawned anything
        await asyncio.gather(*list(mailbox._bg_tasks), return_exceptions=True)

    after = await db.query("SELECT id, status FROM mailbox_messages ORDER BY id")
    assert after == before
    pending = await db.query(
        "SELECT id FROM mailbox_messages WHERE kind='dispatch' AND status='pending'"
    )
    assert pending == []


# ---- loop-fix P11h: one sweep re-drives a bounded batch ----------------------

async def _orphan_dispatch(thread_id: str) -> int:
    return await db.insert(
        "INSERT INTO mailbox_messages (thread_id, author, kind, body, status, created_at) "
        "VALUES (?,'macro-analyst','dispatch','','pending',?)",
        (thread_id, bus.now_iso()),
    )


async def test_sweep_redrive_is_capped_per_tick(monkeypatch):
    """A restart with a large orphan backlog must not re-drive everything in
    one tick: sweep spawns at most SWEEP_REDRIVE_LIMIT dispatches per firing,
    and skipped (in-flight) rows do not consume the cap."""
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO mailbox_threads (id, subject, analyst_id, status, created_at, updated_at) "
        "VALUES ('t-sweepcap','积压','macro-analyst','open',?,?)",
        (now, now),
    )
    # three rows already driven by THIS process (must be skipped, not counted)
    for _ in range(3):
        mailbox._inflight.add(await _orphan_dispatch("t-sweepcap"))
    # a backlog larger than the cap
    for _ in range(mailbox.SWEEP_REDRIVE_LIMIT + 5):
        await _orphan_dispatch("t-sweepcap")

    spawned: list[object] = []

    def capture(coro) -> None:
        spawned.append(coro)
        coro.close()  # never actually drive the dispatch

    monkeypatch.setattr(mailbox, "_spawn_bg", capture)
    await mailbox.sweep()
    assert len(spawned) == mailbox.SWEEP_REDRIVE_LIMIT == 20

    # the rest stays pending for the next tick — nothing was lost
    rows = await db.query(
        "SELECT id FROM mailbox_messages WHERE kind='dispatch' AND status='pending'"
    )
    assert len(rows) == 3 + mailbox.SWEEP_REDRIVE_LIMIT + 5


# ---- R3 P2: the sweep scan itself is bounded and rotates ---------------------

async def _make_thread(thread_id: str) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO mailbox_threads (id, subject, analyst_id, status, created_at, updated_at) "
        "VALUES (?,'积压','macro-analyst','open',?,?)",
        (thread_id, now, now),
    )


async def test_sweep_scan_and_checks_are_bounded_per_tick(monkeypatch):
    """One firing reads a bounded window and schedules a bounded subset.

    Stale bound rows are now intentionally candidates: the reconciler joins
    or conditionally drives the SAME task id, so recovery is safe without
    trusting an in-memory owner registry.  The scan still performs no N+1
    task probes and never schedules more than the redrive cap.
    """
    from app.router import executor as executor_mod

    await _make_thread("t-scanbound")
    now = bus.now_iso()
    backlog = mailbox.SWEEP_SCAN_LIMIT + 10
    for i in range(backlog):
        await db.execute(
            "INSERT INTO tasks (id, prompt, status, source, created_at) "
            "VALUES (?,'x','running','mailbox-scan',?)",
            (f"live-task-{i:04d}", now),
        )
        await db.execute(
            "INSERT INTO mailbox_messages (thread_id, author, kind, body, task_id, status, created_at) "
            "VALUES ('t-scanbound','macro-analyst','dispatch','',?,'pending',?)",
            (f"live-task-{i:04d}", now),
        )

    fetch_sizes: list[int] = []
    real_query = db.query

    async def spy_query(sql, params=()):
        rows = await real_query(sql, params)
        if "mailbox_messages" in sql and "pending" in sql:
            fetch_sizes.append(len(rows))
        return rows

    get_task_calls = 0
    real_get_task = executor_mod.get_task

    async def spy_get_task(task_id):
        nonlocal get_task_calls
        get_task_calls += 1
        return await real_get_task(task_id)

    spawned: list[object] = []

    def capture(coro) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(mailbox.db, "query", spy_query)
    monkeypatch.setattr(executor_mod, "get_task", spy_get_task)
    monkeypatch.setattr(mailbox, "_spawn_bg", capture)

    await mailbox.sweep()

    assert len(spawned) == mailbox.SWEEP_REDRIVE_LIMIT
    assert fetch_sizes and max(fetch_sizes) <= mailbox.SWEEP_SCAN_LIMIT
    assert get_task_calls == 0


async def test_sweep_does_not_let_inflight_veto_stale_durable_state(monkeypatch):
    """R5 P2: a hung local coroutine cannot override a stale DB lease."""
    await _make_thread("t-stale-inflight")
    orphan_id = await _orphan_dispatch("t-stale-inflight")
    mailbox._inflight.add(orphan_id)

    spawned: list[object] = []

    def capture(coro) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(mailbox, "_spawn_bg", capture)

    await mailbox.sweep()
    assert len(spawned) == 1

    row = await db.query_one(
        "SELECT status FROM mailbox_messages WHERE id = ?", (orphan_id,)
    )
    assert row["status"] == "pending"  # untouched until the dispatch really runs


# ---- R4/R5: durable dispatch claim and task binding --------------------------


async def _backdate_lease(sql: str, params: tuple) -> None:
    """Tolerant lease aging: pre-fix schema has no lease columns — the flow
    is then unprotected anyway, which is exactly what the red run exposes."""
    try:
        await db.execute(sql, params)
    except Exception:  # noqa: BLE001
        pass


async def test_late_dispatch_worker_cannot_overwrite_task_id_or_reply(monkeypatch):
    """R5 P1: lease expiry may attach another driver, never another task.

    Worker 1 parks before the queued->running claim. Worker 2 sees the stale
    lease but drives the SAME durable task id and wins that conditional claim.
    Worker 1 later observes the terminal row; exactly one model generation,
    reply, and event exist.
    """
    await _make_thread("t-latewriter")
    mid = await _orphan_dispatch("t-latewriter")

    worker1_parked = asyncio.Event()
    release_worker1 = asyncio.Event()
    drivers = 0
    model_runs = 0

    async def fake_drive(task_id):
        nonlocal drivers, model_runs
        drivers += 1
        if drivers == 1:
            worker1_parked.set()
            await release_worker1.wait()
        claimed = await db.execute(
            "UPDATE tasks SET status='running', started_at=? WHERE id=? AND status='queued'",
            (bus.now_iso(), task_id),
        )
        if claimed:
            model_runs += 1
            await db.execute(
                "UPDATE tasks SET status='completed', output='reply from winner', "
                "exit_code=0, finished_at=? WHERE id=? AND status='running'",
                (bus.now_iso(), task_id),
            )
        return await mailbox.executor.get_task(task_id)

    monkeypatch.setattr(mailbox, "_drive_bound_task", fake_drive)

    w1 = asyncio.create_task(mailbox._run_dispatch("t-latewriter", mid))
    await asyncio.wait_for(worker1_parked.wait(), timeout=5)

    # durable lease ages out while worker 1 is parked before the task claim
    await _backdate_lease(
        "UPDATE mailbox_messages SET leased_at='2026-07-20T00:00:00+00:00' WHERE id=?",
        (mid,),
    )
    await mailbox._run_dispatch("t-latewriter", mid)  # worker2: full run

    release_worker1.set()
    await asyncio.wait_for(w1, timeout=5)

    msg = await db.query_one("SELECT * FROM mailbox_messages WHERE id = ?", (mid,))
    assert msg["status"] == "done"
    assert msg["dispatch_attempts"] == 1
    assert model_runs == 1
    tasks = await db.query(
        "SELECT id, status FROM tasks WHERE mailbox_dispatch_id=?", (mid,),
    )
    assert tasks == [{"id": msg["task_id"], "status": "completed"}]
    replies = await db.query(
        "SELECT body FROM mailbox_messages WHERE thread_id='t-latewriter' AND kind='reply'"
    )
    assert [r["body"] for r in replies] == ["reply from winner"]
    events = await bus.replay(0, types=["mailbox.reply"])
    assert len([e for e in events if e.ref_id == "t-latewriter"]) == 1


async def test_sweep_reclaims_only_stale_leases(monkeypatch):
    """A pending dispatch whose lease is fresh belongs to a live worker (or a
    crash younger than the TTL): sweep must not re-drive it. Once the lease
    ages past the stale horizon, the next sweep reclaims it."""
    await _make_thread("t-leases")
    mid = await _orphan_dispatch("t-leases")
    await _backdate_lease(
        "UPDATE mailbox_messages SET lease_id='deadworker', leased_at=? WHERE id=?",
        (bus.now_iso(), mid),
    )

    spawned: list[object] = []

    def capture(coro) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(mailbox, "_spawn_bg", capture)

    await mailbox.sweep()
    assert spawned == []  # fresh lease: not sweepable

    await _backdate_lease(
        "UPDATE mailbox_messages SET leased_at='2026-07-20T00:00:00+00:00' WHERE id=?",
        (mid,),
    )
    await mailbox.sweep()
    assert len(spawned) == 1  # stale lease reclaimed


# ---- R5: atomic reply + durable binding + bounded failure -------------------

async def _complete_prepared_task(task_id: str, output: str = "durable analyst reply"):
    await db.execute(
        "UPDATE tasks SET status='completed', output=?, exit_code=0, finished_at=? "
        "WHERE id=? AND status='queued'",
        (output, bus.now_iso(), task_id),
    )
    return await mailbox.executor.get_task(task_id)


async def test_reply_insert_failure_rolls_back_terminal_and_reuses_completed_task(monkeypatch):
    """R5 P1: done/reply/thread/event are one transaction.

    A normal SQLite failure cannot leave done-without-reply. The completed
    durable task remains bound, so retrying settlement performs zero new
    model calls and produces exactly one reply/event.
    """
    await _make_thread("t-atomic-reply")
    mid = await _orphan_dispatch("t-atomic-reply")

    calls = 0

    async def complete(task_id):
        nonlocal calls
        calls += 1
        return await _complete_prepared_task(task_id)

    monkeypatch.setattr(mailbox, "_drive_bound_task", complete)
    await db.execute(
        "CREATE TRIGGER fail_mailbox_reply BEFORE INSERT ON mailbox_messages "
        "WHEN NEW.kind='reply' BEGIN SELECT RAISE(ABORT, 'synthetic reply failure'); END"
    )
    await mailbox._run_dispatch("t-atomic-reply", mid)

    dispatch = await db.query_one("SELECT * FROM mailbox_messages WHERE id=?", (mid,))
    assert dispatch["status"] == "pending"
    assert dispatch["task_id"] and dispatch["dispatch_attempts"] == 1
    assert dispatch["reconcile_attempts"] == 1
    assert await db.query(
        "SELECT id FROM mailbox_messages WHERE dispatch_id=?", (mid,),
    ) == []
    assert [e for e in await bus.replay(0, types=["mailbox.reply"])
            if e.ref_id == "t-atomic-reply"] == []

    await db.execute("DROP TRIGGER fail_mailbox_reply")
    await mailbox._run_dispatch("t-atomic-reply", mid)

    dispatch = await db.query_one("SELECT * FROM mailbox_messages WHERE id=?", (mid,))
    assert dispatch["status"] == "done" and dispatch["reply_event_id"]
    assert calls == 1  # second pass parsed/settled durable output only
    replies = await db.query(
        "SELECT body, task_id, dispatch_id FROM mailbox_messages WHERE dispatch_id=?",
        (mid,),
    )
    assert replies == [{
        "body": "durable analyst reply",
        "task_id": dispatch["task_id"],
        "dispatch_id": mid,
    }]
    events = [e for e in await bus.replay(0, types=["mailbox.reply"])
              if e.ref_id == "t-atomic-reply"]
    assert len(events) == 1 and events[0].payload["dispatch_id"] == mid
    assert (await db.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE mailbox_dispatch_id=?", (mid,),
    ))["n"] == 1


async def test_dispatch_model_failures_stop_at_attempt_ceiling(monkeypatch):
    """R5 P1: repeated hard/terminal failures cannot burn unbounded quota."""
    await _make_thread("t-attempt-cap")
    mid = await _orphan_dispatch("t-attempt-cap")

    async def fail(task_id):
        await db.execute(
            "UPDATE tasks SET status='failed', error='synthetic failure', finished_at=? "
            "WHERE id=? AND status='queued'",
            (bus.now_iso(), task_id),
        )
        return await mailbox.executor.get_task(task_id)

    monkeypatch.setattr(mailbox, "_drive_bound_task", fail)
    for _ in range(mailbox.DISPATCH_MAX_ATTEMPTS + 2):
        await mailbox._run_dispatch("t-attempt-cap", mid)

    dispatch = await db.query_one("SELECT * FROM mailbox_messages WHERE id=?", (mid,))
    assert dispatch["status"] == "failed"
    assert dispatch["dispatch_attempts"] == mailbox.DISPATCH_MAX_ATTEMPTS
    tasks = await db.query(
        "SELECT status FROM tasks WHERE mailbox_dispatch_id=? ORDER BY created_at, id", (mid,),
    )
    assert len(tasks) == mailbox.DISPATCH_MAX_ATTEMPTS
    assert {row["status"] for row in tasks} == {"failed"}
    assert await db.query(
        "SELECT id FROM mailbox_messages WHERE dispatch_id=?", (mid,),
    ) == []


async def test_completed_result_settlement_is_itself_bounded(monkeypatch):
    """A poison reply projection cannot spin every scheduler tick forever."""
    await _make_thread("t-reconcile-cap")
    mid = await _orphan_dispatch("t-reconcile-cap")

    monkeypatch.setattr(mailbox, "_drive_bound_task", _complete_prepared_task)
    await db.execute(
        "CREATE TRIGGER fail_mailbox_reply_forever BEFORE INSERT ON mailbox_messages "
        "WHEN NEW.kind='reply' BEGIN SELECT RAISE(ABORT, 'permanent reply failure'); END"
    )
    for _ in range(mailbox.DISPATCH_MAX_RECONCILE_ATTEMPTS + 2):
        await mailbox._run_dispatch("t-reconcile-cap", mid)

    dispatch = await db.query_one("SELECT * FROM mailbox_messages WHERE id=?", (mid,))
    assert dispatch["status"] == "failed"
    assert dispatch["dispatch_attempts"] == 1
    assert dispatch["reconcile_attempts"] == mailbox.DISPATCH_MAX_RECONCILE_ATTEMPTS
    assert (await db.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE mailbox_dispatch_id=?", (mid,),
    ))["n"] == 1


async def test_boot_recovery_preserves_and_schedules_same_prepared_task(monkeypatch):
    """R5 P1: restart adopts the canonical mailbox task instead of orphaning it."""
    await _make_thread("t-mailbox-boot")
    mid = await _orphan_dispatch("t-mailbox-boot")
    _thread, analyst, hand, prompt = await mailbox._prepare_dispatch("t-mailbox-boot", mid)
    task_id, _lease, reason = await mailbox._book_dispatch_task(
        "t-mailbox-boot", mid, hand=hand, model=analyst.model, prompt=prompt,
    )
    assert reason == "ok" and task_id
    await db.execute(
        "UPDATE tasks SET status='running', started_at=? WHERE id=? AND status='queued'",
        (bus.now_iso(), task_id),
    )

    await mailbox.executor.recover_orphans()
    task = await db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "queued"

    spawned: list[object] = []

    def capture(coro) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(mailbox, "_spawn_bg", capture)
    await mailbox.recover_orphans()
    assert len(spawned) == 1
    dispatch = await db.query_one("SELECT * FROM mailbox_messages WHERE id=?", (mid,))
    assert dispatch["task_id"] == task_id and dispatch["dispatch_attempts"] == 1
    assert (await db.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE mailbox_dispatch_id=?", (mid,),
    ))["n"] == 1


def test_stale_lease_cutoff_tracks_executor_timeout(monkeypatch):
    """TTL = max(DISPATCH_LEASE_TTL_S, default_timeout_s + 300): the 45min
    constant is only a floor — a larger configured executor timeout widens the
    reclaim horizon with it, so a slow-but-alive worker is never swept."""
    base = datetime.fromisoformat(bus.now_iso())

    # default 1800s timeout + 300s belt is BELOW the floor: the floor wins
    age = (base - datetime.fromisoformat(mailbox._stale_lease_cutoff())).total_seconds()
    assert mailbox.DISPATCH_LEASE_TTL_S - 2 <= age <= mailbox.DISPATCH_LEASE_TTL_S + 2

    monkeypatch.setattr(get_settings(), "default_timeout_s", 7200)
    age = (base - datetime.fromisoformat(mailbox._stale_lease_cutoff())).total_seconds()
    assert 7200 + 300 - 2 <= age <= 7200 + 300 + 2
