from __future__ import annotations

import asyncio

import pytest

from app import bus
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
