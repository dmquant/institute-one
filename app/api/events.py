from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import bus

router = APIRouter(prefix="/api/events", tags=["events"])

_HEARTBEAT_SECONDS = 25.0


@router.get("")
async def list_events(since: int = 0, types: str | None = None, limit: int = 200):
    type_list = [t.strip() for t in types.split(",")] if types else None
    events = await bus.replay(since, type_list, limit)
    return [e.to_dict() for e in events]


@router.get("/stream")
async def stream(since: int = 0, types: str | None = None):
    """SSE: replays from cursor, then live. Heartbeats every 25s."""
    type_list = [t.strip() for t in types.split(",")] if types else None

    async def gen():
        last = since
        for e in await bus.replay(last, type_list, 500):
            last = e.id
            yield f"id: {e.id}\nevent: {e.type}\ndata: {json.dumps(e.to_dict(), ensure_ascii=False)}\n\n"
        sub = bus.subscribe()
        next_event: asyncio.Task[bus.Event] | None = None
        try:
            while True:
                if next_event is None:
                    next_event = asyncio.create_task(anext(sub))
                done, _ = await asyncio.wait(
                    {next_event}, timeout=_HEARTBEAT_SECONDS,
                )
                if not done:
                    yield ": heartbeat\n\n"
                    continue
                try:
                    e = next_event.result()
                except StopAsyncIteration:
                    return
                next_event = None
                if e.id <= last:
                    continue
                if type_list and not any(e.type.startswith(t) for t in type_list):
                    continue
                last = e.id
                yield f"id: {e.id}\nevent: {e.type}\ndata: {json.dumps(e.to_dict(), ensure_ascii=False)}\n\n"
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_event
            await sub.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
