"""Operator favorites over heterogeneous institute artifacts."""
from __future__ import annotations

from typing import Any

from .. import bus, db


REF_KINDS = (
    "research",
    "whiteboard",
    "daily",
    "briefing",
    "thesis",
    "forecast",
    "chain_entity",
    "research_tree",
)
MAX_REF_ID_LEN = 500
MAX_NOTE_LEN = 2000


_SELECT = """
SELECT
  f.id,
  f.ref_kind,
  f.ref_id,
  f.note,
  f.created_at,
  COALESCE(
    CASE f.ref_kind
      WHEN 'research' THEN rq.topic
      WHEN 'whiteboard' THEN wb.topic
      WHEN 'daily' THEN
        CASE WHEN wr.id IS NOT NULL THEN '每日日报 · ' || substr(wr.started_at, 1, 10) END
      WHEN 'briefing' THEN
        CASE WHEN wr.id IS NOT NULL THEN '晨会简报 · ' || substr(wr.started_at, 1, 10) END
      WHEN 'thesis' THEN th.name_zh
      WHEN 'forecast' THEN fc.claim
      WHEN 'chain_entity' THEN cn.name
      WHEN 'research_tree' THEN rt.root_topic
    END,
    f.ref_id
  ) AS title,
  CASE f.ref_kind
    WHEN 'research' THEN rq.status
    WHEN 'whiteboard' THEN wb.status
    WHEN 'daily' THEN wr.status
    WHEN 'briefing' THEN wr.status
    WHEN 'thesis' THEN th.status
    WHEN 'forecast' THEN fc.status
    WHEN 'research_tree' THEN rt.status
  END AS status
FROM favorites f
LEFT JOIN research_queue rq
  ON f.ref_kind = 'research' AND rq.id = f.ref_id
LEFT JOIN whiteboard_boards wb
  ON f.ref_kind = 'whiteboard' AND wb.id = f.ref_id
LEFT JOIN workflow_runs wr
  ON f.ref_kind IN ('daily', 'briefing')
 AND wr.id = f.ref_id
 AND wr.workflow_id = f.ref_kind
LEFT JOIN theses th
  ON f.ref_kind = 'thesis' AND th.id = f.ref_id
LEFT JOIN forecasts fc
  ON f.ref_kind = 'forecast' AND fc.id = f.ref_id
LEFT JOIN chain_nodes cn
  ON f.ref_kind = 'chain_entity' AND cn.id = f.ref_id
LEFT JOIN research_trees rt
  ON f.ref_kind = 'research_tree' AND rt.id = f.ref_id
"""


def _kind(ref_kind: str) -> str:
    kind = (ref_kind or "").strip()
    if kind not in REF_KINDS:
        raise ValueError(f"unknown ref_kind {kind!r} (one of {', '.join(REF_KINDS)})")
    return kind


def _ref(ref_kind: str, ref_id: str) -> tuple[str, str]:
    kind = _kind(ref_kind)
    item_id = (ref_id or "").strip()
    if not item_id:
        raise ValueError("ref_id must not be empty")
    if len(item_id) > MAX_REF_ID_LEN:
        raise ValueError(f"ref_id exceeds {MAX_REF_ID_LEN} chars")
    return kind, item_id


async def _get(ref_kind: str, ref_id: str) -> dict[str, Any] | None:
    return await db.query_one(
        _SELECT + " WHERE f.ref_kind = ? AND f.ref_id = ?",
        (ref_kind, ref_id),
    )


async def add(ref_kind: str, ref_id: str, note: str = "") -> dict[str, Any]:
    """Create or refresh one favorite; the unique pair makes retries idempotent."""
    kind, item_id = _ref(ref_kind, ref_id)
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise ValueError(f"note exceeds {MAX_NOTE_LEN} chars")
    await db.execute(
        "INSERT INTO favorites (ref_kind, ref_id, note, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(ref_kind, ref_id) DO UPDATE SET note=excluded.note",
        (kind, item_id, note, bus.now_iso()),
    )
    row = await _get(kind, item_id)
    if row is None:  # the upsert above guarantees the row
        raise RuntimeError("favorite disappeared after upsert")
    return row


async def remove(ref_kind: str, ref_id: str) -> bool:
    """Remove a favorite; repeating the request is a harmless no-op."""
    kind, item_id = _ref(ref_kind, ref_id)
    return bool(
        await db.execute(
            "DELETE FROM favorites WHERE ref_kind = ? AND ref_id = ?",
            (kind, item_id),
        )
    )


async def list_favorites(kind: str | None = None) -> list[dict[str, Any]]:
    params: tuple[str, ...] = ()
    where = ""
    if kind is not None:
        normalized = _kind(kind)
        where = " WHERE f.ref_kind = ?"
        params = (normalized,)
    return await db.query(
        _SELECT + where + " ORDER BY f.created_at DESC, f.id DESC",
        params,
    )
