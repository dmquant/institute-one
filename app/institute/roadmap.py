"""Roadmap control plane — durable backend for the global coding process.

Cards, checklists, dependencies, evidence, and coding sessions live in SQLite
(rows are truth). ``roadmap/backlog.json`` is a seed/import/export artifact:
``import_backlog()`` upserts by card id, merges checklist items by text,
reconciles dependencies to the seed, and preserves local status unless
``force`` is set. Contract: roadmap/02-data-model.md.

Every move is a conditional-claim transition (``UPDATE … WHERE status = <the
status we validated against>``) so concurrent movers can never double-apply.
User-visible changes append to ``roadmap_events`` and emit namespaced
``roadmap.<event>`` bus events (e.g. ``roadmap.card.moved``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings

log = logging.getLogger("institute.roadmap")

STATUSES = ("inbox", "ready", "in_progress", "review", "verify", "done", "parked")
# parked is outside the forward flow, so it never counts as a forward move
_FORWARD_RANK = {s: i for i, s in enumerate(("inbox", "ready", "in_progress", "review", "verify", "done"))}
TYPES = {"docs", "feature", "schema", "test", "ui", "workflow", "ops", "decision"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
RISKS = {"low", "medium", "high"}
CHECKLIST_KINDS = {"acceptance", "implementation", "review"}
EVIDENCE_KINDS = {"command", "test", "screenshot", "diff", "doc", "operator"}
EVIDENCE_STATUSES = {"pass", "fail", "info", "override"}
SESSION_STATUSES = {"active", "completed", "partial", "blocked", "cancelled"}
SESSION_TERMINAL = {"completed", "partial", "blocked", "cancelled"}
_SUMMARY_REQUIRED_STATUSES = {"completed", "partial", "blocked"}

# summaries are normalized (str.strip) on every write, so a blank summary is
# stored as exactly ''; the TRIM belt keeps pre-normalization rows honest.
_NONBLANK_SUMMARY_SQL = "TRIM({col}, ' ' || CHAR(9) || CHAR(10) || CHAR(13)) != ''"

# review gate: at least one completed session whose summary is not blank.
# {card} is either a bound "?" (pre-check) or "roadmap_cards.id" (correlated
# into the conditional claim so the gate cannot be raced).
_REVIEW_SESSION_EXISTS = (
    "EXISTS (SELECT 1 FROM roadmap_coding_sessions s "
    "WHERE s.card_id = {card} AND s.status = 'completed' "
    "AND " + _NONBLANK_SUMMARY_SQL.format(col="s.summary") + ")"
)

# gate scopes match the Obsidian plugin (obsidian-plugin/src/roadmap.ts renderGates)
RELEASE_GATES = (
    ("Release A", "Thesis Registry + Forecastable Research", ("M0", "M1", "M2", "M3")),
    ("Release B", "Market Data + Forecast Ledger", ("M4", "M5", "M6")),
    ("Release C", "Roadmap Control Plane", ("M7",)),
)

_CARD_JSON_FIELDS = ("design_links", "expected_files", "verification", "tags")
_UPDATABLE_FIELDS = {
    "title", "summary", "problem", "implementation", "agent_prompt", "owner",
    "phase", "type", "priority", "risk", "blocked_reason", "sort_order",
    *_CARD_JSON_FIELDS,
}


class RoadmapError(ValueError):
    """Validation failure (the API maps this to 400)."""


class MoveConflict(RoadmapError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


# ---- helpers ---------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _det_id(*parts: str) -> str:
    """Deterministic id so re-imports merge instead of duplicating."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _loads_list(text: str | None) -> list:
    try:
        val = json.loads(text or "[]")
    except ValueError:
        return []
    return val if isinstance(val, list) else []


def _dumps(val: Any) -> str:
    return json.dumps(list(val or []), ensure_ascii=False)


def _validate_enum(value: Any, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise RoadmapError(f"unknown {label} {value!r}; allowed: {', '.join(sorted(allowed))}")


def _require_str_list(val: Any, label: str) -> list[str]:
    """Shape-check a list-of-strings field (a bare string would silently explode into characters)."""
    if val is None:
        return []
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise RoadmapError(f"{label} must be a list of strings")
    return val


def _require_number(val: Any, label: str) -> float:
    try:
        number = float(val)
    except (TypeError, ValueError):
        raise RoadmapError(f"{label} must be a number") from None
    if not math.isfinite(number):  # float("NaN") would fail the NOT NULL column as 500
        raise RoadmapError(f"{label} must be a finite number")
    return number


def _reject_reserved(value: str, label: str) -> str:
    """\\x1f joins deterministic-id parts and reconcile pairs: a value carrying
    it could alias two different (id, relation) tuples onto one encoding."""
    if "\x1f" in value:
        raise RoadmapError(f"{label} contains a reserved control character")
    return value


def _like_escape(text: str) -> str:
    r"""Escape LIKE wildcards in user input (pair with ``ESCAPE '\'``)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _phase_token(phase: str | None) -> str:
    """Leading milestone token of a phase ("M7 Roadmap Control Plane" -> "M7")."""
    return (phase or "").split(" ")[0]


def _find_dependency_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """First dependency cycle in the graph as a node path, or None (iterative DFS)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    for start in edges:
        if color.get(start, WHITE) != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, idx = stack[-1]
            if idx == 0:
                color[node] = GRAY
                path.append(node)
            children = edges.get(node, [])
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                state = color.get(child, WHITE)
                if state == GRAY:
                    return path[path.index(child):] + [child]
                if state == WHITE:
                    stack.append((child, 0))
            else:
                color[node] = BLACK
                path.pop()
                stack.pop()
    return None


def _card_out(row: dict[str, Any]) -> dict[str, Any]:
    card = dict(row)
    for field in _CARD_JSON_FIELDS:
        card[field] = _loads_list(card.pop(f"{field}_json", None))
    return card


def _session_out(row: dict[str, Any]) -> dict[str, Any]:
    sess = dict(row)
    sess["planned_files"] = _loads_list(sess.pop("planned_files_json", None))
    sess["touched_files"] = _loads_list(sess.pop("touched_files_json", None))
    return sess


async def _record_event(event_type: str, card_id: str | None, payload: dict[str, Any]) -> None:
    """Append to roadmap_events and mirror onto the bus as roadmap.<event_type>."""
    await db.execute(
        "INSERT INTO roadmap_events (id, card_id, event_type, payload_json, created_at) VALUES (?,?,?,?,?)",
        (_new_id(), card_id, event_type, json.dumps(payload, ensure_ascii=False), bus.now_iso()),
    )
    await bus.emit(f"roadmap.{event_type}", "roadmap_card" if card_id else "roadmap", card_id or "", payload)


# ---- seed import -----------------------------------------------------------

def default_backlog_path() -> Path:
    return get_settings().repo_root / "roadmap" / "backlog.json"


_SEED_TEXT_FIELDS = ("title", "phase", "summary", "problem", "implementation", "agent_prompt")
# stored-row columns matching the seed field tuple built in pass 1, in order
_SEED_ROW_COLUMNS = (
    "title", "type", "phase", "priority", "risk", "summary", "problem", "implementation",
    "agent_prompt", "design_links_json", "expected_files_json", "verification_json", "tags_json",
)


async def import_backlog(path: str | Path | None = None, force: bool = False) -> dict[str, Any]:
    """Idempotent upsert of a backlog seed file by card id.

    Local status wins unless ``force``; checklist items merge by text (checked
    state survives); dependencies are reconciled to the seed (import is their
    only writer). All writes happen in one transaction after full up-front
    validation, so a seed that fails (or crashes mid-import) writes nothing.
    Cards whose seed fields equal the stored row are reported ``unchanged``
    and keep their ``updated_at``.
    """
    src = Path(path) if path else default_backlog_path()
    if not src.is_absolute():
        src = get_settings().repo_root / src
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RoadmapError(f"backlog file not found: {src}") from None
    except ValueError as exc:
        raise RoadmapError(f"backlog is not valid JSON: {exc}") from None
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise RoadmapError("backlog has no 'cards' list")

    # validate everything up front so a bad seed imports nothing
    existing_ids = {r["id"] for r in await db.query("SELECT id FROM roadmap_cards")}
    seed_ids: set[str] = set()
    for c in cards:
        if not isinstance(c, dict):
            raise RoadmapError("every card must be an object")
        # checked pre-strip: Python strips \x1f as whitespace, so a trailing
        # separator would silently vanish instead of being rejected
        cid = _reject_reserved(str(c.get("id") or ""), "card id").strip()
        if not cid or not str(c.get("title") or "").strip():
            raise RoadmapError("every card needs an id and a title")
        if cid in seed_ids:
            raise RoadmapError(f"duplicate card id in seed: {cid}")
        seed_ids.add(cid)
        _validate_enum(c.get("status", "inbox"), set(STATUSES), "status")
        _validate_enum(c.get("type", "feature"), TYPES, "type")
        _validate_enum(c.get("priority", "P2"), PRIORITIES, "priority")
        _validate_enum(c.get("risk", "medium"), RISKS, "risk")
        for field in _SEED_TEXT_FIELDS:
            if field in c and not isinstance(c[field], str):
                raise RoadmapError(f"card {cid}: {field} must be a string")
        for field in ("owner", "blocked_reason"):
            if c.get(field) is not None and not isinstance(c[field], str):
                raise RoadmapError(f"card {cid}: {field} must be a string")
        if "sort_order" in c:
            _require_number(c["sort_order"], f"card {cid}: sort_order")
        for field in ("acceptance", "acceptance_checked", "dependencies", *_CARD_JSON_FIELDS):
            _require_str_list(c.get(field), f"card {cid}: {field}")
        # export-fidelity extensions (written by export_backlog, optional in seeds)
        for meta in c.get("dependencies_meta") or []:
            if (
                not isinstance(meta, dict)
                or not isinstance(meta.get("depends_on_id"), str)
                or not isinstance(meta.get("relation", "blocks"), str)
                or meta.get("source", "import") not in ("import", "manual")
            ):
                raise RoadmapError(f"card {cid}: dependencies_meta entries need depends_on_id/relation/source")
            _reject_reserved(str(meta.get("relation", "blocks")), f"card {cid}: dependency relation")
        for extra in c.get("checklists_extra") or []:
            if (
                not isinstance(extra, dict)
                or extra.get("kind") not in CHECKLIST_KINDS
                or not isinstance(extra.get("text"), str) or not extra["text"].strip()
            ):
                raise RoadmapError(f"card {cid}: checklists_extra entries need a valid kind and text")
    known = existing_ids | seed_ids
    for c in cards:
        meta_ids = [m["depends_on_id"] for m in (c.get("dependencies_meta") or [])]
        for dep in [*(c.get("dependencies") or []), *meta_ids]:
            if dep not in known:
                raise RoadmapError(f"card {c['id']} depends on unknown card {dep!r}")

    now = bus.now_iso()
    created = updated = unchanged = 0
    forced: list[dict[str, Any]] = []

    # one transaction: a mid-import failure rolls back to zero writes.
    # NB: transaction() holds the db write lock — use the yielded conn directly
    # (db.execute/insert or bus.emit in here would deadlock); events after commit.
    async with db.transaction() as conn:
        # pass 1: card rows (dependencies reference cards, so rows must exist first)
        for i, c in enumerate(cards):
            cid = str(c["id"]).strip()
            status = c.get("status", "inbox")
            fields = (
                str(c["title"]).strip(), c.get("type", "feature"), c.get("phase", ""),
                c.get("priority", "P2"), c.get("risk", "medium"), c.get("summary", ""),
                c.get("problem", ""), c.get("implementation", ""), c.get("agent_prompt", ""),
                _dumps(c.get("design_links")), _dumps(c.get("expected_files")),
                _dumps(c.get("verification")), _dumps(c.get("tags")),
            )
            cur = await conn.execute("SELECT * FROM roadmap_cards WHERE id = ?", (cid,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                await conn.execute(
                    "INSERT INTO roadmap_cards (id, title, type, phase, priority, risk, summary, problem, "
                    "implementation, agent_prompt, design_links_json, expected_files_json, verification_json, "
                    "tags_json, status, owner, blocked_reason, sort_order, created_at, updated_at, "
                    "completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, *fields, status, c.get("owner"), c.get("blocked_reason"),
                     float(c.get("sort_order", (i + 1) * 10)),
                     now, now, now if status == "done" else None),
                )
                created += 1
                continue
            changed = fields != tuple(row[col] for col in _SEED_ROW_COLUMNS)
            if changed:
                await conn.execute(
                    "UPDATE roadmap_cards SET title=?, type=?, phase=?, priority=?, risk=?, summary=?, "
                    "problem=?, implementation=?, agent_prompt=?, design_links_json=?, expected_files_json=?, "
                    "verification_json=?, tags_json=?, updated_at=? WHERE id=?",
                    (*fields, now, cid),
                )
            if force and status != row["status"]:  # local status wins unless forced
                await conn.execute(
                    "UPDATE roadmap_cards SET status=?, completed_at=?, updated_at=? WHERE id=?",
                    (status, now if status == "done" else None, now, cid),
                )
                forced.append({"card_id": cid, "from": row["status"], "to": status})
                changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1

        # pass 2: checklists + dependencies (deterministic ids -> merge, not duplicate)
        for c in cards:
            cid = str(c["id"]).strip()
            for j, text in enumerate(c.get("acceptance") or []):
                await conn.execute(
                    "INSERT OR IGNORE INTO roadmap_checklists (id, card_id, kind, text, checked, sort_order, "
                    "created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                    (_det_id(cid, "acceptance", text), cid, "acceptance", text, (j + 1) * 10.0, now, now),
                )
            # export fidelity: checked state and non-acceptance checklists come
            # back on restore; checking is merge-only (never unchecks local work)
            for text in c.get("acceptance_checked") or []:
                await conn.execute(
                    "UPDATE roadmap_checklists SET checked = 1, updated_at = ? "
                    "WHERE card_id = ? AND kind = 'acceptance' AND text = ? AND checked = 0",
                    (now, cid, text),
                )
            for j, extra in enumerate(c.get("checklists_extra") or []):
                text = extra["text"].strip()
                await conn.execute(
                    "INSERT OR IGNORE INTO roadmap_checklists (id, card_id, kind, text, checked, sort_order, "
                    "created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                    (_det_id(cid, extra["kind"], text), cid, extra["kind"], text, (j + 1) * 10.0, now, now),
                )
                if extra.get("checked"):
                    await conn.execute(
                        "UPDATE roadmap_checklists SET checked = 1, updated_at = ? "
                        "WHERE card_id = ? AND kind = ? AND text = ? AND checked = 0",
                        (now, cid, extra["kind"], text),
                    )
            # dependencies_meta (written by export) carries relation + source so
            # a restore does not flip manual edges to importer ownership; plain
            # `dependencies` remains the seed-file shape (import/blocks)
            meta = c.get("dependencies_meta")
            edges = (
                [(m["depends_on_id"], m.get("relation", "blocks") or "blocks",
                  m.get("source", "import")) for m in meta]
                if meta is not None
                else [(dep, "blocks", "import") for dep in (c.get("dependencies") or [])]
            )
            # reconcile: import owns only rows it wrote itself (source='import'),
            # so seed-dropped deps are deleted (a stale dep would block
            # move-to-done forever) while operator-added rows survive. The
            # match is on the (target, relation) PAIR — a relation change must
            # replace the old edge, not accumulate alongside it
            import_pairs = [f"{dep}\x1f{relation}" for dep, relation, source in edges if source == "import"]
            if import_pairs:
                marks = ",".join("?" for _ in import_pairs)
                await conn.execute(
                    f"DELETE FROM roadmap_dependencies WHERE card_id = ? AND source = 'import' "
                    f"AND (depends_on_id || CHAR(31) || relation) NOT IN ({marks})",
                    (cid, *import_pairs),
                )
            else:
                await conn.execute(
                    "DELETE FROM roadmap_dependencies WHERE card_id = ? AND source = 'import'", (cid,)
                )
            for dep, relation, source in edges:
                await conn.execute(
                    "INSERT OR IGNORE INTO roadmap_dependencies (id, card_id, depends_on_id, relation, "
                    "source, created_at) VALUES (?,?,?,?,?,?)",
                    (_det_id(cid, "dep", dep, relation), cid, dep, relation, source, now),
                )

        # the FINAL graph (import edges + surviving manual edges) must stay
        # acyclic — a cycle would deadlock the move-to-done dependency gate,
        # and add_dependency alone cannot see what this import is writing
        cur = await conn.execute("SELECT card_id, depends_on_id FROM roadmap_dependencies")
        dep_rows = await cur.fetchall()
        await cur.close()
        graph: dict[str, list[str]] = {}
        for r in dep_rows:
            graph.setdefault(r["card_id"], []).append(r["depends_on_id"])
        cycle = _find_dependency_cycle(graph)
        if cycle:
            raise RoadmapError(
                "import would create a dependency cycle: " + " -> ".join(cycle)
            )

    result = {"created": created, "updated": updated, "unchanged": unchanged, "total": len(cards)}
    for f in forced:  # forced status flips stay on the per-card audit trail
        await _record_event("import.status_forced", f["card_id"], {"from": f["from"], "to": f["to"]})
    await _record_event("import.completed", None, {"path": str(src), "force": force, **result})
    return result


# ---- cards -----------------------------------------------------------------

async def list_cards(
    status: str | None = None,
    phase: str | None = None,
    type: str | None = None,  # noqa: A002 - mirrors the card field name
    priority: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if phase:  # milestone-token match so "M1" finds "M1 …" but not "M10 …"
        clauses.append("(phase = ? OR phase LIKE ? ESCAPE '\\')")
        params.append(phase)
        params.append(f"{_like_escape(phase)} %")
    if type:
        clauses.append("type = ?")
        params.append(type)
    if priority:
        clauses.append("priority = ?")
        params.append(priority)
    if search:
        like = f"%{search}%"
        clauses.append("(id LIKE ? OR title LIKE ? OR summary LIKE ?)")
        params.extend([like, like, like])
    sql = "SELECT * FROM roadmap_cards"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY sort_order, id"
    rows = await db.query(sql, params)

    deps: dict[str, list[str]] = {}
    for d in await db.query("SELECT card_id, depends_on_id FROM roadmap_dependencies ORDER BY depends_on_id"):
        deps.setdefault(d["card_id"], []).append(d["depends_on_id"])
    cards = []
    for r in rows:
        card = _card_out(r)
        card["dependencies"] = deps.get(card["id"], [])
        cards.append(card)
    return cards


async def get_card(card_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM roadmap_cards WHERE id = ?", (card_id,))
    if row is None:
        return None
    card = _card_out(row)
    card["checklists"] = await db.query(
        "SELECT * FROM roadmap_checklists WHERE card_id = ? ORDER BY kind, sort_order", (card_id,)
    )
    card["dependencies"] = await db.query(
        "SELECT d.*, c.status AS depends_on_status FROM roadmap_dependencies d "
        "LEFT JOIN roadmap_cards c ON c.id = d.depends_on_id WHERE d.card_id = ? ORDER BY d.depends_on_id",
        (card_id,),
    )
    card["evidence"] = await db.query(
        "SELECT * FROM roadmap_evidence WHERE card_id = ? ORDER BY created_at, rowid", (card_id,)
    )
    card["sessions"] = [
        _session_out(s) for s in await db.query(
            "SELECT * FROM roadmap_coding_sessions WHERE card_id = ? ORDER BY started_at DESC, rowid DESC",
            (card_id,),
        )
    ]
    return card


async def update_card(card_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    row = await db.query_one("SELECT id FROM roadmap_cards WHERE id = ?", (card_id,))
    if row is None:
        return None
    if "status" in fields:
        raise RoadmapError("status changes go through move(), not update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise RoadmapError(f"unknown card fields: {', '.join(sorted(unknown))}")
    if "type" in fields:
        _validate_enum(fields["type"], TYPES, "type")
    if "priority" in fields:
        _validate_enum(fields["priority"], PRIORITIES, "priority")
    if "risk" in fields:
        _validate_enum(fields["risk"], RISKS, "risk")
    if "sort_order" in fields:
        fields["sort_order"] = _require_number(fields["sort_order"], "sort_order")
    for key in _CARD_JSON_FIELDS:
        if key in fields:
            fields[key] = _require_str_list(fields[key], key)
    if not fields:
        return await get_card(card_id)

    sets, params = [], []
    for key, val in fields.items():
        if key in _CARD_JSON_FIELDS:
            sets.append(f"{key}_json = ?")
            params.append(_dumps(val))
        else:
            sets.append(f"{key} = ?")
            params.append(val)
    sets.append("updated_at = ?")
    params.append(bus.now_iso())
    params.append(card_id)
    await db.execute(f"UPDATE roadmap_cards SET {', '.join(sets)} WHERE id = ?", params)
    await _record_event("card.updated", card_id, {"fields": sorted(fields)})
    return await get_card(card_id)


async def move(
    card_id: str,
    to_status: str,
    *,
    override: bool = False,
    reason: str = "",
    owner: str | None = None,
    sort_order: float | None = None,
    expected_status: str | None = None,
) -> dict[str, Any] | None:
    """Move a card between statuses (02-data-model.md move semantics).

    Unless ``override``: a blocked card cannot move forward; ``ready`` needs a
    non-empty acceptance checklist; ``in_progress`` needs an owner; ``done``
    needs every dependency done AND at least one piece of evidence. The write
    is a conditional claim on the status we validated against.
    """
    _validate_enum(to_status, set(STATUSES), "status")
    if sort_order is not None:
        sort_order = _require_number(sort_order, "sort_order")
    card = await db.query_one("SELECT * FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    from_status = card["status"]
    if expected_status is not None and expected_status != from_status:
        raise MoveConflict(f"card {card_id} is {from_status!r}, expected {expected_status!r}")

    if not override:
        forward = _FORWARD_RANK.get(to_status, -1) > _FORWARD_RANK.get(from_status, -1)
        if card["blocked_reason"] and forward:
            raise RoadmapError(
                f"card {card_id} is blocked ({card['blocked_reason']}); set override to move it forward"
            )
        if to_status == "ready":
            n = await db.query_one(
                "SELECT COUNT(*) AS n FROM roadmap_checklists WHERE card_id = ? AND kind = 'acceptance'",
                (card_id,),
            )
            if not n or not n["n"]:
                raise RoadmapError("cannot move to ready: acceptance checklist is empty")
        if to_status == "in_progress" and not (owner or card["owner"]):
            raise RoadmapError("cannot move to in_progress without an owner")
        if to_status == "review" and from_status != "review":
            completed = await db.query_one(
                "SELECT 1 AS ok WHERE " + _REVIEW_SESSION_EXISTS.format(card="?"),
                (card_id,),
            )
            if completed is None:
                raise RoadmapError(
                    "cannot move to review without a completed coding session summary; "
                    "set override to force"
                )
        if to_status == "done":
            open_deps = await db.query(
                "SELECT d.depends_on_id FROM roadmap_dependencies d "
                "JOIN roadmap_cards c ON c.id = d.depends_on_id "
                "WHERE d.card_id = ? AND c.status != 'done' ORDER BY d.depends_on_id",
                (card_id,),
            )
            if open_deps:
                ids = ", ".join(r["depends_on_id"] for r in open_deps)
                raise RoadmapError(f"cannot move to done: dependencies not done ({ids}); set override to force")
            ev = await db.query_one("SELECT COUNT(*) AS n FROM roadmap_evidence WHERE card_id = ?", (card_id,))
            if not ev or not ev["n"]:
                raise RoadmapError("cannot move to done without evidence; set override to force")

    now = bus.now_iso()
    # only write owner/sort_order when explicitly passed — re-writing pre-read
    # values would clobber a concurrent PATCH the status claim cannot see
    sets = ["status = ?", "completed_at = ?", "updated_at = ?"]
    params: list[Any] = [to_status, now if to_status == "done" else None, now]
    if owner is not None:
        sets.append("owner = ?")
        params.append(owner)
    if sort_order is not None:
        sets.append("sort_order = ?")
        params.append(sort_order)
    params.extend([card_id, from_status])
    claim_sql = f"UPDATE roadmap_cards SET {', '.join(sets)} WHERE id = ? AND status = ?"
    # the review gate re-checks INSIDE the claim: the pre-check above is only a
    # friendly 400 — without this a session reopened between check and claim
    # would let the card land in review with zero eligible sessions (TOCTOU)
    review_gated = not override and to_status == "review" and from_status != "review"
    if review_gated:
        claim_sql += " AND " + _REVIEW_SESSION_EXISTS.format(card="roadmap_cards.id")
    claimed = await db.execute(claim_sql, params)
    if not claimed:
        # classify the failure from ONE snapshot (status and gate read in the
        # same statement, so they cannot drift apart between two queries):
        # status drift -> 409; stable status with a failing gate -> 400
        current = await db.query_one(
            "SELECT status, " + _REVIEW_SESSION_EXISTS.format(card="roadmap_cards.id")
            + " AS gate_ok FROM roadmap_cards WHERE id = ?",
            (card_id,),
        )
        if (
            review_gated and current is not None
            and current["status"] == from_status and not current["gate_ok"]
        ):
            raise RoadmapError(
                "cannot move to review without a completed coding session summary; "
                "set override to force"
            )
        raise MoveConflict(f"card {card_id} changed concurrently; reload and retry")
    await _record_event(
        "card.moved", card_id,
        {"from": from_status, "to": to_status, "override": bool(override), "reason": reason},
    )
    return await get_card(card_id)


# ---- card creation + claim (02-data-model.md, card M7-008) -------------------

CREATE_STATUSES = {"inbox", "ready"}


async def create_card(data: dict[str, Any]) -> dict[str, Any]:
    """Create one card through the API (same contract as a seed card).

    Ready-at-birth requires a non-empty acceptance list — the same gate
    move() enforces. Dependencies must reference existing cards.
    """
    # reserved-char check runs pre-strip (str.strip eats \x1f as whitespace)
    cid = _reject_reserved(str(data.get("id") or ""), "card id").strip()
    title = str(data.get("title") or "").strip()
    if not cid or not title:
        raise RoadmapError("a card needs an id and a title")
    status = data.get("status", "inbox")
    _validate_enum(status, CREATE_STATUSES, "creation status")
    _validate_enum(data.get("type", "feature"), TYPES, "type")
    _validate_enum(data.get("priority", "P2"), PRIORITIES, "priority")
    _validate_enum(data.get("risk", "medium"), RISKS, "risk")
    for field in _SEED_TEXT_FIELDS:
        if field in data and not isinstance(data[field], str):
            raise RoadmapError(f"{field} must be a string")
    acceptance = _require_str_list(data.get("acceptance"), "acceptance")
    dependencies = _require_str_list(data.get("dependencies"), "dependencies")
    for field in _CARD_JSON_FIELDS:
        _require_str_list(data.get(field), field)
    if status == "ready" and not [a for a in acceptance if a.strip()]:
        raise RoadmapError("cannot create a ready card with an empty acceptance checklist")

    now = bus.now_iso()
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT id FROM roadmap_cards WHERE id = ?", (cid,))
        if await cur.fetchone():
            await cur.close()
            raise RoadmapError(f"card {cid!r} already exists")
        await cur.close()
        for dep in dependencies:
            cur = await conn.execute("SELECT id FROM roadmap_cards WHERE id = ?", (dep,))
            found = await cur.fetchone()
            await cur.close()
            if found is None:
                raise RoadmapError(f"card {cid} depends on unknown card {dep!r}")
        cur = await conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS next FROM roadmap_cards")
        next_sort = (await cur.fetchone())["next"]
        await cur.close()
        # an explicit null sort_order means "auto" — float(None) is a 500
        sort_val = data.get("sort_order")
        sort_order = float(next_sort) if sort_val is None else _require_number(sort_val, "sort_order")
        await conn.execute(
            "INSERT INTO roadmap_cards (id, title, type, phase, priority, risk, summary, problem, "
            "implementation, agent_prompt, design_links_json, expected_files_json, verification_json, "
            "tags_json, status, owner, sort_order, created_at, updated_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (cid, title, data.get("type", "feature"), data.get("phase", ""),
             data.get("priority", "P2"), data.get("risk", "medium"), data.get("summary", ""),
             data.get("problem", ""), data.get("implementation", ""), data.get("agent_prompt", ""),
             _dumps(data.get("design_links")), _dumps(data.get("expected_files")),
             _dumps(data.get("verification")), _dumps(data.get("tags")),
             status, data.get("owner"), sort_order, now, now),
        )
        for j, text in enumerate(t for t in acceptance if t.strip()):
            await conn.execute(
                "INSERT OR IGNORE INTO roadmap_checklists (id, card_id, kind, text, checked, sort_order, "
                "created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                (_det_id(cid, "acceptance", text), cid, "acceptance", text, (j + 1) * 10.0, now, now),
            )
        for dep in dependencies:
            await conn.execute(
                "INSERT OR IGNORE INTO roadmap_dependencies (id, card_id, depends_on_id, relation, "
                "source, created_at) VALUES (?,?,?,?,'manual',?)",
                (_det_id(cid, "dep", dep, "blocks"), cid, dep, "blocks", now),
            )
    await _record_event("card.created", cid, {"status": status, "title": title})
    return await get_card(cid)  # type: ignore[return-value]


async def claim_card(
    card_id: str, owner: str, *, expected_status: str | None = None
) -> dict[str, Any] | None:
    """Atomically take ownership and start work (inbox/ready -> in_progress).

    The claimed-FROM status is bound INTO the UPDATE (the caller's
    ``expected_status``, else the pre-read status), so the optimistic
    concurrency contract holds under drift and the audit event's ``from`` is
    exactly the status the claim verified. Blank blockers count as unblocked,
    matching move().
    """
    owner = str(owner or "").strip()
    if not owner:
        raise RoadmapError("claim needs an owner")
    card = await db.query_one("SELECT * FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    from_status = expected_status if expected_status is not None else card["status"]
    if from_status not in ("inbox", "ready"):
        raise MoveConflict(
            f"card {card_id} cannot be claimed from {from_status!r} (needs inbox/ready)"
        )

    now = bus.now_iso()
    claimed = await db.execute(
        "UPDATE roadmap_cards SET status = 'in_progress', owner = ?, updated_at = ? "
        "WHERE id = ? AND status = ? AND (owner IS NULL OR owner = '' OR owner = ?) "
        "AND (blocked_reason IS NULL OR blocked_reason = '')",
        (owner, now, card_id, from_status, owner),
    )
    if not claimed:
        current = await db.query_one(
            "SELECT status, owner, blocked_reason FROM roadmap_cards WHERE id = ?", (card_id,)
        )
        if (
            current is not None and current["blocked_reason"]
            and current["status"] in ("inbox", "ready")
        ):
            raise RoadmapError(
                f"card {card_id} is blocked ({current['blocked_reason']}); resolve or override via move"
            )
        status_now = current["status"] if current else "missing"
        owner_now = current["owner"] if current else None
        raise MoveConflict(
            f"card {card_id} cannot be claimed (status {status_now!r}, owner {owner_now!r})"
        )
    await _record_event("card.claimed", card_id, {"owner": owner, "from": from_status})
    return await get_card(card_id)


# ---- checklists ----------------------------------------------------------------

async def add_checklist_item(card_id: str, kind: str, text: str) -> dict[str, Any] | None:
    _validate_enum(kind, CHECKLIST_KINDS, "checklist kind")
    text = str(text or "").strip()
    if not text:
        raise RoadmapError("a checklist item needs text")
    card = await db.query_one("SELECT id FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    now = bus.now_iso()
    item_id = _det_id(card_id, kind, text)  # same id scheme as import -> merges, never duplicates
    inserted = await db.execute(
        "INSERT OR IGNORE INTO roadmap_checklists (id, card_id, kind, text, checked, sort_order, "
        "created_at, updated_at) SELECT ?,?,?,?,0,COALESCE(MAX(sort_order),0)+10,?,? "
        "FROM roadmap_checklists WHERE card_id = ? AND kind = ?",
        (item_id, card_id, kind, text, now, now, card_id, kind),
    )
    if not inserted:
        raise RoadmapError(f"checklist item already exists on {card_id}: {text!r}")
    await _record_event("checklist.added", card_id, {"item_id": item_id, "kind": kind})
    return await db.query_one("SELECT * FROM roadmap_checklists WHERE id = ?", (item_id,))


async def set_checklist_item(
    item_id: str, *, checked: bool | None = None, text: str | None = None
) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM roadmap_checklists WHERE id = ?", (item_id,))
    if row is None:
        return None
    if checked is None and text is None:
        return row
    if text is not None and not str(text).strip():
        raise RoadmapError("a checklist item needs text")
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [bus.now_iso()]
    if checked is not None:
        sets.append("checked = ?")
        params.append(1 if checked else 0)
    if text is not None:
        sets.append("text = ?")
        params.append(str(text).strip())
    params.append(item_id)
    try:
        await db.execute(f"UPDATE roadmap_checklists SET {', '.join(sets)} WHERE id = ?", params)
    except sqlite3.IntegrityError as exc:
        # renaming onto a text that already exists on the same card+kind
        # violates the merge index — that is operator input, not a crash
        raise RoadmapError(
            f"checklist item already exists on {row['card_id']}: {str(text).strip()!r}"
        ) from exc
    if checked is not None:
        await _record_event(
            "checklist.checked", row["card_id"], {"item_id": item_id, "checked": bool(checked)}
        )
    return await db.query_one("SELECT * FROM roadmap_checklists WHERE id = ?", (item_id,))


async def remove_checklist_item(item_id: str) -> bool:
    row = await db.query_one("SELECT card_id FROM roadmap_checklists WHERE id = ?", (item_id,))
    if row is None:
        return False
    await db.execute("DELETE FROM roadmap_checklists WHERE id = ?", (item_id,))
    await _record_event("checklist.removed", row["card_id"], {"item_id": item_id})
    return True


# ---- dependencies ----------------------------------------------------------------

async def add_dependency(
    card_id: str, depends_on_id: str, relation: str = "blocks"
) -> dict[str, Any] | None:
    # reserved-char check runs pre-strip (str.strip eats \x1f as whitespace)
    relation = _reject_reserved(str(relation or "blocks"), "dependency relation").strip() or "blocks"
    if card_id == depends_on_id:
        raise RoadmapError("a card cannot depend on itself")
    card = await db.query_one("SELECT id FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None

    dep_id = _det_id(card_id, "dep", depends_on_id, relation)
    now = bus.now_iso()
    # the cycle walk and the INSERT share one write transaction: two claims
    # racing each other (A->B and B->A) both passing a pre-transaction walk is
    # exactly how a cycle would slip in
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT id FROM roadmap_cards WHERE id = ?", (depends_on_id,))
        target = await cur.fetchone()
        await cur.close()
        if target is None:
            raise RoadmapError(f"unknown card {depends_on_id!r}")
        # reject dependency cycles: with a cycle neither card could ever reach
        # done without an override (walk the transitive 'depends on' closure)
        seen = {card_id}
        frontier = [depends_on_id]
        while frontier:
            cur = await conn.execute(
                "SELECT depends_on_id FROM roadmap_dependencies WHERE card_id IN "
                f"({','.join('?' for _ in frontier)})",
                frontier,
            )
            rows = await cur.fetchall()
            await cur.close()
            frontier = []
            for r in rows:
                dep = r["depends_on_id"]
                if dep == card_id:
                    raise RoadmapError(f"dependency would create a cycle via {depends_on_id!r}")
                if dep not in seen:
                    seen.add(dep)
                    frontier.append(dep)
        cur = await conn.execute(
            "INSERT OR IGNORE INTO roadmap_dependencies (id, card_id, depends_on_id, relation, source, "
            "created_at) VALUES (?,?,?,?,'manual',?)",
            (dep_id, card_id, depends_on_id, relation, now),
        )
        inserted = cur.rowcount
        await cur.close()
        if not inserted:
            raise RoadmapError(f"{card_id} already depends on {depends_on_id}")
    await _record_event(
        "dependency.added", card_id, {"depends_on_id": depends_on_id, "relation": relation}
    )
    return await db.query_one("SELECT * FROM roadmap_dependencies WHERE id = ?", (dep_id,))


async def remove_dependency(dep_id: str) -> bool:
    row = await db.query_one("SELECT card_id, depends_on_id FROM roadmap_dependencies WHERE id = ?", (dep_id,))
    if row is None:
        return False
    await db.execute("DELETE FROM roadmap_dependencies WHERE id = ?", (dep_id,))
    await _record_event(
        "dependency.removed", row["card_id"], {"depends_on_id": row["depends_on_id"], "dep_id": dep_id}
    )
    return True


# ---- decisions ---------------------------------------------------------------------

async def open_decision(
    title: str, question: str, *, card_id: str | None = None, options: list[str] | None = None
) -> dict[str, Any]:
    title = str(title or "").strip()
    question = str(question or "").strip()
    if not title or not question:
        raise RoadmapError("a decision needs a title and a question")
    options = _require_str_list(options, "options")
    if card_id is not None:
        card = await db.query_one("SELECT id FROM roadmap_cards WHERE id = ?", (card_id,))
        if card is None:
            raise RoadmapError(f"unknown card {card_id!r}")
    did = _new_id()
    await db.execute(
        "INSERT INTO roadmap_decisions (id, card_id, title, question, options_json, decision, status, "
        "created_at, resolved_at) VALUES (?,?,?,?,?,NULL,'open',?,NULL)",
        (did, card_id, title, question, _dumps(options), bus.now_iso()),
    )
    await _record_event("decision.opened", card_id, {"decision_id": did, "title": title})
    return await get_decision(did)  # type: ignore[return-value]


async def get_decision(decision_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM roadmap_decisions WHERE id = ?", (decision_id,))
    if row is None:
        return None
    decision = dict(row)
    decision["options"] = _loads_list(decision.pop("options_json", None))
    return decision


async def list_decisions(
    card_id: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if card_id:
        clauses.append("card_id = ?")
        params.append(card_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM roadmap_decisions"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    out = []
    for r in await db.query(sql, params):
        decision = dict(r)
        decision["options"] = _loads_list(decision.pop("options_json", None))
        out.append(decision)
    return out


async def resolve_decision(decision_id: str, decision: str) -> dict[str, Any] | None:
    decision = str(decision or "").strip()
    if not decision:
        raise RoadmapError("resolving a decision needs the decision text")
    row = await db.query_one("SELECT * FROM roadmap_decisions WHERE id = ?", (decision_id,))
    if row is None:
        return None
    # conditional claim: a decision resolves exactly once
    claimed = await db.execute(
        "UPDATE roadmap_decisions SET decision = ?, status = 'resolved', resolved_at = ? "
        "WHERE id = ? AND status = 'open'",
        (decision, bus.now_iso(), decision_id),
    )
    if not claimed:
        raise MoveConflict(f"decision {decision_id} is not open; cannot resolve it")
    await _record_event(
        "decision.resolved", row["card_id"], {"decision_id": decision_id, "decision": decision}
    )
    return await get_decision(decision_id)


# ---- agent prompt (card M7-007, template: roadmap/06-agent-protocol.md) ---------

_PROMPT_CONSTRAINTS = (
    "- keep changes scoped to this card;",
    "- do not introduce hosted infrastructure;",
    "- do not push;",
    "- preserve user changes;",
    "- add or update tests according to risk;",
    "- record follow-up work as roadmap cards rather than silently expanding scope.",
)


def _prompt_section(title: str, rows: list[str]) -> list[str]:
    lines = [f"{title}:"]
    lines.extend(rows if rows else ["- none"])
    lines.append("")
    return lines


async def generate_agent_prompt(card_id: str) -> dict[str, Any] | None:
    """Deterministic coding-agent prompt from the card's CURRENT state.

    No timestamps, no runtime data: the same card state always renders the
    same string. A non-empty ``agent_prompt`` card field is an operator
    override and is returned verbatim.
    """
    card = await get_card(card_id)
    if card is None:
        return None
    if str(card["agent_prompt"] or "").strip():
        return {"card_id": card_id, "prompt": card["agent_prompt"], "generated": False}

    acceptance = [c["text"] for c in card["checklists"] if c["kind"] == "acceptance"]
    deps = [d["depends_on_id"] for d in card["dependencies"]]
    lines: list[str] = [
        f"You are implementing roadmap card {card['id']}: {card['title']}.",
        "",
        f"Phase: {card['phase'] or 'none'}",
        f"Type: {card['type']} · Priority: {card['priority']} · Risk: {card['risk']}",
        "",
    ]
    if card["summary"]:
        lines.extend([f"Summary: {card['summary']}", ""])
    if card["problem"]:
        lines.extend([f"Problem: {card['problem']}", ""])
    lines.extend(_prompt_section("Design links", [f"- {d}" for d in card["design_links"]]))
    lines.extend(_prompt_section("Expected files", [f"- {f}" for f in card["expected_files"]]))
    lines.extend(_prompt_section("Dependencies", [f"- {d}" for d in deps]))
    lines.extend(_prompt_section("Acceptance criteria", [f"- {a}" for a in acceptance]))
    lines.extend(_prompt_section("Verification", [f"- {v}" for v in card["verification"]]))
    lines.extend(["Constraints:", *_PROMPT_CONSTRAINTS, ""])
    lines.append("Implement the card, run verification, and summarize changed files and results.")
    return {"card_id": card_id, "prompt": "\n".join(lines), "generated": True}


# ---- export ---------------------------------------------------------------------------

async def export_backlog() -> dict[str, Any]:
    """Backlog-compatible JSON snapshot of the live rows (GET /export).

    A restore through import_backlog() is state-faithful, not just seed-shaped:
    ``dependencies_meta`` preserves edge relation + ownership (a manual edge
    stays manual, so later seed reconciles cannot delete it), and
    ``blocked_reason`` / ``acceptance_checked`` / ``checklists_extra`` carry
    the operator's process state. Plain seed consumers can ignore all four.
    """
    cards = await db.query("SELECT * FROM roadmap_cards ORDER BY sort_order, id")
    checklists = await db.query(
        "SELECT card_id, kind, text, checked FROM roadmap_checklists ORDER BY kind, sort_order, rowid"
    )
    deps = await db.query(
        "SELECT card_id, depends_on_id, relation, source FROM roadmap_dependencies ORDER BY depends_on_id"
    )
    acceptance: dict[str, list[dict[str, Any]]] = {}
    extra_checklists: dict[str, list[dict[str, Any]]] = {}
    for c in checklists:
        bucket = acceptance if c["kind"] == "acceptance" else extra_checklists
        bucket.setdefault(c["card_id"], []).append(c)
    dependencies: dict[str, list[dict[str, Any]]] = {}
    for d in deps:
        dependencies.setdefault(d["card_id"], []).append(d)

    phases: list[str] = []
    out_cards = []
    for row in cards:
        card = _card_out(row)
        if card["phase"] and card["phase"] not in phases:
            phases.append(card["phase"])
        card_deps = dependencies.get(card["id"], [])
        card_acceptance = acceptance.get(card["id"], [])
        exported = {
            "id": card["id"],
            "title": card["title"],
            "type": card["type"],
            "phase": card["phase"],
            "status": card["status"],
            "priority": card["priority"],
            "risk": card["risk"],
            "summary": card["summary"],
            "design_links": card["design_links"],
            "expected_files": card["expected_files"],
            "dependencies": [d["depends_on_id"] for d in card_deps],
            "acceptance": [a["text"] for a in card_acceptance],
            "verification": card["verification"],
            "sort_order": card["sort_order"],
        }
        if any(d["relation"] != "blocks" or d["source"] != "import" for d in card_deps):
            exported["dependencies_meta"] = [
                {"depends_on_id": d["depends_on_id"], "relation": d["relation"], "source": d["source"]}
                for d in card_deps
            ]
        checked = [a["text"] for a in card_acceptance if a["checked"]]
        if checked:
            exported["acceptance_checked"] = checked
        extras = extra_checklists.get(card["id"], [])
        if extras:
            exported["checklists_extra"] = [
                {"kind": e["kind"], "text": e["text"], "checked": bool(e["checked"])} for e in extras
            ]
        for optional in ("problem", "implementation", "agent_prompt"):
            if card[optional]:
                exported[optional] = card[optional]
        if card["owner"]:
            exported["owner"] = card["owner"]
        if card["blocked_reason"]:
            exported["blocked_reason"] = card["blocked_reason"]
        if card["tags"]:
            exported["tags"] = card["tags"]
        out_cards.append(exported)
    return {"version": 1, "columns": list(STATUSES), "phases": phases, "cards": out_cards}


# ---- evidence ---------------------------------------------------------------

async def add_evidence(
    card_id: str,
    kind: str,
    title: str,
    body: str = "",
    status: str = "info",
    artifact_ref: str | None = None,
) -> dict[str, Any] | None:
    _validate_enum(kind, EVIDENCE_KINDS, "evidence kind")
    _validate_enum(status, EVIDENCE_STATUSES, "evidence status")
    if not str(title).strip():
        raise RoadmapError("evidence needs a title")
    card = await db.query_one("SELECT id FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    eid = _new_id()
    await db.execute(
        "INSERT INTO roadmap_evidence (id, card_id, kind, title, body, status, artifact_ref, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (eid, card_id, kind, title.strip(), body, status, artifact_ref, bus.now_iso()),
    )
    await _record_event("evidence.added", card_id, {"evidence_id": eid, "kind": kind, "status": status})
    return await db.query_one("SELECT * FROM roadmap_evidence WHERE id = ?", (eid,))


# ---- coding sessions ---------------------------------------------------------

async def create_session(
    card_id: str,
    actor: str,
    goal: str,
    planned_files: list[str] | None = None,
) -> dict[str, Any] | None:
    if not str(actor).strip() or not str(goal).strip():
        raise RoadmapError("a coding session needs an actor and a goal")
    planned_files = _require_str_list(planned_files, "planned_files")
    card = await db.query_one("SELECT id FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    sid = _new_id()
    await db.execute(
        "INSERT INTO roadmap_coding_sessions (id, card_id, actor, goal, status, planned_files_json, "
        "touched_files_json, summary, started_at) VALUES (?,?,?,?,'active',?,'[]','',?)",
        (sid, card_id, actor.strip(), goal.strip(), _dumps(planned_files), bus.now_iso()),
    )
    await _record_event("session.started", card_id, {"session_id": sid, "actor": actor.strip(), "goal": goal.strip()})
    return await get_session(sid)


async def get_session(session_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM roadmap_coding_sessions WHERE id = ?", (session_id,))
    if row is None:
        return None
    sess = _session_out(row)
    sess["commands"] = await db.query(
        "SELECT * FROM roadmap_session_commands WHERE session_id = ? ORDER BY rowid", (session_id,)
    )
    return sess


_SESSION_UPDATABLE = {"status", "goal", "summary", "planned_files", "touched_files"}


async def update_session(session_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM roadmap_coding_sessions WHERE id = ?", (session_id,))
    if row is None:
        return None
    unknown = set(fields) - _SESSION_UPDATABLE
    if unknown:
        raise RoadmapError(f"unknown session fields: {', '.join(sorted(unknown))}")
    to_status = fields.get("status")
    if to_status is not None:
        _validate_enum(to_status, SESSION_STATUSES, "session status")
    if "goal" in fields and (not isinstance(fields["goal"], str) or not fields["goal"].strip()):
        raise RoadmapError("session goal must be a non-empty string")
    if "summary" in fields and not isinstance(fields["summary"], str):
        raise RoadmapError("session summary must be a string")
    # invariant: a completed/partial/blocked session always carries a non-blank
    # summary — both when entering that state and on any later summary edit
    # (otherwise a summary-only PATCH could blank it and starve the review gate)
    effective_status = to_status if to_status is not None else row["status"]
    if effective_status in _SUMMARY_REQUIRED_STATUSES and (to_status is not None or "summary" in fields):
        summary = fields.get("summary", row["summary"])
        if not isinstance(summary, str) or not summary.strip():
            raise RoadmapError(f"a {effective_status} coding session needs a summary")

    sets, params = [], []
    for key, val in fields.items():
        if key == "status":
            continue
        if key in ("planned_files", "touched_files"):
            sets.append(f"{key}_json = ?")
            params.append(_dumps(_require_str_list(val, key)))
        else:
            sets.append(f"{key} = ?")
            # summaries are stored normalized so '' is the only blank value
            params.append(val.strip() if key == "summary" and isinstance(val, str) else val)

    now = bus.now_iso()
    if to_status in SESSION_TERMINAL:
        # conditional claim: only an active session can finish, exactly once
        sets.extend(["status = ?", "finished_at = ?"])
        params.extend([to_status, now])
        params.append(session_id)
        claim = f"UPDATE roadmap_coding_sessions SET {', '.join(sets)} WHERE id = ? AND status = 'active'"
        # the summary invariant rides IN the claim when this request does not
        # write one itself: the pre-read summary may have been blanked by a
        # concurrent PATCH after validation (TOCTOU)
        needs_db_summary = to_status in _SUMMARY_REQUIRED_STATUSES and "summary" not in fields
        if needs_db_summary:
            claim += " AND " + _NONBLANK_SUMMARY_SQL.format(col="summary")
        claimed = await db.execute(claim, params)
        if not claimed:
            # classify from ONE snapshot: blank summary on a still-active row
            # is a validation error, anything else is a lost claim
            cur = await db.query_one(
                "SELECT status, "
                + _NONBLANK_SUMMARY_SQL.format(col="summary")
                + " AS has_summary FROM roadmap_coding_sessions WHERE id = ?",
                (session_id,),
            )
            if (
                needs_db_summary and cur is not None
                and cur["status"] == "active" and not cur["has_summary"]
            ):
                raise RoadmapError(f"a {to_status} coding session needs a summary")
            raise MoveConflict(f"session {session_id} is not active; cannot finish it")
        await _record_event(
            "session.completed", row["card_id"], {"session_id": session_id, "status": to_status}
        )
    elif to_status == "active" and row["status"] != "active":
        # reopen: conditional claim on the terminal status we validated against
        sets.extend(["status = 'active'", "finished_at = NULL"])
        params.extend([session_id, row["status"]])
        claimed = await db.execute(
            f"UPDATE roadmap_coding_sessions SET {', '.join(sets)} WHERE id = ? AND status = ?",
            params,
        )
        if not claimed:
            raise MoveConflict(f"session {session_id} changed concurrently; reload and retry")
        await _record_event(
            "session.reopened", row["card_id"], {"session_id": session_id, "from": row["status"]}
        )
    elif sets:
        # plain-field writes claim the status they were validated against: a
        # blank summary validated while 'active' must not land after the
        # session concurrently completed (it would starve the review gate)
        params.extend([session_id, row["status"]])
        claimed = await db.execute(
            f"UPDATE roadmap_coding_sessions SET {', '.join(sets)} WHERE id = ? AND status = ?", params
        )
        if not claimed:
            raise MoveConflict(f"session {session_id} changed concurrently; reload and retry")
    return await get_session(session_id)


async def append_command(
    session_id: str,
    command_label: str,
    command_text: str,
    exit_code: int | None = None,
    output_excerpt: str | None = None,
    attach_as_evidence: bool = False,
) -> dict[str, Any] | None:
    if not str(command_label).strip() or not str(command_text).strip():
        raise RoadmapError("a session command needs a label and the command text")
    sess = await db.query_one(
        "SELECT id, card_id FROM roadmap_coding_sessions WHERE id = ?", (session_id,)
    )
    if sess is None:
        return None
    cid = _new_id()
    eid = _new_id() if attach_as_evidence else None
    created_at = bus.now_iso()
    payload = {"evidence_id": eid, "kind": "command", "session_id": session_id, "command_id": cid}
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO roadmap_session_commands (id, session_id, command_label, command_text, exit_code, "
            "output_excerpt, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                cid, session_id, command_label.strip(), command_text.strip(),
                exit_code, output_excerpt, created_at,
            ),
        )
        if eid is not None:
            evidence_status = "info" if exit_code is None else ("pass" if exit_code == 0 else "fail")
            body = f"$ {command_text.strip()}"
            if output_excerpt:
                body += f"\n\n{output_excerpt}"
            await conn.execute(
                "INSERT INTO roadmap_evidence "
                "(id, card_id, kind, title, body, status, artifact_ref, created_at) "
                "VALUES (?,?,'command',?,?,?,?,?)",
                (
                    eid, sess["card_id"], command_label.strip(), body, evidence_status,
                    f"roadmap-session:{session_id}/command:{cid}", created_at,
                ),
            )
            # the audit row commits WITH the business rows: a post-commit
            # failure must not surface as an error (the client would retry
            # and duplicate the command + evidence)
            await conn.execute(
                "INSERT INTO roadmap_events (id, card_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (_new_id(), sess["card_id"], "evidence.added",
                 json.dumps(payload, ensure_ascii=False), created_at),
            )
    if eid is not None:
        try:  # bus mirror is observability, not truth — never fail a committed write
            await bus.emit("roadmap.evidence.added", "roadmap_card", sess["card_id"], payload)
        except Exception:  # noqa: BLE001 - CancelledError still propagates (rows stay committed)
            log.exception("bus mirror failed for committed evidence %s", eid)
    # build the response from the values just committed instead of re-reading:
    # no awaits remain after the commit except the best-effort mirror above
    # (NB: like every POST here, a client that cancels mid-response and blindly
    # retries will duplicate the command — retries need operator judgment)
    return {
        "id": cid, "session_id": session_id, "command_label": command_label.strip(),
        "command_text": command_text.strip(), "exit_code": exit_code,
        "output_excerpt": output_excerpt, "created_at": created_at, "evidence_id": eid,
    }


async def list_sessions(
    card_id: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    sql = (
        "SELECT s.*, (SELECT COUNT(*) FROM roadmap_session_commands c WHERE c.session_id = s.id) AS n_commands "
        "FROM roadmap_coding_sessions s"
    )
    clauses, params = [], []
    if card_id:
        clauses.append("s.card_id = ?")
        params.append(card_id)
    if status:
        clauses.append("s.status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY s.started_at DESC, s.rowid DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return [_session_out(r) for r in await db.query(sql, params)]


# ---- release gates -----------------------------------------------------------

async def release_gates() -> list[dict[str, Any]]:
    """Gate progress computed from card phases (rows are truth, gates are a projection).

    ``evidence_ready`` counts the not-yet-done scoped cards that already carry
    at least one piece of evidence — readiness is a function of card status
    AND evidence (05-global-coding-process.md), not status alone.
    """
    cards = await db.query("SELECT id, phase, status FROM roadmap_cards")
    with_evidence = {
        r["card_id"] for r in await db.query("SELECT DISTINCT card_id FROM roadmap_evidence")
    }
    gates = []
    for name, description, prefixes in RELEASE_GATES:
        scoped = [c for c in cards if _phase_token(c["phase"]) in prefixes]
        done = [c for c in scoped if c["status"] == "done"]
        remaining = [c for c in scoped if c["status"] != "done"]
        total = len(scoped)
        gates.append({
            "name": name,
            "description": description,
            "prefixes": list(prefixes),
            "total": total,
            "done": len(done),
            "pct": round(100 * len(done) / total) if total else 0,
            "status": "met" if total and len(done) == total else "open",
            "remaining": sorted(c["id"] for c in remaining),
            "evidence_ready": sorted(c["id"] for c in remaining if c["id"] in with_evidence),
        })
    return gates
