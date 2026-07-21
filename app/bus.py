"""In-process event bus.

Every ``emit()`` appends to the ``events`` table (the durable cursor used by
``GET /api/events?since=``), fans out to live SSE subscribers, and invokes any
registered prefix handlers (the vault exporter hooks in via ``on()``).

Handlers must never break the emitter: exceptions are swallowed and logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from . import db

log = logging.getLogger("institute.bus")

Handler = Callable[["Event"], Awaitable[None]]

_subscribers: set[asyncio.Queue] = set()
_handlers: list[tuple[str, Handler]] = []


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Event:
    id: int
    type: str
    ref_kind: str = ""
    ref_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "ref_kind": self.ref_kind,
            "ref_id": self.ref_id,
            "payload": self.payload,
            "created_at": self.created_at,
        }


def on(type_prefix: str, handler: Handler) -> None:
    """Register an async handler for events whose type starts with ``type_prefix``."""
    _handlers.append((type_prefix, handler))


async def _publish(event: Event) -> None:
    """Fan out an event that is already durable.

    Keeping persistence separate from publication lets domain transactions
    insert an ``events`` row atomically with their own state and then wake
    live listeners after commit.  Durable cursor readers remain correct even
    if the process dies in the small commit-to-publish window.
    """
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # slow consumer: drop, the cursor endpoint recovers
            pass

    for prefix, handler in _handlers:
        if event.type.startswith(prefix):
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 - handlers must never break the emitter
                log.exception("event handler failed for %s", event.type)


async def emit(type: str, ref_kind: str = "", ref_id: str = "", payload: dict | None = None) -> Event:
    payload = payload or {}
    created = now_iso()
    event_id = await db.insert(
        "INSERT INTO events (type, ref_kind, ref_id, payload, created_at) VALUES (?,?,?,?,?)",
        (type, ref_kind, str(ref_id), json.dumps(payload, ensure_ascii=False), created),
    )
    event = Event(id=event_id, type=type, ref_kind=ref_kind, ref_id=str(ref_id), payload=payload, created_at=created)
    await _publish(event)
    return event


async def publish_durable(event_id: int) -> Event | None:
    """Publish one event row inserted by an enclosing domain transaction.

    This function never inserts a second row.  It is therefore safe for an
    atomic domain transition to persist the canonical event itself, commit,
    and call here for low-latency SSE/handler delivery.  If the call is lost
    to a crash, ``replay()`` still exposes the durable event exactly once.
    """
    row = await db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    event = Event(
        id=row["id"], type=row["type"], ref_kind=row["ref_kind"],
        ref_id=row["ref_id"], payload=payload, created_at=row["created_at"],
    )
    await _publish(event)
    return event


async def subscribe() -> AsyncIterator[Event]:
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    try:
        while True:
            yield await q.get()
    finally:
        _subscribers.discard(q)


async def replay(since: int, types: list[str] | None = None, limit: int = 200) -> list[Event]:
    sql = "SELECT * FROM events WHERE id > ?"
    params: list[Any] = [since]
    if types:
        sql += " AND (" + " OR ".join("type LIKE ?" for _ in types) + ")"
        params.extend(f"{t}%" for t in types)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    rows = await db.query(sql, params)
    return [
        Event(
            id=r["id"], type=r["type"], ref_kind=r["ref_kind"], ref_id=r["ref_id"],
            payload=json.loads(r["payload"] or "{}"), created_at=r["created_at"],
        )
        for r in rows
    ]
