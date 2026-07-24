from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import bus, db
from ..hands.registry import get_registry
from ..institute.scorecard import previous_work_date

router = APIRouter(prefix="/api/hands", tags=["hands"])


@router.get("")
async def hands_status():
    return get_registry().status_snapshot()


# ---- hand weights (migrations/0009; consumed by registry.pick_weighted_hand) ----
# Static paths registered before the /{name}/... routes on principle (today's
# parametrized routes are all two-segment, so there is no actual collision).

class WeightEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # keep in sync with registry.WEIGHT_SCOPES / migrations/0009 CHECK
    scope: Literal["whiteboard", "research", "daily", "mailbox", "default"]
    hand: str = Field(min_length=1, max_length=64, pattern=r"^\S+$")
    # allow_inf_nan=False: inf passes ge=0 and the SQLite CHECK but breaks
    # random.choices downstream (REVIEW-B2 #4) — reject at the boundary
    weight: float = Field(ge=0, allow_inf_nan=False)


class WeightsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[WeightEntry]
    replace: bool = False  # True = full replacement (rows not in entries are deleted)


async def refresh_weights_cache() -> dict[str, dict[str, float]]:
    """Reload hand_weights rows into the registry's process-local cache.

    The registry is synchronous and never reads the DB itself (see the note in
    app/hands/registry.py); every write path here must call this, and boot code
    may call it once after init_registry() to pre-warm (PATCH-NOTES-B2.md).
    """
    rows = await db.query("SELECT scope, hand, weight FROM hand_weights")
    scoped: dict[str, dict[str, float]] = {}
    for r in rows:
        scoped.setdefault(r["scope"], {})[r["hand"]] = r["weight"]
    get_registry().set_weights_cache(scoped)
    return scoped


@router.get("/weights")
async def get_weights():
    rows = await db.query(
        "SELECT scope, hand, weight, updated_at FROM hand_weights ORDER BY scope, hand"
    )
    # opportunistic lazy-load: any weights read re-syncs the registry cache,
    # so a missed boot pre-warm heals on first inspection (REVIEW-B2 M3)
    scoped: dict[str, dict[str, float]] = {}
    for r in rows:
        scoped.setdefault(r["scope"], {})[r["hand"]] = r["weight"]
    get_registry().set_weights_cache(scoped)
    return rows


@router.put("/weights")
async def put_weights(body: WeightsPut):
    """Upsert weight rows (single entry or a batch); replace=True swaps the full set.

    One transaction: a crash or a concurrent PUT can't leave a half-replaced
    set (db.transaction holds the write lock — statements go through the
    yielded conn, never db.execute, which would deadlock).
    """
    now = bus.now_iso()
    async with db.transaction() as conn:
        if body.replace:
            await conn.execute("DELETE FROM hand_weights")
        for e in body.entries:
            await conn.execute(
                "INSERT INTO hand_weights (scope, hand, weight, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(scope, hand) DO UPDATE SET "
                "weight = excluded.weight, updated_at = excluded.updated_at",
                (e.scope, e.hand, e.weight, now),
            )
    weights = await refresh_weights_cache()
    return {"ok": True, "upserted": len(body.entries), "weights": weights}


# ---- scorecard + stats (written by app/institute/scorecard.run_once) ----------

@router.get("/scorecard")
async def get_scorecard(
    date: str | None = Query(
        None, description="SGT work date; default = previous SGT day (the last settled one)"
    ),
):
    d = date or previous_work_date()
    try:
        date_cls.fromisoformat(d)  # real calendar validation, not just shape
    except ValueError:
        raise HTTPException(400, f"date must be a real YYYY-MM-DD date, got {d!r}")
    rows = await db.query(
        "SELECT hand, work_date, task_id, verdict, reason, created_at "
        "FROM hand_scorecard WHERE work_date = ? ORDER BY hand, task_id",
        (d,),
    )
    counts: dict[str, int] = {"ok": 0, "stub": 0, "false_complete": 0}
    by_hand: dict[str, dict[str, int]] = {}
    for r in rows:
        counts[r["verdict"]] += 1
        h = by_hand.setdefault(r["hand"], {"ok": 0, "stub": 0, "false_complete": 0})
        h[r["verdict"]] += 1
    return {"date": d, "counts": counts, "by_hand": by_hand, "entries": rows}


@router.get("/stats")
async def get_stats(hours: int = Query(24, ge=1, le=720)):
    # bus.now_iso() is the project time helper (CLAUDE.md rule 7) — parse it
    # back rather than calling datetime.now() raw
    since = (
        datetime.fromisoformat(bus.now_iso()) - timedelta(hours=hours)
    ).replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    rows = await db.query(
        "SELECT hand, window_start, window_hours, tasks_total, tasks_ok, tasks_failed, "
        "tasks_rate_limited, duration_samples, avg_duration_ms, updated_at "
        "FROM hand_stats WHERE window_start >= ? ORDER BY window_start, hand",
        (since,),
    )
    by_hand: dict[str, dict[str, float]] = {}
    for r in rows:
        h = by_hand.setdefault(r["hand"], {
            "tasks_total": 0, "tasks_ok": 0, "tasks_failed": 0, "tasks_rate_limited": 0,
            "_dur_weighted": 0.0, "_dur_n": 0,
        })
        for k in ("tasks_total", "tasks_ok", "tasks_failed", "tasks_rate_limited"):
            h[k] += r[k]
        if r["avg_duration_ms"] is not None and r["duration_samples"]:
            # weight by the rows that actually produced the average — a true
            # task-level mean across windows, not a tasks_total approximation
            h["_dur_weighted"] += r["avg_duration_ms"] * r["duration_samples"]
            h["_dur_n"] += r["duration_samples"]
    for h in by_hand.values():
        dur_w, dur_n = h.pop("_dur_weighted"), h.pop("_dur_n")
        h["avg_duration_ms"] = (dur_w / dur_n) if dur_n else None
    return {"hours": hours, "since": since, "by_hand": by_hand, "windows": rows}


# ---- per-hand operations -------------------------------------------------------

@router.post("/{name}/cooldown/clear")
async def clear_cooldown(name: str):
    registry = get_registry()
    if registry.get(name) is None:
        raise HTTPException(404, f"unknown hand {name}")
    registry.clear_cooldown(name)
    return {"ok": True}


@router.get("/{name}/health")
async def hand_health(name: str):
    registry = get_registry()
    hand = registry.get(name)
    if hand is None:
        raise HTTPException(404, f"unknown hand {name}")
    return {"name": name, "healthy": await hand.health_check(), "available": registry.is_available(name)}
