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
import uuid
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings

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
        return float(val)
    except (TypeError, ValueError):
        raise RoadmapError(f"{label} must be a number") from None


def _like_escape(text: str) -> str:
    r"""Escape LIKE wildcards in user input (pair with ``ESCAPE '\'``)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _phase_token(phase: str | None) -> str:
    """Leading milestone token of a phase ("M7 Roadmap Control Plane" -> "M7")."""
    return (phase or "").split(" ")[0]


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
        cid = str(c.get("id") or "").strip()
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
        if c.get("owner") is not None and not isinstance(c["owner"], str):
            raise RoadmapError(f"card {cid}: owner must be a string")
        if "sort_order" in c:
            _require_number(c["sort_order"], f"card {cid}: sort_order")
        for field in ("acceptance", "dependencies", *_CARD_JSON_FIELDS):
            _require_str_list(c.get(field), f"card {cid}: {field}")
    known = existing_ids | seed_ids
    for c in cards:
        for dep in c.get("dependencies") or []:
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
                    "tags_json, status, owner, sort_order, created_at, updated_at, completed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, *fields, status, c.get("owner"), float(c.get("sort_order", (i + 1) * 10)),
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
            # reconcile: import is the only dependency writer, so deps dropped from
            # the seed are deleted (a stale dep would block move-to-done forever)
            deps = c.get("dependencies") or []
            if deps:
                marks = ",".join("?" for _ in deps)
                await conn.execute(
                    f"DELETE FROM roadmap_dependencies WHERE card_id = ? AND depends_on_id NOT IN ({marks})",
                    (cid, *deps),
                )
            else:
                await conn.execute("DELETE FROM roadmap_dependencies WHERE card_id = ?", (cid,))
            for dep in deps:
                await conn.execute(
                    "INSERT OR IGNORE INTO roadmap_dependencies (id, card_id, depends_on_id, relation, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (_det_id(cid, "dep", dep, "blocks"), cid, dep, "blocks", now),
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
    claimed = await db.execute(
        f"UPDATE roadmap_cards SET {', '.join(sets)} WHERE id = ? AND status = ?", params
    )
    if not claimed:
        raise MoveConflict(f"card {card_id} changed concurrently; reload and retry")
    await _record_event(
        "card.moved", card_id,
        {"from": from_status, "to": to_status, "override": bool(override), "reason": reason},
    )
    return await get_card(card_id)


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

    sets, params = [], []
    for key, val in fields.items():
        if key == "status":
            continue
        if key in ("planned_files", "touched_files"):
            sets.append(f"{key}_json = ?")
            params.append(_dumps(_require_str_list(val, key)))
        else:
            sets.append(f"{key} = ?")
            params.append(val)

    now = bus.now_iso()
    if to_status in SESSION_TERMINAL:
        # conditional claim: only an active session can finish, exactly once
        sets.extend(["status = ?", "finished_at = ?"])
        params.extend([to_status, now])
        params.append(session_id)
        claimed = await db.execute(
            f"UPDATE roadmap_coding_sessions SET {', '.join(sets)} WHERE id = ? AND status = 'active'",
            params,
        )
        if not claimed:
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
        params.append(session_id)
        await db.execute(
            f"UPDATE roadmap_coding_sessions SET {', '.join(sets)} WHERE id = ?", params
        )
    return await get_session(session_id)


async def append_command(
    session_id: str,
    command_label: str,
    command_text: str,
    exit_code: int | None = None,
    output_excerpt: str | None = None,
) -> dict[str, Any] | None:
    if not str(command_label).strip() or not str(command_text).strip():
        raise RoadmapError("a session command needs a label and the command text")
    sess = await db.query_one("SELECT id FROM roadmap_coding_sessions WHERE id = ?", (session_id,))
    if sess is None:
        return None
    cid = _new_id()
    await db.execute(
        "INSERT INTO roadmap_session_commands (id, session_id, command_label, command_text, exit_code, "
        "output_excerpt, created_at) VALUES (?,?,?,?,?,?,?)",
        (cid, session_id, command_label.strip(), command_text.strip(), exit_code, output_excerpt, bus.now_iso()),
    )
    return await db.query_one("SELECT * FROM roadmap_session_commands WHERE id = ?", (cid,))


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
    """Gate progress computed from card phases (rows are truth, gates are a projection)."""
    cards = await db.query("SELECT id, phase, status FROM roadmap_cards")
    gates = []
    for name, description, prefixes in RELEASE_GATES:
        scoped = [c for c in cards if _phase_token(c["phase"]) in prefixes]
        done = [c for c in scoped if c["status"] == "done"]
        total = len(scoped)
        gates.append({
            "name": name,
            "description": description,
            "prefixes": list(prefixes),
            "total": total,
            "done": len(done),
            "pct": round(100 * len(done) / total) if total else 0,
            "status": "met" if total and len(done) == total else "open",
            "remaining": sorted(c["id"] for c in scoped if c["status"] != "done"),
        })
    return gates
