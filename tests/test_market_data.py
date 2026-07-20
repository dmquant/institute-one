"""Market data PIT store (card M4-001): schema + domain module + API.

Covers the three acceptance lines: (a) PIT tables carry valid_time/as_known_at
and an as_of earlier than a correction sees the old version; (b) benchmark
marks are separate from securities; (c) the calendar can represent closed
(market-level) and suspended (per-security) days. Schema-level tests exercise
raw SQL against migrations/0006_market_data.sql; domain/API sections cover
app/institute/market_data.py and app/api/market_data.py. The autouse
``app_runtime`` fixture applies migrations (db.init()) with foreign_keys=ON.

The market router is not yet mounted in app/main.py (mounting is outside this
card's partition — see PATCH-NOTES-A7.md), so API tests build a bare FastAPI
app around the router instead of create_app().
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import market_data


async def _mk_security(sid: str, *, market: str, name_en: str | None = "some name") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_en, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, sid.split(".")[0], market, name_en, now, now),
    )


async def _mk_bar_raw(
    bid: str,
    security_id: str,
    bar_date: str,
    *,
    freq: str = "1d",
    close: float = 10.0,
    valid_time: str | None = "auto",
    as_known_at: str | None = "auto",
) -> None:
    """Raw INSERT so schema tests can probe NOT NULL / UNIQUE directly."""
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO price_bars (id, security_id, freq, bar_date, open, high, low, close, "
        "volume, valid_time, as_known_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            bid, security_id, freq, bar_date, close, close, close, close, 100.0,
            f"{bar_date}T00:00:00+00:00" if valid_time == "auto" else valid_time,
            now if as_known_at == "auto" else as_known_at,
            now,
        ),
    )


def _make_app() -> FastAPI:
    # the router is not mounted in main.py yet (PATCH-NOTES-A7.md), so tests
    # mount it on a bare app; db/migrations come from the autouse fixture
    from app.api import market_data as api_market_data

    app = FastAPI()
    app.include_router(api_market_data.router)
    return app


# ==== acceptance (a): PIT dual time ==========================================

async def test_pit_tables_carry_valid_time_and_as_known_at():
    # both columns are NOT NULL on every PIT table
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_bar_raw("b-1", "NVDA.US", "2026-06-30", valid_time=None)
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_bar_raw("b-1", "NVDA.US", "2026-06-30", as_known_at=None)
    await _mk_bar_raw("b-1", "NVDA.US", "2026-06-30")
    row = await db.query_one("SELECT valid_time, as_known_at FROM price_bars WHERE id = 'b-1'")
    assert row["valid_time"] and row["as_known_at"]

    now = bus.now_iso()
    await db.execute(
        "INSERT INTO benchmarks (id, name_zh, created_at, updated_at) VALUES (?,?,?,?)",
        ("CSI300", "沪深300", now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO benchmark_marks (id, benchmark_id, mark_date, value, valid_time, as_known_at, created_at) "
            "VALUES (?,?,?,?,NULL,?,?)",
            ("m-1", "CSI300", "2026-06-30", 4000.0, now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO corporate_actions (id, security_id, action_type, ex_date, valid_time, as_known_at, created_at) "
            "VALUES (?,?,?,?,?,NULL,?)",
            ("ca-1", "NVDA.US", "split", "2026-06-30", now, now),
        )
    await db.execute(
        "INSERT INTO corporate_actions (id, security_id, action_type, ex_date, ratio, valid_time, as_known_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("ca-1", "NVDA.US", "split", "2026-06-30", 10.0, now, now, now),
    )


async def test_bar_versions_share_natural_key_distinct_as_known_at():
    # the same (security, freq, bar_date) accepts multiple as_known_at versions;
    # an exact duplicate of the full version key is rejected at the raw level
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    await _mk_bar_raw("b-1", "NVDA.US", "2026-06-30", as_known_at="2026-07-01T08:00:00+00:00")
    await _mk_bar_raw("b-2", "NVDA.US", "2026-06-30", as_known_at="2026-07-03T08:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_bar_raw("b-3", "NVDA.US", "2026-06-30", as_known_at="2026-07-01T08:00:00+00:00")
    rows = await db.query("SELECT id FROM price_bars WHERE security_id = 'NVDA.US'")
    assert len(rows) == 2


async def test_pit_as_of_before_correction_sees_old_version():
    # THE acceptance query: a correction appends a later as_known_at version;
    # asking with an as_of between publish and correction returns the original
    await _mk_security("688256.SH", market="CN_A", name_en="Cambricon")
    await market_data.upsert_bar({
        "security_id": "688256.SH", "bar_date": "2026-06-30",
        "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
        "volume": 1000, "as_known_at": "2026-07-01T08:00:00+00:00",
    })
    await market_data.upsert_bar({  # correction two days later
        "security_id": "688256.SH", "bar_date": "2026-06-30",
        "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.8,
        "volume": 1200, "as_known_at": "2026-07-03T08:00:00+00:00",
    })

    # before anything was published: the bar does not exist yet
    assert await market_data.get_bars_pit("688256.SH", as_of="2026-06-30T23:59:59+00:00") == []
    # after publish, before the correction: the OLD version
    bars = await market_data.get_bars_pit("688256.SH", as_of="2026-07-02T00:00:00+00:00")
    assert [b["close"] for b in bars] == [10.5]
    # exactly at the correction instant (<=): the new version
    bars = await market_data.get_bars_pit("688256.SH", as_of="2026-07-03T08:00:00+00:00")
    assert [b["close"] for b in bars] == [10.8]
    # no as_of = latest known
    bars = await market_data.get_bars_pit("688256.SH")
    assert [b["close"] for b in bars] == [10.8]
    assert bars[0]["volume"] == 1200


async def test_pit_bare_date_as_of_and_date_range():
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    for bar_date, close in (("2026-06-29", 1.0), ("2026-06-30", 2.0), ("2026-07-01", 3.0)):
        await market_data.upsert_bar({
            "security_id": "NVDA.US", "bar_date": bar_date,
            "open": close, "high": close, "low": close, "close": close,
            "as_known_at": f"{bar_date}T21:00:00+00:00",  # known the evening of the bar day
        })
    # a bare-date as_of means that day 00:00 UTC — the same evening's bar is not yet known
    bars = await market_data.get_bars_pit("NVDA.US", as_of="2026-06-30")
    assert [b["close"] for b in bars] == [1.0]
    # date-range filters compose with PIT
    bars = await market_data.get_bars_pit("NVDA.US", start="2026-06-30", end="2026-07-01")
    assert [b["close"] for b in bars] == [2.0, 3.0]


async def test_exact_replay_is_noop_and_same_key_different_facts_rejected():
    # version rows are IMMUTABLE: an exact-payload replay is a legal idempotent
    # no-op; the same version key with different facts is a conflict and the
    # stored version stays untouched
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    known = "2026-07-01T08:00:00+00:00"
    payload = {
        "security_id": "NVDA.US", "bar_date": "2026-06-30",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "as_known_at": known,
    }
    first = await market_data.upsert_bar(payload)
    replay = await market_data.upsert_bar(dict(payload))  # exact replay: no-op
    assert replay["id"] == first["id"]
    assert len(await db.query("SELECT id FROM price_bars")) == 1

    with pytest.raises(market_data.TransitionConflict, match="immutable.*later as_known_at"):
        await market_data.upsert_bar({**payload, "close": 10.1})
    with pytest.raises(market_data.TransitionConflict, match="metadata"):
        await market_data.upsert_bar({**payload, "metadata": {"note": "sneaky"}})
    # the stored version survived both attempts unchanged
    rows = await db.query("SELECT close, metadata_json FROM price_bars WHERE security_id = 'NVDA.US'")
    assert [(r["close"], r["metadata_json"]) for r in rows] == [(10.0, "{}")]

    # benchmark marks follow the same immutability rules
    await market_data.upsert_benchmark({"id": "SPX", "name_en": "S&P 500"})
    mark = {"mark_date": "2026-06-30", "value": 6000.0, "as_known_at": known}
    first_mark = await market_data.upsert_benchmark_mark("SPX", mark)
    replay_mark = await market_data.upsert_benchmark_mark("SPX", dict(mark))
    assert replay_mark["id"] == first_mark["id"]
    with pytest.raises(market_data.TransitionConflict, match="immutable"):
        await market_data.upsert_benchmark_mark("SPX", {**mark, "value": 6001.0})
    rows = await db.query("SELECT value FROM benchmark_marks WHERE benchmark_id = 'SPX'")
    assert [r["value"] for r in rows] == [6000.0]


async def test_subsecond_corrections_are_distinct_versions():
    # two corrections inside ONE second must land as two versions, and PIT
    # reads slice between them (must-fix #2: no sub-second collapsing)
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    base = {"security_id": "NVDA.US", "bar_date": "2026-06-30", "open": 10.0, "high": 11.0, "low": 9.0}
    await market_data.upsert_bar({**base, "close": 10.0, "as_known_at": "2026-07-01T08:00:00.100000+00:00"})
    await market_data.upsert_bar({**base, "close": 10.2, "as_known_at": "2026-07-01T08:00:00.900000+00:00"})
    assert len(await db.query("SELECT id FROM price_bars")) == 2

    bars = await market_data.get_bars_pit("NVDA.US", as_of="2026-07-01T08:00:00.500000+00:00")
    assert [b["close"] for b in bars] == [10.0]
    bars = await market_data.get_bars_pit("NVDA.US", as_of="2026-07-01T08:00:00.900000+00:00")
    assert [b["close"] for b in bars] == [10.2]
    # a second-precision as_of (…T08:00:00) is BEFORE both sub-second versions
    assert await market_data.get_bars_pit("NVDA.US", as_of="2026-07-01T08:00:00+00:00") == []


async def test_default_as_known_at_clock_avoids_same_second_collision():
    # omitting as_known_at twice in a row (same wall-clock second) must yield
    # two versions, not a conflict: the default clock carries microseconds
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    base = {"security_id": "NVDA.US", "bar_date": "2026-06-30", "open": 10.0, "high": 11.0, "low": 9.0}
    v1 = await market_data.upsert_bar({**base, "close": 10.0})
    v2 = await market_data.upsert_bar({**base, "close": 10.2})  # correction, same second
    assert v1["as_known_at"] < v2["as_known_at"]
    assert "." in v1["as_known_at"]  # microsecond precision, ISO UTC shape
    assert v1["as_known_at"].endswith("+00:00")
    bars = await market_data.get_bars_pit("NVDA.US")
    assert [b["close"] for b in bars] == [10.2]
    assert len(await db.query("SELECT id FROM price_bars")) == 2


async def test_timestamp_normalization_shapes():
    # every accepted input shape lands as microsecond-precision UTC +00:00, so
    # string order == time order across the whole store (no mixed precision)
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    base = {"security_id": "NVDA.US", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
    for bar_date, as_known_at, expect in (
        ("2026-06-01", "2026-07-01T16:00:00+08:00", "2026-07-01T08:00:00.000000+00:00"),  # non-UTC offset
        ("2026-06-02", "2026-07-01T08:00:00Z", "2026-07-01T08:00:00.000000+00:00"),       # Z suffix
        ("2026-06-03", "2026-07-01", "2026-07-01T00:00:00.000000+00:00"),                 # bare date
        ("2026-06-04", "2026-07-01T08:00:00.987654+00:00", "2026-07-01T08:00:00.987654+00:00"),  # sub-second kept
        ("2026-06-05", "2026-07-01T08:00:00", "2026-07-01T08:00:00.000000+00:00"),        # naive = UTC
    ):
        bar = await market_data.upsert_bar({**base, "bar_date": bar_date, "as_known_at": as_known_at})
        assert bar["as_known_at"] == expect, as_known_at
    # empty string is not "use the default" — it is a validation error
    with pytest.raises(market_data.MarketDataError, match="ISO-8601"):
        await market_data.upsert_bar({**base, "bar_date": "2026-06-06", "as_known_at": ""})
    with pytest.raises(market_data.MarketDataError, match="ISO-8601"):
        await market_data.upsert_bar({**base, "bar_date": "2026-06-06", "valid_time": ""})


async def test_bar_validation_and_fk():
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    base = {"security_id": "NVDA.US", "bar_date": "2026-06-30", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5}
    with pytest.raises(market_data.MarketDataError, match="security '600000.SH' not found"):
        await market_data.upsert_bar({**base, "security_id": "600000.SH"})
    with pytest.raises(market_data.MarketDataError, match="unknown freq"):
        await market_data.upsert_bar({**base, "freq": "5m"})  # domain gate; schema is open
    with pytest.raises(market_data.MarketDataError, match="must be YYYY-MM-DD"):
        await market_data.upsert_bar({**base, "bar_date": "20260630"})
    with pytest.raises(market_data.MarketDataError, match="below low"):
        await market_data.upsert_bar({**base, "high": 8.0})
    with pytest.raises(market_data.MarketDataError, match="volume"):
        await market_data.upsert_bar({**base, "volume": -5})
    with pytest.raises(market_data.MarketDataError, match="adj_factor"):
        await market_data.upsert_bar({**base, "adj_factor": 0})
    with pytest.raises(market_data.MarketDataError, match="unknown bar fields"):
        await market_data.upsert_bar({**base, "clsoe": 1.0})
    with pytest.raises(market_data.MarketDataError, match="not ISO-8601"):
        await market_data.upsert_bar({**base, "as_known_at": "yesterday"})

    # deleting the security removes its bars (rows are truth; bars follow)
    await market_data.upsert_bar(base)
    await db.execute("DELETE FROM securities WHERE id = 'NVDA.US'")
    assert await db.query("SELECT id FROM price_bars") == []


# ==== acceptance (b): benchmarks separate from securities ======================

async def test_benchmark_marks_are_separate_from_securities():
    # a benchmark needs NO securities row: CSI300 exists only in benchmarks
    bench = await market_data.upsert_benchmark({"id": "CSI300", "name_zh": "沪深300", "market": "CN_A"})
    assert bench["id"] == "CSI300"
    assert await db.query_one("SELECT id FROM securities WHERE id = 'CSI300'") is None

    # marks reference benchmarks(id) — a securities id is NOT a valid target
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    now = bus.now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO benchmark_marks (id, benchmark_id, mark_date, value, valid_time, as_known_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("m-bad", "NVDA.US", "2026-06-30", 100.0, now, now, now),
        )
    with pytest.raises(market_data.MarketDataError, match="benchmark 'NVDA.US' not found"):
        await market_data.upsert_benchmark_mark("NVDA.US", {"mark_date": "2026-06-30", "value": 100.0})

    mark = await market_data.upsert_benchmark_mark("CSI300", {"mark_date": "2026-06-30", "value": 4000.0})
    assert mark["benchmark_id"] == "CSI300"
    # and the mark rows never touch the securities table
    assert await db.query_one("SELECT COUNT(*) AS n FROM securities") == {"n": 1}


async def test_benchmark_marks_pit_revision():
    # marks get the same PIT treatment as bars (restated index closes)
    await market_data.upsert_benchmark({"id": "HSI", "name_en": "Hang Seng Index"})
    await market_data.upsert_benchmark_mark(
        "HSI", {"mark_date": "2026-06-30", "value": 24000.0, "as_known_at": "2026-07-01T09:00:00+00:00"}
    )
    await market_data.upsert_benchmark_mark(
        "HSI", {"mark_date": "2026-06-30", "value": 24010.0, "as_known_at": "2026-07-02T09:00:00+00:00"}
    )
    marks = await market_data.get_marks_pit("HSI", as_of="2026-07-01T12:00:00+00:00")
    assert [m["value"] for m in marks] == [24000.0]
    marks = await market_data.get_marks_pit("HSI")
    assert [m["value"] for m in marks] == [24010.0]


async def test_benchmark_upsert_and_validation():
    await market_data.upsert_benchmark({"id": "SPX", "name_en": "S&P 500"})
    # id-keyed upsert refreshes in place
    again = await market_data.upsert_benchmark({"id": "SPX", "name_en": "S&P 500 Index", "currency": "USD"})
    assert again["name_en"] == "S&P 500 Index"
    assert [b["id"] for b in await market_data.list_benchmarks()] == ["SPX"]

    with pytest.raises(market_data.MarketDataError, match="needs an id"):
        await market_data.upsert_benchmark({"name_en": "nameless"})
    with pytest.raises(market_data.MarketDataError, match="name_zh or name_en"):
        await market_data.upsert_benchmark({"id": "BARE"})
    with pytest.raises(market_data.MarketDataError, match="unknown market"):
        await market_data.upsert_benchmark({"id": "X", "name_en": "x", "market": "MOON"})
    assert await market_data.get_benchmark("NOPE") is None


# ==== acceptance (c): closed and suspended days ================================

async def test_calendar_represents_closed_days():
    await market_data.set_calendar_day("CN_A", "2026-10-01", False, note="国庆节")
    await market_data.set_calendar_day("CN_A", "2026-09-30", True)
    assert await market_data.is_trading_day("CN_A", "2026-10-01") is False
    assert await market_data.is_trading_day("CN_A", "2026-09-30") is True
    assert await market_data.is_trading_day("CN_A", "2026-09-29") is None  # unknown ≠ closed

    days = await market_data.get_calendar("CN_A", start="2026-09-30", end="2026-10-01")
    assert [(d["cal_date"], d["is_open"]) for d in days] == [("2026-09-30", 1), ("2026-10-01", 0)]

    # upsert flips a day in place (holiday announced after seeding)
    await market_data.set_calendar_day("CN_A", "2026-09-30", False, note="临时休市")
    assert await market_data.is_trading_day("CN_A", "2026-09-30") is False
    assert len(await market_data.get_calendar("CN_A")) == 2

    with pytest.raises(market_data.MarketDataError, match="unknown market"):
        await market_data.set_calendar_day("A-share", "2026-10-01", True)  # raw bundle string
    with pytest.raises(market_data.MarketDataError, match="must be YYYY-MM-DD"):
        await market_data.set_calendar_day("CN_A", "2026/10/01", True)


async def test_suspensions_represent_per_security_halts():
    await _mk_security("688256.SH", market="CN_A", name_en="Cambricon")
    susp = await market_data.add_suspension(
        "688256.SH", "2026-07-01", "2026-07-03", reason="重大资产重组"
    )
    assert susp["reason"] == "重大资产重组"
    # inclusive interval boundaries
    assert await market_data.is_suspended("688256.SH", "2026-06-30") is False
    assert await market_data.is_suspended("688256.SH", "2026-07-01") is True
    assert await market_data.is_suspended("688256.SH", "2026-07-03") is True
    assert await market_data.is_suspended("688256.SH", "2026-07-04") is False

    # open-ended halt: end_date NULL means "still suspended"
    open_susp = await market_data.add_suspension("688256.SH", "2026-08-01")
    assert await market_data.is_suspended("688256.SH", "2026-12-31") is True
    assert [s["id"] for s in await market_data.list_suspensions(active_on="2026-08-15")] == [open_susp["id"]]

    # closing is a conditional claim on end_date IS NULL
    closed = await market_data.close_suspension(open_susp["id"], "2026-08-10")
    assert closed["end_date"] == "2026-08-10"
    with pytest.raises(market_data.TransitionConflict, match="already closed"):
        await market_data.close_suspension(open_susp["id"], "2026-08-11")
    assert await market_data.close_suspension("no-such-id", "2026-08-11") is None
    assert await market_data.is_suspended("688256.SH", "2026-12-31") is False

    with pytest.raises(market_data.MarketDataError, match="not found"):
        await market_data.add_suspension("600000.SH", "2026-07-01")
    with pytest.raises(market_data.MarketDataError, match="before start_date"):
        await market_data.add_suspension("688256.SH", "2026-07-10", "2026-07-05")

    # cascade with the security (rows are truth; halts follow)
    await db.execute("DELETE FROM securities WHERE id = '688256.SH'")
    assert await market_data.list_suspensions() == []


async def test_trading_status_combines_closed_and_suspended():
    await _mk_security("0700.HK", market="HK", name_en="Tencent")
    await market_data.set_calendar_day("HK", "2026-07-01", False, note="HKSAR Establishment Day")
    await market_data.set_calendar_day("HK", "2026-07-02", True)
    await market_data.set_calendar_day("HK", "2026-07-03", True)
    await market_data.add_suspension("0700.HK", "2026-07-03", "2026-07-03", reason="pending announcement")

    closed = await market_data.get_trading_status("0700.HK", "2026-07-01")
    assert (closed["market_open"], closed["suspended"], closed["tradable"]) == (False, False, False)
    normal = await market_data.get_trading_status("0700.HK", "2026-07-02")
    assert (normal["market_open"], normal["suspended"], normal["tradable"]) == (True, False, True)
    halted = await market_data.get_trading_status("0700.HK", "2026-07-03")
    assert (halted["market_open"], halted["suspended"], halted["tradable"]) == (True, True, False)
    # unknown calendar day: market_open unknown, but a halt still decides tradable
    await market_data.add_suspension("0700.HK", "2026-07-06", "2026-07-06")
    unknown = await market_data.get_trading_status("0700.HK", "2026-07-06")
    assert (unknown["market_open"], unknown["suspended"], unknown["tradable"]) == (None, True, False)
    assert await market_data.get_trading_status("9999.HK", "2026-07-01") is None


async def test_calendar_schema_guards():
    now = bus.now_iso()
    # PK (market, cal_date): one row per market-day
    await db.execute(
        "INSERT INTO trading_calendar (market, cal_date, is_open, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("CN_A", "2026-10-01", 0, now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO trading_calendar (market, cal_date, is_open, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("CN_A", "2026-10-01", 1, now, now),
        )
    for market, cal_date, is_open in (
        ("A-share", "2026-10-01", 1),   # raw bundle market string
        ("CN_A", "2026-10-1", 1),       # not YYYY-MM-DD
        ("CN_A", "2026-10-02", 2),      # is_open is 0/1
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO trading_calendar (market, cal_date, is_open, created_at, updated_at) VALUES (?,?,?,?,?)",
                (market, cal_date, is_open, now, now),
            )
    # suspensions: interval sanity is a schema CHECK too
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO security_suspensions (id, security_id, start_date, end_date, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("s-bad", "NVDA.US", "2026-07-10", "2026-07-05", now, now),
        )


# ==== API surface ===============================================================

async def test_api_calendar_and_bars_roundtrip():
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/market/calendar", json={
            "market": "US", "cal_date": "2026-07-03", "is_open": False, "note": "Independence Day (observed)",
        })
        assert r.status_code == 200
        r = await client.get("/api/market/calendar", params={"market": "US"})
        assert [(d["cal_date"], d["is_open"]) for d in r.json()] == [("2026-07-03", 0)]

        for as_known_at, close in (("2026-07-01T21:00:00+00:00", 10.5), ("2026-07-03T21:00:00+00:00", 10.8)):
            r = await client.post("/api/market/bars", json={
                "security_id": "NVDA.US", "bar_date": "2026-06-30",
                "open": 10.0, "high": 11.0, "low": 9.5, "close": close, "as_known_at": as_known_at,
            })
            assert r.status_code == 200
        r = await client.get("/api/market/bars", params={
            "security_id": "NVDA.US", "as_of": "2026-07-02T00:00:00+00:00",
        })
        assert [b["close"] for b in r.json()] == [10.5]
        r = await client.get("/api/market/bars", params={"security_id": "NVDA.US"})
        assert [b["close"] for b in r.json()] == [10.8]

        # domain validation maps to 400; pydantic typos to 422
        r = await client.get("/api/market/bars", params={"security_id": "NVDA.US", "as_of": "yesterday"})
        assert r.status_code == 400
        r = await client.post("/api/market/bars", json={
            "security_id": "NVDA.US", "bar_date": "2026-06-30",
            "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "clsoe": 1,
        })
        assert r.status_code == 422


# ==== shared_data (0014, card B5) ==============================================

async def test_shared_data_schema_upsert_semantics():
    # NOT a PIT table (see 0014 header): (topic, work_date) upserts in place
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO shared_data (id, topic, work_date, content, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("sd-1", "贵州茅台", "2026-07-20", "v1", now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE(topic, work_date)
        await db.execute(
            "INSERT INTO shared_data (id, topic, work_date, content, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("sd-2", "贵州茅台", "2026-07-20", "v2", now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):  # work_date is GLOB-checked
        await db.execute(
            "INSERT INTO shared_data (id, topic, work_date, content, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("sd-3", "t", "20260720", "x", now, now),
        )
    await db.execute(
        "INSERT INTO shared_data (id, topic, work_date, content, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(topic, work_date) DO UPDATE SET content = excluded.content, "
        "updated_at = excluded.updated_at",
        ("sd-4", "贵州茅台", "2026-07-20", "v2", now, now),
    )
    rows = await db.query("SELECT id, content FROM shared_data WHERE topic = '贵州茅台'")
    assert [(r["id"], r["content"]) for r in rows] == [("sd-1", "v2")]  # updated in place


async def test_api_benchmarks_suspensions_and_status():
    await _mk_security("0700.HK", market="HK", name_en="Tencent")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/market/benchmarks", json={"id": "HSI", "name_en": "Hang Seng Index"})
        assert r.status_code == 200
        r = await client.post("/api/market/benchmarks/HSI/marks", json={"mark_date": "2026-06-30", "value": 24000.0})
        assert r.status_code == 200
        r = await client.get("/api/market/benchmarks/HSI/marks")
        assert [m["value"] for m in r.json()] == [24000.0]
        assert (await client.get("/api/market/benchmarks/NOPE")).status_code == 404
        r = await client.post("/api/market/benchmarks/NOPE/marks", json={"mark_date": "2026-06-30", "value": 1.0})
        assert r.status_code == 400  # domain: benchmark not found pre-insert

        r = await client.post("/api/market/suspensions", json={
            "security_id": "0700.HK", "start_date": "2026-07-01", "reason": "halt",
        })
        assert r.status_code == 200
        sid = r.json()["id"]
        r = await client.post(f"/api/market/suspensions/{sid}/close", json={"end_date": "2026-07-02"})
        assert r.status_code == 200
        # closing twice loses the conditional claim -> 409
        r = await client.post(f"/api/market/suspensions/{sid}/close", json={"end_date": "2026-07-03"})
        assert r.status_code == 409
        assert (await client.post("/api/market/suspensions/nope/close", json={"end_date": "2026-07-03"})).status_code == 404

        r = await client.get("/api/market/status/0700.HK", params={"cal_date": "2026-07-01"})
        assert r.status_code == 200
        assert r.json()["suspended"] is True
        assert (await client.get("/api/market/status/9999.HK", params={"cal_date": "2026-07-01"})).status_code == 404
