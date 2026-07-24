from __future__ import annotations

import asyncio
import json

import pytest

from app import bus, db
from app.api import events as events_api


@pytest.mark.asyncio
async def test_sse_heartbeat_keeps_live_subscription(monkeypatch):
    queue: asyncio.Queue[bus.Event] = asyncio.Queue()
    subscription_closed = asyncio.Event()

    async def fake_replay(*_args, **_kwargs):
        return []

    async def fake_subscribe():
        try:
            while True:
                yield await queue.get()
        finally:
            subscription_closed.set()

    monkeypatch.setattr(events_api, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(bus, "replay", fake_replay)
    monkeypatch.setattr(bus, "subscribe", fake_subscribe)

    response = await events_api.stream()
    body = response.body_iterator
    assert await asyncio.wait_for(anext(body), timeout=0.2) == ": heartbeat\n\n"

    event = bus.Event(id=1, type="task.completed", ref_kind="task", ref_id="1")
    await queue.put(event)
    chunk = await asyncio.wait_for(anext(body), timeout=0.2)
    assert "id: 1\n" in chunk
    assert "event: task.completed\n" in chunk

    await body.aclose()
    assert subscription_closed.is_set()


@pytest.mark.asyncio
async def test_publish_durable_fans_out_without_inserting_duplicate():
    """A domain transaction may own the events INSERT and publish it later."""
    seen: list[int] = []

    async def handler(event: bus.Event) -> None:
        seen.append(event.id)

    before = list(bus._handlers)
    try:
        bus.on("mailbox.reply", handler)
        event_id = await db.insert(
            "INSERT INTO events (type, ref_kind, ref_id, payload, created_at) "
            "VALUES ('mailbox.reply','thread','t-1',?,?)",
            (json.dumps({"dispatch_id": 7}), bus.now_iso()),
        )
        event = await bus.publish_durable(event_id)
        assert event is not None and event.payload == {"dispatch_id": 7}
        assert seen == [event_id]
        assert (await db.query_one("SELECT COUNT(*) AS n FROM events"))["n"] == 1
    finally:
        bus._handlers[:] = before
