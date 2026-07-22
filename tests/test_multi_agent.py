"""Multi-agent primitives (Phase 7): spawn/wait fan-out, join, the HTTP face.

Everything runs on the echo hand (conftest pins it): fan_out task counts,
persona wrapping, ordering; the spawn/wait split (a wait timeout never
cancels the tasks — REVIEW-C5 M1); the four join modes over synthetic Tasks;
the API's validation (unknown analyst 400, ≤5 agents cap, wait_s budget with
202 semantics) and synchronous run.

M8-012 additions: structured majority ballots (VERDICT-line extraction +
tally); durable groups (CRUD, validation, run history surviving group
deletion); durable runs (intent row + parent_run_id linkage, settle-on-read
reconnect, partial-spawn recording and stale recovery); the API's group CRUD
/ group run / run-history / reconnect endpoints.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

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


async def test_majority_fan_out_injects_protocol_but_echo_does_not_cast_itself():
    tasks = await multi_agent.fan_out(
        ["macro-analyst", "equity-analyst"], "请选择看多或看空", mode="majority_vote",
        timeout_s=60,
    )
    assert all(multi_agent.MAJORITY_BALLOT_PROTOCOL_MARKER in t.prompt for t in tasks)
    # Echo mirrors the production prompt. The inline protocol example must not
    # be mistaken for a model ballot, and free text must fail closed.
    result = multi_agent.join(tasks, "majority_vote")
    assert result["ok"] is False and result["votes"] == 0
    assert result["valid_ballots"] == 0 and result["invalid_ballots"] == 2
    assert {o["ballot_error"] for o in result["outputs"]} == {
        "missing_structured_verdict"
    }


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
        [_task("a", output="依据 A。\nVERDICT: 看多"),
         _task("b", output="依据 B。\nVERDICT: 看多 "),
         _task("c", output="依据 C。\nVERDICT: 看空")],
        "majority_vote",
    )
    assert win["ok"] is True and win["output"] == "看多" and win["votes"] == 2

    # exact-match only: structured labels that differ by one char do not converge
    split = multi_agent.join(
        [_task("a", output="VERDICT: 看多"), _task("b", output="VERDICT: 看多。"),
         _task("c", output="VERDICT: 偏多")],
        "majority_vote",
    )
    assert split["ok"] is False and split["output"] is None and split["votes"] == 1

    # failures count against the quorum: 2 identical of 4 is not a majority
    quorum = multi_agent.join(
        [_task("a", output="VERDICT: X"), _task("b", output="VERDICT: X"),
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

        # one analyst cannot occupy multiple panel seats / votes
        r = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "macro-analyst"], "prompt": "x",
        })
        assert r.status_code == 400 and "duplicate" in r.json()["detail"]

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
        assert (await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "x", "promtp": "typo",
        })).status_code == 422


async def test_domain_rejects_duplicate_agents_before_persist_or_spawn():
    with pytest.raises(ValueError, match="duplicate"):
        await multi_agent.start_run(
            ["macro-analyst", "macro-analyst"], "不能重复计票", timeout_s=60,
        )
    assert await db.query("SELECT * FROM multi_agent_runs") == []


async def test_domain_rejects_more_than_five_agents_before_persist_or_spawn():
    six = [
        "macro-analyst", "equity-analyst", "policy-analyst",
        "tech-analyst", "consumer-analyst", "commodity-analyst",
    ]
    with pytest.raises(ValueError, match="at most 5"):
        await multi_agent.start_run(six, "domain 也必须守上限", timeout_s=60)
    with pytest.raises(ValueError, match="at most 5"):
        await multi_agent.spawn_fan_out(six, "primitive 也必须守上限", timeout_s=60)
    assert await db.query("SELECT * FROM multi_agent_runs") == []
    assert await db.query("SELECT * FROM tasks WHERE source = 'multi_agent'") == []


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


# ---- structured ballots (M8-012) --------------------------------------------------


def test_extract_ballot_requires_valid_verdict_line():
    assert multi_agent.extract_ballot("论证很长……\nVERDICT: 看多\n") == "看多"
    # the LAST verdict line wins; case-insensitive; full-width colon accepted
    assert multi_agent.extract_ballot("verdict: 初判\n中间推理\nVERDICT：看空") == "看空"
    assert multi_agent.extract_ballot("  没有裁决行  ") is None
    assert multi_agent.extract_ballot("") is None
    assert multi_agent.extract_ballot("VERDICT:   \n") is None
    assert multi_agent.extract_ballot(
        "VERDICT: " + "X" * (multi_agent.MAJORITY_BALLOT_MAX_CHARS + 1)
    ) is None


def test_join_majority_vote_structured_ballots():
    """M8-012: prose differs, the VERDICT line converges — and the result
    carries the structured tally plus each output's ballot."""
    r = multi_agent.join(
        [_task("a", output="从流动性角度看多。\nVERDICT: 看多"),
         _task("b", output="从盈利角度也看多。\nVERDICT: 看多"),
         _task("c", output="政策风险太大。\nVERDICT: 看空")],
        "majority_vote",
    )
    assert r["ok"] is True and r["output"] == "看多" and r["votes"] == 2
    assert r["ballots"] == [{"ballot": "看多", "votes": 2}, {"ballot": "看空", "votes": 1}]
    assert [o["ballot"] for o in r["outputs"]] == ["看多", "看多", "看空"]


def test_join_majority_vote_failed_task_ballot_is_null():
    r = multi_agent.join(
        [_task("a", output="VERDICT: X"), _task("b", status="failed", error="boom")],
        "majority_vote",
    )
    assert r["outputs"][0]["ballot"] == "X" and r["outputs"][1]["ballot"] is None
    assert r["outputs"][1]["ballot_status"] == "not_counted"
    assert r["outputs"][1]["ballot_error"] == "task_not_completed"
    assert r["ok"] is False and r["ballots"] == [{"ballot": "X", "votes": 1}]


def test_join_majority_vote_free_text_is_invalid_and_not_counted():
    """Identical essays cannot manufacture a majority without VERDICT lines."""
    r = multi_agent.join(
        [_task("a", output="看多，因为流动性改善"),
         _task("b", output="看多，因为流动性改善"),
         _task("c", output="反方论证\nVERDICT: 看空")],
        "majority_vote",
    )
    assert r["ok"] is False and r["output"] is None and r["votes"] == 1
    assert r["valid_ballots"] == 1 and r["invalid_ballots"] == 2
    assert r["ballots"] == [{"ballot": "看空", "votes": 1}]
    assert [o["ballot"] for o in r["outputs"]] == [None, None, "看空"]
    assert [o["ballot_status"] for o in r["outputs"]] == ["invalid", "invalid", "valid"]
    assert r["outputs"][0]["ballot_error"] == "missing_structured_verdict"


# ---- durable groups (M8-012) --------------------------------------------------------


async def test_group_crud_roundtrip():
    g = await multi_agent.create_group(
        "  宏观\n对比组 ", ["macro-analyst", "equity-analyst"],
        description="多空对比", mode="majority_vote",
    )
    assert g["name"] == "宏观 对比组"                     # collapsed to one plain line
    assert g["agents"] == ["macro-analyst", "equity-analyst"]
    assert g["mode"] == "majority_vote" and g["hand"] is None
    assert await multi_agent.get_group(g["id"]) == g
    assert any(x["id"] == g["id"] for x in await multi_agent.list_groups())

    upd = await multi_agent.update_group(g["id"], {"agents": ["policy-analyst"], "mode": "all"})
    assert upd["agents"] == ["policy-analyst"] and upd["mode"] == "all"
    assert upd["name"] == "宏观 对比组"                   # untouched fields survive
    assert upd["updated_at"] >= g["updated_at"]

    assert await multi_agent.delete_group(g["id"]) is True
    assert await multi_agent.get_group(g["id"]) is None
    assert await multi_agent.delete_group(g["id"]) is False   # idempotent probe
    assert await multi_agent.update_group(g["id"], {"name": "x"}) is None


async def test_group_validation():
    with pytest.raises(ValueError, match="unknown analysts"):
        await multi_agent.create_group("g1", ["macro-analyst", "ghost"])
    with pytest.raises(ValueError, match="must not be empty"):
        await multi_agent.create_group("g2", [])
    with pytest.raises(ValueError, match="duplicate"):
        await multi_agent.create_group("g3", ["macro-analyst", "macro-analyst"])
    with pytest.raises(ValueError, match="at most"):
        await multi_agent.create_group("g4", [
            "macro-analyst", "equity-analyst", "policy-analyst",
            "tech-analyst", "consumer-analyst", "commodity-analyst",
        ])
    with pytest.raises(ValueError, match="unknown join mode"):
        await multi_agent.create_group("g5", ["macro-analyst"], mode="quorum")
    with pytest.raises(ValueError, match="name must not be empty"):
        await multi_agent.create_group("   ", ["macro-analyst"])
    await multi_agent.create_group("dup", ["macro-analyst"])
    with pytest.raises(ValueError, match="already exists"):
        await multi_agent.create_group("dup", ["equity-analyst"])
    # nothing above left a group behind except 'dup'
    assert [g["name"] for g in await multi_agent.list_groups()] == ["dup"]


async def test_deleting_group_keeps_run_history():
    g = await multi_agent.create_group("留痕组", ["macro-analyst"])
    run = await multi_agent.start_run(
        ["macro-analyst"], "留痕", group_id=g["id"], timeout_s=60,
    )
    await multi_agent.wait_fan_out(run["task_ids"])
    assert (await multi_agent.settle_run(run["id"]))["status"] == "completed"

    assert await multi_agent.delete_group(g["id"]) is True
    rec = await multi_agent.get_run_record(run["id"])
    assert rec is not None
    assert rec["group_id"] is None                       # ON DELETE SET NULL
    assert rec["agents"] == ["macro-analyst"]            # frozen at spawn


# ---- durable runs: reconnect + partial-spawn recovery (M8-012) ------------------------


async def test_start_run_persists_durable_row_and_task_linkage():
    run = await multi_agent.start_run(
        ["macro-analyst", "equity-analyst"], "请表个态", mode="all", timeout_s=60,
    )
    assert run["status"] == "running" and len(run["task_ids"]) == 2
    assert run["agents"] == ["macro-analyst", "equity-analyst"]
    assert run["prompt"] == "请表个态"                    # the input snapshot
    # linkage is atomic with the task rows themselves (crash recovery data)
    rows = await db.query("SELECT id FROM tasks WHERE parent_run_id = ?", (run["id"],))
    assert {r["id"] for r in rows} == set(run["task_ids"])

    await multi_agent.wait_fan_out(run["task_ids"])
    settled = await multi_agent.settle_run(run["id"])
    assert settled["status"] == "completed" and settled["finished_at"]
    v = settled["verdict"]
    assert v["kind"] == "fan_out" and v["mode"] == "all" and v["ok"] is True
    assert [o["agent"] for o in v["outputs"]] == ["macro-analyst", "equity-analyst"]
    # storage-lean verdict: per-task refs only, full text stays in tasks rows
    assert all("output" not in o for o in v["outputs"])


async def test_settle_run_is_reconnect_safe(monkeypatch):
    """Settling mid-flight returns the running row unchanged; after the tasks
    finish, settle claims once and replays idempotently (the reconnect read)."""
    gate = _gate_echo(monkeypatch)
    run = await multi_agent.start_run(["macro-analyst"], "慢一点", timeout_s=60)
    mid = await multi_agent.settle_run(run["id"])
    assert mid["status"] == "running" and mid["verdict"] is None
    gate.set()
    await multi_agent.wait_fan_out(run["task_ids"])
    one = await multi_agent.settle_run(run["id"])
    two = await multi_agent.settle_run(run["id"])
    assert one["status"] == two["status"] == "completed"
    assert one["verdict"] == two["verdict"]
    assert one["finished_at"] == two["finished_at"]       # second call claimed nothing


async def test_start_run_records_partial_spawn_failure(monkeypatch):
    """S4-P2-06: a spawn-layer failure mid-loop claims the row failed WITH the
    already-spawned ids recorded — recoverable, not orphaned."""
    orig = executor.spawn
    calls = {"n": 0}

    async def flaky_spawn(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom on second spawn")
        return await orig(*args, **kwargs)

    monkeypatch.setattr(executor, "spawn", flaky_spawn)
    with pytest.raises(multi_agent.RunSpawnError, match="boom on second spawn") as caught:
        await multi_agent.start_run(["macro-analyst", "equity-analyst"], "x", timeout_s=60)

    recs = await multi_agent.list_run_records()
    assert len(recs) == 1
    assert caught.value.run_id == recs[0]["id"]
    assert caught.value.task_ids == recs[0]["task_ids"]
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert recs[0]["status"] == "failed"
    assert "spawn failed after 1 of 2 agents" in recs[0]["error"]
    assert len(recs[0]["task_ids"]) == 1                  # the spawned id is on record
    await multi_agent.wait_fan_out(recs[0]["task_ids"])   # and it ran to completion


async def test_api_spawn_failure_returns_failed_run_context(monkeypatch):
    """A partial executor spawn is a durable, inspectable 503 — never an
    opaque framework 500 that loses the run id or already-spawned task ids."""
    orig = executor.spawn
    calls = {"n": 0}

    async def flaky_spawn(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("executor temporarily unavailable")
        return await orig(*args, **kwargs)

    monkeypatch.setattr(executor, "spawn", flaky_spawn)
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        response = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "equity-analyst"],
            "prompt": "记住失败 run", "mode": "all",
        })
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["agents"] == ["macro-analyst", "equity-analyst"]
        assert body["mode"] == "all"
        assert len(body["task_ids"]) == 1
        assert "1 of 2" in body["error"]
        assert "keep running" in body["detail"]

        reconnect = await client.get(f"/api/multi-agent/runs/{body['run_id']}")
        assert reconnect.status_code == 200
        record = reconnect.json()
        assert record["id"] == body["run_id"]
        assert record["status"] == "failed"
        assert record["task_ids"] == body["task_ids"]
        assert record["error"] == body["error"]

    await multi_agent.wait_fan_out(body["task_ids"])


async def test_settle_run_recovers_task_ids_from_parent_run_id():
    """Crash window: the task_ids write never landed — settle re-derives the
    ids from the tasks table (parent_run_id was written with each spawn)."""
    run = await multi_agent.start_run(["macro-analyst", "equity-analyst"], "恢复", timeout_s=60)
    await multi_agent.wait_fan_out(run["task_ids"])
    await db.execute("UPDATE multi_agent_runs SET task_ids = '[]' WHERE id = ?", (run["id"],))

    settled = await multi_agent.settle_run(run["id"])
    assert settled["status"] == "completed"
    assert set(settled["task_ids"]) == set(run["task_ids"])   # re-derived + persisted


async def test_settle_run_partial_spawn_stale_vs_young():
    """A 'running' row with fewer tasks than agents settles as failed only
    once it is provably stale — a young row may still be mid-spawn."""
    async def _mk(run_id: str, created_at: str) -> str:
        await db.execute(
            "INSERT INTO multi_agent_runs (id, agents, mode, prompt, status, task_ids, created_at) "
            "VALUES (?,?,?,?, 'running', '[]', ?)",
            (run_id, json.dumps(["macro-analyst", "equity-analyst"]), "all", "p", created_at),
        )
        tid = await executor.spawn(
            "echo", "半途而废", source="multi_agent", parent_run_id=run_id, timeout_s=60,
        )
        await multi_agent.wait_fan_out([tid])
        return tid

    young_tid = await _mk("marunyoung01", (datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    young = await multi_agent.settle_run("marunyoung01")
    assert young["status"] == "running"                   # not stale: left alone
    assert young_tid  # (spawned task is terminal; only staleness gates the claim)

    stale_created = (
        datetime.now(timezone.utc) - timedelta(seconds=multi_agent.RUN_SPAWN_STALE_S + 60)
    ).isoformat(timespec="seconds")
    stale_tid = await _mk("marunstale01", stale_created)
    stale = await multi_agent.settle_run("marunstale01")
    assert stale["status"] == "failed"
    assert "partial spawn: 1 of 2 tasks spawned" in stale["error"]
    assert stale["task_ids"] == [stale_tid]               # what DID spawn is on record


async def test_list_run_records_filters_and_validation():
    run = await multi_agent.start_run(["macro-analyst"], "历史", timeout_s=60)
    await multi_agent.wait_fan_out(run["task_ids"])
    await multi_agent.settle_run(run["id"])
    assert [r["id"] for r in await multi_agent.list_run_records(status="completed")] == [run["id"]]
    assert await multi_agent.list_run_records(status="running") == []
    assert await multi_agent.list_run_records(group_id="nope") == []
    with pytest.raises(ValueError, match="unknown status"):
        await multi_agent.list_run_records(status="bogus")


# ---- API: durable runs + groups (M8-012) ---------------------------------------------


async def test_api_run_persists_record_and_reconnect_read():
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "equity-analyst"], "prompt": "一句话表态", "mode": "all",
        })
        assert r.status_code == 200
        run_id = r.json()["run_id"]

        rr = await client.get(f"/api/multi-agent/runs/{run_id}")
        assert rr.status_code == 200
        body = rr.json()
        assert body["status"] == "completed" and body["verdict"]["ok"] is True
        # the reconnect read re-reads full text from the tasks rows
        assert [o["agent"] for o in body["outputs"]] == ["macro-analyst", "equity-analyst"]
        assert all("一句话表态" in o["output"] for o in body["outputs"])

        assert (await client.get("/api/multi-agent/runs/nope")).status_code == 404
        assert (await client.get("/api/multi-agent/runs", params={"status": "bogus"})).status_code == 400


async def test_api_majority_missing_verdict_is_explainable_invalid_ballot():
    """The production API prompt has the contract, while a non-compliant hand
    degrades to zero valid votes instead of electing duplicated free text."""
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        response = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst", "equity-analyst"],
            "prompt": "请选择看多或看空", "mode": "majority_vote",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False and body["votes"] == 0
        assert body["valid_ballots"] == 0 and body["invalid_ballots"] == 2
        assert all(o["ballot"] is None and o["ballot_status"] == "invalid" for o in body["outputs"])
        assert all(o["ballot_error"] == "missing_structured_verdict" for o in body["outputs"])

        task = await executor.get_task(body["outputs"][0]["task_id"])
        assert task is not None
        assert multi_agent.MAJORITY_BALLOT_PROTOCOL_MARKER in task.prompt

        reconnect = (await client.get(f"/api/multi-agent/runs/{body['run_id']}")).json()
        assert reconnect["prompt"] == "请选择看多或看空"  # caller input snapshot stays clean
        verdict = reconnect["verdict"]
        assert verdict["ok"] is False
        assert verdict["valid_ballots"] == 0 and verdict["invalid_ballots"] == 2
        assert verdict["outputs"][0]["ballot_error"] == "missing_structured_verdict"


async def test_api_202_reconnect_settles_on_read(monkeypatch):
    """The 202 path now carries run_id; the run record answers 'running' while
    tasks fly and settles on the first read after they finish (reconnect)."""
    gate = _gate_echo(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.post("/api/multi-agent/run", json={
            "agents": ["macro-analyst"], "prompt": "慢", "wait_s": 0.2,
        })
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        mid = await client.get(f"/api/multi-agent/runs/{run_id}")
        assert mid.status_code == 200 and mid.json()["status"] == "running"

        gate.set()
        await multi_agent.wait_fan_out(r.json()["task_ids"])
        done = await client.get(f"/api/multi-agent/runs/{run_id}")
    assert done.json()["status"] == "completed"
    assert done.json()["verdict"]["ok"] is True
    assert done.json()["outputs"][0]["status"] == "completed"


async def test_api_group_crud_and_group_run():
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.post("/api/multi-agent/groups", json={
            "name": "多空对比", "agents": ["macro-analyst", "equity-analyst"],
            "mode": "best_effort",
        })
        assert r.status_code == 200
        gid = r.json()["id"]
        assert any(g["id"] == gid for g in (await client.get("/api/multi-agent/groups")).json())
        assert (await client.get(f"/api/multi-agent/groups/{gid}")).json()["mode"] == "best_effort"

        r = await client.put(f"/api/multi-agent/groups/{gid}", json={"description": "改描述"})
        assert r.status_code == 200 and r.json()["description"] == "改描述"

        # validation face: duplicate name / bad mode / unknown ids -> 400; 404s
        assert (await client.post("/api/multi-agent/groups", json={
            "name": "多空对比", "agents": ["macro-analyst"]})).status_code == 400
        assert (await client.put(f"/api/multi-agent/groups/{gid}",
                                 json={"mode": "quorum"})).status_code == 400
        assert (await client.get("/api/multi-agent/groups/nope")).status_code == 404

        # every mutation body is strict: typos are explicit 422s, never
        # successful requests with silently ignored fields.
        assert (await client.post("/api/multi-agent/groups", json={
            "name": "严格合同", "agents": ["macro-analyst"], "oops": True,
        })).status_code == 422
        assert (await client.put(f"/api/multi-agent/groups/{gid}", json={
            "descriptin": "typo",
        })).status_code == 422
        assert (await client.post(f"/api/multi-agent/groups/{gid}/run", json={
            "prompt": "x", "wait_seconds": 1,
        })).status_code == 422
        assert (await client.delete("/api/multi-agent/groups/nope")).status_code == 404

        # run the panel with its stored routing strategy
        r = await client.post(f"/api/multi-agent/groups/{gid}/run", json={"prompt": "组内表态"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "best_effort" and body["ok"] is True
        assert [o["agent"] for o in body["outputs"]] == ["macro-analyst", "equity-analyst"]

        # run history is filterable by group
        r = await client.get("/api/multi-agent/runs", params={"group_id": gid})
        assert [x["id"] for x in r.json()] == [body["run_id"]]

        assert (await client.post("/api/multi-agent/groups/nope/run",
                                  json={"prompt": "x"})).status_code == 404
        assert (await client.post(f"/api/multi-agent/groups/{gid}/run",
                                  json={"prompt": "  "})).status_code == 400

        # delete -> 204; the run record keeps its frozen history
        assert (await client.delete(f"/api/multi-agent/groups/{gid}")).status_code == 204
        r = await client.get(f"/api/multi-agent/runs/{body['run_id']}")
        assert r.status_code == 200 and r.json()["group_id"] is None
