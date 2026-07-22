"""Roadmap control plane — durable backend for the global coding process.

Cards, checklists, dependencies, evidence, coding sessions, and decisions live
in SQLite (rows are truth). ``roadmap/backlog.json`` is a seed/import/export
artifact: ``import_backlog()`` upserts by card id, merges checklist items by
text, reconciles dependencies to the seed, and preserves local status unless
``force`` is set; ``export_backlog()`` produces a seed-compatible snapshot.
Contract: roadmap/02-data-model.md.

Every move is a conditional-claim transition (``UPDATE … WHERE status = <the
status we validated against>``) so concurrent movers can never double-apply.
User-visible changes append to ``roadmap_events`` and emit namespaced
``roadmap.<event>`` bus events (e.g. ``roadmap.card.moved``).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
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
DECISION_STATUSES = {"open", "resolved"}
# new cards start at the top of the funnel; anything further requires move() gates
CREATE_STATUSES = {"inbox", "ready"}

# gate scopes match the Obsidian plugin (obsidian-plugin/src/roadmap.ts renderGates)
RELEASE_GATES = (
    ("Release A", "Thesis Registry + Forecastable Research", ("M0", "M1", "M2", "M3")),
    ("Release B", "Market Data + Forecast Ledger", ("M4", "M5", "M6")),
    ("Release C", "Roadmap Control Plane", ("M7",)),
    ("Release D", "Post-Audit Hardening", ("M8",)),
    ("Release E", "North Star R1", ("M9",)),
    ("Release F", "Bounded-Autonomy Loop", ("M10",)),
)

NEW_CARD_STATUS_POLICIES = {"seed", "inbox"}

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


class IdempotencyConflict(MoveConflict):
    """An idempotency key was reused with a different request (HTTP 409)."""


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


def _decision_out(row: dict[str, Any]) -> dict[str, Any]:
    dec = dict(row)
    dec["options"] = _loads_list(dec.pop("options_json", None))
    return dec


async def _record_event(event_type: str, card_id: str | None, payload: dict[str, Any]) -> None:
    """Append to roadmap_events and mirror onto the bus as roadmap.<event_type>."""
    await db.execute(
        "INSERT INTO roadmap_events (id, card_id, event_type, payload_json, created_at) VALUES (?,?,?,?,?)",
        (_new_id(), card_id, event_type, json.dumps(payload, ensure_ascii=False), bus.now_iso()),
    )
    await bus.emit(f"roadmap.{event_type}", "roadmap_card" if card_id else "roadmap", card_id or "", payload)


# ---- retry-safe create mutations --------------------------------------------

_IDEMPOTENCY_KEY_MAX_LENGTH = 200


def _normalize_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    normalized = str(key).strip()
    if not normalized:
        raise RoadmapError("idempotency key must not be empty")
    if len(normalized) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise RoadmapError(
            f"idempotency key must be at most {_IDEMPOTENCY_KEY_MAX_LENGTH} characters"
        )
    return normalized


def _request_hash(payload: dict[str, Any]) -> str:
    """Hash a semantic, normalized request body deterministically."""
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RoadmapError(f"idempotent request is not JSON-serializable: {exc}") from None
    return hashlib.sha256(encoded).hexdigest()


async def _conn_rows(conn: Any, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(row) for row in rows]


async def _conn_one(conn: Any, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = await _conn_rows(conn, sql, params)
    return rows[0] if rows else None


async def _store_idempotency_result(
    conn: Any,
    scope: str,
    key: str,
    request_hash: str,
    result: dict[str, Any],
) -> None:
    await conn.execute(
        "INSERT INTO roadmap_idempotency_keys "
        "(scope, idempotency_key, request_hash, response_json, created_at) VALUES (?,?,?,?,?)",
        (
            scope,
            key,
            request_hash,
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            bus.now_iso(),
        ),
    )


async def _idempotent_create(
    scope: str,
    idempotency_key: str | None,
    request: dict[str, Any],
    mutation: Callable[[Any], Awaitable[tuple[dict[str, Any] | None, bool]]],
) -> tuple[dict[str, Any] | None, bool]:
    """Run ``mutation`` and its idempotency record in one DB transaction.

    The bool reports whether this call created the resource, allowing callers
    to suppress duplicate audit/bus events on replay (or naturally-idempotent
    dependency insertion). A replay returns the original serialized response,
    even if the live resource was later edited.
    """
    key = _normalize_idempotency_key(idempotency_key)
    fingerprint = _request_hash(request) if key is not None else None
    async with db.transaction() as conn:
        if key is not None:
            row = await _conn_one(
                conn,
                "SELECT request_hash, response_json FROM roadmap_idempotency_keys "
                "WHERE scope = ? AND idempotency_key = ?",
                (scope, key),
            )
            if row is not None:
                if row["request_hash"] != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {key!r} was already used for a different request"
                    )
                try:
                    response = json.loads(row["response_json"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"corrupt roadmap idempotency response for scope {scope!r}"
                    ) from exc
                if not isinstance(response, dict):
                    raise RuntimeError(
                        f"corrupt roadmap idempotency response for scope {scope!r}"
                    )
                return response, False

        result, created = await mutation(conn)
        if key is not None and result is not None:
            await _store_idempotency_result(conn, scope, key, fingerprint, result)
        return result, created


async def _card_from_conn(conn: Any, card_id: str) -> dict[str, Any] | None:
    row = await _conn_one(conn, "SELECT * FROM roadmap_cards WHERE id = ?", (card_id,))
    if row is None:
        return None
    card = _card_out(row)
    card["checklists"] = await _conn_rows(
        conn,
        "SELECT * FROM roadmap_checklists WHERE card_id = ? ORDER BY kind, sort_order, id",
        (card_id,),
    )
    card["dependencies"] = await _conn_rows(
        conn,
        "SELECT d.*, c.status AS depends_on_status FROM roadmap_dependencies d "
        "LEFT JOIN roadmap_cards c ON c.id = d.depends_on_id "
        "WHERE d.card_id = ? ORDER BY d.depends_on_id",
        (card_id,),
    )
    card["evidence"] = await _conn_rows(
        conn,
        "SELECT * FROM roadmap_evidence WHERE card_id = ? ORDER BY created_at, rowid",
        (card_id,),
    )
    card["sessions"] = [
        _session_out(session) for session in await _conn_rows(
            conn,
            "SELECT * FROM roadmap_coding_sessions WHERE card_id = ? "
            "ORDER BY started_at DESC, rowid DESC",
            (card_id,),
        )
    ]
    return card


async def _session_from_conn(conn: Any, session_id: str) -> dict[str, Any] | None:
    row = await _conn_one(
        conn, "SELECT * FROM roadmap_coding_sessions WHERE id = ?", (session_id,)
    )
    if row is None:
        return None
    session = _session_out(row)
    session["commands"] = await _conn_rows(
        conn,
        "SELECT * FROM roadmap_session_commands WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    )
    return session


# ---- seed import -----------------------------------------------------------

def default_backlog_path() -> Path:
    return get_settings().repo_root / "roadmap" / "backlog.json"


_SEED_TEXT_FIELDS = ("title", "phase", "summary", "problem", "implementation", "agent_prompt")
# stored-row columns matching the seed field tuple built in pass 1, in order
_SEED_ROW_COLUMNS = (
    "title", "type", "phase", "priority", "risk", "summary", "problem", "implementation",
    "agent_prompt", "design_links_json", "expected_files_json", "verification_json", "tags_json",
)

_SEED_FIELD_NAMES = (
    "title", "type", "phase", "priority", "risk", "summary", "problem", "implementation",
    "agent_prompt", "design_links", "expected_files", "verification", "tags",
)


def _seed_values(card: dict[str, Any]) -> tuple[Any, ...]:
    """Stored values controlled by the seed for an existing card."""
    return (
        str(card["title"]).strip(), card.get("type", "feature"), card.get("phase", ""),
        card.get("priority", "P2"), card.get("risk", "medium"), card.get("summary", ""),
        card.get("problem", ""), card.get("implementation", ""), card.get("agent_prompt", ""),
        _dumps(card.get("design_links")), _dumps(card.get("expected_files")),
        _dumps(card.get("verification")), _dumps(card.get("tags")),
    )


async def _import_state(conn: Any | None = None) -> tuple[list[dict[str, Any]], ...]:
    """Read the board snapshot used to build or apply one reconciliation plan."""
    statements = (
        "SELECT * FROM roadmap_cards ORDER BY id",
        "SELECT card_id, text, checked FROM roadmap_checklists "
        "WHERE kind = 'acceptance' ORDER BY card_id, sort_order, id",
        "SELECT card_id, depends_on_id FROM roadmap_dependencies ORDER BY card_id, depends_on_id",
    )
    if conn is None:
        result = []
        for sql in statements:
            result.append([dict(r) for r in await db.query(sql)])
        return tuple(result)

    result = []
    for sql in statements:
        cur = await conn.execute(sql)
        rows = await cur.fetchall()
        await cur.close()
        result.append([dict(r) for r in rows])
    return tuple(result)


def _reject_dependency_cycles(edges: dict[str, set[str]]) -> None:
    """Reject any directed cycle and include a deterministic cycle path."""
    state: dict[str, int] = {}
    stack: list[str] = []
    positions: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for target in sorted(edges.get(node, ())):
            if state.get(target, 0) == 0:
                visit(target)
            elif state[target] == 1:
                cycle = stack[positions[target]:] + [target]
                raise RoadmapError(f"dependency cycle: {' -> '.join(cycle)}")
        stack.pop()
        positions.pop(node)
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            visit(node)


def _build_import_plan(
    cards: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    checklist_rows: list[dict[str, Any]],
    dependency_rows: list[dict[str, Any]],
    *,
    force: bool,
    new_card_status_policy: str,
) -> dict[str, Any]:
    """Build a deterministic reconciliation plan without writing anything."""
    existing = {row["id"]: row for row in existing_rows}
    seed_ids = {card["id"] for card in cards}
    known = set(existing) | seed_ids

    live_dependencies: dict[str, set[str]] = {}
    for row in dependency_rows:
        cid, target = row["card_id"], row["depends_on_id"]
        if cid == target:
            raise RoadmapError(f"card {cid} cannot depend on itself")
        live_dependencies.setdefault(cid, set()).add(target)

    seed_dependencies: dict[str, set[str]] = {}
    for card in cards:
        cid = card["id"]
        deps = set(card.get("dependencies") or [])
        if cid in deps:
            raise RoadmapError(f"card {cid} cannot depend on itself")
        for target in deps:
            if target not in known:
                raise RoadmapError(f"card {cid} depends on unknown card {target!r}")
        seed_dependencies[cid] = deps

    # Seed cards replace their outgoing dependency set; live-only cards retain
    # their existing edges. Validate that exact post-import graph before writes.
    effective_dependencies = {
        cid: set(targets) for cid, targets in live_dependencies.items() if cid not in seed_ids
    }
    effective_dependencies.update(seed_dependencies)
    for targets in tuple(effective_dependencies.values()):
        for target in targets:
            effective_dependencies.setdefault(target, set())
    _reject_dependency_cycles(effective_dependencies)

    live_acceptance: dict[str, list[str]] = {}
    for row in checklist_rows:
        live_acceptance.setdefault(row["card_id"], []).append(row["text"])

    created_cards: list[dict[str, Any]] = []
    updated_cards: list[dict[str, Any]] = []
    unchanged_cards: list[str] = []
    status_drift: list[dict[str, Any]] = []
    dependency_changes: list[dict[str, Any]] = []
    checklist_changes: list[dict[str, Any]] = []

    for card in cards:
        cid = card["id"]
        row = existing.get(cid)
        seed_status = card.get("status", "inbox")
        current_dependencies = live_dependencies.get(cid, set())
        desired_dependencies = seed_dependencies[cid]
        added_dependencies = sorted(desired_dependencies - current_dependencies)
        removed_dependencies = sorted(current_dependencies - desired_dependencies)
        if added_dependencies or removed_dependencies:
            dependency_changes.append({
                "card_id": cid,
                "added": added_dependencies,
                "removed": removed_dependencies,
            })

        current_acceptance = live_acceptance.get(cid, [])
        current_acceptance_set = set(current_acceptance)
        seed_acceptance = list(dict.fromkeys(card.get("acceptance") or []))
        seed_acceptance_set = set(seed_acceptance)
        added_acceptance = [text for text in seed_acceptance if text not in current_acceptance_set]
        preserved_live = [text for text in current_acceptance if text not in seed_acceptance_set]
        if added_acceptance or preserved_live:
            checklist_changes.append({
                "card_id": cid,
                "added": added_acceptance,
                "preserved_live_only": preserved_live,
            })

        if row is None:
            applied_status = seed_status if new_card_status_policy == "seed" else "inbox"
            created_cards.append({
                "card_id": cid,
                "seed_status": seed_status,
                "applied_status": applied_status,
            })
            continue

        changed_fields = [
            field
            for field, desired, column in zip(_SEED_FIELD_NAMES, _seed_values(card), _SEED_ROW_COLUMNS)
            if desired != row[column]
        ]
        if seed_status != row["status"]:
            action = "apply_seed" if force else "preserve_live"
            status_drift.append({
                "card_id": cid,
                "live_status": row["status"],
                "seed_status": seed_status,
                "action": action,
            })
            if force:
                changed_fields.append("status")
        if force and card.get("blocked_reason") != row["blocked_reason"]:
            changed_fields.append("blocked_reason")
        if added_acceptance:
            changed_fields.append("acceptance")
        if added_dependencies or removed_dependencies:
            changed_fields.append("dependencies")
        if changed_fields:
            updated_cards.append({"card_id": cid, "fields": changed_fields})
        else:
            unchanged_cards.append(cid)

    live_only = [
        {"card_id": row["id"], "title": row["title"], "status": row["status"]}
        for row in existing_rows if row["id"] not in seed_ids
    ]
    return {
        "created": len(created_cards),
        "updated": len(updated_cards),
        "unchanged": len(unchanged_cards),
        "total": len(cards),
        "created_cards": created_cards,
        "updated_cards": updated_cards,
        "unchanged_cards": unchanged_cards,
        "status_drift": status_drift,
        "live_only": live_only,
        "dependency_changes": dependency_changes,
        "checklist_changes": checklist_changes,
    }


async def import_backlog(
    path: str | Path | None = None,
    force: bool = False,
    *,
    dry_run: bool = False,
    new_card_status_policy: str = "seed",
) -> dict[str, Any]:
    """Plan or apply an idempotent seed reconciliation by card id.

    ``dry_run`` is strictly read-only: it returns the exact card/status/edge/
    checklist plan and emits no audit or bus events. On apply, existing local
    status still wins unless ``force`` (the compatible default). New cards use
    seed status by default; ``new_card_status_policy='inbox'`` funnels every
    new card through normal move gates instead of importing review/done state.
    The final effective dependency graph is validated before any write, and
    all writes remain one transaction.
    """
    _validate_enum(new_card_status_policy, NEW_CARD_STATUS_POLICIES, "new card status policy")
    src = Path(path) if path else default_backlog_path()
    if not src.is_absolute():
        src = get_settings().repo_root / src
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RoadmapError(f"backlog file not found: {src}") from None
    except ValueError as exc:
        raise RoadmapError(f"backlog is not valid JSON: {exc}") from None
    raw_cards = data.get("cards")
    if not isinstance(raw_cards, list):
        raise RoadmapError("backlog has no 'cards' list")

    # File-shape validation is independent of board state and happens before
    # taking the DB write lock. IDs are normalized once for all later passes.
    cards: list[dict[str, Any]] = []
    seed_ids: set[str] = set()
    for raw in raw_cards:
        if not isinstance(raw, dict):
            raise RoadmapError("every card must be an object")
        cid = str(raw.get("id") or "").strip()
        if not cid or not str(raw.get("title") or "").strip():
            raise RoadmapError("every card needs an id and a title")
        if cid in seed_ids:
            raise RoadmapError(f"duplicate card id in seed: {cid}")
        seed_ids.add(cid)
        card = dict(raw)
        card["id"] = cid
        _validate_enum(card.get("status", "inbox"), set(STATUSES), "status")
        _validate_enum(card.get("type", "feature"), TYPES, "type")
        _validate_enum(card.get("priority", "P2"), PRIORITIES, "priority")
        _validate_enum(card.get("risk", "medium"), RISKS, "risk")
        for field in _SEED_TEXT_FIELDS:
            if field in card and not isinstance(card[field], str):
                raise RoadmapError(f"card {cid}: {field} must be a string")
        if card.get("owner") is not None and not isinstance(card["owner"], str):
            raise RoadmapError(f"card {cid}: owner must be a string")
        if card.get("blocked_reason") is not None and not isinstance(card["blocked_reason"], str):
            raise RoadmapError(f"card {cid}: blocked_reason must be a string")
        if "sort_order" in card:
            _require_number(card["sort_order"], f"card {cid}: sort_order")
        for field in ("acceptance", "dependencies", *_CARD_JSON_FIELDS):
            _require_str_list(card.get(field), f"card {cid}: {field}")
        cards.append(card)

    if dry_run:
        plan = _build_import_plan(
            cards, *(await _import_state()), force=force,
            new_card_status_policy=new_card_status_policy,
        )
        return {
            "dry_run": True,
            "path": str(src),
            "force": force,
            "new_card_status_policy": new_card_status_policy,
            **plan,
        }

    now = bus.now_iso()
    # Build and validate the exact plan inside the write transaction, so a
    # concurrent edge addition cannot race cycle validation and reconciliation.
    async with db.transaction() as conn:
        plan = _build_import_plan(
            cards, *(await _import_state(conn)), force=force,
            new_card_status_policy=new_card_status_policy,
        )
        for i, card in enumerate(cards):
            cid = card["id"]
            status = card.get("status", "inbox")
            fields = _seed_values(card)
            cur = await conn.execute("SELECT * FROM roadmap_cards WHERE id = ?", (cid,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                applied_status = status if new_card_status_policy == "seed" else "inbox"
                await conn.execute(
                    "INSERT INTO roadmap_cards (id, title, type, phase, priority, risk, summary, problem, "
                    "implementation, agent_prompt, design_links_json, expected_files_json, verification_json, "
                    "tags_json, status, owner, blocked_reason, sort_order, created_at, updated_at, completed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, *fields, applied_status, card.get("owner"), card.get("blocked_reason"),
                     float(card.get("sort_order", (i + 1) * 10)), now, now,
                     now if applied_status == "done" else None),
                )
                continue
            if fields != tuple(row[column] for column in _SEED_ROW_COLUMNS):
                await conn.execute(
                    "UPDATE roadmap_cards SET title=?, type=?, phase=?, priority=?, risk=?, summary=?, "
                    "problem=?, implementation=?, agent_prompt=?, design_links_json=?, expected_files_json=?, "
                    "verification_json=?, tags_json=?, updated_at=? WHERE id=?",
                    (*fields, now, cid),
                )
            if force and status != row["status"]:
                await conn.execute(
                    "UPDATE roadmap_cards SET status=?, completed_at=?, updated_at=? WHERE id=?",
                    (status, now if status == "done" else None, now, cid),
                )
            if force and card.get("blocked_reason") != row["blocked_reason"]:
                await conn.execute(
                    "UPDATE roadmap_cards SET blocked_reason=?, updated_at=? WHERE id=?",
                    (card.get("blocked_reason"), now, cid),
                )

        # Deterministic checklist ids merge checked state; seed dependency sets
        # authoritatively reconcile each seed card's outgoing edges.
        for card in cards:
            cid = card["id"]
            for j, text in enumerate(card.get("acceptance") or []):
                await conn.execute(
                    "INSERT OR IGNORE INTO roadmap_checklists (id, card_id, kind, text, checked, sort_order, "
                    "created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                    (_det_id(cid, "acceptance", text), cid, "acceptance", text, (j + 1) * 10.0, now, now),
                )
            deps = card.get("dependencies") or []
            if deps:
                marks = ",".join("?" for _ in deps)
                await conn.execute(
                    f"DELETE FROM roadmap_dependencies WHERE card_id = ? AND depends_on_id NOT IN ({marks})",
                    (cid, *deps),
                )
            else:
                await conn.execute("DELETE FROM roadmap_dependencies WHERE card_id = ?", (cid,))
            for target in deps:
                await conn.execute(
                    "INSERT OR IGNORE INTO roadmap_dependencies (id, card_id, depends_on_id, relation, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (_det_id(cid, "dep", target, "blocks"), cid, target, "blocks", now),
                )

    result = {key: plan[key] for key in ("created", "updated", "unchanged", "total")}
    for drift in plan["status_drift"]:
        if drift["action"] == "apply_seed":
            await _record_event(
                "import.status_forced", drift["card_id"],
                {"from": drift["live_status"], "to": drift["seed_status"]},
            )
    await _record_event(
        "import.completed", None,
        {
            "path": str(src), "force": force,
            "new_card_status_policy": new_card_status_policy, **result,
        },
    )
    return result


async def export_backlog() -> dict[str, Any]:
    """Seed-compatible snapshot of the live board (GET /api/roadmap/export).

    Re-importing the snapshot is a no-op (every card reports ``unchanged``),
    and importing it into an empty database rebuilds the board — including
    ``blocked_reason``, which gates claim/move (import reads it on INSERT and,
    like status, local wins unless ``force``). One known loss, outside the
    seed format: checklist ``checked`` state exports as bare text.
    """
    cards = await db.query("SELECT * FROM roadmap_cards ORDER BY sort_order, id")
    acceptance: dict[str, list[str]] = {}
    for r in await db.query(
        "SELECT card_id, text FROM roadmap_checklists WHERE kind = 'acceptance' ORDER BY sort_order, rowid"
    ):
        acceptance.setdefault(r["card_id"], []).append(r["text"])
    deps: dict[str, list[str]] = {}
    for r in await db.query("SELECT card_id, depends_on_id FROM roadmap_dependencies ORDER BY depends_on_id"):
        deps.setdefault(r["card_id"], []).append(r["depends_on_id"])

    out: list[dict[str, Any]] = []
    phases: list[str] = []
    for row in cards:
        card = _card_out(row)
        if card["phase"] and card["phase"] not in phases:
            phases.append(card["phase"])
        item: dict[str, Any] = {
            "id": card["id"], "title": card["title"], "type": card["type"], "phase": card["phase"],
            "status": card["status"], "priority": card["priority"], "risk": card["risk"],
        }
        if card["owner"]:
            item["owner"] = card["owner"]
        for field in ("summary", "problem", "implementation", "agent_prompt"):
            if card[field]:
                item[field] = card[field]
        item["design_links"] = card["design_links"]
        item["expected_files"] = card["expected_files"]
        item["dependencies"] = deps.get(card["id"], [])
        item["acceptance"] = acceptance.get(card["id"], [])
        item["verification"] = card["verification"]
        item["tags"] = card["tags"]
        item["sort_order"] = card["sort_order"]
        if card["blocked_reason"]:
            item["blocked_reason"] = card["blocked_reason"]
        out.append(item)
    return {"version": 1, "columns": list(STATUSES), "phases": phases, "cards": out}


# ---- cards -----------------------------------------------------------------

async def create_card(
    fields: dict[str, Any], *, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Create a card (POST /api/roadmap/cards). New cards start in inbox/ready;
    anything further must go through move() so the gates apply."""
    requested_id = str(fields.get("id") or "").strip() or None
    title = str(fields.get("title") or "").strip()
    if not title:
        raise RoadmapError("a card needs a title")
    status = fields.get("status", "inbox")
    _validate_enum(status, CREATE_STATUSES, "create status")
    type_ = fields.get("type", "feature")
    _validate_enum(type_, TYPES, "type")
    priority = fields.get("priority", "P2")
    _validate_enum(priority, PRIORITIES, "priority")
    risk = fields.get("risk", "medium")
    _validate_enum(risk, RISKS, "risk")
    owner = fields.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise RoadmapError("owner must be a string")
    for key in ("phase", "summary", "problem", "implementation", "agent_prompt"):
        if not isinstance(fields.get(key, ""), str):
            raise RoadmapError(f"{key} must be a string")
    lists = {key: _require_str_list(fields.get(key), key) for key in _CARD_JSON_FIELDS}
    acceptance = [t.strip() for t in _require_str_list(fields.get("acceptance"), "acceptance")]
    if any(not t for t in acceptance):  # a whitespace item would sneak past the ready gate
        raise RoadmapError("acceptance items need text")
    if len(set(acceptance)) != len(acceptance):  # duplicates share a deterministic id
        raise RoadmapError("acceptance items must be unique")
    if status == "ready" and not acceptance:
        raise RoadmapError("cannot create in ready: acceptance checklist is empty")
    sort_order = _require_number(fields.get("sort_order", 0), "sort_order")

    request = {
        "id": requested_id,
        "title": title,
        "type": type_,
        "phase": fields.get("phase", ""),
        "status": status,
        "priority": priority,
        "risk": risk,
        "owner": owner,
        "summary": fields.get("summary", ""),
        "problem": fields.get("problem", ""),
        "implementation": fields.get("implementation", ""),
        "agent_prompt": fields.get("agent_prompt", ""),
        **lists,
        "acceptance": acceptance,
        "sort_order": sort_order,
    }

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        card_id = requested_id or _new_id()
        now = bus.now_iso()
        try:
            await conn.execute(
                "INSERT INTO roadmap_cards (id, title, type, phase, priority, risk, summary, problem, "
                "implementation, agent_prompt, design_links_json, expected_files_json, verification_json, "
                "tags_json, status, owner, sort_order, created_at, updated_at, completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (card_id, title, type_, fields.get("phase", ""), priority, risk,
                 fields.get("summary", ""), fields.get("problem", ""), fields.get("implementation", ""),
                 fields.get("agent_prompt", ""), _dumps(lists["design_links"]), _dumps(lists["expected_files"]),
                 _dumps(lists["verification"]), _dumps(lists["tags"]), status, owner, sort_order, now, now),
            )
        except sqlite3.IntegrityError:
            raise RoadmapError(f"card {card_id} already exists") from None
        for j, text in enumerate(acceptance):
            await conn.execute(
                "INSERT INTO roadmap_checklists (id, card_id, kind, text, checked, sort_order, "
                "created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                (_det_id(card_id, "acceptance", text), card_id, "acceptance", text, (j + 1) * 10.0, now, now),
            )
        return await _card_from_conn(conn, card_id), True

    result, created = await _idempotent_create(
        "roadmap.cards.create", idempotency_key, request, mutation
    )
    assert result is not None
    if created:
        await _record_event(
            "card.created", result["id"], {"title": title, "status": status}
        )
    return result


async def claim_card(card_id: str, owner: str) -> dict[str, Any] | None:
    """Claim an unowned ready/inbox card and move it to in_progress.

    Conditional claim: ``UPDATE … WHERE owner is empty AND status = <seen>``
    guarantees exactly one concurrent claimer wins (the rest get MoveConflict).
    """
    if not str(owner).strip():
        raise RoadmapError("claim needs an owner")
    owner = owner.strip()
    card = await db.query_one("SELECT * FROM roadmap_cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    if card["owner"]:
        raise MoveConflict(f"card {card_id} is already owned by {card['owner']}")
    from_status = card["status"]
    if from_status not in ("inbox", "ready"):
        raise RoadmapError(f"cannot claim a card in {from_status!r}; only inbox/ready cards are claimable")
    if card["blocked_reason"]:  # claim is a forward move — same gate as move()
        raise RoadmapError(f"card {card_id} is blocked ({card['blocked_reason']}); resolve or override via move")
    claimed = await db.execute(
        "UPDATE roadmap_cards SET owner = ?, status = 'in_progress', updated_at = ? "
        "WHERE id = ? AND (owner IS NULL OR owner = '') AND status = ?",
        (owner, bus.now_iso(), card_id, from_status),
    )
    if not claimed:
        raise MoveConflict(f"card {card_id} changed concurrently; reload and retry")
    await _record_event("card.claimed", card_id, {"owner": owner, "from": from_status, "to": "in_progress"})
    return await get_card(card_id)


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
        # id as tie-breaker: equal sort_order must not float on query-plan changes
        # (generate_agent_prompt's byte-determinism depends on this ordering)
        "SELECT * FROM roadmap_checklists WHERE card_id = ? ORDER BY kind, sort_order, id", (card_id,)
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
            if val is None:  # explicit JSON null -> 400, not a NOT NULL 500
                raise RoadmapError(f"{key} must be a string")
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
    non-empty acceptance checklist; ``in_progress`` needs an owner; ``review``
    needs a non-cancelled coding session with a non-empty summary
    (05-global-coding-process §5); ``done`` needs every dependency done AND at
    least one piece of evidence. The write is a conditional claim on the
    status we validated against.
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
        if to_status == "review":
            # cancelled sessions never open the gate — their summary (if any)
            # documents an abandoned attempt, not reviewable work
            n = await db.query_one(
                "SELECT COUNT(*) AS n FROM roadmap_coding_sessions "
                "WHERE card_id = ? AND status != 'cancelled' AND TRIM(summary) != ''",
                (card_id,),
            )
            if not n or not n["n"]:
                raise RoadmapError(
                    "cannot move to review without a session summary; set override to force"
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


# ---- agent prompt (M7-007) ---------------------------------------------------

# Hard-rule digest for coding agents: CLAUDE.md pointers + the migration/test
# discipline + roadmap/06-agent-protocol.md guardrails. A tuple of constants —
# no clock, no randomness — so the prompt for an unchanged card is
# byte-identical across calls (M7-007 acceptance: deterministic generation).
_PROMPT_CONSTRAINTS = (
    "read CLAUDE.md first and follow its hard rules (one execution path, "
    "conditional-claim transitions, bus.now_iso() timestamps)",
    "keep changes scoped to this card and its expected files",
    "migrations are additive only: add a new numbered migrations/*.sql file, never edit old ones",
    "add or update tests for every behavior change and run the card's verification commands before finishing",
    "do not git push, do not introduce hosted infrastructure, preserve unrelated user changes",
    "record follow-up work as roadmap cards instead of silently expanding scope",
)


def _prompt_section(label: str, items: list[str]) -> list[str]:
    lines = [f"{label}:"]
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- (none)")
    return lines


async def generate_agent_prompt(card_id: str) -> str | None:
    """Deterministic coding-agent prompt from the card's current state (M7-007).

    Template: roadmap/06-agent-protocol.md. Renders only durable card state
    (metadata, checklists, dependencies + their live status) — never
    timestamps or generated ids — so the same card state always produces the
    same bytes and card edits are diffable across generations.
    """
    card = await get_card(card_id)
    if card is None:
        return None
    acceptance = [c["text"] for c in card["checklists"] if c["kind"] == "acceptance"]
    dependencies = [
        f"{d['depends_on_id']} ({d['depends_on_status'] or 'missing'})"
        for d in card["dependencies"]
    ]
    lines = [
        f"You are implementing roadmap card {card['id']}: {card['title']}.",
        "",
        f"Phase: {card['phase'] or '(none)'}",
        f"Type: {card['type']} | Priority: {card['priority']} | Risk: {card['risk']}",
    ]
    if card["summary"]:
        lines += ["", "Summary:", card["summary"]]
    if card["problem"]:
        lines += ["", "Problem:", card["problem"]]
    lines += ["", *_prompt_section("Design links", card["design_links"])]
    lines += ["", *_prompt_section("Expected files", card["expected_files"])]
    lines += ["", *_prompt_section("Dependencies", dependencies)]
    lines += ["", *_prompt_section("Acceptance criteria", acceptance)]
    lines += ["", *_prompt_section("Verification", card["verification"])]
    lines += ["", *_prompt_section("Constraints", list(_PROMPT_CONSTRAINTS))]
    if card["agent_prompt"]:  # operator-written notes on the card ride along
        lines += ["", "Card notes:", card["agent_prompt"]]
    lines += ["", "Implement the card, run verification, and summarize changed files and results."]
    return "\n".join(lines)


# ---- evidence ---------------------------------------------------------------

async def add_evidence(
    card_id: str,
    kind: str,
    title: str,
    body: str = "",
    status: str = "info",
    artifact_ref: str | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    _validate_enum(kind, EVIDENCE_KINDS, "evidence kind")
    _validate_enum(status, EVIDENCE_STATUSES, "evidence status")
    if not str(title).strip():
        raise RoadmapError("evidence needs a title")
    normalized_title = title.strip()
    request = {
        "kind": kind, "title": normalized_title, "body": body,
        "status": status, "artifact_ref": artifact_ref,
    }

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        if await _conn_one(conn, "SELECT id FROM roadmap_cards WHERE id = ?", (card_id,)) is None:
            return None, False
        evidence_id = _new_id()
        await conn.execute(
            "INSERT INTO roadmap_evidence (id, card_id, kind, title, body, status, artifact_ref, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (evidence_id, card_id, kind, normalized_title, body, status, artifact_ref, bus.now_iso()),
        )
        return await _conn_one(
            conn, "SELECT * FROM roadmap_evidence WHERE id = ?", (evidence_id,)
        ), True

    result, created = await _idempotent_create(
        f"roadmap.cards/{card_id}/evidence.create", idempotency_key, request, mutation
    )
    if created and result is not None:
        await _record_event(
            "evidence.added", card_id,
            {"evidence_id": result["id"], "kind": kind, "status": status},
        )
    return result


# ---- checklists ---------------------------------------------------------------

async def add_checklist_item(
    card_id: str,
    kind: str,
    text: str,
    sort_order: float | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    _validate_enum(kind, CHECKLIST_KINDS, "checklist kind")
    if not str(text).strip():
        raise RoadmapError("a checklist item needs text")
    text = text.strip()
    requested_sort_order = None if sort_order is None else _require_number(sort_order, "sort_order")
    request = {"kind": kind, "text": text, "sort_order": requested_sort_order}

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        if await _conn_one(conn, "SELECT id FROM roadmap_cards WHERE id = ?", (card_id,)) is None:
            return None, False
        applied_sort_order = requested_sort_order
        if applied_sort_order is None:
            tail = await _conn_one(
                conn,
                "SELECT COALESCE(MAX(sort_order), 0) AS m FROM roadmap_checklists "
                "WHERE card_id = ? AND kind = ?",
                (card_id, kind),
            )
            applied_sort_order = (tail["m"] if tail else 0) + 10.0
        now = bus.now_iso()
        item_id = _det_id(card_id, kind, text)
        try:
            await conn.execute(
                "INSERT INTO roadmap_checklists "
                "(id, card_id, kind, text, checked, sort_order, created_at, updated_at) "
                "VALUES (?,?,?,?,0,?,?,?)",
                (item_id, card_id, kind, text, applied_sort_order, now, now),
            )
        except sqlite3.IntegrityError:
            raise RoadmapError(
                f"checklist item already exists on {card_id}: {text!r}"
            ) from None
        return await _conn_one(
            conn, "SELECT * FROM roadmap_checklists WHERE id = ?", (item_id,)
        ), True

    result, created = await _idempotent_create(
        f"roadmap.cards/{card_id}/checklists.create", idempotency_key, request, mutation
    )
    if created and result is not None:
        await _record_event(
            "checklist.added", card_id,
            {"checklist_id": result["id"], "kind": kind, "text": text},
        )
    return result


async def update_checklist_item(item_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """PATCH a checklist item. Renaming re-derives the deterministic id from the
    new text in the same UPDATE (id == _det_id(card, kind, text) is the seed
    merge key — a stale id would make the next import duplicate the item), so
    the old id stops resolving; the response and a ``checklist.renamed`` event
    carry the new id."""
    row = await db.query_one("SELECT * FROM roadmap_checklists WHERE id = ?", (item_id,))
    if row is None:
        return None
    unknown = set(fields) - {"checked", "text", "sort_order"}
    if unknown:
        raise RoadmapError(f"unknown checklist fields: {', '.join(sorted(unknown))}")
    sets, params = [], []
    new_id, new_text = item_id, None
    if "checked" in fields:
        sets.append("checked = ?")
        params.append(1 if fields["checked"] else 0)
    if "text" in fields:
        if not isinstance(fields["text"], str) or not fields["text"].strip():
            raise RoadmapError("text must be a non-empty string")
        text = fields["text"].strip()
        if text != row["text"]:
            new_text = text
            new_id = _det_id(row["card_id"], row["kind"], text)
            sets.extend(["id = ?", "text = ?"])
            params.extend([new_id, text])
    if "sort_order" in fields:
        sets.append("sort_order = ?")
        params.append(_require_number(fields["sort_order"], "sort_order"))
    if not sets:
        return row
    sets.append("updated_at = ?")
    params.extend([bus.now_iso(), item_id])
    try:
        await db.execute(f"UPDATE roadmap_checklists SET {', '.join(sets)} WHERE id = ?", params)
    except sqlite3.IntegrityError:  # text collides with a sibling item (unique per card+kind)
        raise RoadmapError(f"checklist item already exists on {row['card_id']}: {fields['text']!r}") from None
    if new_text is not None:
        await _record_event(
            "checklist.renamed", row["card_id"],
            {"from_id": item_id, "to_id": new_id, "from_text": row["text"], "to_text": new_text},
        )
    if "checked" in fields:
        await _record_event(
            "checklist.checked", row["card_id"],
            {"checklist_id": new_id, "checked": bool(fields["checked"])},
        )
    return await db.query_one("SELECT * FROM roadmap_checklists WHERE id = ?", (new_id,))


async def delete_checklist_item(item_id: str) -> bool:
    row = await db.query_one("SELECT card_id, kind, text FROM roadmap_checklists WHERE id = ?", (item_id,))
    if row is None:
        return False
    await db.execute("DELETE FROM roadmap_checklists WHERE id = ?", (item_id,))
    await _record_event(
        "checklist.removed", row["card_id"],
        {"checklist_id": item_id, "kind": row["kind"], "text": row["text"]},
    )
    return True


# ---- dependencies ---------------------------------------------------------------

async def add_dependency(
    card_id: str,
    depends_on_id: str,
    relation: str = "blocks",
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    """Add a dependency edge (02-data-model.md: ids must be known; a self-edge
    or a cycle would deadlock the move-to-done gate, so both are rejected).

    NB: import_backlog() reconciles edges to the seed — a manually added edge
    survives until the next import unless the seed also carries it.
    """
    relation = str(relation).strip()  # normalize BEFORE deriving the id, or
    if not relation:                  # " blocks " and "blocks" would split identities
        raise RoadmapError("relation must be a non-empty string")
    dep_id = _det_id(card_id, "dep", depends_on_id, relation)  # same identity as import
    request = {"depends_on_id": depends_on_id, "relation": relation}

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        if await _conn_one(conn, "SELECT id FROM roadmap_cards WHERE id = ?", (card_id,)) is None:
            return None, False
        if depends_on_id == card_id:
            raise RoadmapError(f"card {card_id} cannot depend on itself")
        if await _conn_one(
            conn, "SELECT id FROM roadmap_cards WHERE id = ?", (depends_on_id,)
        ) is None:
            raise RoadmapError(f"depends on unknown card {depends_on_id!r}")

        # The generic transaction holds the write lock, so two concurrent adds
        # cannot both pass the reachability check and close a loop.
        rows = await _conn_rows(
            conn, "SELECT card_id, depends_on_id FROM roadmap_dependencies"
        )
        edges: dict[str, list[str]] = {}
        for r in rows:
            edges.setdefault(r["card_id"], []).append(r["depends_on_id"])
        # if card_id is reachable from depends_on_id, the new edge closes a loop
        seen, frontier = set(), [depends_on_id]
        while frontier:
            node = frontier.pop()
            if node == card_id:
                raise RoadmapError(f"dependency cycle: {depends_on_id} already depends on {card_id}")
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(edges.get(node, ()))
        cur = await conn.execute(
            "INSERT OR IGNORE INTO roadmap_dependencies (id, card_id, depends_on_id, relation, created_at) "
            "VALUES (?,?,?,?,?)",
            (dep_id, card_id, depends_on_id, relation, bus.now_iso()),
        )
        created = cur.rowcount
        await cur.close()
        return await _conn_one(
            conn, "SELECT * FROM roadmap_dependencies WHERE id = ?", (dep_id,)
        ), bool(created)

    result, created = await _idempotent_create(
        f"roadmap.cards/{card_id}/dependencies.create", idempotency_key, request, mutation
    )
    if created:
        await _record_event(
            "dependency.added", card_id, {"dependency_id": dep_id, "depends_on_id": depends_on_id},
        )
    return result


async def remove_dependency(dep_id: str) -> bool:
    row = await db.query_one("SELECT card_id, depends_on_id FROM roadmap_dependencies WHERE id = ?", (dep_id,))
    if row is None:
        return False
    await db.execute("DELETE FROM roadmap_dependencies WHERE id = ?", (dep_id,))
    await _record_event(
        "dependency.removed", row["card_id"],
        {"dependency_id": dep_id, "depends_on_id": row["depends_on_id"]},
    )
    return True


# ---- coding sessions ---------------------------------------------------------

async def create_session(
    card_id: str,
    actor: str,
    goal: str,
    planned_files: list[str] | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    if not str(actor).strip() or not str(goal).strip():
        raise RoadmapError("a coding session needs an actor and a goal")
    normalized_actor, normalized_goal = actor.strip(), goal.strip()
    planned_files = _require_str_list(planned_files, "planned_files")
    request = {
        "actor": normalized_actor, "goal": normalized_goal,
        "planned_files": planned_files,
    }

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        if await _conn_one(conn, "SELECT id FROM roadmap_cards WHERE id = ?", (card_id,)) is None:
            return None, False
        session_id = _new_id()
        await conn.execute(
            "INSERT INTO roadmap_coding_sessions "
            "(id, card_id, actor, goal, status, planned_files_json, "
            "touched_files_json, summary, started_at) VALUES (?,?,?,?,'active',?,'[]','',?)",
            (
                session_id, card_id, normalized_actor, normalized_goal,
                _dumps(planned_files), bus.now_iso(),
            ),
        )
        return await _session_from_conn(conn, session_id), True

    result, created = await _idempotent_create(
        f"roadmap.cards/{card_id}/sessions.create", idempotency_key, request, mutation
    )
    if created and result is not None:
        await _record_event(
            "session.started", card_id,
            {
                "session_id": result["id"], "actor": normalized_actor,
                "goal": normalized_goal,
            },
        )
    return result


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
            if val is None:  # explicit JSON null -> 400, not a NOT NULL 500
                raise RoadmapError(f"{key} must be a string")
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
    as_evidence: bool = False,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    """Record a command run inside a session; ``as_evidence`` additionally
    attaches it to the session's card as command evidence (M7-005 acceptance:
    exit code 0 -> pass, non-zero -> fail, unknown -> info)."""
    if not str(command_label).strip() or not str(command_text).strip():
        raise RoadmapError("a session command needs a label and the command text")
    normalized_label, normalized_text = command_label.strip(), command_text.strip()
    request = {
        "command_label": normalized_label,
        "command_text": normalized_text,
        "exit_code": exit_code,
        "output_excerpt": output_excerpt,
        "as_evidence": bool(as_evidence),
    }
    event_card_id: str | None = None

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        nonlocal event_card_id
        session = await _conn_one(
            conn,
            "SELECT id, card_id FROM roadmap_coding_sessions WHERE id = ?",
            (session_id,),
        )
        if session is None:
            return None, False
        event_card_id = session["card_id"]
        command_id = _new_id()
        await conn.execute(
            "INSERT INTO roadmap_session_commands "
            "(id, session_id, command_label, command_text, exit_code, output_excerpt, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                command_id, session_id, normalized_label, normalized_text,
                exit_code, output_excerpt, bus.now_iso(),
            ),
        )
        row = await _conn_one(
            conn, "SELECT * FROM roadmap_session_commands WHERE id = ?", (command_id,)
        )
        assert row is not None
        if as_evidence:
            evidence_status = (
                "info" if exit_code is None else ("pass" if exit_code == 0 else "fail")
            )
            evidence_body = (
                normalized_text if output_excerpt is None
                else f"{normalized_text}\n{output_excerpt}"
            )
            evidence_id = _new_id()
            await conn.execute(
                "INSERT INTO roadmap_evidence "
                "(id, card_id, kind, title, body, status, artifact_ref, created_at) "
                "VALUES (?,?,'command',?,?,?,?,?)",
                (
                    evidence_id, session["card_id"], normalized_label, evidence_body,
                    evidence_status, f"session_command:{command_id}", bus.now_iso(),
                ),
            )
            row["evidence_id"] = evidence_id
        return row, True

    result, created = await _idempotent_create(
        f"roadmap.sessions/{session_id}/commands.create", idempotency_key, request, mutation
    )
    if created and as_evidence and result is not None:
        evidence_status = "info" if exit_code is None else ("pass" if exit_code == 0 else "fail")
        await _record_event(
            "evidence.added", event_card_id,
            {
                "evidence_id": result["evidence_id"], "kind": "command",
                "status": evidence_status,
            },
        )
    return result


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


# ---- decisions -----------------------------------------------------------------

async def open_decision(
    title: str,
    question: str,
    card_id: str | None = None,
    options: list[str] | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not str(title).strip() or not str(question).strip():
        raise RoadmapError("a decision needs a title and a question")
    normalized_title, normalized_question = title.strip(), question.strip()
    options = _require_str_list(options, "options")
    card_id = card_id or None  # normalize "" -> NULL (board-level decision)
    request = {
        "title": normalized_title, "question": normalized_question,
        "options": options,
    }
    parent_scope = f"card:{card_id}" if card_id else "board"

    async def mutation(conn: Any) -> tuple[dict[str, Any] | None, bool]:
        if card_id and await _conn_one(
            conn, "SELECT id FROM roadmap_cards WHERE id = ?", (card_id,)
        ) is None:
            raise RoadmapError(f"unknown card {card_id!r}")
        decision_id = _new_id()
        await conn.execute(
            "INSERT INTO roadmap_decisions "
            "(id, card_id, title, question, options_json, decision, status, created_at, resolved_at) "
            "VALUES (?,?,?,?,?,NULL,'open',?,NULL)",
            (
                decision_id, card_id, normalized_title, normalized_question,
                _dumps(options), bus.now_iso(),
            ),
        )
        row = await _conn_one(
            conn, "SELECT * FROM roadmap_decisions WHERE id = ?", (decision_id,)
        )
        assert row is not None
        return _decision_out(row), True

    result, created = await _idempotent_create(
        f"roadmap.decisions/{parent_scope}.create", idempotency_key, request, mutation
    )
    assert result is not None
    if created:
        await _record_event(
            "decision.opened", card_id,
            {"decision_id": result["id"], "title": normalized_title},
        )
    return result


async def get_decision(decision_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM roadmap_decisions WHERE id = ?", (decision_id,))
    return _decision_out(row) if row else None


async def list_decisions(
    card_id: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if card_id:
        clauses.append("card_id = ?")
        params.append(card_id)
    if status:
        _validate_enum(status, DECISION_STATUSES, "decision status")
        clauses.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM roadmap_decisions"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return [_decision_out(r) for r in await db.query(sql, params)]


_DECISION_UPDATABLE = {"status", "decision", "title", "question", "options"}


async def update_decision(decision_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """PATCH a decision; ``status='resolved'`` (with the ``decision`` text) is a
    conditional claim — an open decision resolves exactly once.

    A resolved decision is immutable (05-global-coding-process: do not rewrite
    history) — every field write below is therefore guarded by
    ``WHERE status = 'open'``, so a lost race surfaces as MoveConflict."""
    row = await db.query_one("SELECT * FROM roadmap_decisions WHERE id = ?", (decision_id,))
    if row is None:
        return None
    unknown = set(fields) - _DECISION_UPDATABLE
    if unknown:
        raise RoadmapError(f"unknown decision fields: {', '.join(sorted(unknown))}")
    if row["status"] != "open" and fields:
        raise MoveConflict(f"decision {decision_id} is resolved and immutable")
    to_status = fields.get("status")
    if to_status is not None and to_status != "resolved":
        _validate_enum(to_status, DECISION_STATUSES, "decision status")
        raise RoadmapError("a decision can only be patched to resolved")

    sets, params = [], []
    for key in ("title", "question", "decision"):
        if key in fields:
            val = fields[key]
            if not isinstance(val, str) or (key != "decision" and not val.strip()):
                raise RoadmapError(f"{key} must be a non-empty string")
            sets.append(f"{key} = ?")
            params.append(val.strip() if key != "decision" else val)
    if "options" in fields:
        sets.append("options_json = ?")
        params.append(_dumps(_require_str_list(fields["options"], "options")))

    if to_status == "resolved":
        if not str(fields.get("decision", "")).strip():
            raise RoadmapError("resolving a decision requires the decision text")
        sets.extend(["status = 'resolved'", "resolved_at = ?"])
        params.extend([bus.now_iso(), decision_id])
        claimed = await db.execute(
            f"UPDATE roadmap_decisions SET {', '.join(sets)} WHERE id = ? AND status = 'open'", params
        )
        if not claimed:
            raise MoveConflict(f"decision {decision_id} is not open; cannot resolve it")
        await _record_event(
            "decision.resolved", row["card_id"],
            {"decision_id": decision_id, "decision": fields["decision"].strip()},
        )
    elif sets:
        # field edits are also conditional on open: the pre-check above can go
        # stale between the read and this write (concurrent resolve)
        params.append(decision_id)
        claimed = await db.execute(
            f"UPDATE roadmap_decisions SET {', '.join(sets)} WHERE id = ? AND status = 'open'", params
        )
        if not claimed:
            raise MoveConflict(f"decision {decision_id} is resolved and immutable")
    return await get_decision(decision_id)


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


# ---- global process view (M7-006) ---------------------------------------------

async def process_overview() -> dict[str, Any]:
    """Global coding process aggregate (GET /api/roadmap/process).

    One payload the portal renders without opening each card: active coding
    sessions, open decisions, release-gate readiness, and every blocked card.
    Release readiness is computed from card status AND evidence
    (05-global-coding-process: a gate is a projection over cards and
    evidence): ``evidence_ready`` counts scoped cards carrying at least one
    pass-verdict evidence row, and a gate is ``ready`` only when every scoped
    card is done and evidence-backed.
    """
    cards = await db.query(
        "SELECT id, title, phase, status, owner, blocked_reason FROM roadmap_cards ORDER BY sort_order, id"
    )

    # open dependency edges (dependency target not done) per card
    open_deps: dict[str, list[str]] = {}
    for r in await db.query(
        "SELECT d.card_id, d.depends_on_id FROM roadmap_dependencies d "
        "JOIN roadmap_cards c ON c.id = d.depends_on_id "
        "WHERE c.status != 'done' ORDER BY d.depends_on_id"
    ):
        open_deps.setdefault(r["card_id"], []).append(r["depends_on_id"])

    # cards backed by at least one pass-verdict evidence row
    evidence_pass = {
        r["card_id"]
        for r in await db.query("SELECT DISTINCT card_id FROM roadmap_evidence WHERE status = 'pass'")
    }

    active_sessions = [
        _session_out(r) for r in await db.query(
            "SELECT s.*, c.title AS card_title, "
            "(SELECT COUNT(*) FROM roadmap_session_commands sc WHERE sc.session_id = s.id) AS n_commands "
            "FROM roadmap_coding_sessions s JOIN roadmap_cards c ON c.id = s.card_id "
            "WHERE s.status = 'active' ORDER BY s.started_at DESC, s.rowid DESC"
        )
    ]

    open_decisions = [
        _decision_out(r) for r in await db.query(
            "SELECT d.*, c.title AS card_title FROM roadmap_decisions d "
            "LEFT JOIN roadmap_cards c ON c.id = d.card_id "
            "WHERE d.status = 'open' ORDER BY d.created_at DESC, d.rowid DESC"
        )
    ]

    # blocked process items: operator-set blocked_reason or open dependencies.
    # done cards passed their gates already, so they are not process blockers.
    blocked_cards = [
        {
            "id": c["id"], "title": c["title"], "phase": c["phase"], "status": c["status"],
            "owner": c["owner"], "blocked_reason": c["blocked_reason"],
            "open_dependencies": open_deps.get(c["id"], []),
        }
        for c in cards
        if c["status"] != "done" and (c["blocked_reason"] or open_deps.get(c["id"]))
    ]

    gates = []
    for name, description, prefixes in RELEASE_GATES:
        scoped = [c for c in cards if _phase_token(c["phase"]) in prefixes]
        total = len(scoped)
        done = sum(1 for c in scoped if c["status"] == "done")
        evidence_ready = sum(1 for c in scoped if c["id"] in evidence_pass)
        blockers = sorted(
            c["id"] for c in scoped
            if c["status"] != "done" and (c["blocked_reason"] or open_deps.get(c["id"]))
        )
        gates.append({
            "gate": name,
            "description": description,
            "prefixes": list(prefixes),
            "cards_total": total,
            "cards_done": done,
            "evidence_ready": evidence_ready,
            "blockers": blockers,
            "ready": bool(total) and done == total and evidence_ready == total,
        })

    return {
        "active_sessions": active_sessions,
        "open_decisions": open_decisions,
        "release_gates": gates,
        "blocked_cards": blocked_cards,
    }
