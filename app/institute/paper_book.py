"""Paper book — virtual positions opened from forecasts (Phase 5, card C3).

Positions are nominal (size 1.0): the book measures the QUALITY OF CALLS, not
capital. Two ungated scheduler jobs drive it (both spend zero model quota):

    opener (5-min tick)   ``opener_tick()`` scans open long/short forecasts
                          that have no position yet, prices the entry with the
                          B6 knowledge-time read — the last adjusted close at
                          or before made_at's calendar date AS KNOWN AT
                          made_at (PIT ``as_of = made_at``, never look-ahead) —
                          and opens up to the position cap (admin_state key
                          'paper_book', default 20), one open position per
                          security. No usable entry price → skip, retry next
                          tick. Neutral and already-expired forecasts are
                          never opened.
    MTM (00:00 SGT)       ``mark_to_market()`` marks every open position at
                          the latest KNOWN adjusted close inside the position
                          window (PIT ``as_of = None`` — settlement-time
                          knowledge, the B6 exit-leg convention), applies the
                          close triggers, and upserts one nav_history row per
                          SGT work date (idempotent re-runs refresh the row).

MARK WINDOW (REVIEW-C3 H2): a position's economic life ends at the
forecast's expiry, so the mark is the last usable bar dated
``<= min(work_date, expires_at's calendar date)`` — exactly the B6 exit-leg
window. Prices printed AFTER expiry can never set a close price nor flip a
horizon close into stop/target, no matter how late the MTM (or a manual
close) runs after the fact; running later only adds knowledge (corrections)
about bars inside the window, never new bars.

Close state machine (signed return = (mark/entry - 1), negated for shorts):

    open --(ret <= -stop_pct)--------> closed(stop)      at the window mark
    open --(ret >= target_pct)-------> closed(target)    at the window mark
    open --(forecast expired)--------> closed(horizon)   at the window mark
    open --(expired, no usable mark)-> closed(unpriced)  close_price/realized NULL
    open --(POST close API)----------> closed(manual)    at the window mark

Stop/target take precedence over horizon when both fire on the same mark.
Every transition is a conditional claim (``UPDATE … WHERE status='open'``,
rowcount-checked). On every close the forecast is offered to
``forecasts.settle_forecast`` via ``_maybe_settle`` — a conditional courtesy:
it only fires when the forecast is still open AND expired (B6 refuses
pre-expiry settlement, so stop/target closes leave the forecast to its own
expiry), and a lost claim (concurrent settler) is swallowed — double
settlement is impossible (B6's transaction + UNIQUE(forecast_id) backstop).

FAILS CLOSED, never guesses (REVIEW-C3 H3): entry/mark/close prices must
pass the B6 positive-finite whitelist. A position with no usable mark
(deleted security, no bars) has UNKNOWN value: it is excluded from nav and
counted in ``n_unpriced`` — never marked "flat at entry" as if 0 were known.
An EXPIRED unpriceable position closes as 'unpriced' with close_price NULL
and realized_pnl NULL (the slot is freed, the unknown stays unknown and is
excluded from realized aggregates forever; the forecast itself settles
'invalid' through B6's own fails-closed path). Manual close of an
unpriceable position is refused. benchmark_nav is CSI300 normalized to a
base pinned on first sight (admin_state 'paper_book:benchmark_base'); no
usable mark → NULL, never a proxy.

NAV: ``nav = 1.0 + Σ realized_pnl(closed, realized_pnl NOT NULL)
+ Σ unrealized(open, priceable)``; ``nav_history.n_unpriced`` counts the
positions excluded for unknown value (open unpriceable + closed 'unpriced')
— a nonzero count flags the nav row as a partial statement.

CONCURRENCY (REVIEW-C3 M3): the opener's invariants are database facts, not
pre-read hopes — UNIQUE(forecast_id) (one position per forecast, ever), the
0017 partial unique index (at most one OPEN position per security), and a
conditional ``INSERT … SELECT … WHERE open_count < cap`` (the INSERT is the
arbiter, B6/0012 precedent) make concurrent opener ticks single-winner; the
losers count the collision and move on.

Journal: ``render_journal(work_date)`` renders the day's opens/closes/NAV as
markdown; the MTM job emits ``paper_book.marked`` and the vault exporter
handler projects it to ``Book/journal/<date>.md``.

Attribution (REVIEW-C3 M5): every ``paper_book.closed`` payload carries the
``analyst_id`` recorded on the forecast's extraction claim (0019 provenance;
NULL when unattributed) — memory.py's paper-outcome collector consumes these
events so a closed call flows back into its author's standing memory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import bus, db
from ..util import new_id
from . import forecasts, market_data
from .prompts import work_date

log = logging.getLogger("institute.paper_book")

SIZE = 1.0                      # nominal notional per position
DEFAULT_MAX_POSITIONS = 20
# loop-fix P8c: one opener tick considers at most this many candidates — the
# 5-min tick drains a large backlog batch by batch instead of turning one
# tick into an unbounded full-table sweep on the event loop
OPENER_BATCH_LIMIT = 50
DEFAULT_STOP_PCT = 0.05
DEFAULT_TARGET_PCT = 0.10
BENCHMARK_ID = "CSI300"
ADMIN_KEY = "paper_book"
BENCH_BASE_KEY = "paper_book:benchmark_base"
# loop-fix R3: the opener's rotation position — (made_at, id) of the last
# candidate CONSIDERED, persisted so the batched tick round-robins through
# the whole candidate keyset instead of re-selecting the same head forever
OPENER_CURSOR_KEY = "paper_book:opener_cursor"
RECONCILE_DIFFERENCE_EVENT = "paper_book.reconcile_difference"
STATUSES = ("open", "closed")
CLOSE_REASONS = ("stop", "target", "horizon", "manual", "unpriced")


class PaperBookError(ValueError):
    """Validation failure (the API maps this to 400)."""


class TransitionConflict(PaperBookError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


class RiskLimitConflict(TransitionConflict):
    """An opening risk invariant rejected the position (API maps to 409)."""


# ---- config (admin_state, 0011 idiom) ----------------------------------------

async def max_positions() -> int:
    """Position cap from admin_state key 'paper_book'; built-in default 20.
    Whitelisted: a finite integer >= 1, anything else degrades to the default."""
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (ADMIN_KEY,))
    if row is None:
        return DEFAULT_MAX_POSITIONS
    try:
        raw = json.loads(row["value"]).get("max_positions")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return DEFAULT_MAX_POSITIONS
        n = int(raw)
        return n if math.isfinite(raw) and n >= 1 else DEFAULT_MAX_POSITIONS
    except Exception:  # noqa: BLE001 - corrupt config degrades to the default
        return DEFAULT_MAX_POSITIONS


async def _opener_cursor() -> tuple[str, str] | None:
    """The persisted (made_at, id) rotation position, or None = start from the
    head. Pure scheduling state: a missing or corrupt row degrades to the
    head (the max_positions idiom) — worst case one rotation restarts."""
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (OPENER_CURSOR_KEY,))
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
        made_at, fid = str(data.get("made_at") or ""), str(data.get("id") or "")
        return (made_at, fid) if made_at and fid else None
    except Exception:  # noqa: BLE001 - corrupt cursor degrades to the head
        return None


# ---- pricing helpers (B6 whitelist reused verbatim) ---------------------------

def _signed_return(direction: str, entry: Any, price: Any) -> float | None:
    """Direction-adjusted return, or None when any input fails the B6
    positive-finite whitelist or the result is non-finite (fails closed)."""
    if not forecasts._usable_price(entry) or not forecasts._usable_price(price):
        return None
    ret = price / entry - 1.0
    if direction == "short":
        ret = -ret
    return ret if math.isfinite(ret) else None


def _bar_price(bar: dict[str, Any]) -> float | None:
    try:
        price = forecasts._adj_close(bar)
    except (TypeError, ValueError):
        return None
    return price if forecasts._usable_price(price) else None


async def _entry_bar(security_id: str, made_at: str) -> tuple[str, float] | None:
    """The B6 entry leg: last bar at or before made_at's calendar date AS
    KNOWN AT made_at. Corrections ingested after made_at can never move it.

    Single-row read (loop-fix P8c): only the newest such bar is ever used —
    an unusable newest bar still fails closed (skip, retry next tick), never
    falls back to an older price."""
    bar = await market_data.get_last_bar_pit(security_id, made_at, end=made_at[:10])
    if bar is None:
        return None
    price = _bar_price(bar)
    return (bar["bar_date"], price) if price is not None else None


async def _latest_mark(security_id: str, end_date: str) -> tuple[str, float] | None:
    """The B6 exit leg: last bar dated <= end_date as known NOW (latest PIT).

    Callers clamp ``end_date`` to the position's window
    (min(work_date, expires_date), see _mark_window) — a bar printed after
    the forecast expired must never price a close (REVIEW-C3 H2). Single-row
    read (loop-fix P8c), same fails-closed posture as _entry_bar."""
    bar = await market_data.get_last_bar_pit(security_id, end=end_date)
    if bar is None:
        return None
    price = _bar_price(bar)
    return (bar["bar_date"], price) if price is not None else None


def _mark_window(wd: str, expires_at: str) -> str:
    """The last calendar date a mark may come from: the earlier of the work
    date and the forecast's expiry date (both YYYY-MM-DD; string min == date
    min). Running MTM later than expiry only adds knowledge about bars
    INSIDE the window (corrections), never new bars — the same endpoint the
    B6 settlement exit leg uses, so paper close and settlement can never
    price from two different windows."""
    return min(wd, expires_at[:10])


# ---- opener (5-min tick) -------------------------------------------------------

async def _insert_position(fc: dict[str, Any], entry_date: str, entry_price: float,
                           cap: int, now: str) -> str | None:
    """One conditional INSERT is the whole arbitration (REVIEW-C3 M3, the
    B6/0012 "the INSERT is the arbiter" precedent): the cap check rides in
    the statement's own WHERE (atomic under SQLite's single writer — no
    check-then-insert window), UNIQUE(forecast_id) rejects a concurrent open
    of the same forecast, and the 0017 partial unique index rejects a second
    OPEN position on the same security.

    Returns the new POSITION id on success, None on a lost race; the
    aggregate cap is ALWAYS enforced (the old INSTITUTE_PAPER_BOOK_ENFORCE_CAPS
    escape hatch is gone — a risk limit that can be switched off is not a
    limit) and a cap refusal raises ``RiskLimitConflict``. The schema
    invariants (one position per forecast, one open position per security)
    are hard regardless.
    """
    pid = new_id()
    sql = (
        "INSERT INTO paper_positions (id, forecast_id, security_id, direction, "
        "entry_date, entry_price, size, stop_pct, target_pct, status, opened_at, updated_at) "
        "SELECT ?,?,?,?,?,?,?,?,?,'open',?,? "
        "WHERE (SELECT COUNT(*) FROM paper_positions WHERE status = 'open') < ?"
    )
    params: tuple[Any, ...] = (
        pid, fc["id"], fc["security_id"], fc["direction"], entry_date,
        entry_price, SIZE, DEFAULT_STOP_PCT, DEFAULT_TARGET_PCT, now, now, cap,
    )
    try:
        inserted = await db.execute(sql, params)
    except sqlite3.IntegrityError:
        return None  # a concurrent opener won this forecast or security
    if not inserted:
        raise RiskLimitConflict(
            f"paper-book max_positions cap {cap} reached; opening forecast {fc['id']} refused"
        )
    return pid


async def _emit_opened(
    position_id: str, fc: dict[str, Any], entry_date: str, entry_price: float,
) -> None:
    # ref_id is the POSITION id (loop-fix P11d) — ref_kind always said
    # 'paper_position' and paper_book.closed already refs the position row;
    # payload keeps forecast_id so payload consumers are unaffected
    await bus.emit("paper_book.opened", "paper_position", position_id, {
        "position_id": position_id,
        "forecast_id": fc["id"], "security_id": fc["security_id"],
        "direction": fc["direction"], "entry_date": entry_date,
        "entry_price": entry_price,
    })


async def opener_tick() -> dict[str, Any]:
    """Open positions for open long/short forecasts that have none yet.

    Entry semantics are B6's (frozen at made_at knowledge — see _entry_bar);
    a forecast with no usable entry price is skipped and retried next tick.
    Caps: at most ``max_positions()`` open positions overall, at most one
    open position per security — both enforced BY THE DATABASE at insert
    time (see _insert_position; the cap is not switchable); the candidate
    query and the in-tick security set are just cheap pre-filters. One tick
    considers at most OPENER_BATCH_LIMIT candidates (loop-fix P8c) and the
    batch ROTATES through the (made_at, id) keyset via a persisted cursor
    with wrap-around (loop-fix R3): a permanently unpriceable head occupies
    only its own slice instead of starving every candidate behind it —
    skipped rows are reconsidered next rotation, not next tick. Expired
    forecasts are never opened, and neither are origin='backfill' rows (a
    backdated made_at would hand the book a historically frozen entry — the
    look-ahead the ledger exists to prevent; backfill stays out of every
    performance scope). Per-item failures never stop the sweep; every opened
    row stamps its own bus.now_iso() (loop-fix P11f), not one tick-start
    timestamp.
    """
    now = bus.now_iso()
    cap = await max_positions()
    row = await db.query_one("SELECT COUNT(*) AS n FROM paper_positions WHERE status = 'open'")
    summary = {"cap": cap, "open_before": row["n"] if row else 0, "opened": 0,
               "skipped_no_price": 0, "skipped_security_dup": 0, "lost_race": 0,
               "rejected_cap": 0, "considered": 0}
    if summary["open_before"] >= cap:
        summary["rejected_cap"] = 1
        return summary

    base_where = (
        "f.status = 'open' "
        "AND f.direction IN ('long','short') AND f.security_id IS NOT NULL "
        "AND f.expires_at > ? AND f.origin <> 'backfill' "
        "AND NOT EXISTS (SELECT 1 FROM paper_positions p WHERE p.forecast_id = f.id) "
        "AND NOT EXISTS (SELECT 1 FROM paper_positions q "
        "                WHERE q.security_id = f.security_id AND q.status = 'open')"
    )
    # keyset rotation (R3): first the slice strictly AFTER the cursor, then —
    # if the batch has room — wrap to the head up to and including the cursor.
    # The two slices partition the keyset, so one batch never sees a row twice.
    cursor = await _opener_cursor()
    candidates: list[dict[str, Any]] = []
    if cursor is not None:
        candidates = await db.query(
            f"SELECT * FROM forecasts f WHERE {base_where} "
            "AND (f.made_at > ? OR (f.made_at = ? AND f.id > ?)) "
            "ORDER BY f.made_at, f.id LIMIT ?",
            (now, cursor[0], cursor[0], cursor[1], OPENER_BATCH_LIMIT),
        )
    if len(candidates) < OPENER_BATCH_LIMIT:
        head_clause = ""
        params: list[Any] = [now]
        if cursor is not None:
            head_clause = " AND (f.made_at < ? OR (f.made_at = ? AND f.id <= ?))"
            params += [cursor[0], cursor[0], cursor[1]]
        params.append(OPENER_BATCH_LIMIT - len(candidates))
        candidates += await db.query(
            f"SELECT * FROM forecasts f WHERE {base_where}{head_clause} "
            "ORDER BY f.made_at, f.id LIMIT ?",
            params,
        )
    seen_securities: set[str] = set()
    for fc in candidates:
        summary["considered"] += 1
        if fc["security_id"] in seen_securities:
            summary["skipped_security_dup"] += 1
            continue
        try:
            entry = await _entry_bar(fc["security_id"], fc["made_at"])
            if entry is None:
                summary["skipped_no_price"] += 1
                continue  # no usable knowledge at made_at — retry next tick
            entry_date, entry_price = entry
            opened_at = bus.now_iso()  # P11f: per-iteration clock, not tick start
            try:
                pid = await _insert_position(fc, entry_date, entry_price, cap, opened_at)
            except RiskLimitConflict:
                summary["rejected_cap"] += 1
                break  # the database says the enforced book is full
            if pid is None:
                summary["lost_race"] += 1
                continue
            seen_securities.add(fc["security_id"])
            summary["opened"] += 1
            await _emit_opened(pid, fc, entry_date, entry_price)
        except Exception:  # noqa: BLE001 - one bad forecast must not stop the sweep
            log.exception("opener failed for forecast %s", fc["id"])
    if summary["considered"]:
        # advance the rotation to the last row actually considered (an early
        # cap break leaves the unconsidered remainder first in line after the
        # wrap); scheduling state only — concurrent ticks may race the write,
        # the position inserts above are what actually arbitrate opens
        last = candidates[summary["considered"] - 1]
        await db.execute(
            "INSERT INTO admin_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (OPENER_CURSOR_KEY, json.dumps({"made_at": last["made_at"], "id": last["id"]})),
        )
    return summary


async def open_forecast_position(forecast_id: str) -> dict[str, Any] | None:
    """Open one forecast through the same database-enforced path as the tick.

    This is the operator/API path: validation failures are 400s, while an
    aggregate cap, per-security open-position limit, duplicate forecast, or
    concurrent state change is a 409. Every cap is hard — there is no
    advisory mode; schema uniqueness remains non-bypassable. Backfilled
    forecasts (origin='backfill') are refused: their frozen historical entry
    would be pure look-ahead in the book.
    """
    fc = await db.query_one("SELECT * FROM forecasts WHERE id = ?", (forecast_id,))
    if fc is None:
        return None
    now = bus.now_iso()
    if fc["status"] != "open":
        raise TransitionConflict(f"forecast {forecast_id} is {fc['status']!r}, not open")
    if fc["direction"] not in ("long", "short"):
        raise PaperBookError(f"forecast {forecast_id} direction {fc['direction']!r} is not tradable")
    if fc["origin"] == "backfill":
        raise PaperBookError(
            f"forecast {forecast_id} is a backfill record (origin='backfill'); "
            "backfills never enter the paper book"
        )
    if not fc["security_id"]:
        raise PaperBookError(f"forecast {forecast_id} has no security to open")
    if fc["expires_at"] <= now:
        raise PaperBookError(f"forecast {forecast_id} expired at {fc['expires_at']}")
    entry = await _entry_bar(fc["security_id"], fc["made_at"])
    if entry is None:
        raise PaperBookError(
            f"no usable entry price known for {fc['security_id']} at {fc['made_at']}"
        )
    entry_date, entry_price = entry
    pid = await _insert_position(fc, entry_date, entry_price, await max_positions(), now)
    if pid is None:  # lost race — diagnose which invariant won
        prior = await db.query_one(
            "SELECT id FROM paper_positions WHERE forecast_id = ?", (forecast_id,))
        if prior:
            raise TransitionConflict(f"forecast {forecast_id} already has a paper position")
        other = await db.query_one(
            "SELECT id FROM paper_positions WHERE security_id = ? AND status = 'open'",
            (fc["security_id"],),
        )
        if other:
            raise RiskLimitConflict(
                f"security {fc['security_id']} already has open position {other['id']}"
            )
        raise TransitionConflict(f"opening forecast {forecast_id} lost a concurrent claim")
    await _emit_opened(pid, fc, entry_date, entry_price)
    return await db.query_one(
        "SELECT * FROM paper_positions WHERE forecast_id = ?", (forecast_id,))


# ---- closes ----------------------------------------------------------------------

async def _forecast_analyst(forecast_id: str) -> str | None:
    """Attribution for a closed position (REVIEW-C3 M5): forecast → extraction
    item (0019) → claim row's analyst_id. None for manual/unattributed
    forecasts — the outcome then simply does not flow into any memory."""
    row = await db.query_one(
        "SELECT e.analyst_id FROM forecast_extraction_items i "
        "JOIN forecast_extractions e ON e.id = i.extraction_id "
        "WHERE i.forecast_id = ?", (forecast_id,),
    )
    return (row or {}).get("analyst_id") or None


async def _close(
    pos: dict[str, Any], reason: str, close_price: float | None,
    realized: float | None, now: str,
) -> bool:
    """Conditional claim open→closed; False when the row changed under us."""
    claimed = await db.execute(
        "UPDATE paper_positions SET status = 'closed', close_reason = ?, close_price = ?, "
        "realized_pnl = ?, closed_at = ?, updated_at = ? WHERE id = ? AND status = 'open'",
        (reason, close_price, realized, now, now, pos["id"]),
    )
    if not claimed:
        return False
    await bus.emit("paper_book.closed", "paper_position", pos["id"], {
        "forecast_id": pos["forecast_id"], "security_id": pos["security_id"],
        "direction": pos["direction"], "reason": reason,
        "entry_price": pos["entry_price"], "close_price": close_price,
        "realized_pnl": realized,
        "analyst_id": await _forecast_analyst(pos["forecast_id"]),
    })
    return True


async def _maybe_settle(forecast_id: str) -> str | None:
    """Offer the forecast to B6 settlement — conditionally, never twice.

    Only fires when the forecast is still open AND expired (settle_forecast
    refuses pre-expiry; stop/target closes leave the forecast to expire on
    its own). The open→settled claim lives inside settle_forecast (one
    transaction + UNIQUE(forecast_id) backstop); losing it to a concurrent
    settler is fine — it means the settlement already exists.
    """
    fc = await db.query_one(
        "SELECT status, expires_at FROM forecasts WHERE id = ?", (forecast_id,)
    )
    if fc is None or fc["status"] != "open" or fc["expires_at"] > bus.now_iso():
        return None
    try:
        settled = await forecasts.settle_forecast(forecast_id)
        return (settled or {}).get("settlement", {}).get("verdict")
    except forecasts.TransitionConflict:
        return None  # concurrent settler won — exactly-once holds
    except forecasts.ForecastError as exc:
        log.warning("settle skipped for forecast %s: %s", forecast_id, exc)
    except Exception:  # noqa: BLE001 - settlement must never break the book
        log.exception("settle failed for forecast %s", forecast_id)
    return None


# ---- daily MTM / NAV (00:00 SGT) ---------------------------------------------------

async def _benchmark_nav(wd: str) -> float | None:
    """CSI300 mark <= wd (latest knowledge) normalized to the pinned base.

    The base is pinned in admin_state on FIRST SIGHT of a usable mark — only
    when no base row exists at all, and only the call whose INSERT actually
    landed answers 1.0 (rowcount-checked; a pin-race loser re-reads and
    normalizes against the winner's base — loop-fix R3). A stored base that
    is corrupt or unusable is NOT a first sight (loop-fix P11e): silently
    re-pinning at today's level would quietly reset the whole benchmark_nav
    history, so it fails closed to NULL with a log.error and the row is left
    in place; the deliberate operator repin path is deleting the admin_state
    row. No usable mark → NULL, never a proxy.
    """
    marks = await market_data.get_marks_pit(BENCHMARK_ID, end=wd)
    if not marks:
        return None
    value = marks[-1].get("value")
    if not forecasts._usable_price(value):
        return None
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (BENCH_BASE_KEY,))
    if row is None:
        # first sight: pin the base. The INSERT is the arbiter (rowcount-
        # checked, R3): only the call whose row actually landed may answer
        # 1.0 — a loser that read stale absence must re-read and normalize
        # against the base that REALLY stuck, or it writes an inconsistent
        # benchmark NAV for its own work date.
        pinned = await db.execute(
            "INSERT INTO admin_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (BENCH_BASE_KEY, json.dumps({
                "benchmark_id": BENCHMARK_ID, "mark_date": marks[-1]["mark_date"],
                "value": value,
            })),
        )
        if pinned:
            return 1.0
        row = await db.query_one(
            "SELECT value FROM admin_state WHERE key = ?", (BENCH_BASE_KEY,))
        if row is None:  # pin lost AND row gone again: fail closed, next MTM re-pins
            return None
    try:
        base = json.loads(row["value"]).get("value")
    except Exception:  # noqa: BLE001 - corrupt base: fail closed below
        base = None
    if not forecasts._usable_price(base):
        log.error(
            "benchmark base %r is corrupt/unusable (%r): benchmark_nav fails closed to "
            "NULL; DELETE that admin_state row to re-pin deliberately",
            BENCH_BASE_KEY, row["value"],
        )
        return None
    ratio = value / base
    return ratio if math.isfinite(ratio) else None


async def mark_to_market(wd: str | None = None) -> dict[str, Any]:
    """Mark every open position, apply close triggers, upsert the day's NAV.

    Marks come from the position's own window — the last usable bar dated
    <= min(wd, expiry date) (REVIEW-C3 H2): a late MTM run can never price a
    close off post-expiry bars nor flip a horizon close into stop/target.
    Unknown value is never asserted as 0 (REVIEW-C3 H3): unpriceable
    positions are excluded from nav and counted in n_unpriced; expired ones
    close as 'unpriced' with NULL price/realized. Idempotent per work date
    (nav_history upsert); safe to re-run — already-closed positions stay
    closed (conditional claims), settlement is offered at most once (B6
    guards). Never raises for per-position problems.
    """
    wd = wd or work_date()
    now = bus.now_iso()
    positions = await db.query(
        "SELECT p.*, f.expires_at AS fc_expires_at FROM paper_positions p "
        "JOIN forecasts f ON f.id = p.forecast_id WHERE p.status = 'open' "
        "ORDER BY p.opened_at, p.id",
    )
    unrealized = 0.0
    closed: list[dict[str, Any]] = []
    unpriced_open = 0
    for pos in positions:
        try:
            expired = pos["fc_expires_at"] <= now
            mark = None
            if pos["security_id"]:
                # H2: never read past the forecast's economic window
                mark = await _latest_mark(
                    pos["security_id"], _mark_window(wd, pos["fc_expires_at"])
                )
            ret = None
            if mark is not None:
                ret = _signed_return(pos["direction"], pos["entry_price"], mark[1])
            if ret is None:
                # H3: no usable knowledge — the value is UNKNOWN, not zero.
                # Open: exclude from nav, count as unpriced. Expired: close
                # 'unpriced' with NULL price/realized so a dead security
                # cannot leak the cap forever while the unknown stays
                # unknown (the forecast settles 'invalid' via B6's own
                # fails-closed path).
                if expired:
                    if await _close(pos, "unpriced", None, None, now):
                        closed.append({**pos, "close_reason": "unpriced",
                                       "close_price": None, "realized_pnl": None})
                        await _maybe_settle(pos["forecast_id"])
                else:
                    unpriced_open += 1
                continue
            if ret <= -pos["stop_pct"]:
                reason = "stop"
            elif ret >= pos["target_pct"]:
                reason = "target"
            elif expired:
                reason = "horizon"
            else:
                unrealized += ret * pos["size"]
                continue
            realized = ret * pos["size"]
            if await _close(pos, reason, mark[1], realized, now):
                closed.append({**pos, "close_reason": reason,
                               "close_price": mark[1], "realized_pnl": realized})
                await _maybe_settle(pos["forecast_id"])
        except Exception:  # noqa: BLE001 - one bad position must not stop the mark
            log.exception("MTM failed for position %s", pos["id"])

    # SUM skips NULL realized_pnl by SQL semantics; the WHERE makes the
    # H3 contract explicit — 'unpriced' closes never enter the aggregate
    agg = await db.query_one(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS realized FROM paper_positions "
        "WHERE status = 'closed' AND realized_pnl IS NOT NULL"
    )
    realized_cum = float(agg["realized"] if agg else 0.0)
    unpriced_closed = await db.query_one(
        "SELECT COUNT(*) AS n FROM paper_positions "
        "WHERE status = 'closed' AND close_reason = 'unpriced'"
    )
    n_unpriced = unpriced_open + (unpriced_closed["n"] if unpriced_closed else 0)
    open_agg = await db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS gross FROM paper_positions "
        "WHERE status = 'open'"
    )
    n_open = open_agg["n"] if open_agg else 0
    gross = float(open_agg["gross"] if open_agg else 0.0)
    nav = 1.0 + realized_cum + unrealized
    if not math.isfinite(nav):  # only reachable through corrupt stored numbers
        log.error("NAV for %s is not finite (realized=%r unrealized=%r); skipping write",
                  wd, realized_cum, unrealized)
        return {"work_date": wd, "error": "nav not finite", "closed": len(closed)}
    benchmark_nav = await _benchmark_nav(wd)

    await db.execute(
        "INSERT INTO nav_history (work_date, nav, benchmark_nav, gross_exposure, n_open, "
        "n_unpriced, realized_pnl_cum, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(work_date) DO UPDATE SET nav = excluded.nav, "
        "benchmark_nav = excluded.benchmark_nav, gross_exposure = excluded.gross_exposure, "
        "n_open = excluded.n_open, n_unpriced = excluded.n_unpriced, "
        "realized_pnl_cum = excluded.realized_pnl_cum, updated_at = excluded.updated_at",
        (wd, nav, benchmark_nav, gross, n_open, n_unpriced, realized_cum, now, now),
    )
    summary = {
        "work_date": wd, "nav": nav, "benchmark_nav": benchmark_nav,
        "n_open": n_open, "gross_exposure": gross, "realized_pnl_cum": realized_cum,
        "unrealized": unrealized, "closed": len(closed), "n_unpriced": n_unpriced,
    }
    await bus.emit("paper_book.marked", "paper_book", wd, {
        **{k: v for k, v in summary.items() if k != "closed"},
        "closed": [
            {"position_id": c["id"], "forecast_id": c["forecast_id"],
             "security_id": c["security_id"], "reason": c["close_reason"],
             "close_price": c["close_price"], "realized_pnl": c["realized_pnl"]}
            for c in closed
        ],
    })
    return summary


# ---- manual close (API) --------------------------------------------------------------

async def close_position(position_id: str) -> dict[str, Any] | None:
    """Operator close at the latest usable mark inside the position's window
    (reason='manual'). The window clamp (H2) holds here too: closing long
    after expiry still prices from bars dated <= the expiry date.

    Fails closed: an unpriceable position (deleted security / no usable
    bars) is REFUSED, never closed at an invented price — free the slot by
    waiting for data or letting the expiry path take it ('unpriced').
    """
    pos = await db.query_one("SELECT * FROM paper_positions WHERE id = ?", (position_id,))
    if pos is None:
        return None
    if pos["status"] != "open":
        raise TransitionConflict(f"position {position_id} is already closed")
    if not pos["security_id"]:
        raise PaperBookError(
            f"position {position_id} has no security (deleted); no usable price to close at"
        )
    fc = await db.query_one(
        "SELECT expires_at FROM forecasts WHERE id = ?", (pos["forecast_id"],))
    end = _mark_window(work_date(), fc["expires_at"]) if fc else work_date()
    mark = await _latest_mark(pos["security_id"], end)
    if mark is None:
        raise PaperBookError(
            f"no usable price known for {pos['security_id']}; cannot close manually"
        )
    ret = _signed_return(pos["direction"], pos["entry_price"], mark[1])
    if ret is None:  # defensive: entry passed the whitelist at open
        raise PaperBookError(f"position {position_id} return is not computable")
    now = bus.now_iso()
    if not await _close(pos, "manual", mark[1], ret * pos["size"], now):
        raise TransitionConflict(f"position {position_id} changed concurrently; reload and retry")
    await _maybe_settle(pos["forecast_id"])
    return await get_position(position_id)


# ---- historical reconciliation -----------------------------------------------------

def _reconcile_close_decision(
    pos: dict[str, Any],
    mark: tuple[str, float] | None,
    expired: bool,
) -> tuple[str, float | None, float | None] | None:
    """Apply the live MTM close state machine without writing anything."""
    ret = _signed_return(pos["direction"], pos["entry_price"], mark[1]) \
        if mark is not None else None
    if ret is None:
        return ("unpriced", None, None) if expired else None
    if ret <= -pos["stop_pct"]:
        reason = "stop"
    elif ret >= pos["target_pct"]:
        reason = "target"
    elif expired:
        reason = "horizon"
    else:
        return None
    return reason, mark[1], ret * pos["size"]


async def _reconcile_open_decision(
    pos: dict[str, Any],
    wd: str,
    expired: bool,
) -> tuple[str, float | None, float | None, str | None] | None:
    """Replay daily PIT marks and return the first missed close transition."""
    if not pos["security_id"]:
        return ("unpriced", None, None, None) if expired else None
    end = _mark_window(wd, pos["fc_expires_at"])
    bars = await market_data.get_bars_pit(
        pos["security_id"], start=pos["entry_date"], end=end
    )
    for bar in bars:
        price = _bar_price(bar)
        ret = _signed_return(pos["direction"], pos["entry_price"], price)
        if ret is None:
            continue
        if ret <= -pos["stop_pct"]:
            return "stop", price, ret * pos["size"], bar["bar_date"]
        if ret >= pos["target_pct"]:
            return "target", price, ret * pos["size"], bar["bar_date"]
    if not expired:
        return None
    # Horizon uses the newest row's latest-known version exactly like live
    # MTM. If that endpoint is unusable, do not fall back to an older price.
    mark = None
    if bars:
        price = _bar_price(bars[-1])
        if price is not None:
            mark = (bars[-1]["bar_date"], price)
    decision = _reconcile_close_decision(pos, mark, True)
    if decision is None:  # expired always yields horizon or unpriced
        return None
    return (*decision, mark[0] if mark is not None else None)


async def _historical_trigger_mark(
    pos: dict[str, Any],
    end: str,
) -> tuple[str, float] | None:
    """First current-known bar satisfying a stored stop/target close reason."""
    bars = await market_data.get_bars_pit(
        pos["security_id"], start=pos["entry_date"], end=end
    )
    for bar in bars:
        price = _bar_price(bar)
        ret = _signed_return(pos["direction"], pos["entry_price"], price)
        if ret is None:
            continue
        if pos["close_reason"] == "stop" and ret <= -pos["stop_pct"]:
            return bar["bar_date"], price
        if pos["close_reason"] == "target" and ret >= pos["target_pct"]:
            return bar["bar_date"], price
    return None


def _difference_ref(
    position_id: str,
    mark_date: str,
    recorded_price: float | None,
    current_price: float,
) -> str:
    """Stable identity for one observed PIT correction.

    A later version for the same valid-time bar has a different current price
    and therefore a different id; replaying the same knowledge state gets the
    same id and is an idempotent no-op.
    """
    recorded = "none" if recorded_price is None else float(recorded_price).hex()
    raw = f"{position_id}|{mark_date}|{recorded}|{float(current_price).hex()}"
    return f"{position_id}:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


async def _record_reconcile_difference(
    ref_id: str,
    payload: dict[str, Any],
    now: str,
) -> int:
    """Append one durable audit event, atomically deduplicated by stable ref."""
    event_type, ref_kind = RECONCILE_DIFFERENCE_EVENT, "paper_position"
    return await db.execute(
        "INSERT INTO events (type, ref_kind, ref_id, payload, created_at) "
        "SELECT ?,?,?,?,? WHERE NOT EXISTS ("
        "SELECT 1 FROM events WHERE type = ? AND ref_kind = ? AND ref_id = ?)",
        (
            event_type, ref_kind, ref_id,
            json.dumps(payload, ensure_ascii=False, allow_nan=False), now,
            event_type, ref_kind, ref_id,
        ),
    )


async def reconcile(*, dry_run: bool = False, wd: str | None = None) -> dict[str, Any]:
    """Repair missed closes/settlements and audit PIT price corrections.

    Every historical position is inspected under the same window and close
    precedence as live MTM. Open positions that now satisfy stop/target or
    expiry are closed conditionally; every expired linked forecast still open
    is settled. Closed-position prices are compared with the latest currently
    known PIT version at their historical close window. A mismatch NEVER
    rewrites ``paper_positions`` or ``forecast_settlements``: it appends a
    deduplicated ``paper_book.reconcile_difference`` event instead.

    ``dry_run`` performs the identical reads/decisions but makes no writes.
    Sequential replays are idempotent: conditional status claims remove
    completed close/settle work, and discrepancy event refs are deterministic.
    """
    wd = wd or work_date()
    now = bus.now_iso()
    positions = await db.query(
        "SELECT p.*, f.expires_at AS fc_expires_at, f.status AS fc_status "
        "FROM paper_positions p JOIN forecasts f ON f.id = p.forecast_id "
        "ORDER BY p.opened_at, p.id"
    )
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "work_date": wd,
        "considered": len(positions),
        "closed": 0,
        "settled": 0,
        "differences_found": 0,
        "differences_recorded": 0,
        "would_close": 0,
        "would_settle": 0,
        "would_record_differences": 0,
        "operations": 0,
        "planned_operations": 0,
        "actions": [],
        "differences": [],
        "errors": [],
    }

    # First repair state transitions. The snapshot may race another worker;
    # _close and settle_forecast are conditional claims, so only one wins.
    for pos in positions:
        expired = pos["fc_expires_at"] <= now
        try:
            if pos["status"] == "open":
                decision = await _reconcile_open_decision(pos, wd, expired)
                if decision is not None:
                    reason, close_price, realized, mark_date = decision
                    action = {
                        "position_id": pos["id"],
                        "forecast_id": pos["forecast_id"],
                        "action": "close",
                        "reason": reason,
                        "source_mark_date": mark_date,
                        "close_price": close_price,
                        "realized_pnl": realized,
                    }
                    summary["actions"].append(action)
                    if dry_run:
                        summary["would_close"] += 1
                    elif await _close(pos, reason, close_price, realized, now):
                        summary["closed"] += 1

            # A stop/target/manual close before expiry deliberately did not
            # settle then; reconciliation picks it up once expiry has arrived.
            if expired and pos["fc_status"] == "open":
                if dry_run:
                    summary["would_settle"] += 1
                    summary["actions"].append({
                        "position_id": pos["id"],
                        "forecast_id": pos["forecast_id"],
                        "action": "settle",
                    })
                else:
                    try:
                        settled = await forecasts.settle_forecast(
                            pos["forecast_id"], note="paper-book reconciliation"
                        )
                        if settled is not None:
                            summary["settled"] += 1
                    except forecasts.TransitionConflict:
                        pass  # another reconciler/settler won the same claim
                    except forecasts.ForecastError as exc:
                        summary["errors"].append({
                            "position_id": pos["id"],
                            "stage": "settle",
                            "error": str(exc),
                        })
        except Exception as exc:  # noqa: BLE001 - one corrupt historical row must not stop the sweep
            log.exception("reconcile transition failed for position %s", pos["id"])
            summary["errors"].append({
                "position_id": pos["id"],
                "stage": "transition",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Then compare rows that were already historical at sweep start. Positions
    # closed above used this exact current mark and cannot differ on this run.
    for pos in positions:
        if pos["status"] != "closed" or not pos["security_id"]:
            continue
        try:
            if pos["close_reason"] in ("horizon", "unpriced"):
                comparison_end = pos["fc_expires_at"][:10]
            else:
                close_day = str(pos["closed_at"] or pos["fc_expires_at"])[:10]
                comparison_end = _mark_window(close_day, pos["fc_expires_at"])
            mark = None
            if pos["close_reason"] in ("stop", "target"):
                mark = await _historical_trigger_mark(pos, comparison_end)
            if mark is None:
                mark = await _latest_mark(pos["security_id"], comparison_end)
            if mark is None:
                continue  # current source is still unknown: no discrepancy can be asserted
            recorded = pos["close_price"] if forecasts._usable_price(pos["close_price"]) else None
            if recorded is not None and math.isclose(
                float(recorded), mark[1], rel_tol=1e-12, abs_tol=0.0
            ):
                continue
            ref_id = _difference_ref(pos["id"], mark[0], recorded, mark[1])
            prior = await db.query_one(
                "SELECT 1 AS x FROM events "
                "WHERE type = ? AND ref_kind = 'paper_position' AND ref_id = ?",
                (RECONCILE_DIFFERENCE_EVENT, ref_id),
            )
            difference = {
                "position_id": pos["id"],
                "forecast_id": pos["forecast_id"],
                "security_id": pos["security_id"],
                "close_reason": pos["close_reason"],
                "comparison_end": comparison_end,
                "source_mark_date": mark[0],
                "recorded_close_price": recorded,
                "current_known_price": mark[1],
                "event_ref": ref_id,
                "already_recorded": prior is not None,
            }
            summary["differences"].append(difference)
            if prior is None:
                if dry_run:
                    summary["would_record_differences"] += 1
                else:
                    summary["differences_recorded"] += await _record_reconcile_difference(
                        ref_id, difference, now
                    )
        except Exception as exc:  # noqa: BLE001 - one bad comparison must not stop repairs
            log.exception("reconcile price comparison failed for position %s", pos["id"])
            summary["errors"].append({
                "position_id": pos["id"],
                "stage": "price_comparison",
                "error": f"{type(exc).__name__}: {exc}",
            })

    summary["differences_found"] = len(summary["differences"])
    summary["operations"] = (
        summary["closed"] + summary["settled"] + summary["differences_recorded"]
    )
    summary["planned_operations"] = (
        summary["would_close"] + summary["would_settle"]
        + summary["would_record_differences"]
    )
    return summary


# ---- reads ----------------------------------------------------------------------------

async def get_position(position_id: str) -> dict[str, Any] | None:
    return await db.query_one("SELECT * FROM paper_positions WHERE id = ?", (position_id,))


async def list_positions(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if status and status not in STATUSES:
        raise PaperBookError(f"unknown status {status!r}; allowed: {', '.join(STATUSES)}")
    sql = "SELECT * FROM paper_positions"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY opened_at DESC, id LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return await db.query(sql, params)


async def nav_series(days: int = 90) -> list[dict[str, Any]]:
    """The most recent N nav_history rows, ascending by work_date."""
    rows = await db.query(
        "SELECT * FROM nav_history ORDER BY work_date DESC LIMIT ?",
        (min(max(days, 1), 3650),),
    )
    return list(reversed(rows))


# ---- journal rendering (consumed by the vault exporter handler) ------------------------

def _fmt(val: Any, nd: int = 4) -> str:
    if val is None:
        return "—"
    return f"{float(val):,.{nd}f}".rstrip("0").rstrip(".")


def _sgt_day_window(wd: str) -> tuple[str, str]:
    """UTC ISO [start, end) of one SGT calendar day (fixed +8, no DST).

    NOT the ±8h prefix-skew shortcut the daily caps use: the MTM job fires at
    00:00 SGT — the PREVIOUS UTC date — so a UTC-prefix match would
    systematically drop every close the job itself makes from that day's
    journal. The explicit window is exact.
    """
    d = date.fromisoformat(wd)
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(hours=8)
    return (
        start.isoformat(timespec="seconds"),
        (start + timedelta(days=1)).isoformat(timespec="seconds"),
    )


async def render_journal(wd: str | None = None) -> str:
    """Markdown journal for one SGT work date: opens, closes, NAV summary.

    Opened/closed rows are matched against the exact SGT day window (see
    _sgt_day_window). Returns "" when the date has no book activity and no
    NAV row (the exporter then skips the note).
    """
    wd = wd or work_date()
    lo, hi = _sgt_day_window(wd)
    opened = await db.query(
        "SELECT * FROM paper_positions WHERE opened_at >= ? AND opened_at < ? "
        "ORDER BY opened_at, id",
        (lo, hi),
    )
    closed = await db.query(
        "SELECT * FROM paper_positions WHERE closed_at >= ? AND closed_at < ? "
        "ORDER BY closed_at, id",
        (lo, hi),
    )
    nav_row = await db.query_one("SELECT * FROM nav_history WHERE work_date = ?", (wd,))
    if not opened and not closed and nav_row is None:
        return ""

    lines = [f"# 纸面交易日志 · {wd}", ""]
    if nav_row:
        bench = _fmt(nav_row["benchmark_nav"]) if nav_row["benchmark_nav"] is not None \
            else "—（无基准数据）"
        lines += [
            "## NAV",
            "",
            f"- NAV：{_fmt(nav_row['nav'])}（基准 {BENCHMARK_ID}：{bench}）",
            f"- 持仓：{nav_row['n_open']} 个 · 总敞口 {_fmt(nav_row['gross_exposure'], 2)}",
            f"- 累计已实现盈亏：{_fmt(nav_row['realized_pnl_cum'])}（仅计已定价平仓）",
        ]
        n_unpriced = nav_row.get("n_unpriced") or 0
        if n_unpriced:
            lines.append(
                f"- ⚠ 不完整：{n_unpriced} 个仓位价值未知（unpriced），未计入 NAV"
            )
        lines.append("")
    if opened:
        lines += ["## 当日开仓", ""]
        for p in opened:
            lines.append(
                f"- `{p['security_id'] or '（已删标的）'}` {p['direction']} · "
                f"入场 {_fmt(p['entry_price'])}（{p['entry_date']}）· "
                f"止损 {p['stop_pct']:.0%} / 止盈 {p['target_pct']:.0%} · "
                f"forecast `{p['forecast_id']}`"
            )
        lines.append("")
    if closed:
        lines += ["## 当日平仓", ""]
        for p in closed:
            price = _fmt(p["close_price"]) if p["close_price"] is not None else "—（无可用价）"
            pnl = f"{p['realized_pnl']:+.4f}" if p["realized_pnl"] is not None \
                else "未知（不计入 NAV）"
            lines.append(
                f"- `{p['security_id'] or '（已删标的）'}` {p['direction']} · "
                f"{p['close_reason']} 平仓 @ {price} · 盈亏 {pnl} · "
                f"forecast `{p['forecast_id']}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
