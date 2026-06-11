from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import bus

router = APIRouter(prefix="/api/events", tags=["events"])


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
        try:
            while True:
                try:
                    e = await asyncio.wait_for(anext(sub), timeout=25)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if e.id <= last:
                    continue
                if type_list and not any(e.type.startswith(t) for t in type_list):
                    continue
                last = e.id
                yield f"id: {e.id}\nevent: {e.type}\ndata: {json.dumps(e.to_dict(), ensure_ascii=False)}\n\n"
        finally:
            await sub.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
