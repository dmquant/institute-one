"""Ad-hoc ask memory injection (PATCH-NOTES-B3 §2e, ROADMAP Phase 2 leftover).

The three operator-facing ask surfaces — POST /api/ask, session chat turns,
MCP institute_ask — now pass ``memory_block=await memory.memory_block(id)``
into ``build_analyst_prompt``, the same pattern as the four workflow
prompt-assembly points. Both directions are pinned: an analyst WITH memory
gets the standing-memory block between persona and task; an analyst WITHOUT
memory produces the pre-injection prompt (empty block is a no-op).

``/api/ask/stream`` stays a deliberate line-for-line mirror of /api/ask
WITHOUT memory (its ``_prepare`` was never extracted into a shared helper —
ask_stream.py is outside this card's partition), so no stream assertions
here; the shared-helper extraction remains proposed in PATCH-NOTES-B8.md.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db

MEM_TEXT = "核心立场：维持贵州茅台看多（首次形成 2026-07-01）；在跟踪：批价周报。"
MEM_HEAD = "## 常备记忆（第 1 版 · 2026-07-18）"


async def _seed_memory(analyst_id: str) -> None:
    await db.execute(
        "INSERT INTO analyst_memory (id, analyst_id, version, work_date, compact_md, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"am-{analyst_id}-1", analyst_id, 1, "2026-07-18", MEM_TEXT, bus.now_iso()),
    )


def _app(*router_modules: str) -> FastAPI:
    import importlib

    app = FastAPI()
    for mod in router_modules:
        app.include_router(importlib.import_module(f"app.api.{mod}").router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---- POST /api/ask -----------------------------------------------------------

async def test_ask_injects_memory_for_analyst_with_memory():
    await _seed_memory("equity-analyst")
    async with _client(_app("tasks")) as client:
        resp = await client.post("/api/ask", json={"prompt": "复盘一下", "analyst_id": "equity-analyst"})
    assert resp.status_code == 200
    task = resp.json()
    assert task["status"] == "completed"
    assert MEM_HEAD in task["prompt"]
    assert MEM_TEXT in task["prompt"]
    # memory slots between persona and task (sandwich order)
    assert task["prompt"].index("权益分析师") < task["prompt"].index(MEM_HEAD) < task["prompt"].index("## 任务")
    # the echo hand saw it too (output mirrors the prompt head)
    assert MEM_TEXT in task["output"]


async def test_ask_without_memory_keeps_prompt_clean():
    async with _client(_app("tasks")) as client:
        resp = await client.post("/api/ask", json={"prompt": "复盘一下", "analyst_id": "macro-analyst"})
    task = resp.json()
    assert task["status"] == "completed"
    assert "常备记忆" not in task["prompt"]  # empty block is a strict no-op
    assert "宏观分析师" in task["prompt"] and "## 任务\n复盘一下" in task["prompt"]


async def test_ask_without_analyst_never_wraps():
    await _seed_memory("equity-analyst")  # present but must not be consulted
    async with _client(_app("tasks")) as client:
        resp = await client.post("/api/ask", json={"prompt": "bare prompt"})
    task = resp.json()
    assert task["prompt"] == "bare prompt"


# ---- session chat turns --------------------------------------------------------

async def test_session_chat_injects_memory():
    await _seed_memory("equity-analyst")
    async with _client(_app("sessions")) as client:
        created = (await client.post(
            "/api/sessions", json={"title": "memory chat", "analyst_id": "equity-analyst"},
        )).json()
        reply = (await client.post(
            f"/api/sessions/{created['id']}/messages", json={"content": "谈谈茅台"},
        )).json()
    prompt = reply["task"]["prompt"]
    assert MEM_HEAD in prompt and MEM_TEXT in prompt
    assert prompt.index("权益分析师") < prompt.index(MEM_HEAD) < prompt.index("## 任务")


async def test_session_chat_without_memory_keeps_prompt_clean():
    async with _client(_app("sessions")) as client:
        created = (await client.post(
            "/api/sessions", json={"title": "clean chat", "analyst_id": "macro-analyst"},
        )).json()
        reply = (await client.post(
            f"/api/sessions/{created['id']}/messages", json={"content": "谈谈利率"},
        )).json()
    assert "常备记忆" not in reply["task"]["prompt"]


# ---- MCP institute_ask ---------------------------------------------------------

async def _mcp_ask(client: AsyncClient, arguments: dict) -> dict:
    r = await client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "institute_ask", "arguments": arguments},
    })
    assert r.status_code == 200
    payload = r.json()
    assert "error" not in payload
    return json.loads(payload["result"]["content"][0]["text"])


async def test_mcp_institute_ask_injects_memory():
    from app.main import create_app

    await _seed_memory("equity-analyst")
    async with _client(create_app()) as client:
        res = await _mcp_ask(client, {"prompt": "复盘一下", "analyst_id": "equity-analyst"})
    assert res["status"] == "completed"
    assert MEM_HEAD in res["output"] and MEM_TEXT in res["output"]
    # the persisted tasks row carries the full injected prompt (audit spine)
    row = await db.query_one("SELECT prompt FROM tasks WHERE id = ?", (res["task_id"],))
    assert MEM_TEXT in row["prompt"]


async def test_mcp_institute_ask_without_memory_keeps_prompt_clean():
    from app.main import create_app

    async with _client(create_app()) as client:
        res = await _mcp_ask(client, {"prompt": "复盘一下", "analyst_id": "macro-analyst"})
    assert res["status"] == "completed"
    assert "常备记忆" not in res["output"]
