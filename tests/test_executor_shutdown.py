"""Graceful-shutdown drain: cancel in-flight work, kill CLI process groups.

Covers app.main._drain_background — the lifespan shutdown hook that must run
before db.close() so cancellation paths can persist final rows and the hands
can kill their subprocess groups (hands/base.run_subprocess kills the pgid on
CancelledError) — plus the pre-shutdown scheduler job snapshot and the
registries added for analyst-daily and shielded research ticks.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from app.hands.base import Hand, HandResult, run_subprocess
from app.hands.registry import get_registry
from app.institute import analyst_daily, research
from app.institute import scheduler as scheduler_mod
from app.main import _drain_background, _scheduler_inflight
from app.router import executor


class HangingHand(Hand):
    """Blocks forever — only a cancel can end it."""

    name = "hanging"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        await asyncio.sleep(3600)
        return HandResult(output="never reached", exit_code=0)


class SubprocessHand(Hand):
    """Runs a real subprocess via run_subprocess (its own process group)."""

    name = "subproc"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        out, _err, code = await run_subprocess(
            ["/bin/sh", "-c", "sleep 300 & echo $! > child.pid; wait"],
            cwd=workspace, timeout_s=timeout_s,
        )
        return HandResult(output=out, exit_code=code)


async def _wait_until_running(task_id: str) -> None:
    for _ in range(500):
        t = await executor.get_task(task_id)
        if t and t.status == "running":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached running")


def _register(reg: set[asyncio.Task], coro, name: str | None = None) -> asyncio.Task:
    t = asyncio.create_task(coro, name=name)
    reg.add(t)
    t.add_done_callback(reg.discard)
    return t


async def test_drain_cancels_inflight_task_and_marks_row_cancelled(tmp_path):
    get_registry().register(HangingHand())
    task_id = await executor.spawn(
        "hanging", "block forever", source="test", workspace=tmp_path, fallback=False,
    )
    await _wait_until_running(task_id)

    await _drain_background(timeout_s=5.0)

    task = await executor.get_task(task_id)
    assert task.status == "cancelled"  # persisted BEFORE the db would close
    assert executor._running == {}  # registry drained


async def test_drain_kills_subprocess_process_group(tmp_path):
    get_registry().register(SubprocessHand())
    task_id = await executor.spawn(
        "subproc", "spawn a child", source="test", workspace=tmp_path, fallback=False,
    )

    pid_file = tmp_path / "child.pid"
    for _ in range(500):
        if pid_file.is_file() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("subprocess never wrote child.pid")
    child_pid = int(pid_file.read_text().strip())
    os.kill(child_pid, 0)  # child alive before the drain

    await _drain_background(timeout_s=5.0)

    task = await executor.get_task(task_id)
    assert task.status == "cancelled"
    # the whole process group died (SIGKILL on the pgid); allow reaping lag
    for _ in range(500):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail(f"child {child_pid} survived the drain")


async def test_drain_noop_when_nothing_running():
    await _drain_background(timeout_s=1.0)  # must not raise or hang


async def test_drain_covers_analyst_daily_and_research_registries():
    """M1: the drain must also sweep analyst_daily._background and
    research._bg_tasks (shielded ticks), not just the original four sets."""
    async def _hang():
        await asyncio.sleep(3600)

    t_daily = _register(analyst_daily._background, _hang())
    t_research = _register(research._bg_tasks, _hang())

    await _drain_background(timeout_s=5.0)

    assert t_daily.cancelled()
    assert t_research.cancelled()
    assert t_daily not in analyst_daily._background
    assert t_research not in research._bg_tasks


async def test_shielded_tick_registers_and_unregisters():
    t = research.shielded_tick()
    assert t in research._bg_tasks
    assert await t is None  # empty queue: tick is a no-op
    assert t not in research._bg_tasks


async def test_drain_consumes_and_logs_task_exceptions(caplog):
    """M1: the done set is inspected — a task that blows up during cancel
    cleanup is logged, never silently dropped, and the drain never raises."""
    async def _boom():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup exploded") from None

    t = _register(research._bg_tasks, _boom(), name="boom-task")
    await asyncio.sleep(0)  # let the coroutine reach its await before the cancel

    with caplog.at_level(logging.WARNING, logger="institute"):
        await _drain_background(timeout_s=5.0)

    assert t.done() and not t.cancelled()
    assert isinstance(t.exception(), RuntimeError)
    assert any(
        "boom-task" in r.getMessage() and "RuntimeError" in r.getMessage()
        for r in caplog.records
    )


async def test_drain_second_sweep_catches_work_spawned_during_cancel():
    """A task cancelled in sweep 1 may enqueue one last piece of work (e.g. a
    scheduler job submitting an executor task); sweep 2 must cancel it too."""
    late: list[asyncio.Task] = []

    async def _spawner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            late.append(_register(research._bg_tasks, asyncio.sleep(3600)))
            raise

    t1 = _register(research._bg_tasks, _spawner())
    await asyncio.sleep(0)  # let the coroutine reach its await before the cancel

    await _drain_background(timeout_s=5.0)

    assert t1.cancelled()
    assert late and late[0].cancelled()
    assert late[0] not in research._bg_tasks


async def test_scheduler_inflight_snapshot_and_drain(monkeypatch):
    """The pre-shutdown snapshot picks in-flight scheduler job tasks out of
    APScheduler's executor internals; the drain then awaits their cancel."""
    assert _scheduler_inflight() == set()  # no scheduler in tests

    async def _hang():
        await asyncio.sleep(3600)

    job = asyncio.create_task(_hang(), name="fake-scheduler-job")

    class FakeExecutor:
        _pending_futures = {job}

    class FakeScheduler:
        _executors = {"default": FakeExecutor()}

    monkeypatch.setattr(scheduler_mod, "_scheduler", FakeScheduler())
    snap = _scheduler_inflight()
    assert snap == {job}

    await _drain_background(timeout_s=5.0, extra=snap)
    assert job.cancelled()


async def test_inflight_jobs_public_accessor(monkeypatch):
    """scheduler.inflight_jobs(): filters done tasks, degrades to an empty set
    on APScheduler internals drift instead of breaking shutdown."""
    assert scheduler_mod.inflight_jobs() == set()  # no scheduler running

    async def _hang():
        await asyncio.sleep(3600)

    live = asyncio.create_task(_hang(), name="live-job")
    done = asyncio.create_task(asyncio.sleep(0), name="done-job")
    await done

    class FakeExecutor:
        _pending_futures = {live, done, "not-a-task"}

    class FakeScheduler:
        _executors = {"default": FakeExecutor()}

    monkeypatch.setattr(scheduler_mod, "_scheduler", FakeScheduler())
    assert scheduler_mod.inflight_jobs() == {live}  # done + non-task filtered

    # internals drift (e.g. a 4.x rename): degrade to empty, never raise
    class DriftedScheduler:
        @property
        def _executors(self):
            raise AttributeError("gone in this APScheduler version")

    monkeypatch.setattr(scheduler_mod, "_scheduler", DriftedScheduler())
    assert scheduler_mod.inflight_jobs() == set()
    assert _scheduler_inflight() == set()  # main-side wrapper stays quiet too

    live.cancel()
    await asyncio.gather(live, return_exceptions=True)
