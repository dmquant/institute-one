"""MCP endpoint round-trips: write tools must go through the domain functions.

research_queue_add honours research.enqueue()'s cooldown gate (structured
refusal, not a JSON-RPC error); topic_pool_add defers entirely to
whiteboard.add_topic() — same content hash, and "did this call insert" comes
from the domain INSERT OR IGNORE result, never from an MCP-side pre-check
(REVIEW-A2 M1/M2). Two tests skip until PATCH-NOTES-A2.md lands the
``inserted`` key in add_topic().
"""
from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import whiteboard


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


async def _domain_reports_inserted() -> bool:
    """Capability probe: PATCH-NOTES-A2.md adds an 'inserted' key to add_topic()."""
    probe = await whiteboard.add_topic("__a2-probe__", question="", source="test")
    return "inserted" in probe


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

    if not await _domain_reports_inserted():
        pytest.skip("whiteboard.add_topic() lacks 'inserted' — apply PATCH-NOTES-A2.md")

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

    if not await _domain_reports_inserted():
        pytest.skip("whiteboard.add_topic() lacks 'inserted' — apply PATCH-NOTES-A2.md")

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
