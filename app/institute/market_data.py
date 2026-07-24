"""Local point-in-time market data store (card M4-001).

Calendar, PIT price bars, benchmarks + marks, and per-security suspensions
over migrations/0006_market_data.sql. Pure local read/write — fetchers are
later cards: nothing here touches the network, and ingest functions accept an
explicit ``as_known_at`` so a historical revision stream can be backfilled.

PIT semantics (price_bars / benchmark_marks): a correction never overwrites —
it appends a row with the same natural key (security_id, freq, bar_date) and
a later ``as_known_at``. Version rows are IMMUTABLE: on a full version-key
collision, an exact-payload replay returns the existing row (idempotent) and
anything else raises TransitionConflict (409) — the correct move is a new
version with a later as_known_at. ``get_bars_pit(security_id, as_of=T)``
answers "what did we know at T": per bar_date, the version with the greatest
as_known_at <= T; ``as_of=None`` means "latest known". PIT timestamps
(valid_time / as_known_at / as_of) are normalized to ONE fixed-width shape —
microseconds precision, UTC +00:00 offset — before storage or comparison, so
lexicographic order == time order with no mixed-precision edge cases; the
default as_known_at clock is microsecond-precision (see _now_known_iso) so
two corrections inside one second get distinct version keys. A bare-date
as_of (YYYY-MM-DD) means that day 00:00:00 UTC — corrections landing later
that day are not yet known.

No bus events here: bar/mark ingest is bulk machine traffic, not a
user-visible change — batch-level events belong to the fetcher cards.
corporate_actions is schema-only for now (0006); its ingest functions arrive
with the fetcher card too.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .. import bus, db
from ..util import new_id

MARKETS = {"CN_A", "HK", "US", "GLOBAL_CONTEXT"}
# open set in the schema (an additive-only migration cannot widen a CHECK);
# the domain gate is what actually restricts freq — extend here when intraday
# bars land
FREQS = {"1d"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MarketDataError(ValueError):
    """Validation failure (the API maps this to 400)."""


class TransitionConflict(MarketDataError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


# ---- helpers ---------------------------------------------------------------

def _validate_enum(value: Any, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise MarketDataError(f"unknown {label} {value!r}; allowed: {', '.join(sorted(allowed))}")


def _require_date(val: Any, label: str) -> str:
    raw = str(val or "").strip()
    if not _DATE_RE.match(raw):
        raise MarketDataError(f"{label} must be YYYY-MM-DD, got {raw!r}")
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise MarketDataError(f"{label} {raw!r} is not a real calendar date") from None
    return raw


def _norm_ts(val: Any, label: str) -> str:
    """Normalize an ISO timestamp (or bare date = that day 00:00:00 UTC; naive
    = UTC) to ONE fixed-width shape: microseconds precision, +00:00 offset.
    Sub-second input is preserved, never truncated. A single shape for every
    stored PIT timestamp AND every as_of keeps string comparison == time
    comparison — mixing second- and microsecond-precision strings would make
    equal instants ('T08:00:00+00:00' vs 'T08:00:00.000000+00:00') compare
    unequal at the '+' vs '.' boundary."""
    raw = str(val or "").strip()
    if not raw:
        raise MarketDataError(f"{label} must be an ISO-8601 timestamp")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise MarketDataError(f"{label} {raw!r} is not ISO-8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _now_known_iso() -> str:
    """Default as_known_at clock. bus.now_iso() is second-precision — two
    corrections of the same bar inside one second would collide on the UNIQUE
    version key — so the version-key clock alone uses microseconds. Same
    ISO-8601 UTC +00:00 format, so the house string-comparison convention
    holds; created_at/updated_at everywhere still use bus.now_iso()."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _require_number(val: Any, label: str, *, nullable: bool = False) -> float | None:
    if val is None and nullable:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        raise MarketDataError(f"{label} must be a number") from None


def _require_dict(val: Any, label: str) -> dict:
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise MarketDataError(f"{label} must be an object")
    return val


def _loads(text: str | None, fallback: Any) -> Any:
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback


def _dumps(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False)


def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if "metadata_json" in out:
        out["metadata"] = _require_dict(_loads(out.pop("metadata_json", None), {}), "metadata")
    return out


async def _check_security(security_id: str) -> None:
    """Readable 400 instead of a raw FK IntegrityError 500."""
    if not await db.query_one("SELECT id FROM securities WHERE id = ?", (security_id,)):
        raise MarketDataError(f"security {security_id!r} not found")


def _map_integrity(exc: sqlite3.IntegrityError, label: str) -> MarketDataError:
    """Concurrent writers can slip past the pre-checks (reads happen before the
    write lock), so constraint failures map onto readable errors, never a 500."""
    msg = str(exc)
    if "FOREIGN KEY" in msg:
        return MarketDataError(f"{label}: referenced row disappeared (foreign key)")
    return MarketDataError(f"{label}: constraint failed: {msg}")


def _require_replay_match(
    existing: dict[str, Any], incoming: dict[str, Any], *, label: str, key_desc: str
) -> None:
    """Version rows are immutable. A full version-key collision is legal only
    as an exact-payload replay (idempotent re-ingest of the same facts); any
    field difference is a conflict — the correct move is a NEW version with a
    later as_known_at, never a rewrite of what was known."""
    diffs = sorted(k for k, v in incoming.items() if existing.get(k) != v)
    if diffs:
        raise TransitionConflict(
            f"{label} version {key_desc} already exists with different {', '.join(diffs)}; "
            "PIT versions are immutable — write a correction with a later as_known_at"
        )


# ---- trading calendar -------------------------------------------------------

async def set_calendar_day(
    market: str,
    cal_date: str,
    is_open: bool,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Upsert one market day (PRIMARY KEY (market, cal_date) is the target)."""
    _validate_enum(market, MARKETS, "market")
    cal_date = _require_date(cal_date, "cal_date")
    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO trading_calendar (market, cal_date, is_open, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(market, cal_date) DO UPDATE SET is_open = excluded.is_open, "
            "note = excluded.note, updated_at = excluded.updated_at",
            (market, cal_date, 1 if is_open else 0, note, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise _map_integrity(exc, "calendar day") from exc
    row = await db.query_one(
        "SELECT * FROM trading_calendar WHERE market = ? AND cal_date = ?", (market, cal_date)
    )
    assert row is not None
    return dict(row)


async def get_calendar(
    market: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    _validate_enum(market, MARKETS, "market")
    clauses, params = ["market = ?"], [market]
    if start:
        clauses.append("cal_date >= ?")
        params.append(_require_date(start, "start"))
    if end:
        clauses.append("cal_date <= ?")
        params.append(_require_date(end, "end"))
    return await db.query(
        f"SELECT * FROM trading_calendar WHERE {' AND '.join(clauses)} ORDER BY cal_date", params
    )


async def is_trading_day(market: str, cal_date: str) -> bool | None:
    """True/False when the calendar knows the day; None when it has no row."""
    _validate_enum(market, MARKETS, "market")
    cal_date = _require_date(cal_date, "cal_date")
    row = await db.query_one(
        "SELECT is_open FROM trading_calendar WHERE market = ? AND cal_date = ?", (market, cal_date)
    )
    return None if row is None else bool(row["is_open"])


# ---- security suspensions ----------------------------------------------------

async def add_suspension(
    security_id: str,
    start_date: str,
    end_date: str | None = None,
    *,
    reason: str = "",
    source: str | None = None,
) -> dict[str, Any]:
    """Record a halt interval (inclusive dates; end_date None = still halted).

    Overlapping intervals are not rejected — the source of truth is upstream
    exchange data (fetcher cards); is_suspended() stays correct regardless.
    """
    start_date = _require_date(start_date, "start_date")
    if end_date is not None:
        end_date = _require_date(end_date, "end_date")
        if end_date < start_date:
            raise MarketDataError(f"end_date {end_date} is before start_date {start_date}")
    await _check_security(security_id)
    sid = new_id()
    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO security_suspensions (id, security_id, start_date, end_date, reason, source, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, security_id, start_date, end_date, reason or "", source, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise _map_integrity(exc, "suspension") from exc
    row = await db.query_one("SELECT * FROM security_suspensions WHERE id = ?", (sid,))
    assert row is not None
    return dict(row)


async def close_suspension(suspension_id: str, end_date: str) -> dict[str, Any] | None:
    """End an open-ended halt — a conditional claim on end_date IS NULL."""
    end_date = _require_date(end_date, "end_date")
    row = await db.query_one("SELECT * FROM security_suspensions WHERE id = ?", (suspension_id,))
    if row is None:
        return None
    if row["end_date"] is not None:
        raise TransitionConflict(f"suspension {suspension_id} already closed at {row['end_date']}")
    if end_date < row["start_date"]:
        raise MarketDataError(f"end_date {end_date} is before start_date {row['start_date']}")
    claimed = await db.execute(
        "UPDATE security_suspensions SET end_date = ?, updated_at = ? WHERE id = ? AND end_date IS NULL",
        (end_date, bus.now_iso(), suspension_id),
    )
    if not claimed:
        raise TransitionConflict(f"suspension {suspension_id} changed concurrently; reload and retry")
    return await db.query_one("SELECT * FROM security_suspensions WHERE id = ?", (suspension_id,))


async def list_suspensions(
    security_id: str | None = None,
    active_on: str | None = None,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if security_id:
        clauses.append("security_id = ?")
        params.append(security_id)
    if active_on:
        d = _require_date(active_on, "active_on")
        clauses.append("start_date <= ? AND (end_date IS NULL OR end_date >= ?)")
        params.extend([d, d])
    sql = "SELECT * FROM security_suspensions"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return await db.query(sql + " ORDER BY start_date, id", params)


async def is_suspended(security_id: str, cal_date: str) -> bool:
    d = _require_date(cal_date, "cal_date")
    row = await db.query_one(
        "SELECT 1 AS hit FROM security_suspensions "
        "WHERE security_id = ? AND start_date <= ? AND (end_date IS NULL OR end_date >= ?) LIMIT 1",
        (security_id, d, d),
    )
    return row is not None


async def get_trading_status(security_id: str, cal_date: str) -> dict[str, Any] | None:
    """The acceptance view — closed vs suspended on one day, combined.

    market_open is None when the calendar has no row for that day (unknown);
    tradable is decidable (False) even then if the security is suspended.
    """
    cal_date = _require_date(cal_date, "cal_date")
    sec = await db.query_one("SELECT id, market FROM securities WHERE id = ?", (security_id,))
    if sec is None:
        return None
    market_open = await is_trading_day(sec["market"], cal_date)
    suspended = await is_suspended(security_id, cal_date)
    if suspended or market_open is False:
        tradable: bool | None = False
    else:
        tradable = None if market_open is None else True
    return {
        "security_id": security_id,
        "market": sec["market"],
        "cal_date": cal_date,
        "market_open": market_open,
        "suspended": suspended,
        "tradable": tradable,
    }


# ---- price bars (PIT) ---------------------------------------------------------

async def upsert_bar(fields: dict[str, Any]) -> dict[str, Any]:
    """Ingest one bar version. The UNIQUE target is the FULL version key
    (security_id, freq, bar_date, as_known_at) and version rows are IMMUTABLE:
    an exact-payload replay is an idempotent no-op returning the existing row;
    the same key with different facts raises TransitionConflict (409) — a
    correction must arrive as a NEW version with a later as_known_at, so
    history is never overwritten.
    """
    data = dict(fields or {})
    security_id = str(data.pop("security_id", "") or "").strip()
    if not security_id:
        raise MarketDataError("a bar needs a security_id")
    freq = data.pop("freq", "1d")
    _validate_enum(freq, FREQS, "freq")
    bar_date = _require_date(data.pop("bar_date", None), "bar_date")

    # non-nullable _require_number never returns None — these are floats
    prices = {k: _require_number(data.pop(k, None), k) for k in ("open", "high", "low", "close")}
    if prices["high"] < prices["low"]:
        raise MarketDataError(f"high {prices['high']} is below low {prices['low']}")
    volume = _require_number(data.pop("volume", None), "volume", nullable=True)
    if volume is not None and volume < 0:
        raise MarketDataError("volume cannot be negative")
    adj_factor = _require_number(data.pop("adj_factor", 1.0), "adj_factor")
    if adj_factor is None or adj_factor <= 0:
        raise MarketDataError("adj_factor must be > 0")

    # None = defaulted; anything else (even "") goes through _norm_ts validation
    valid_time_in = data.pop("valid_time", None)
    as_known_at_in = data.pop("as_known_at", None)
    valid_time = _norm_ts(bar_date if valid_time_in is None else valid_time_in, "valid_time")
    as_known_at = _norm_ts(_now_known_iso() if as_known_at_in is None else as_known_at_in, "as_known_at")
    source = data.pop("source", None)
    metadata = _require_dict(data.pop("metadata", None), "metadata")
    if data:
        raise MarketDataError(f"unknown bar fields: {', '.join(sorted(data))}")

    await _check_security(security_id)
    try:
        inserted = await db.execute(
            "INSERT INTO price_bars (id, security_id, freq, bar_date, open, high, low, close, volume, "
            "adj_factor, valid_time, as_known_at, source, metadata_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(security_id, freq, bar_date, as_known_at) DO NOTHING",
            (
                new_id(), security_id, freq, bar_date, prices["open"], prices["high"], prices["low"],
                prices["close"], volume, adj_factor, valid_time, as_known_at, source, _dumps(metadata),
                bus.now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise _map_integrity(exc, "bar") from exc
    row = await db.query_one(
        "SELECT * FROM price_bars WHERE security_id = ? AND freq = ? AND bar_date = ? AND as_known_at = ?",
        (security_id, freq, bar_date, as_known_at),
    )
    assert row is not None
    out = _row_out(row)
    if not inserted:  # version-key collision: legal only as an exact replay
        _require_replay_match(
            out,
            {**prices, "volume": volume, "adj_factor": adj_factor, "valid_time": valid_time,
             "source": source, "metadata": metadata},
            label="bar",
            key_desc=f"({security_id}, {freq}, {bar_date}, {as_known_at})",
        )
    return out


async def get_bars_pit(
    security_id: str,
    as_of: str | None = None,
    *,
    freq: str = "1d",
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Bars as they were known at ``as_of`` (None = latest known).

    Per bar_date: the version with the greatest as_known_at <= as_of. A bar
    whose every version is newer than as_of does not exist yet at that point
    in time and is omitted — "as_of earlier than the correction sees the old
    version" falls out of the same rule.
    """
    _validate_enum(freq, FREQS, "freq")
    clauses, params = ["b.security_id = ?", "b.freq = ?"], [security_id, freq]
    if start:
        clauses.append("b.bar_date >= ?")
        params.append(_require_date(start, "start"))
    if end:
        clauses.append("b.bar_date <= ?")
        params.append(_require_date(end, "end"))
    inner = (
        "SELECT MAX(v.as_known_at) FROM price_bars v WHERE v.security_id = b.security_id "
        "AND v.freq = b.freq AND v.bar_date = b.bar_date"
    )
    if as_of is not None:
        inner += " AND v.as_known_at <= ?"
        params.append(_norm_ts(as_of, "as_of"))
    clauses.append(f"b.as_known_at = ({inner})")
    rows = await db.query(
        f"SELECT * FROM price_bars b WHERE {' AND '.join(clauses)} ORDER BY b.bar_date", params
    )
    return [_row_out(r) for r in rows]


async def get_last_bar_pit(
    security_id: str,
    as_of: str | None = None,
    *,
    freq: str = "1d",
    end: str | None = None,
) -> dict[str, Any] | None:
    """The single newest bar known at ``as_of`` dated <= ``end`` — exactly
    ``get_bars_pit(security_id, as_of, freq=freq, end=end)[-1]`` (or None),
    without materializing the full history (loop-fix P8c: the paper book's
    entry/mark reads only ever need the last bar). Same PIT rule per
    bar_date (greatest as_known_at <= as_of); a bar whose EVERY version is
    newer than as_of does not exist yet at that knowledge time, so the read
    falls back to the next older bar_date — the anti-look-ahead fallback the
    B6 entry leg depends on."""
    _validate_enum(freq, FREQS, "freq")
    clauses, params = ["b.security_id = ?", "b.freq = ?"], [security_id, freq]
    if end:
        clauses.append("b.bar_date <= ?")
        params.append(_require_date(end, "end"))
    inner = (
        "SELECT MAX(v.as_known_at) FROM price_bars v WHERE v.security_id = b.security_id "
        "AND v.freq = b.freq AND v.bar_date = b.bar_date"
    )
    if as_of is not None:
        inner += " AND v.as_known_at <= ?"
        params.append(_norm_ts(as_of, "as_of"))
    clauses.append(f"b.as_known_at = ({inner})")
    row = await db.query_one(
        f"SELECT * FROM price_bars b WHERE {' AND '.join(clauses)} "
        "ORDER BY b.bar_date DESC LIMIT 1", params
    )
    return _row_out(row) if row is not None else None


# ---- benchmarks ---------------------------------------------------------------

async def upsert_benchmark(fields: dict[str, Any]) -> dict[str, Any]:
    """Create or refresh a benchmark. Deliberately NOT a securities row —
    benchmark ids live in their own namespace (M4-001 acceptance)."""
    data = dict(fields or {})
    bid = str(data.pop("id", "") or "").strip()
    if not bid:
        raise MarketDataError("a benchmark needs an id (e.g. CSI300)")
    name_zh = data.pop("name_zh", None)
    name_en = data.pop("name_en", None)
    if not ((name_zh or "").strip() or (name_en or "").strip()):
        raise MarketDataError("a benchmark needs a name_zh or name_en")
    market = data.pop("market", None)
    if market is not None:
        _validate_enum(market, MARKETS, "market")
    currency = data.pop("currency", None)
    source = data.pop("source", "manual")
    metadata = _require_dict(data.pop("metadata", None), "metadata")
    if data:
        raise MarketDataError(f"unknown benchmark fields: {', '.join(sorted(data))}")

    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO benchmarks (id, name_zh, name_en, market, currency, source, metadata_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name_zh = excluded.name_zh, name_en = excluded.name_en, "
            "market = excluded.market, currency = excluded.currency, source = excluded.source, "
            "metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
            (bid, name_zh, name_en, market, currency, source, _dumps(metadata), now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise _map_integrity(exc, "benchmark") from exc
    row = await db.query_one("SELECT * FROM benchmarks WHERE id = ?", (bid,))
    assert row is not None
    return _row_out(row)


async def get_benchmark(benchmark_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM benchmarks WHERE id = ?", (benchmark_id,))
    return _row_out(row) if row else None


async def list_benchmarks() -> list[dict[str, Any]]:
    return [_row_out(r) for r in await db.query("SELECT * FROM benchmarks ORDER BY id")]


async def upsert_benchmark_mark(
    benchmark_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Ingest one benchmark mark version — same immutable-version PIT rules as
    bars: exact replay = no-op, same key with different facts = 409."""
    data = dict(fields or {})
    mark_date = _require_date(data.pop("mark_date", None), "mark_date")
    value = _require_number(data.pop("value", None), "value")
    valid_time_in = data.pop("valid_time", None)
    as_known_at_in = data.pop("as_known_at", None)
    valid_time = _norm_ts(mark_date if valid_time_in is None else valid_time_in, "valid_time")
    as_known_at = _norm_ts(_now_known_iso() if as_known_at_in is None else as_known_at_in, "as_known_at")
    source = data.pop("source", None)
    metadata = _require_dict(data.pop("metadata", None), "metadata")
    if data:
        raise MarketDataError(f"unknown mark fields: {', '.join(sorted(data))}")

    if not await db.query_one("SELECT id FROM benchmarks WHERE id = ?", (benchmark_id,)):
        raise MarketDataError(f"benchmark {benchmark_id!r} not found")
    try:
        inserted = await db.execute(
            "INSERT INTO benchmark_marks (id, benchmark_id, mark_date, value, valid_time, as_known_at, "
            "source, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(benchmark_id, mark_date, as_known_at) DO NOTHING",
            (new_id(), benchmark_id, mark_date, value, valid_time, as_known_at, source, _dumps(metadata),
             bus.now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        raise _map_integrity(exc, "benchmark mark") from exc
    row = await db.query_one(
        "SELECT * FROM benchmark_marks WHERE benchmark_id = ? AND mark_date = ? AND as_known_at = ?",
        (benchmark_id, mark_date, as_known_at),
    )
    assert row is not None
    out = _row_out(row)
    if not inserted:  # version-key collision: legal only as an exact replay
        _require_replay_match(
            out,
            {"value": value, "valid_time": valid_time, "source": source, "metadata": metadata},
            label="benchmark mark",
            key_desc=f"({benchmark_id}, {mark_date}, {as_known_at})",
        )
    return out


async def get_marks_pit(
    benchmark_id: str,
    as_of: str | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Marks as known at ``as_of`` (None = latest) — same rule as get_bars_pit."""
    clauses, params = ["m.benchmark_id = ?"], [benchmark_id]
    if start:
        clauses.append("m.mark_date >= ?")
        params.append(_require_date(start, "start"))
    if end:
        clauses.append("m.mark_date <= ?")
        params.append(_require_date(end, "end"))
    inner = (
        "SELECT MAX(v.as_known_at) FROM benchmark_marks v "
        "WHERE v.benchmark_id = m.benchmark_id AND v.mark_date = m.mark_date"
    )
    if as_of is not None:
        inner += " AND v.as_known_at <= ?"
        params.append(_norm_ts(as_of, "as_of"))
    clauses.append(f"m.as_known_at = ({inner})")
    rows = await db.query(
        f"SELECT * FROM benchmark_marks m WHERE {' AND '.join(clauses)} ORDER BY m.mark_date", params
    )
    return [_row_out(r) for r in rows]
