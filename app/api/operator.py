"""Operator triage API — actions kanban + triage panel + the HUMAN approval gate.

Iron rules (ROADMAP Phase 6 / proposal §8.2; module docstring of
app/institute/operator.py is the authority):
- shadow suggestions become anything at all ONLY through the explicit human
  endpoint here (``POST /api/operator/dispositions/{id}/approve``) — never via
  vault frontmatter, never via MCP;
- approval is bookkeeping (resolves the action, flags the suggestion): it does
  NOT execute prompts/schedule/config changes. Acting on an approved
  suggestion stays a manual human step this round.

Shadow-exit policy (M8-006 acceptance; ROUND4-AUDIT-S4 S4-P2-05):
- Unshadowing is an explicit HUMAN engineering decision — never a graduation
  the system performs on itself. There is deliberately no feature switch, no
  admin_state key and no API that flips shadow off: route_actions() has no
  code path that writes ``shadow=0``, so exiting shadow requires a code
  change through a reviewed card, not an operator toggle.
- Preconditions before any such card: the unshadow-prep quartet stays closed
  (prompt-injection folding at source/entry/interpolation; the approve gate's
  LIVE confidence-floor recheck below; migrations/0022's propose-once unique
  index; the full-set gating-registry assertion), PLUS accumulated shadow-run
  data showing suggestion quality (approve/dismiss agreement), PLUS a real
  human-auth boundary on this API surface (M8-019/M8-020 token work) so
  "approved by a human in the web UI" is authenticated, not assumed.
- Even after an unshadow, ``human_pinned`` dispositions (prompt/schedule
  territory) never auto-act, and the approve endpoint keeps the live-floor
  recheck — both survive shadow's end by design.
- Rollback is the default posture: anything anomalous returns to
  shadow-only via the same reviewed-card path, and the per-job feature
  switches below (``job:<name>``) can freeze the operator's routing loops
  themselves (operator-fast-route / operator-deep-route) at any time.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import bus, db
from ..institute import operator, scheduler
from ..router import executor

router = APIRouter(prefix="/api/operator", tags=["operator"])

ActionStatus = Literal["open", "in_progress", "done", "dismissed"]
ActionKind = Literal[
    "vault_conflict", "disputed_fact", "scorecard_anomaly",
    "failed_run", "cron_failure", "other",
]


# ---- actions kanban ----------------------------------------------------------

@router.get("/actions")
async def list_actions(
    status: ActionStatus | None = None,
    kind: ActionKind | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    """Kanban data: actions (filterable) with their shadow dispositions inlined
    (the UI needs disposition ids to drive the approve endpoint)."""
    where, params = [], []
    for col, val in (("status", status), ("kind", kind)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT * FROM operator_actions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
    rows = await db.query(sql, [*params, limit])

    dispositions: dict[int, list[dict[str, Any]]] = {}
    if rows:
        placeholders = ",".join("?" for _ in rows)
        for d in await db.query(
            f"SELECT * FROM action_dispositions WHERE action_id IN ({placeholders}) "
            "ORDER BY id",
            [r["id"] for r in rows],
        ):
            dispositions.setdefault(d["action_id"], []).append(d)
    for r in rows:
        r["dispositions"] = dispositions.get(r["id"], [])
    return {"actions": rows, "count": len(rows)}


class ActionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ActionStatus
    resolution: str | None = Field(None, max_length=2000)


# Conditional-claim transition map: target -> allowed current statuses.
# done/dismissed are terminal (a recurring problem re-opens as a NEW action
# via the feeds — ref idempotency only spans live rows).
_ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "in_progress": ("open",),
    "done": ("open", "in_progress"),
    "dismissed": ("open", "in_progress"),
    "open": ("in_progress",),  # release a claim
}


@router.patch("/actions/{action_id}")
async def patch_action(action_id: int, body: ActionPatch):
    """Status transition as a conditional claim: the UPDATE carries the allowed
    source statuses in WHERE, so two operators grabbing the same card resolve
    to one winner and one 409 — never a double disposition."""
    allowed = _ALLOWED_FROM[body.status]
    now = bus.now_iso()
    placeholders = ",".join("?" for _ in allowed)
    if body.status in ("done", "dismissed"):
        n = await db.execute(
            f"UPDATE operator_actions SET status=?, resolution=?, resolved_at=?, updated_at=? "
            f"WHERE id=? AND status IN ({placeholders})",
            (body.status, body.resolution or ("dismissed" if body.status == "dismissed" else "done"),
             now, now, action_id, *allowed),
        )
    else:
        n = await db.execute(
            f"UPDATE operator_actions SET status=?, resolution=NULL, resolved_at=NULL, updated_at=? "
            f"WHERE id=? AND status IN ({placeholders})",
            (body.status, now, action_id, *allowed),
        )
    if n == 0:
        row = await db.query_one("SELECT status FROM operator_actions WHERE id=?", (action_id,))
        if row is None:
            raise HTTPException(404, f"unknown action {action_id}")
        raise HTTPException(
            409, f"action {action_id} is {row['status']!r}; cannot move to {body.status!r}",
        )
    return await db.query_one("SELECT * FROM operator_actions WHERE id=?", (action_id,))


# ---- feature switches (admin_state key 'feature_switches') --------------------
# ENFORCED since M8-006: scheduler.metered() consumes switches named
# ``job:<name>`` (off = the job skips and records a cron_metrics skip row;
# missing = enabled). The stored value is a versioned envelope
# {"version": N, "switches": {...}} so PUT can be compare-and-swap; a legacy
# flat {name: bool} value (pre-M8-006) reads as version 0.

FEATURE_SWITCHES_KEY = "feature_switches"


def _parse_feature_switches(value: str | None) -> tuple[dict[str, bool], int]:
    """(switches, version) from a raw admin_state value; corrupt -> ({}, 0)."""
    if value is None:
        return {}, 0
    try:
        raw = json.loads(value)
    except (ValueError, TypeError):
        return {}, 0
    if not isinstance(raw, dict):
        return {}, 0
    if isinstance(raw.get("switches"), dict):  # versioned envelope
        v = raw.get("version")
        version = v if isinstance(v, int) and v >= 0 else 0
        return {str(k): bool(x) for k, x in raw["switches"].items()}, version
    return {str(k): bool(x) for k, x in raw.items()}, 0  # legacy flat map


async def _read_feature_switches() -> tuple[dict[str, bool], int]:
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (FEATURE_SWITCHES_KEY,)
    )
    return _parse_feature_switches(row["value"] if row else None)


class FeatureSwitchesPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    switches: dict[str, bool]
    expected_version: int = Field(ge=0)


@router.put("/feature-switches")
async def put_feature_switches(body: FeatureSwitchesPut):
    """Replace the full switch set — compare-and-swap (M8-006).

    Full-replace PUT had a lost-update window (two operators read the same
    set, both write, second silently erases the first's edit). The client now
    echoes the version it read (triage's ``feature_switches_version``); the
    write lands only if that version is still current, otherwise 409 and the
    client must reload. The winner is arbitrated by the DB row itself: the
    UPDATE carries the exact raw value we read in its WHERE clause (byte-level
    compare-and-swap — also robust when the stored value is corrupt JSON),
    and first-ever creation uses INSERT OR IGNORE, so two concurrent PUTs
    yield exactly one winner and one 409 with no lock held across the await.
    """
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (FEATURE_SWITCHES_KEY,)
    )
    _, current_version = _parse_feature_switches(row["value"] if row else None)
    if body.expected_version != current_version:
        raise HTTPException(
            409,
            f"feature_switches version conflict: expected_version="
            f"{body.expected_version} but the server is at {current_version} — "
            "reload the switches and re-apply your edit",
        )
    new_version = current_version + 1
    new_value = json.dumps(
        {"version": new_version, "switches": body.switches}, ensure_ascii=False
    )
    if row is None:
        n = await db.execute(
            "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, ?)",
            (FEATURE_SWITCHES_KEY, new_value),
        )
    else:
        n = await db.execute(
            "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
            (new_value, FEATURE_SWITCHES_KEY, row["value"]),
        )
    if n == 0:  # a concurrent PUT landed between our read and our write
        raise HTTPException(
            409,
            "feature_switches changed concurrently — reload the switches "
            "and re-apply your edit",
        )
    return {"feature_switches": body.switches, "version": new_version}


# ---- triage panel --------------------------------------------------------------

@router.get("/triage")
async def triage():
    """One aggregate for the triage page: maintenance + drain depth, feature
    switches, hand-weights summary, cron health summary, vault conflicts,
    open-action distribution."""
    queue = await executor.queue_stats()
    by_status = queue.get("by_status", {})

    weight_rows = await db.query("SELECT scope, hand, weight FROM hand_weights ORDER BY scope, hand")
    weights_by_scope: dict[str, dict[str, float]] = {}
    for r in weight_rows:
        weights_by_scope.setdefault(r["scope"], {})[r["hand"]] = r["weight"]

    from .meta import cron_health  # plain async fn; reuse B1's aggregation
    cron = await cron_health()
    failing = sorted(n for n, j in cron["jobs"].items() if j.get("last_status") == "failed")

    vault_row = await db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN state = 'conflict' THEN 1 ELSE 0 END) AS conflicts FROM vault_index"
    )

    dist = await db.query(
        "SELECT status, kind, COUNT(*) AS n FROM operator_actions GROUP BY status, kind"
    )
    actions_by_status: dict[str, int] = {}
    open_by_kind: dict[str, int] = {}
    for r in dist:
        actions_by_status[r["status"]] = actions_by_status.get(r["status"], 0) + r["n"]
        if r["status"] == "open":
            open_by_kind[r["kind"]] = r["n"]

    switches, switches_version = await _read_feature_switches()

    return {
        "maintenance": {
            "paused": await scheduler.get_maintenance(),
            "drain_depth": by_status.get("queued", 0) + by_status.get("running", 0),
            "queue": queue,
        },
        # flat map (shape consumed by the SPA and the Obsidian plugin);
        # the version rides alongside so the SPA can CAS its PUT
        "feature_switches": switches,
        "feature_switches_version": switches_version,
        "hand_weights": {"configured": len(weight_rows), "by_scope": weights_by_scope},
        "cron": {
            "window_days": cron["window_days"],
            "jobs": len(cron["jobs"]),
            "failing": failing,
        },
        "vault": {
            "ledger_total": (vault_row or {}).get("total") or 0,
            "conflicts": (vault_row or {}).get("conflicts") or 0,
        },
        "actions": {
            "by_status": actions_by_status,
            "open_by_kind": open_by_kind,
            "open": actions_by_status.get("open", 0),
        },
    }


# ---- the human approval gate ---------------------------------------------------

class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field("", max_length=1000)


@router.post("/dispositions/{disposition_id}/approve")
async def approve_disposition(disposition_id: int, body: ApproveBody | None = None):
    """THE human gate (iron rule 3): a shadow suggestion becomes an action's
    resolution ONLY here — an explicit human click in the web UI. Never via
    vault frontmatter, never via MCP (proposal §8.2 invariant).

    Approval is still a RECORDING act: it conditional-claims the action to
    'done' with the suggestion as resolution and flags the disposition row
    'approved'. It executes no system change — dispositions in prompt/schedule
    territory (human_pinned) in particular remain words on a card until a
    human performs the change by hand.

    Confidence-floor semantics are two-layered (F3 P3-1): the stored
    ``low_confidence`` flag is only a PROPOSAL-TIME cache (frozen when the
    router ran; kept for telemetry/UI). The gate itself re-checks the stored
    confidence against the LIVE floor here, at consumption time — so raising
    the floor retroactively blocks older, unflagged proposals, and lowering
    it unblocks flagged ones. Missing confidence never passes."""
    d = await db.query_one(
        "SELECT * FROM action_dispositions WHERE id = ?", (disposition_id,)
    )
    if d is None:
        raise HTTPException(404, f"unknown disposition {disposition_id}")

    # the confidence floor is a CONSUMPTION GATE (REVIEW-C4 M1), enforced
    # against the LIVE floor at approve time (F3 P3-1) — below-floor
    # suggestions are telemetry only; a human can still resolve the ACTION
    # manually via PATCH /actions/{id}.
    floor = await operator.get_confidence_floor()
    if d["confidence"] is None or d["confidence"] < floor:
        shown = "missing" if d["confidence"] is None else f"{d['confidence']:g}"
        raise HTTPException(
            409,
            f"disposition {disposition_id} confidence ({shown}) is below the "
            f"live confidence floor ({floor:g}): re-review it or resolve the "
            "action manually via PATCH /api/operator/actions/{id}",
        )

    note = (body.note if body else "") or ""
    resolution = f"approved disposition #{d['id']}: {d['disposition']}"
    if d["flags"]:
        resolution += f" [{d['flags']}]"
    if note:
        resolution += f" — {note}"
    flags = ",".join([*filter(None, (d["flags"] or "").split(",")), "approved"])
    now = bus.now_iso()

    # one transaction (REVIEW-C4 M2): the action claim and the disposition
    # flag update land together or not at all — no half-approved state
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE operator_actions SET status='done', resolution=?, resolved_at=?, updated_at=? "
            "WHERE id=? AND status IN ('open', 'in_progress')",
            (resolution, now, now, d["action_id"]),
        )
        if cur.rowcount == 0:
            # raising rolls the transaction back; report the loser cleanly
            row = await db.query_one(
                "SELECT status FROM operator_actions WHERE id = ?", (d["action_id"],)
            )
            raise HTTPException(
                409,
                f"action {d['action_id']} is {(row or {}).get('status')!r}; already disposed",
            )
        await conn.execute(
            "UPDATE action_dispositions SET flags = ? WHERE id = ?", (flags, disposition_id)
        )
    action = await db.query_one(
        "SELECT * FROM operator_actions WHERE id = ?", (d["action_id"],)
    )
    return {"approved": disposition_id, "action": action}


# ---- recipes (Phase 6 minimal reuse loop) ----------------------------------
# Human-approved dispositions distilled into reusable routing knowledge.
# Promotion is a HUMAN act in the web UI (it consumes an 'approved' flag that
# only the approve endpoint above writes); a recipe match inside
# route_actions() produces shadow suggestions with zero model calls. Retiring
# is the kill switch. observations/proposals/effect measurement: later cards.

@router.get("/recipes")
async def list_recipes(status: Literal["active", "retired"] | None = None):
    rows = await operator.list_recipes(status)
    return {"recipes": rows, "count": len(rows)}


@router.post("/dispositions/{disposition_id}/promote-recipe")
async def promote_recipe(disposition_id: int):
    """Distill an APPROVED disposition into a recipe (idempotent per source
    disposition: re-promoting returns the existing recipe, 200)."""
    try:
        recipe = await operator.promote_disposition_to_recipe(disposition_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "unknown disposition" in detail else 409, detail)
    return recipe


@router.post("/recipes/{recipe_id}/retire")
async def retire_recipe(recipe_id: int):
    """Conditional claim: active → retired; a repeat (or unknown id) is 4xx."""
    if await operator.retire_recipe(recipe_id):
        return await db.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    row = await db.query_one("SELECT status FROM recipes WHERE id = ?", (recipe_id,))
    if row is None:
        raise HTTPException(404, f"unknown recipe {recipe_id}")
    raise HTTPException(409, f"recipe {recipe_id} is {row['status']!r}; only active recipes retire")
