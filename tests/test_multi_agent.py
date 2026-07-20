"""Multi-agent primitives (Phase 7): spawn/wait fan-out, join, the HTTP face.

Everything runs on the echo hand (conftest pins it): fan_out task counts,
persona wrapping, ordering; the spawn/wait split (a wait timeout never
cancels the tasks — REVIEW-C5 M1); the four join modes over synthetic Tasks;
the API's validation (unknown analyst 400, ≤5 agents cap, wait_s budget with
202 semantics) and synchronous run.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import db
from app.hands.base import EchoHand
from app.institute import multi_agent
from app.router import executor
from app.router.executor import Task


def _task(tid: str, status: str = "completed", output: str = "", error: str | None = None) -> Task:
    return Task(
        id=tid, status=status, hand="echo", requested_hand="echo", model=None,
        prompt="p", source="test", session_id=None, parent_run_id=None,
        workspace_dir="", output=output, error=error,
    )


def _make_app() -> FastAPI:
    from app.api import multi_agent as api_multi_agent

    app = FastAPI()
    app.include_router(api_multi_agent.router)
    return app


def _gate_echo(monkeypatch) -> asyncio.Event:
    """Block every echo execution behind an event (simulates a slow hand)."""
    gate = asyncio.Event()
    orig = EchoHand.execute

    async def gated_execute(self, prompt, workspace, **kwargs):
        await gate.wait()
        return await orig(self, prompt, workspace, **kwargs)

    monkeypatch.setattr(EchoHand, "execute", gated_execute)
    return gate


# ---- fan_out -------------------------------------------------------------------

async def test_fan_out_one_task_per_agent_in_order():
    agents = ["macro-analyst", "equity-analyst", "policy-analyst"]
    tasks = await multi_agent.fan_out(agents, "请就命题表态", timeout_s=60)
    assert len(tasks) == 3
    assert all(t.status == "completed" for t in tasks)
    # persona sandwich: each echo output carries ITS analyst's name, in order
    for agent_id, task in zip(agents, tasks):
        from app.institute.analysts import get_analyst
        assert get_analyst(agent_id).name in task.output
        assert "请就命题表态" in task.output
    # one tasks row per agent, all on the audit spine
    rows = await db.query("SELECT id FROM tasks WHERE source = 'multi_agent'")
    assert {r["id"] for r in rows} == {t.id for t in tasks}


async def test_spawn_wait_split_timeout_never_cancels(monkeypatch):
    """REVIEW-C5 M1 semantics: spawn returns while execution is in flight;
    a wait timeout raises but the tasks keep running and land terminal."""
    gate = _gate_echo(monkeypatch)
    ids = await multi_agent.spawn_fan_out(
        ["macro-analyst", "equity-analyst"], "预算检查", timeout_s=60,
    )
    assert len(ids) == 2                       # spawn didn't block on the gate
    with pytest.raises(asyncio.TimeoutError):
        await multi_agent.wait_fan_out(ids, timeout_s=0.2)
    # not cancelled: open the gate and the same ids run to completion
    gate.set()
    tasks = await multi_agent.wait_fan_out(ids)
    assert [t.status for t in tasks] == ["completed", "completed"]
    assert [t.id for t in tasks] == ids        # rows come back in task_ids order


async def test_fan_out_rejects_unknown_or_empty_agents():
    with pytest.raises(ValueError, match="unknown analyst"):
        await multi_agent.fan_out(["macro-analyst", "nobody"], "x")
    with pytest.raises(ValueError, match="must not be empty"):
        await multi_agent.fan_out([], "x")
    # nothing was spawned before the validation tripped
    assert await db.query("SELECT id FROM tasks WHERE source = 'multi_agent'") == []


# ---- join ----------------------------------------------------------------------

def test_join_all_mode():
    ok = multi_agent.join([_task("a"), _task("b")], "all")
    assert ok["ok"] is True and ok["output"] is None and len(ok["outputs"]) == 2
    bad = multi_agent.join([_task("a"), _task("b", status="failed", error="boom")], "all")
    assert bad["ok"] is False
    assert bad["outputs"][1]["error"] == "boom"
    assert multi_agent.join([], "all")["ok"] is False


def test_join_first_success_mode():
    r = multi_agent.join(
        [_task("a", status="failed"), _task("b", output="第二个"), _task("c", output="第三个")],
        "first_success",
    )
    assert r["ok"] is True and r["output"] == "第二个"
    none = multi_agent.join([_task("a", status="failed")], "first_success")
    assert none["ok"] is False and none["output"] is None


def test_join_majority_vote_mode():
    win = multi_agent.join(
        [_task("a", output="看多"), _task("b", output="看多 "), _task("c", output="看空")],
        "majority_vote",
    )
    assert win["ok"] is True and win["output"] == "看多" and win["votes"] == 2

    # exact-match only: prose that differs by one char never converges
    split = multi_agent.join(
        [_task("a", output="看多，因为流动性"), _task("b", output="看多，因为盈利"), _task("c", output="看多")],
        "majority_vote",
    )
    assert split["ok"] is False and split["output"] is None and split["votes"] == 1

    # failures count against the quorum: 2 identical of 4 is not a majority
    quorum = multi_agent.join(
        [_task("a", output="X"), _task("b", output="X"),
         _task("c", status="failed"), _task("d", status="failed")],
        "majority_vote",
    )
    assert quorum["ok"] is False and quorum["votes"] == 2


def test_join_best_effort_mode():
    r = multi_agent.join(
        [_task("a", output="成了"), _task("b", status="failed", error="boom")], "best_effort"
    )
    assert r["ok"] is True and r["output"] is None
    assert [o["status"] for o in r["outputs"]] == ["completed", "failed"]
    all_dead = multi_agent.join([_task("a", status="failed")], "best_effort")
    assert all_dead["ok"] is False


def test_join_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown join mode"):
        multi_agent.join([_task("a")], "quorum")


# ---- API -----------------------------------------------------------------------

async def test_api_run_happy_path_synchronous():
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "equity-analyst"],
            "prompt": "一句话表态",
            "mode": "all",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "all" and body["ok"] is True
    assert [o["agent"] for o in body["outputs"]] == ["macro-analyst", "equity-analyst"]
    assert all(o["status"] == "completed" and "一句话表态" in o["output"] for o in body["outputs"])


async def test_api_validation_and_cap():
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        # unknown analyst -> 400
        r = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "ghost"], "prompt": "x",
        })
        assert r.status_code == 400 and "ghost" in r.json()["detail"]

        # cap: 6 agents -> 400 (5 is the max)
        six = ["macro-analyst", "equity-analyst", "policy-analyst",
               "tech-analyst", "consumer-analyst", "commodity-analyst"]
        r = await client.post("/api/multi-agent/run", json={"agents": six, "prompt": "x"})
        assert r.status_code == 400 and "at most 5" in r.json()["detail"]

        # exactly 5 passes the cap
        r = await client.post("/api/multi-agent/run", json={
            "agents": six[:5], "prompt": "五人上限", "mode": "best_effort",
        })
        assert r.status_code == 200 and r.json()["ok"] is True

        # empty agents / blank prompt / bad mode / bad timeouts -> 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": [], "prompt": "x"})).status_code == 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "  "})).status_code == 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "x", "mode": "quorum"})).status_code == 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "x", "timeout_s": 0})).status_code == 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "x", "timeout_s": 3601})).status_code == 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "x", "wait_s": 0})).status_code == 400
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "x", "wait_s": 1801})).status_code == 400
        # malformed body stays FastAPI's standard 422 (pydantic layer)
        assert (await client.post("/api/multi-agent/run", json={})).status_code == 422


async def test_api_202_when_wait_budget_elapses_tasks_keep_running(monkeypatch):
    """M1 fix: the request never outlives wait_s — 202 hands back the task
    ids, the tasks are NOT cancelled and finish into the tasks table."""
    gate = _gate_echo(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "equity-analyst"],
            "prompt": "慢任务", "mode": "all", "wait_s": 0.2,
        })
    assert r.status_code == 202
    body = r.json()
    assert body["agents"] == ["macro-analyst", "equity-analyst"]
    assert len(body["task_ids"]) == 2 and "keep running" in body["detail"]
    # the ids are live rows, still in flight
    for tid in body["task_ids"]:
        row = await executor.get_task(tid)
        assert row is not None and row.status in ("queued", "running")
    # open the gate: the same tasks run to completion (poll-later semantics)
    gate.set()
    tasks = await multi_agent.wait_fan_out(body["task_ids"])
    assert all(t.status == "completed" for t in tasks)
