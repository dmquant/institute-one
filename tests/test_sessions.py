"""sessions.list_messages: the read is bounded (default: newest 500) while the
response shape stays a plain ascending list — the SPA Sessions page reads the
array as a message count, the MCP sessions_get tool clamps content in place,
and the API route returns it verbatim."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.institute import sessions


async def test_list_messages_default_returns_all_in_ascending_order():
    session = await sessions.create_session("默认全量", kind="chat")
    for i in range(5):
        await sessions.add_message(session["id"], "user", f"m{i}")

    rows = await sessions.list_messages(session["id"])
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(5)]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


async def test_list_messages_limit_keeps_the_newest_ascending():
    session = await sessions.create_session("倒序取新", kind="chat")
    for i in range(5):
        await sessions.add_message(session["id"], "user", f"m{i}")

    rows = await sessions.list_messages(session["id"], limit=3)
    assert [r["content"] for r in rows] == ["m2", "m3", "m4"]  # newest 3, oldest-first
    assert await sessions.list_messages(session["id"], limit=0) == []


async def test_messages_api_returns_ascending_array():
    """GET /api/sessions/{id}/messages keeps the pre-limit shape: a bare array
    ordered by id ASC, so consumers need no pagination contract."""
    from app.main import create_app

    session = await sessions.create_session("api 形状", kind="chat")
    for i in range(3):
        await sessions.add_message(session["id"], "user", f"m{i}")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(f"/api/sessions/{session['id']}/messages")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert [m["content"] for m in body] == ["m0", "m1", "m2"]
    assert [m["id"] for m in body] == sorted(m["id"] for m in body)
