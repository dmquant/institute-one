"""Thesis registry — lanes and theses as one tree (cards M1-001/M1-002).

A lane is a top-level grouping node (``kind='lane'``); theses hang under a
lane via ``parent_id``. Rows are truth. Every content change (title, view,
direction, status) appends to ``thesis_versions``, so the registry preserves
how a view evolved. market-thesis-data import provenance lives in the
``market_thesis_import_*`` tables (see market_thesis_import.py).
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .. import bus, db

KINDS = {"lane", "thesis"}
DIRECTIONS = {"long", "short", "neutral", "conflicting"}
STATUSES = {"candidate", "active", "paused", "retired", "invalidated"}
CREATE_STATUSES = {"candidate", "active"}

# fields whose change produces a new thesis_versions row
_VERSIONED_FIELDS = ("title", "view", "direction", "status")
_UPDATABLE_FIELDS = {"title", "view", "direction", "status", "parent_id", "tags", "meta"}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]$")


class ThesisError(ValueError):
    """Validation failure (the API maps this to 400)."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _loads(text: str | None, fallback: Any) -> Any:
    try:
        val = json.loads(text) if text else fallback
    except ValueError:
        return fallback
    return val if isinstance(val, type(fallback)) else fallback


def _out(row: dict[str, Any]) -> dict[str, Any]:
    thesis = dict(row)
    thesis["tags"] = _loads(thesis.pop("tags_json", None), [])
    thesis["meta"] = _loads(thesis.pop("meta_json", None), {})
    return thesis


def _validate_enum(value: Any, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise ThesisError(f"unknown {label} {value!r}; allowed: {', '.join(sorted(allowed))}")


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise ThesisError("slug must be a 2-80 char lowercase slug (a-z, 0-9, -)")
    return slug


def _validate_tags(val: Any) -> list[str]:
    if val is None:
        return []
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ThesisError("tags must be a list of strings")
    return val


def _validate_meta(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ThesisError("meta must be an object")
    return val


async def _append_version(
    conn, thesis_id: str, version: int, row: dict[str, Any], author: str, now: str
) -> None:
    await conn.execute(
        "INSERT INTO thesis_versions (id, thesis_id, version, title, view, direction, status, author, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (_new_id(), thesis_id, version, row["title"], row["view"], row["direction"], row["status"], author, now),
    )


# ---- CRUD --------------------------------------------------------------------

async def create_thesis(data: dict[str, Any], *, author: str = "operator") -> dict[str, Any]:
    """Create a lane or thesis. Writes version 1 in the same transaction."""
    title = str(data.get("title") or "").strip()
    if not title:
        raise ThesisError("a thesis needs a title")
    kind = data.get("kind", "thesis")
    _validate_enum(kind, KINDS, "kind")
    direction = data.get("direction", "neutral")
    _validate_enum(direction, DIRECTIONS, "direction")
    status = data.get("status", "candidate")
    _validate_enum(status, CREATE_STATUSES, "creation status")
    tags = _validate_tags(data.get("tags"))
    meta = _validate_meta(data.get("meta"))
    view = str(data.get("view") or "")
    source = data.get("source", "manual")

    thesis_id = _new_id()
    slug = str(data.get("slug") or "").strip() or thesis_id
    _validate_slug(slug)
    parent_id = data.get("parent_id") or None
    if parent_id is not None and kind == "lane":
        raise ThesisError("a lane cannot have a parent")

    now = bus.now_iso()
    # uniqueness/parent checks live INSIDE the transaction: the write lock
    # serializes writers, so two concurrent creates cannot both pass the check
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT id FROM theses WHERE slug = ?", (slug,))
        if await cur.fetchone():
            await cur.close()
            raise ThesisError(f"slug '{slug}' already exists")
        await cur.close()
        if parent_id is not None:
            cur = await conn.execute("SELECT id FROM theses WHERE id = ?", (parent_id,))
            parent = await cur.fetchone()
            await cur.close()
            if parent is None:
                raise ThesisError(f"unknown parent thesis {parent_id!r}")
        await conn.execute(
            "INSERT INTO theses (id, slug, parent_id, kind, title, view, direction, status, tags_json, "
            "meta_json, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (thesis_id, slug, parent_id, kind, title, view, direction, status,
             json.dumps(tags, ensure_ascii=False), json.dumps(meta, ensure_ascii=False), source, now, now),
        )
        await _append_version(
            conn, thesis_id, 1,
            {"title": title, "view": view, "direction": direction, "status": status}, author, now,
        )
    await bus.emit("thesis.created", "thesis", thesis_id, {"slug": slug, "kind": kind, "status": status})
    return await get_thesis(thesis_id)  # type: ignore[return-value]


async def get_thesis(thesis_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM theses WHERE id = ? OR slug = ?", (thesis_id, thesis_id))
    if row is None:
        return None
    thesis = _out(row)
    thesis["versions"] = await db.query(
        "SELECT * FROM thesis_versions WHERE thesis_id = ? ORDER BY version", (thesis["id"],)
    )
    thesis["securities"] = await db.query(
        "SELECT e.*, s.ticker, s.market, s.instrument_type, s.name AS security_name "
        "FROM thesis_security_edges e JOIN securities s ON s.id = e.security_id "
        "WHERE e.thesis_id = ? ORDER BY e.role, s.id",
        (thesis["id"],),
    )
    return thesis


async def list_theses(
    status: str | None = None, kind: str | None = None, parent_id: str | None = None
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if parent_id:
        clauses.append("parent_id = ?")
        params.append(parent_id)
    sql = "SELECT * FROM theses"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at, id"
    return [_out(r) for r in await db.query(sql, params)]


async def tree() -> list[dict[str, Any]]:
    """Lanes (and orphan top-level theses) with their children nested one level."""
    rows = [_out(r) for r in await db.query("SELECT * FROM theses ORDER BY created_at, id")]
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)

    def _attach(node: dict[str, Any]) -> dict[str, Any]:
        node["children"] = [_attach(c) for c in by_parent.get(node["id"], [])]
        return node

    return [_attach(r) for r in by_parent.get(None, [])]


async def update_thesis(
    thesis_id: str, fields: dict[str, Any], *, author: str = "operator"
) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM theses WHERE id = ? OR slug = ?", (thesis_id, thesis_id))
    if row is None:
        return None
    thesis_id = row["id"]
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ThesisError(f"unknown thesis fields: {', '.join(sorted(unknown))}")
    if "direction" in fields:
        _validate_enum(fields["direction"], DIRECTIONS, "direction")
    if "status" in fields:
        _validate_enum(fields["status"], STATUSES, "status")
    if "title" in fields and (not isinstance(fields["title"], str) or not fields["title"].strip()):
        raise ThesisError("title must be a non-empty string")
    if "view" in fields and not isinstance(fields["view"], str):
        raise ThesisError("view must be a string")
    if "tags" in fields:
        fields["tags"] = _validate_tags(fields["tags"])
    if "meta" in fields:
        fields["meta"] = _validate_meta(fields["meta"])
    if "parent_id" in fields and fields["parent_id"] is not None:
        if fields["parent_id"] == thesis_id:
            raise ThesisError("a thesis cannot be its own parent")
        if row["kind"] == "lane":
            raise ThesisError("a lane cannot have a parent")
    if not fields:
        return await get_thesis(thesis_id)

    sets, params = [], []
    for key, val in fields.items():
        if key == "tags":
            sets.append("tags_json = ?")
            params.append(json.dumps(val, ensure_ascii=False))
        elif key == "meta":
            sets.append("meta_json = ?")
            params.append(json.dumps(val, ensure_ascii=False))
        else:
            sets.append(f"{key} = ?")
            params.append(str(val).strip() if key == "title" else val)

    now = bus.now_iso()
    async with db.transaction() as conn:
        # re-read under the write lock: a concurrent update may have landed
        # since the pre-check read, and the version row must merge from the
        # CURRENT row or the history would contradict the table
        cur = await conn.execute("SELECT * FROM theses WHERE id = ?", (thesis_id,))
        fresh = await cur.fetchone()
        await cur.close()
        if fresh is None:
            return None
        if fields.get("parent_id") is not None:
            # parent existence + ancestor-cycle walk on the tree as it exists
            # under the write lock — a pre-transaction walk could race a
            # concurrent reparent and let two updates form a cycle together
            async def _parent_of(node_id: str) -> tuple[bool, str | None]:
                c = await conn.execute("SELECT parent_id FROM theses WHERE id = ?", (node_id,))
                r = await c.fetchone()
                await c.close()
                return (r is not None), (r["parent_id"] if r else None)

            exists, _ = await _parent_of(fields["parent_id"])
            if not exists:
                raise ThesisError(f"unknown parent thesis {fields['parent_id']!r}")
            seen = {thesis_id}
            cursor_id: str | None = fields["parent_id"]
            while cursor_id is not None:
                if cursor_id in seen:
                    raise ThesisError("parent_id would create a cycle")
                seen.add(cursor_id)
                _, cursor_id = await _parent_of(cursor_id)
        await conn.execute(
            f"UPDATE theses SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
            (*params, now, thesis_id),
        )
        versioned_changed = any(
            key in fields and fields[key] != fresh[key] for key in _VERSIONED_FIELDS
        )
        if versioned_changed:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM thesis_versions WHERE thesis_id = ?",
                (thesis_id,),
            )
            version = (await cur.fetchone())["v"] + 1
            await cur.close()
            merged = {k: fields.get(k, fresh[k]) for k in _VERSIONED_FIELDS}
            merged["title"] = str(merged["title"]).strip()
            await _append_version(conn, thesis_id, version, merged, author, now)
    await bus.emit("thesis.updated", "thesis", thesis_id, {"fields": sorted(fields)})
    return await get_thesis(thesis_id)
