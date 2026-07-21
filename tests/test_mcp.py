"""MCP endpoint round-trips: write tools must go through the domain functions.

research_queue_add honours research.enqueue()'s cooldown gate (structured
refusal, not a JSON-RPC error); topic_pool_add defers entirely to
whiteboard.add_topic() — same content hash, and "did this call insert" comes
from the domain INSERT OR IGNORE result, never from an MCP-side pre-check
(REVIEW-A2 M1/M2; PATCH-NOTES-A2.md landed the ``inserted`` key, so the
tests assert it unconditionally).
"""
from __future__ import annotations

import asyncio
import json

from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import projects, whiteboard


async def _call_tool(client: AsyncClient, name: str, arguments: dict, msg_id: int = 1) -> dict:
    """tools/call round-trip; asserts the JSON-RPC 2.0 result envelope."""
    r = await client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    payload = r.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == msg_id
    assert "error" not in payload
    content = payload["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


async def _added_events() -> list[str]:
    events = await bus.replay(0, types=["topic_pool.added"])
    return [e.payload["topic"] for e in events]


# ---- research_queue_add ------------------------------------------------------


async def test_research_queue_add_dedups_pending_topic():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await _call_tool(client, "research_queue_add", {"topic": "NVDA"})
        assert first["duplicate"] is False
        assert first["status"] == "pending"

        second = await _call_tool(client, "research_queue_add", {"topic": "NVDA"}, msg_id=2)
        assert second["duplicate"] is True
        assert second["id"] == first["id"]

    rows = await db.query("SELECT * FROM research_queue WHERE topic = ?", ("NVDA",))
    assert len(rows) == 1


async def test_research_queue_add_refused_by_cooldown():
    from app.main import create_app

    # the topic completed a research run just now -> inside the 30-day cooldown
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at) VALUES (?,?,?,?)",
        ("AAPL", "run0", "done recently", bus.now_iso()),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _call_tool(client, "research_queue_add", {"topic": "AAPL"})
        assert res["queued"] is False
        assert res["refused"] == "cooldown"
        assert res["last_completed_at"]

        rows = await db.query("SELECT * FROM research_queue WHERE topic = ?", ("AAPL",))
        assert rows == []

        # priority > 0 overrides the cooldown (domain semantics pass through MCP)
        forced = await _call_tool(client, "research_queue_add", {"topic": "AAPL", "priority": 1}, msg_id=2)
        assert forced["duplicate"] is False
        assert forced["status"] == "pending"


async def test_research_queue_add_accepts_project_id_without_new_write_tool():
    from app import mcp as mcp_mod
    from app.main import create_app

    tool = mcp_mod._TOOLS["research_queue_add"]
    assert tool["inputSchema"]["properties"]["project_id"]["type"] == "string"
    assert "project_id" not in tool["inputSchema"]["required"]
    assert mcp_mod.WRITE_TOOLS == {"research_queue_add", "topic_pool_add", "institute_ask"}

    project = await projects.create("MCP 项目")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await _call_tool(
            client,
            "research_queue_add",
            {"topic": "MCP 项目研究", "project_id": project["id"]},
        )

    assert result["project_id"] == project["id"]
    row = await db.query_one("SELECT project_id FROM research_queue WHERE id = ?", (result["id"],))
    assert row["project_id"] == project["id"]
    linked = await projects.get(project["id"])
    assert [item["ref_id"] for item in linked["links"]["research"]] == [result["id"]]


async def test_research_queue_add_maps_archived_project_to_validation_error():
    from app.main import create_app

    project = await projects.create("已归档 MCP 项目")
    await projects.archive(project["id"])
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "research_queue_add",
                "arguments": {"topic": "不应入队", "project_id": project["id"]},
            },
        })

    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["category"] == "validation"
    assert "archived" in error["message"]
    assert await db.query("SELECT * FROM research_queue WHERE topic = ?", ("不应入队",)) == []


# ---- topic_pool_add ----------------------------------------------------------


async def test_topic_pool_add_exact_duplicate_across_sources():
    from app.main import create_app

    seeded = await whiteboard.add_topic("光模块景气度", question="2026 需求可持续吗", source="research")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _call_tool(client, "topic_pool_add",
                               {"topic": "光模块景气度", "question": "2026 需求可持续吗"})
    assert res["added"] is False
    assert res["duplicate"] is True
    assert res["id"] == seeded["id"]

    rows = await db.query("SELECT * FROM topic_pool WHERE topic = ?", ("光模块景气度",))
    assert len(rows) == 1
    assert await _added_events() == []  # duplicates never emit


async def test_topic_pool_add_hash_alias_not_reported_as_new():
    """REVIEW-A2 M1: the domain hash concatenates without a separator, so
    ("机器人产业链", "") and ("机器人", "产业链") collide. MCP must not present the
    pre-existing row as a fresh insert, and must not emit a phantom event."""
    from app.main import create_app

    seeded = await whiteboard.add_topic("机器人产业链", question="", source="research")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _call_tool(client, "topic_pool_add", {"topic": "机器人", "question": "产业链"})
    assert res["added"] is False
    assert res["duplicate"] is True
    assert res["id"] == seeded["id"]

    rows = await db.query("SELECT topic, question FROM topic_pool")
    assert rows == [{"topic": "机器人产业链", "question": ""}]  # the alias inserted nothing
    assert await _added_events() == []


async def test_topic_pool_add_lands_on_domain_hash():
    """MCP first, domain second: both resolve to the same row (same content hash)."""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _call_tool(client, "topic_pool_add", {"topic": "低空经济", "question": "商业化节奏"})
    assert res["id"] is not None

    again = await whiteboard.add_topic("低空经济", question="商业化节奏", source="research")
    assert again["id"] == res["id"]
    rows = await db.query("SELECT * FROM topic_pool WHERE topic = ?", ("低空经济",))
    assert len(rows) == 1


async def test_topic_pool_add_reports_genuine_insert():
    from app.main import create_app

    # PATCH-NOTES-A2 contract: 'added' comes from the domain INSERT result
    probe = await whiteboard.add_topic("__a2-probe__", question="", source="test")
    assert "inserted" in probe

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _call_tool(client, "topic_pool_add", {"topic": "储能出海", "question": ""})
        assert res["added"] is True
        assert res["duplicate"] is False

        rerun = await _call_tool(client, "topic_pool_add", {"topic": "储能出海", "question": ""}, msg_id=2)
        assert rerun["added"] is False
        assert rerun["duplicate"] is True
        assert rerun["id"] == res["id"]

    assert await _added_events() == ["储能出海"]  # exactly one event, from the real insert


async def test_topic_pool_add_concurrent_calls_single_added():
    """REVIEW-A2 M2: two concurrent identical calls -> one row, one added=true,
    one topic_pool.added event."""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1, r2 = await asyncio.gather(
            _call_tool(client, "topic_pool_add", {"topic": "并发同参", "question": "q"}, msg_id=1),
            _call_tool(client, "topic_pool_add", {"topic": "并发同参", "question": "q"}, msg_id=2),
        )

    assert sorted([r1["added"], r2["added"]]) == [False, True]
    rows = await db.query("SELECT * FROM topic_pool WHERE topic = ?", ("并发同参",))
    assert len(rows) == 1
    assert await _added_events() == ["并发同参"]
