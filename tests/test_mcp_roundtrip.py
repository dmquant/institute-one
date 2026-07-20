"""MCP read surface round-trip (ROADMAP Phase 8 "MCP expansion").

test_mcp.py owns the three write tools' domain semantics; this file owns the
rest of the JSON-RPC surface:

- tools/list returns a complete schema for every tool (typed properties,
  required ⊆ properties, additionalProperties always False);
- the write surface is EXACTLY three tools (README promise) — any new tool
  must land in the read-tool smoke table below or the guard fails;
- every registered read tool answers one real empty-database call (lists come
  back empty, detail tools reject unknown ids as -32602 validation errors,
  never -32000 internals);
- Phase-8 expansion tools clamp their JSON text at 8KB.

Tools for parallel-partition domains (projects / research trees) register
defensively: absent module -> absent tool. The smoke table carries them so the
suite stays green on any checkout state of those partitions.
"""
from __future__ import annotations

import json
from typing import Any

from httpx import ASGITransport, AsyncClient

from app import db
from app import mcp as mcp_mod
from app.institute import sessions

# ---- the read-tool smoke table ------------------------------------------------
# tool name -> (arguments for one empty-db call, expectation)
# expectation:
#   "list"        result is a JSON array
#   "dict"        result is a JSON object
#   "validation"  the call must fail as -32602 with data.category == "validation"
#                 (detail tools probed with an unknown id)
# Every registered read tool MUST have a row here — the completeness guard
# turns "someone added a tool without a smoke entry" into a test failure.
SMOKE: dict[str, tuple[dict[str, Any], str]] = {
    # v0.1 read surface
    "institute_meta": ({}, "dict"),
    "analysts_list": ({}, "list"),
    "whiteboard_list_boards": ({"status": "active"}, "list"),
    "whiteboard_get_board": ({"board_id": "zz-missing"}, "validation"),
    "mailbox_list_threads": ({"status": "open"}, "list"),
    "mailbox_get_thread": ({"thread_id": "zz-missing"}, "validation"),
    "research_queue_list": ({"status": "pending"}, "list"),
    "research_log_recent": ({"limit": 5}, "list"),
    "workflows_list": ({}, "list"),
    "workflow_runs_recent": ({"limit": 5}, "list"),
    "archive_search": ({"query": "test"}, "dict"),
    "events_recent": ({"limit": 5}, "list"),
    # Phase 3 (C1)
    "fact_cards_list": ({"status": "pending"}, "list"),
    "fact_cards_get": ({"card_id": "zz-missing"}, "validation"),
    "claim_check": ({"text": "英伟达 2026 年数据中心收入将增长"}, "dict"),
    # Phase 8 expansion
    "sessions_list": ({"kind": "chat"}, "list"),
    "sessions_get": ({"session_id": "zz-missing"}, "validation"),
    "paper_positions_list": ({"status": "open"}, "list"),
    "paper_book_nav": ({"days": 30}, "list"),
    "chain_nodes_list": ({"q": "华为", "kind": "company"}, "list"),
    "chain_node_get": ({"node_id": "zz-missing"}, "validation"),
    "chain_graph": ({"center": "zz-missing"}, "validation"),
    "cron_health": ({}, "dict"),
    "operator_actions_list": ({"status": "open"}, "dict"),
    "forecasts_list": ({"status": "open"}, "list"),
    "forecasts_get": ({"forecast_id": "zz-missing"}, "validation"),
    "hand_weights_list": ({}, "list"),
    "hand_scorecard": ({}, "dict"),
    "maintenance_status": ({}, "dict"),
    # parallel partitions (registered only when the module exists on this checkout)
    "projects_list": ({"status": "active"}, "list"),
    "projects_get": ({"project_id": "zz-missing"}, "validation"),
    "research_trees_list": ({"limit": 5}, "list"),
}

# tools whose backing tables belong to a parallel partition's migration: a
# checkout where the module landed before its migration answers -32000
# "no such table" — tolerated as "partition mid-flight", not a failure.
PARALLEL_PARTITION_TOOLS = {"projects_list", "projects_get", "research_trees_list"}


async def _rpc(client: AsyncClient, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    r = await client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {},
    })
    assert r.status_code == 200
    payload = r.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == msg_id
    return payload


async def _call_tool_raw(client: AsyncClient, name: str, arguments: dict) -> dict:
    """tools/call round-trip; returns the whole JSON-RPC payload (result or error)."""
    return await _rpc(client, "tools/call", {"name": name, "arguments": arguments})


def _result_text(payload: dict) -> str:
    content = payload["result"]["content"]
    assert content[0]["type"] == "text"
    return content[0]["text"]


def _client() -> AsyncClient:
    from app.main import create_app

    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


# ---- protocol plumbing ---------------------------------------------------------

async def test_initialize_ping_and_health():
    async with _client() as client:
        init = await _rpc(client, "initialize")
        assert init["result"]["protocolVersion"] == mcp_mod.PROTOCOL_VERSION
        assert init["result"]["serverInfo"]["name"] == "institute-one"

        pong = await _rpc(client, "ping", msg_id=2)
        assert pong["result"] == {}

        r = await client.get("/api/mcp/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert set(body["tools"]) == set(mcp_mod._TOOLS)


async def test_tools_list_schemas_are_complete():
    async with _client() as client:
        payload = await _rpc(client, "tools/list")
    tools = payload["result"]["tools"]
    assert {t["name"] for t in tools} == set(mcp_mod._TOOLS)

    for t in tools:
        assert t["description"].strip(), f"{t['name']}: empty description"
        schema = t["inputSchema"]
        assert schema["type"] == "object", t["name"]
        assert schema["additionalProperties"] is False, t["name"]
        props = schema["properties"]
        assert isinstance(props, dict), t["name"]
        assert set(schema["required"]) <= set(props), f"{t['name']}: required not in properties"
        for key, spec in props.items():
            assert spec.get("type") in {"string", "integer", "number", "boolean", "array", "object"}, (
                f"{t['name']}.{key}: missing/unknown type"
            )

    # Phase-8 expansion tools additionally describe every property and carry
    # the 8KB output cap (empty-schema tools have nothing to describe).
    for name, tool in mcp_mod._TOOLS.items():
        if name in mcp_mod.WRITE_TOOLS or tool.get("output_cap") is None:
            continue
        assert tool["output_cap"] == mcp_mod._READ_OUTPUT_CAP, name
        for key, spec in tool["inputSchema"]["properties"].items():
            assert (spec.get("description") or "").strip() or "enum" in spec, (
                f"expansion tool {name}.{key}: property needs a description"
            )


# ---- the write-tool red line ----------------------------------------------------

async def test_write_surface_is_exactly_three_tools():
    """README: "Read tools plus exactly three writes". Guard both directions:
    the declared write set is exactly the three known names AND every other
    registered tool is accounted for in the read smoke table — a new mutating
    tool cannot slip in as an unlisted "read"."""
    assert mcp_mod.WRITE_TOOLS == {"research_queue_add", "topic_pool_add", "institute_ask"}
    assert len(mcp_mod.WRITE_TOOLS) == 3
    assert mcp_mod.WRITE_TOOLS <= set(mcp_mod._TOOLS), "write tools must all be registered"

    read_tools = set(mcp_mod._TOOLS) - mcp_mod.WRITE_TOOLS
    unaccounted = read_tools - set(SMOKE)
    assert not unaccounted, (
        f"tools registered without a smoke entry: {sorted(unaccounted)} — add them to "
        "SMOKE (read tool) or, deliberately and with review, to WRITE_TOOLS"
    )


# ---- every read tool answers on an empty database --------------------------------

async def test_every_read_tool_smokes_on_empty_db():
    import pytest

    skipped: list[str] = []
    async with _client() as client:
        for i, name in enumerate(sorted(set(mcp_mod._TOOLS) - mcp_mod.WRITE_TOOLS), start=1):
            args, expect = SMOKE[name]
            payload = await _call_tool_raw(client, name, args)

            if "error" in payload:
                err = payload["error"]
                if (
                    name in PARALLEL_PARTITION_TOOLS
                    and err["code"] == -32000
                    and "no such table" in err["message"]
                ):
                    skipped.append(name)  # partition mid-flight: module landed, migration not yet
                    continue
                assert expect == "validation", f"{name}: unexpected error {err}"
                assert err["code"] == -32602, f"{name}: {err}"
                assert err.get("data", {}).get("category") == "validation", f"{name}: {err}"
                continue

            assert expect != "validation", f"{name}: expected a validation rejection, got a result"
            body = json.loads(_result_text(payload))
            if expect == "list":
                assert isinstance(body, list), f"{name}: expected a list, got {type(body)}"
            else:
                assert isinstance(body, dict), f"{name}: expected an object, got {type(body)}"

    if skipped:
        pytest.skip(f"parallel-partition tools without their migration yet: {sorted(skipped)}")


async def test_empty_db_shapes_of_key_aggregates():
    async with _client() as client:
        meta = json.loads(_result_text(await _call_tool_raw(client, "institute_meta", {})))
        assert {"version", "work_date", "hands", "queue"} <= set(meta)

        cron = json.loads(_result_text(await _call_tool_raw(client, "cron_health", {})))
        assert cron["window_days"] == 30
        # S4-P0-03: empty cron_metrics still answers the full scheduler
        # registry (20 jobs), all with zeroed metric fields
        assert len(cron["jobs"]) == 20
        assert all(j["fires"] == 0 and j["last_fired_at"] is None for j in cron["jobs"].values())

        actions = json.loads(_result_text(await _call_tool_raw(client, "operator_actions_list", {})))
        assert actions == {"actions": [], "count": 0}

        scorecard = json.loads(_result_text(await _call_tool_raw(client, "hand_scorecard", {})))
        assert scorecard["counts"] == {"ok": 0, "stub": 0, "false_complete": 0}
        assert scorecard["entries"] == []

        maint = json.loads(_result_text(await _call_tool_raw(client, "maintenance_status", {})))
        assert maint["paused"] is False
        assert maint["drain_depth"] == 0

        bad = await _call_tool_raw(client, "hand_scorecard", {"date": "2026-13-99"})
        assert bad["error"]["code"] == -32602


async def test_sessions_roundtrip_and_8kb_output_cap():
    """Seeded read: sessions_list/get see a real session; a message pile bigger
    than 8KB comes back clamped with the explicit truncation marker."""
    session = await sessions.create_session("MCP 冒烟会话", kind="chat")
    for i in range(30):
        await sessions.add_message(session["id"], "assistant", f"第{i:02d}段 " + "内容" * 200)

    async with _client() as client:
        listed = json.loads(_result_text(await _call_tool_raw(client, "sessions_list", {})))
        assert [s["id"] for s in listed] == [session["id"]]

        payload = await _call_tool_raw(client, "sessions_get", {"session_id": session["id"]})
        text = _result_text(payload)
        assert len(text.encode("utf-8")) <= mcp_mod._READ_OUTPUT_CAP
        assert text.endswith("…[truncated at 8KB]")

        # a small result is untouched, valid JSON
        small = _result_text(await _call_tool_raw(client, "sessions_list", {"limit": 1}))
        assert json.loads(small)[0]["id"] == session["id"]


async def test_validation_of_unknown_arguments_and_tools():
    async with _client() as client:
        unknown = await _call_tool_raw(client, "no_such_tool", {})
        assert unknown["error"]["code"] == -32602

        extra = await _call_tool_raw(client, "sessions_list", {"bogus": 1})
        assert extra["error"]["code"] == -32602
        assert "unknown argument" in extra["error"]["message"]

        wrong_type = await _call_tool_raw(client, "research_log_recent", {"limit": "five"})
        assert wrong_type["error"]["code"] == -32602

        bad_enum = await _call_tool_raw(client, "whiteboard_list_boards", {"status": "bogus"})
        assert bad_enum["error"]["code"] == -32602


async def test_read_tools_write_nothing():
    """The read surface must leave zero rows behind in the mutable domain
    tables (events excluded by design: bus writes are the emitters' business —
    no read tool emits)."""
    watched = (
        "tasks", "research_queue", "topic_pool", "sessions", "messages",
        "whiteboard_boards", "mailbox_threads", "fact_cards", "forecasts",
        "paper_positions", "operator_actions", "chain_nodes", "events",
    )

    async def counts() -> dict[str, int]:
        return {
            t: (await db.query_one(f"SELECT COUNT(*) AS n FROM {t}"))["n"]  # noqa: S608 - fixed names
            for t in watched
        }

    before = await counts()
    async with _client() as client:
        for name in sorted(set(mcp_mod._TOOLS) - mcp_mod.WRITE_TOOLS):
            args, _expect = SMOKE[name]
            await _call_tool_raw(client, name, args)
    assert await counts() == before
