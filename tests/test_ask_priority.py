"""Interactive asks prefer an idle hand + the per-task cancel protocol.

ROADMAP Phase 0 "Interactive asks queue behind long workflow steps":
``executor.hand_busy()`` reads the per-hand mutex; ``tasks.resolve_ask``
(shared by /api/ask, /api/ask/stream via ``prepare_ask`` and by the MCP
institute_ask tool) reroutes an unpinned busy hand to the first
idle+available hand in its fallback chain. Busy hands are faked by
holding the real ``executor._hand_lock`` — no model calls beyond echo.

Cancel: POST /api/tasks/{id}/cancel — queued rows flip conditionally (and a
submit parked on the mutex is woken), running tasks are cancelled through
``executor._running`` (the shutdown drain's mechanism — process-group kill
physics are covered by tests/test_executor_shutdown.py — applied to one
task), terminal tasks answer 409, unknown ids 404.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from app import bus, db
from app.api import ask_stream as ask_stream_api
from app.api import tasks as tasks_api
from app.api.tasks import AskBody, prepare_ask
from app.hands import registry as registry_mod
from app.hands.base import EchoHand, Hand, HandResult
from app.hands.registry import get_registry
from app.main import create_app
from app.router import executor


class SecondEchoHand(EchoHand):
    """A second always-available hand to reroute onto."""

    name = "echo2"


class HangingHand(Hand):
    """Blocks until cancelled (same shape as test_executor_shutdown)."""

    name = "hanging"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        await asyncio.sleep(3600)
        return HandResult(output="never reached", exit_code=0)


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@asynccontextmanager
async def _busy(hand: str):
    """Fake a busy hand by holding its real executor mutex."""
    lock = executor._hand_lock(hand)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


async def _wait_status(task_id: str, status: str) -> None:
    for _ in range(500):
        t = await executor.get_task(task_id)
        if t and t.status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached {status}")


async def _reap(task_id: str) -> None:
    atask = executor._running.get(task_id)
    if atask is not None:
        await asyncio.gather(atask, return_exceptions=True)


# ---- hand_busy ---------------------------------------------------------------

async def test_hand_busy_reads_the_per_hand_mutex():
    assert executor.hand_busy("echo") is False  # lazily created: no lock yet
    async with _busy("echo"):
        assert executor.hand_busy("echo") is True
    assert executor.hand_busy("echo") is False


# ---- prepare_ask: idle-hand preference ----------------------------------------

async def test_busy_unpinned_hand_reroutes_to_first_idle_chain_hand(monkeypatch):
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"):
        hand, prompt = await prepare_ask(AskBody(prompt="快答"))
    assert hand == "echo2"
    assert prompt == "快答"


async def test_idle_hand_is_kept():
    hand, _ = await prepare_ask(AskBody(prompt="hi"))
    assert hand == "echo"


async def test_explicit_hand_is_never_rerouted(monkeypatch):
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"):
        hand, _ = await prepare_ask(AskBody(prompt="hi", hand="echo"))
    assert hand == "echo"  # pinned: queue behind the busy hand as before


async def test_explicit_model_pins_the_hand_too(monkeypatch):
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"):
        hand, _ = await prepare_ask(AskBody(prompt="hi", model="some-model"))
    assert hand == "echo"  # a model is hand-family-specific: no reroute


async def test_whole_chain_busy_queues_as_before(monkeypatch):
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"), _busy("echo2"):
        hand, _ = await prepare_ask(AskBody(prompt="hi"))
    assert hand == "echo"


async def test_unavailable_chain_hands_are_skipped(monkeypatch):
    """An idle hand that is not available (not installed / cooling / degraded)
    must not be picked — the walk continues to the next idle+available one."""
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(
        registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["claude", "echo2"],
    )
    assert not get_registry().is_available("claude")  # disabled in tests
    async with _busy("echo"):
        hand, _ = await prepare_ask(AskBody(prompt="hi"))
    assert hand == "echo2"


async def test_ask_endpoint_reroutes_and_completes(monkeypatch):
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"):
        async with _client() as client:
            resp = await client.post("/api/ask", json={"prompt": "空闲手接单"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["hand"] == "echo2"
    assert body["status"] == "completed"
    assert "空闲手接单" in body["output"]


async def test_ask_stream_shares_the_same_preprocessing(monkeypatch):
    """/api/ask/stream reuses tasks.prepare_ask (shared, not mirrored) — the
    idle-hand preference arrives there automatically."""
    assert ask_stream_api.prepare_ask is tasks_api.prepare_ask
    assert not hasattr(ask_stream_api, "_prepare")  # the old mirror is gone

    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"):
        async with _client() as client:
            resp = await client.post("/api/ask/stream", json={"prompt": "流式空闲手"})
    assert resp.status_code == 200
    frames = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    done = frames[-1]
    assert done["type"] == "done"
    assert done["task"]["hand"] == "echo2"
    assert done["task"]["status"] == "completed"


# ---- MCP institute_ask: same shared preprocessing -----------------------------


async def _mcp_call(client: httpx.AsyncClient, name: str, arguments: dict) -> dict:
    r = await client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    payload = r.json()
    assert "error" not in payload
    return json.loads(payload["result"]["content"][0]["text"])


async def test_mcp_institute_ask_shares_the_idle_hand_preference(monkeypatch):
    """MCP institute_ask routes through tasks.resolve_ask (the same helper
    prepare_ask wraps): an unpinned busy hand reroutes to an idle sibling."""
    get_registry().register(SecondEchoHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "echo", ["echo2"])
    async with _busy("echo"):
        async with _client() as client:
            res = await _mcp_call(client, "institute_ask", {"prompt": "空闲手接单"})
    assert res["hand"] == "echo2"
    assert res["status"] == "completed"
    assert "空闲手接单" in res["output"]


async def test_mcp_institute_ask_passes_model_and_timeout_through():
    """The drift fix: model/timeout_s are accepted and land on the task row."""
    async with _client() as client:
        res = await _mcp_call(client, "institute_ask", {
            "prompt": "透传参数", "model": "echo-model", "timeout_s": 42,
        })
    assert res["status"] == "completed"
    row = await db.query_one("SELECT model, timeout_s FROM tasks WHERE id = ?", (res["task_id"],))
    assert row["model"] == "echo-model"
    assert row["timeout_s"] == 42


# ---- cancel protocol -----------------------------------------------------------

async def test_cancel_queued_row_flips_and_emits(tmp_path):
    await executor._create_row(
        task_id="cq0000000001", hand="echo", prompt="never runs", source="api",
        model=None, session_id=None, parent_run_id=None, workspace=tmp_path, timeout_s=60,
    )
    async with _client() as client:
        resp = await client.post("/api/tasks/cq0000000001/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}

    task = await executor.get_task("cq0000000001")
    assert task.status == "cancelled"
    assert task.error == "cancelled while queued"
    events = [e for e in await bus.replay(0, types=["task.cancelled"]) if e.ref_id == "cq0000000001"]
    assert len(events) == 1  # the direct row flip emits like _finish does


async def test_cancel_wakes_a_submit_parked_on_the_hand_mutex(tmp_path):
    """A queued task whose submit is waiting on a busy mutex: cancel flips the
    row FIRST (terminal before any wake), then releases the parked task —
    it must not sit out the lock, and the row must never resurrect."""
    async with _busy("echo"):
        task_id = await executor.spawn(
            "echo", "parked behind the mutex", source="api",
            workspace=tmp_path, fallback=False,
        )
        atask = executor._running.get(task_id)
        assert atask is not None

        async with _client() as client:
            resp = await client.post(f"/api/tasks/{task_id}/cancel")
        assert resp.status_code == 200

        await asyncio.gather(atask, return_exceptions=True)  # woken, not lock-bound
        task = await executor.get_task(task_id)
        assert task.status == "cancelled"

    # lock released: nothing revives the row
    await asyncio.sleep(0.05)
    task = await executor.get_task(task_id)
    assert task.status == "cancelled"
    assert task_id not in executor._running


async def test_cancel_running_task_cancels_the_inflight_asyncio_task(tmp_path):
    get_registry().register(HangingHand())
    task_id = await executor.spawn(
        "hanging", "block forever", source="api", workspace=tmp_path, fallback=False,
    )
    await _wait_status(task_id, "running")

    async with _client() as client:
        resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}

    await _reap(task_id)
    task = await executor.get_task(task_id)
    assert task.status == "cancelled"
    assert task.error == "cancelled by operator"  # the executor's CancelledError path
    assert task_id not in executor._running


async def test_cancel_terminal_task_is_409_idempotent(tmp_path):
    task = await executor.submit("echo", "done already", source="api", workspace=tmp_path)
    assert task.status == "completed"
    async with _client() as client:
        resp = await client.post(f"/api/tasks/{task.id}/cancel")
        assert resp.status_code == 409
        assert "completed" in resp.json()["detail"]

        # cancelled tasks are terminal too: repeating a cancel stays 409
        await executor._create_row(
            task_id="ct0000000001", hand="echo", prompt="x", source="api",
            model=None, session_id=None, parent_run_id=None, workspace=tmp_path, timeout_s=60,
        )
        first = await client.post("/api/tasks/ct0000000001/cancel")
        assert first.status_code == 200
        second = await client.post("/api/tasks/ct0000000001/cancel")
        assert second.status_code == 409
        assert "cancelled" in second.json()["detail"]


async def test_cancel_unknown_task_is_404():
    async with _client() as client:
        resp = await client.post("/api/tasks/nope00000000/cancel")
    assert resp.status_code == 404


async def test_cancel_phantom_running_row_flips_directly(tmp_path):
    """A 'running' row with no live asyncio task (defensive: restart residue
    is normally swept at boot) must still be cancellable, not stuck."""
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, workspace_dir,"
        "                   timeout_s, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("phantom000001", "echo", "ghost", "running", "api", str(tmp_path), 60, bus.now_iso()),
    )
    async with _client() as client:
        resp = await client.post("/api/tasks/phantom000001/cancel")
    assert resp.status_code == 200
    task = await executor.get_task("phantom000001")
    assert task.status == "cancelled"
    assert task.error == "cancelled by operator"
