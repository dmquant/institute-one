"""Thesis registry — the primary research object (card M1-002).

Lanes and theses share one table (``theses``; lanes are rows with
kind='lane', a thesis points at its lane — or parent thesis — via
parent_id). Projection fields on the row are the living view; every content
revision appends a ``thesis_versions`` row (version increments per thesis,
supersedes_id -> the row it replaces) so history is never lost. Lifecycle
moves are conditional-claim transitions (``UPDATE … WHERE status = <the
status we validated against>``). User-visible changes emit namespaced
``thesis.*`` bus events. Contract: design/local-thesis-alpha/
02-thesis-stock-model.md + migrations/0003_theses.sql.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .. import bus, db

STATUSES = ("candidate", "active", "watch", "dormant", "retired")
# new theses enter the lifecycle at the top; watch/dormant/retired are reached
# via set_status transitions (02-thesis-stock-model.md lifecycle table)
CREATE_STATUSES = {"candidate", "active"}
KINDS = {"lane", "thesis"}
VIEWS = {"bullish", "bearish", "neutral", "avoid", "conflicting", "unknown"}
CONFIDENCES = {"low", "medium", "high"}

# columns on `theses` that PATCH may rewrite without touching version history
_PROJECTION_FIELDS = {
    "name_zh", "name_en", "slug", "parent_id", "scope", "exclusions",
    "owner_analyst", "priority", "conviction_score", "alpha_prior_score",
    "first_seen", "last_seen", "source", "source_href", "source_network_href",
    "metadata",
}
_NUMBER_FIELDS = {"priority", "conviction_score", "alpha_prior_score"}
_NULLABLE_NUMBERS = {"conviction_score", "alpha_prior_score"}
# content fields: any revision here appends a thesis_versions row; view and
# confidence also mirror onto the projection (theses.current_view/confidence)
_VERSION_LIST_FIELDS = ("drivers", "risks", "kpis", "catalysts", "stock_map")
_VERSION_FIELDS = {"view", "confidence", "summary", "run_id", *_VERSION_LIST_FIELDS}


class ThesisError(ValueError):
    """Validation failure (the API maps this to 400)."""


class TransitionConflict(ThesisError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


# ---- helpers ---------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _validate_enum(value: Any, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise ThesisError(f"unknown {label} {value!r}; allowed: {', '.join(sorted(allowed))}")


def _require_list(val: Any, label: str) -> list:
    if val is None:
        return []
    if not isinstance(val, list):
        raise ThesisError(f"{label} must be a list")
    return val


def _require_number(val: Any, label: str, *, nullable: bool = False) -> float | None:
    if val is None and nullable:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        raise ThesisError(f"{label} must be a number") from None


def _require_dict(val: Any, label: str) -> dict:
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ThesisError(f"{label} must be an object")
    return val


def _loads(text: str | None, fallback: Any) -> Any:
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback


def _dumps(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False)


def _thesis_out(row: dict[str, Any]) -> dict[str, Any]:
    thesis = dict(row)
    thesis["metadata"] = _require_dict(_loads(thesis.pop("metadata_json", None), {}), "metadata")
    return thesis


def _version_out(row: dict[str, Any]) -> dict[str, Any]:
    ver = dict(row)
    for field in _VERSION_LIST_FIELDS:
        ver[field] = _require_list(_loads(ver.pop(f"{field}_json", None), []), field)
    return ver


async def _check_slug_free(slug: str, *, exclude_id: str | None = None) -> None:
    """Duplicate slugs must fail with a readable error, not an IntegrityError."""
    holder = await db.query_one("SELECT id FROM theses WHERE slug = ?", (slug,))
    if holder and holder["id"] != exclude_id:
        raise ThesisError(f"duplicate slug {slug!r} (already used by thesis {holder['id']!r})")


async def _check_parent(parent_id: str, *, child_id: str | None = None) -> None:
    """Parent must exist; re-parenting must not create a cycle."""
    if parent_id == child_id:
        raise ThesisError("a thesis cannot be its own parent")
    parent = await db.query_one("SELECT id, parent_id FROM theses WHERE id = ?", (parent_id,))
    if parent is None:
        raise ThesisError(f"parent thesis {parent_id!r} not found")
    seen = {parent_id}
    node = parent
    while node and node["parent_id"]:
        up = node["parent_id"]
        if up == child_id:
            raise ThesisError(f"parent {parent_id!r} is a descendant of {child_id!r} (cycle)")
        if up in seen:  # defensive: pre-existing loop in the data
            break
        seen.add(up)
        node = await db.query_one("SELECT id, parent_id FROM theses WHERE id = ?", (up,))


def _split_view_alias(fields: dict[str, Any]) -> None:
    """Accept ``current_view`` (the column name) as an alias for ``view``."""
    if "current_view" in fields:
        val = fields.pop("current_view")
        fields.setdefault("view", val)


def _map_integrity(
    exc: sqlite3.IntegrityError,
    *,
    thesis_id: str | None = None,
    slug: str | None = None,
    parent_id: str | None = None,
) -> ThesisError:
    """Concurrent writers can slip past the pre-checks (those reads happen before
    the write lock), so constraint failures at INSERT time map back onto the same
    readable errors — never a raw IntegrityError 500."""
    msg = str(exc)
    if "thesis_versions" in msg:
        return TransitionConflict(f"thesis {thesis_id} changed concurrently; retry")
    if "theses.slug" in msg:
        return ThesisError(f"duplicate slug {slug!r}")
    if "theses.id" in msg:
        return ThesisError(f"duplicate thesis id {thesis_id!r}")
    if "FOREIGN KEY" in msg:
        return ThesisError(f"parent thesis {parent_id!r} not found")
    return ThesisError(f"constraint failed: {msg}")


# ---- create ----------------------------------------------------------------

async def create_thesis(fields: dict[str, Any]) -> dict[str, Any]:
    """Create a lane or thesis (POST contract: enters as candidate or active).

    Content fields (summary/drivers/…), when provided, seed thesis_versions
    version 1 so the very first view is already on the history trail.
    """
    data = dict(fields or {})
    _split_view_alias(data)

    kind = data.pop("kind", "thesis")
    _validate_enum(kind, KINDS, "kind")
    status = data.pop("status", "candidate")
    _validate_enum(status, set(STATUSES), "status")
    if status not in CREATE_STATUSES:
        raise ThesisError(
            f"a new thesis starts as candidate or active, not {status!r}; "
            "watch/dormant/retired are reached via status transitions"
        )
    name_zh = str(data.pop("name_zh", "") or "").strip()
    if not name_zh:
        raise ThesisError("a thesis needs a name_zh")
    tid = str(data.pop("id", "") or "").strip()
    slug = str(data.pop("slug", "") or "").strip()
    if not tid and not slug:
        raise ThesisError("a thesis needs an id or a slug")
    tid = tid or slug
    slug = slug or tid

    view = data.pop("view", "unknown")
    _validate_enum(view, VIEWS, "view")
    confidence = data.pop("confidence", "medium")
    _validate_enum(confidence, CONFIDENCES, "confidence")
    parent_id = data.pop("parent_id", None) or None
    priority = _require_number(data.pop("priority", 0), "priority")
    conviction = _require_number(data.pop("conviction_score", None), "conviction_score", nullable=True)
    alpha_prior = _require_number(data.pop("alpha_prior_score", None), "alpha_prior_score", nullable=True)
    metadata = _require_dict(data.pop("metadata", None), "metadata")

    summary = str(data.pop("summary", "") or "")
    run_id = data.pop("run_id", None)
    content_lists = {f: _require_list(data.pop(f, None), f) for f in _VERSION_LIST_FIELDS}

    scalars = {
        key: data.pop(key, default)
        for key, default in (
            ("name_en", None), ("scope", ""), ("exclusions", ""), ("owner_analyst", None),
            ("first_seen", None), ("last_seen", None), ("source", "manual"),
            ("source_href", None), ("source_network_href", None),
        )
    }
    if data:
        raise ThesisError(f"unknown thesis fields: {', '.join(sorted(data))}")

    if await db.query_one("SELECT id FROM theses WHERE id = ?", (tid,)):
        raise ThesisError(f"duplicate thesis id {tid!r}")
    await _check_slug_free(slug)
    if parent_id:
        await _check_parent(parent_id, child_id=tid)

    now = bus.now_iso()
    seed_version = bool(summary or any(content_lists.values()))
    version_id = _new_id()
    # one transaction so a thesis never lands without its seeded version row.
    # NB: transaction() holds the db write lock — use the yielded conn directly
    # (db.execute/insert or bus.emit in here would deadlock); events after commit.
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO theses (id, parent_id, kind, slug, name_zh, name_en, status, scope, exclusions, "
                "owner_analyst, priority, confidence, current_view, conviction_score, alpha_prior_score, "
                "first_seen, last_seen, source, source_href, source_network_href, metadata_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid, parent_id, kind, slug, name_zh, scalars["name_en"], status, scalars["scope"],
                    scalars["exclusions"], scalars["owner_analyst"], priority, confidence, view,
                    conviction, alpha_prior, scalars["first_seen"], scalars["last_seen"], scalars["source"],
                    scalars["source_href"], scalars["source_network_href"], _dumps(metadata), now, now,
                ),
            )
            if seed_version:
                await conn.execute(
                    "INSERT INTO thesis_versions (id, thesis_id, version, supersedes_id, run_id, view, "
                    "confidence, summary, drivers_json, risks_json, kpis_json, catalysts_json, "
                    "stock_map_json, created_at) VALUES (?,?,1,NULL,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id, tid, run_id, view, confidence, summary,
                        *(_dumps(content_lists[f]) for f in _VERSION_LIST_FIELDS), now,
                    ),
                )
    except sqlite3.IntegrityError as exc:  # concurrent duplicate create lost the insert race
        raise _map_integrity(exc, thesis_id=tid, slug=slug, parent_id=parent_id) from exc
    await bus.emit(
        "thesis.created", "thesis", tid,
        {"kind": kind, "slug": slug, "status": status, "parent_id": parent_id,
         "version": 1 if seed_version else None},
    )
    return await get_thesis(tid)


# ---- read ------------------------------------------------------------------

async def get_thesis(thesis_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM theses WHERE id = ?", (thesis_id,))
    if row is None:
        return None
    thesis = _thesis_out(row)
    thesis["versions"] = [
        _version_out(v) for v in await db.query(
            "SELECT * FROM thesis_versions WHERE thesis_id = ? ORDER BY version", (thesis_id,)
        )
    ]
    thesis["children"] = [
        r["id"] for r in await db.query(
            "SELECT id FROM theses WHERE parent_id = ? ORDER BY priority DESC, id", (thesis_id,)
        )
    ]
    return thesis


async def list_theses(
    status: str | None = None,
    kind: str | None = None,
    parent_id: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Flat listing (rows only, no version history) with optional filters."""
    if status:
        _validate_enum(status, set(STATUSES), "status")
    if kind:
        _validate_enum(kind, KINDS, "kind")
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
    if search:
        like = f"%{search}%"
        clauses.append("(id LIKE ? OR slug LIKE ? OR name_zh LIKE ? OR name_en LIKE ?)")
        params.extend([like, like, like, like])
    sql = "SELECT * FROM theses"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY priority DESC, id"
    return [_thesis_out(r) for r in await db.query(sql, params)]


async def tree() -> list[dict[str, Any]]:
    """Lanes→theses shaping: every node carries its children, lanes sort first
    at the root, and a thesis whose parent is missing surfaces as a root."""
    rows = [_thesis_out(r) for r in await db.query("SELECT * FROM theses ORDER BY priority DESC, id")]
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["children"] = []
    roots: list[dict[str, Any]] = []
    for r in rows:
        parent = by_id.get(r["parent_id"]) if r["parent_id"] else None
        if parent is not None and parent is not r:
            parent["children"].append(r)
        else:
            roots.append(r)
    roots.sort(key=lambda r: 0 if r["kind"] == "lane" else 1)  # stable: keeps priority order within kind
    return roots


# ---- update (projection + version history) ----------------------------------

async def update_thesis(thesis_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Rewrite projection fields; any content revision appends a version row.

    Content fields not present in the revision are carried over from the
    latest version, so partial updates never erase drivers/risks/… history.
    """
    row = await db.query_one("SELECT * FROM theses WHERE id = ?", (thesis_id,))
    if row is None:
        return None
    fields = dict(fields or {})
    _split_view_alias(fields)
    if "status" in fields:
        raise ThesisError("status changes go through set_status(), not update")
    for immutable in ("id", "kind", "created_at", "updated_at"):
        if immutable in fields:
            raise ThesisError(f"{immutable} cannot be updated")
    unknown = set(fields) - _PROJECTION_FIELDS - _VERSION_FIELDS
    if unknown:
        raise ThesisError(f"unknown thesis fields: {', '.join(sorted(unknown))}")
    if not fields:
        return await get_thesis(thesis_id)

    # validate everything up front so a bad patch writes nothing
    if "view" in fields:
        _validate_enum(fields["view"], VIEWS, "view")
    if "confidence" in fields:
        _validate_enum(fields["confidence"], CONFIDENCES, "confidence")
    for key in ("scope", "exclusions", "source", "summary"):  # NOT NULL columns: omit, never null
        if key in fields and fields[key] is None:
            raise ThesisError(f"{key} cannot be null (omit the field to keep it)")
    if "name_zh" in fields and not str(fields["name_zh"] or "").strip():
        raise ThesisError("name_zh cannot be empty")
    if "slug" in fields:
        slug = str(fields["slug"] or "").strip()
        if not slug:
            raise ThesisError("slug cannot be empty")
        fields["slug"] = slug
        await _check_slug_free(slug, exclude_id=thesis_id)
    if "parent_id" in fields:
        fields["parent_id"] = fields["parent_id"] or None  # falsy = clean un-parenting (mirrors create)
        if fields["parent_id"]:
            await _check_parent(fields["parent_id"], child_id=thesis_id)
    for key in _NUMBER_FIELDS & set(fields):
        fields[key] = _require_number(fields[key], key, nullable=key in _NULLABLE_NUMBERS)
    if "metadata" in fields:
        fields["metadata"] = _require_dict(fields["metadata"], "metadata")
    for key in _VERSION_LIST_FIELDS:
        if key in fields:
            fields[key] = _require_list(fields[key], key)

    revising = bool(set(fields) & (_VERSION_FIELDS - {"run_id"}))
    if "run_id" in fields and not revising:
        raise ThesisError("run_id only annotates a content revision (send it with view/summary/…)")

    now = bus.now_iso()
    sets, params = [], []
    for key in _PROJECTION_FIELDS & set(fields):
        if key == "metadata":
            sets.append("metadata_json = ?")
            params.append(_dumps(fields[key]))
        else:
            sets.append(f"{key} = ?")
            params.append(fields[key])

    version_no = None
    version_id = _new_id()
    sets.append("updated_at = ?")
    params.append(now)

    try:
        async with db.transaction() as conn:  # NB: use conn directly; events after commit
            if revising:
                # read the head INSIDE the write lock: two concurrent revisions must
                # not both see the same prev and claim the same version + 1
                cur = await conn.execute(
                    "SELECT * FROM thesis_versions WHERE thesis_id = ? ORDER BY version DESC LIMIT 1",
                    (thesis_id,),
                )
                latest = await cur.fetchone()
                await cur.close()
                prev = _version_out(dict(latest)) if latest else None
                view = fields.get("view", prev["view"] if prev else row["current_view"])
                confidence = fields.get("confidence", prev["confidence"] if prev else row["confidence"])
                summary = fields.get("summary", prev["summary"] if prev else "")
                content = {f: fields.get(f, prev[f] if prev else []) for f in _VERSION_LIST_FIELDS}
                version_no = (prev["version"] + 1) if prev else 1
                await conn.execute(
                    "INSERT INTO thesis_versions (id, thesis_id, version, supersedes_id, run_id, view, "
                    "confidence, summary, drivers_json, risks_json, kpis_json, catalysts_json, "
                    "stock_map_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id, thesis_id, version_no, prev["id"] if prev else None,
                        fields.get("run_id"), view, confidence, summary,
                        *(_dumps(content[f]) for f in _VERSION_LIST_FIELDS), now,
                    ),
                )
                # the projection mirrors the new head of the version chain
                sets.extend(["current_view = ?", "confidence = ?"])
                params.extend([view, confidence])
            await conn.execute(
                f"UPDATE theses SET {', '.join(sets)} WHERE id = ?", (*params, thesis_id)
            )
    except sqlite3.IntegrityError as exc:  # a concurrent writer slipped past the pre-checks
        raise _map_integrity(
            exc, thesis_id=thesis_id, slug=fields.get("slug"), parent_id=fields.get("parent_id")
        ) from exc
    await bus.emit(
        "thesis.updated", "thesis", thesis_id,
        {"fields": sorted(fields), "version": version_no},
    )
    return await get_thesis(thesis_id)


# ---- lifecycle transitions ----------------------------------------------------

async def set_status(
    thesis_id: str,
    to_status: str,
    *,
    expected_status: str | None = None,
    reason: str = "",
) -> dict[str, Any] | None:
    """Lifecycle move as a conditional claim on the status we validated against."""
    _validate_enum(to_status, set(STATUSES), "status")
    row = await db.query_one("SELECT id, status FROM theses WHERE id = ?", (thesis_id,))
    if row is None:
        return None
    from_status = row["status"]
    if expected_status is not None and expected_status != from_status:
        raise TransitionConflict(f"thesis {thesis_id} is {from_status!r}, expected {expected_status!r}")
    if to_status == from_status:
        return await get_thesis(thesis_id)
    claimed = await db.execute(
        "UPDATE theses SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (to_status, bus.now_iso(), thesis_id, from_status),
    )
    if not claimed:
        raise TransitionConflict(f"thesis {thesis_id} changed concurrently; reload and retry")
    await bus.emit(
        "thesis.status_changed", "thesis", thesis_id,
        {"from": from_status, "to": to_status, "reason": reason},
    )
    return await get_thesis(thesis_id)
