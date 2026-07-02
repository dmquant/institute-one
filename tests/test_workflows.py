"""Workflow engine: reconcile, full run on echo, cancel between steps."""
from __future__ import annotations

import asyncio

from app import bus
from app.config import get_settings
from app.hands.base import Hand, HandResult
from app.hands.registry import get_registry
from app.institute import workflows
from app.router import executor


async def test_reconcile_from_disk_loads_repo_workflows():
    n = await workflows.reconcile_from_disk()
    assert n == 3
    ids = {w["id"] for w in await workflows.list_workflows()}
    assert {"briefing", "daily", "research"} <= ids

    briefing = await workflows.get_workflow("briefing")
    assert briefing["name"]
    assert len(briefing["steps"]) == 3
    assert isinstance(briefing["variables"], list)

    # reconcile is an upsert: running it again neither duplicates nor fails
    assert await workflows.reconcile_from_disk() == 3
    assert len(await workflows.list_workflows()) == len(ids)


async def test_run_briefing_on_echo_completes_with_three_steps():
    await workflows.reconcile_from_disk()
    run = await workflows.run_workflow_and_wait("briefing", source="test")

    assert run["status"] == "completed"
    assert len(run["results"]) == 3
    assert all(r["status"] == "completed" for r in run["results"])
    assert all(r["task_id"] for r in run["results"])
    assert run["current_step"] == 3

    # one executor task per step, tied to the run
    tasks = await executor.list_tasks(parent_run_id=run["id"])
    assert len(tasks) == 3
    assert all(t["status"] == "completed" for t in tasks)

    events = await bus.replay(0, types=["workflow.completed"])
    mine = [e for e in events if e.ref_id == run["id"]]
    assert len(mine) == 1
    assert mine[0].payload["workflow_id"] == "briefing"
    assert len(mine[0].payload["results"]) == 3


class GatedHand(Hand):
    """First call returns immediately; later calls block until released."""

    name = "gated"
    hand_type = "cli"

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return HandResult(output="step one done", exit_code=0)
        await self.release.wait()
        return HandResult(output="late finish", exit_code=0)


class RecordingHand(Hand):
    hand_type = "cli"

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.calls += 1
        return HandResult(output=f"{self.name} done", exit_code=0)


async def test_research_workflow_uses_configured_research_hands(monkeypatch):
    await workflows.reconcile_from_disk()
    settings = get_settings()
    monkeypatch.setattr(settings, "research_hands", "codex,agy")
    codex = RecordingHand("codex")
    agy = RecordingHand("agy")
    get_registry().register(codex)
    get_registry().register(agy)

    run = await workflows.run_workflow_and_wait("research", variables={"TOPIC": "AI"}, source="test")

    assert run["status"] == "completed"
    tasks = await executor.list_tasks(parent_run_id=run["id"], limit=20)
    assert len(tasks) == 7
    assert {t["requested_hand"] for t in tasks} <= {"codex", "agy"}
    assert {t["hand"] for t in tasks} <= {"codex", "agy"}
    assert codex.calls > 0
    assert agy.calls > 0


async def test_cancel_run_stops_between_steps(monkeypatch):
    await workflows.reconcile_from_disk()
    gated = GatedHand()
    get_registry().register(gated)
    monkeypatch.setattr(get_settings(), "default_hand", "gated")

    run_id = await workflows.run_workflow("briefing", source="test")

    # wait until step 2 is in flight (blocked inside the gated hand)
    for _ in range(200):
        if gated.calls >= 2:
            break
        await asyncio.sleep(0.02)
    assert gated.calls == 2

    assert await workflows.cancel_run(run_id) is True
    gated.release.set()  # even if released now, the run must stay cancelled

    for _ in range(200):
        run = await workflows.get_run(run_id)
        if run["status"] != "running" and not workflows._driving:
            break
        await asyncio.sleep(0.02)

    run = await workflows.get_run(run_id)
    assert run["status"] == "cancelled"
    assert len(run["results"]) == 1  # only step 1 ever recorded
    assert gated.calls == 2          # step 3 never started

    events = await bus.replay(0, types=["workflow.cancelled"])
    assert any(e.ref_id == run_id for e in events)
