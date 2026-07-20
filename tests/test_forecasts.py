"""Forecast ledger (card M5-001): schema + domain module + API.

Covers the three acceptance lines: (a) a forecast requires thesis, claim,
horizon, direction, and settlement_rule; (b) settlement records
hit/miss/partial/invalid; (c) invalid or missing benchmark data fails closed
to 'invalid'. Prices are staged through the card M4-001 PIT store
(market_data.upsert_bar / upsert_benchmark_mark), never mocked.

Knowledge-time convention in these fixtures: bars/marks default to
``as_known_at = <date>T12:00:00Z`` and MADE is 23:00 that evening, so the
entry-leg PIT read (frozen at made_at — the anti-look-ahead contract from
REVIEW-B6) sees the entry-day value; look-ahead regressions then write
versions with as_known_at AFTER made_at and assert they cannot move the
entry.

The forecasts router is not yet mounted in app/main.py (mounting is outside
this card's partition — see PATCH-NOTES-B6.md), so API tests build a bare
FastAPI app around the router instead of create_app().
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import forecasts, market_data

MADE = "2026-06-01T23:00:00+00:00"      # horizon 10 -> expires 2026-06-11T23:00, long past
EXPIRES = "2026-06-11T23:00:00+00:00"


def _known(date: str) -> str:
    """Default knowledge time for a bar/mark: noon of its own day — before
    MADE (23:00) for the entry day, so the entry leg can see it."""
    return f"{date}T12:00:00+00:00"


async def _mk_thesis(tid: str = "t-macro", name: str = "宏观论点") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO theses (id, kind, slug, name_zh, status, current_view, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tid, "thesis", tid, name, "active", "unknown", now, now),
    )


async def _mk_security(sid: str, name: str = "Test Co") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_en, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, sid.split(".")[0], "US", name, now, now),
    )


async def _bar(
    sid: str, bar_date: str, close: float, *, adj: float = 1.0, known: str | None = None,
) -> None:
    await market_data.upsert_bar({
        "security_id": sid, "bar_date": bar_date,
        "open": close, "high": close, "low": close, "close": close, "adj_factor": adj,
        "as_known_at": known or _known(bar_date),
    })


async def _mark(bid: str, mark_date: str, value: float, *, known: str | None = None) -> None:
    await market_data.upsert_benchmark_mark(bid, {
        "mark_date": mark_date, "value": value, "as_known_at": known or _known(mark_date),
    })


async def _forecast(
    sec_id: str | None,
    *,
    direction: str = "long",
    rule: dict | str | None = None,
    thesis: str = "t-macro",
    made: str = MADE,
    horizon: int = 10,
    claim: str = "十日内显著上行",
) -> dict:
    return await forecasts.create_forecast({
        "thesis_id": thesis, "security_id": sec_id, "claim": claim,
        "direction": direction, "horizon_days": horizon,
        "settlement_rule": rule or {"type": "absolute_move", "threshold": 0.05},
        "made_at": made,
    })


# ==== acceptance (a): required fields =========================================

async def test_create_requires_thesis_claim_horizon_direction_rule():
    await _mk_thesis()
    await _mk_security("AAA.US")
    base = {
        "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "上行",
        "direction": "long", "horizon_days": 10,
        "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
    }
    for field in ("thesis_id", "claim", "direction", "horizon_days", "settlement_rule"):
        broken = {k: v for k, v in base.items() if k != field}
        with pytest.raises(forecasts.ForecastError):
            await forecasts.create_forecast(broken)

    with pytest.raises(forecasts.ForecastError, match="not found"):
        await forecasts.create_forecast({**base, "thesis_id": "no-such"})
    with pytest.raises(forecasts.ForecastError, match="not found"):
        await forecasts.create_forecast({**base, "security_id": "NOPE.US"})
    with pytest.raises(forecasts.ForecastError, match="unknown direction"):
        await forecasts.create_forecast({**base, "direction": "sideways"})
    for bad_horizon in (0, -3, 2.5, "soon"):
        with pytest.raises(forecasts.ForecastError, match="horizon_days"):
            await forecasts.create_forecast({**base, "horizon_days": bad_horizon})
    with pytest.raises(forecasts.ForecastError, match="conviction"):
        await forecasts.create_forecast({**base, "conviction": 1.5})
    with pytest.raises(forecasts.ForecastError, match="unknown forecast fields"):
        await forecasts.create_forecast({**base, "bogus": 1})
    # both launch rule types price the security, so it is required at create
    with pytest.raises(forecasts.ForecastError, match="security_id"):
        await forecasts.create_forecast({k: v for k, v in base.items() if k != "security_id"})
    assert await forecasts.list_forecasts() == []  # failed creates wrote nothing

    fc = await forecasts.create_forecast({**base, "made_at": MADE, "conviction": 0.7})
    assert fc["status"] == "open"
    assert fc["made_at"] == MADE
    assert fc["expires_at"] == EXPIRES  # made_at + horizon_days
    assert fc["settlement"] is None
    events = await bus.replay(0, types=["forecast.created"])
    assert [e.ref_id for e in events] == [fc["id"]]


async def test_create_rejects_non_finite_numbers():
    """REVIEW-B6: NaN/Inf slip past bare comparisons — every numeric field
    must be finite (and bool is not a number)."""
    await _mk_thesis()
    await _mk_security("AAA.US")
    base = {
        "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "上行",
        "direction": "long", "horizon_days": 10,
        "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
    }
    for bad in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(forecasts.ForecastError, match="threshold"):
            await forecasts.create_forecast({
                **base, "settlement_rule": {"type": "absolute_move", "threshold": bad}})
    for bad in (float("nan"), float("inf"), True):
        with pytest.raises(forecasts.ForecastError, match="conviction"):
            await forecasts.create_forecast({**base, "conviction": bad})
    for bad in (float("nan"), float("inf")):
        with pytest.raises(forecasts.ForecastError, match="horizon_days"):
            await forecasts.create_forecast({**base, "horizon_days": bad})
    assert await forecasts.list_forecasts() == []


async def test_settlement_rule_parsing_and_canonical_storage():
    parse = forecasts.parse_settlement_rule
    assert parse({"type": "absolute_move", "threshold": 0.05}) == {
        "type": "absolute_move", "threshold": 0.05}
    assert parse('{"type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "CSI300"}') == {
        "type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "CSI300"}

    for bad, why in (
        ("not json", "valid JSON"),
        ('["absolute_move"]', "JSON object"),
        ({"type": "coin_flip", "threshold": 0.1}, "unknown settlement_rule type"),
        ({"type": "absolute_move"}, "threshold"),
        ({"type": "absolute_move", "threshold": "big"}, "must be a number"),
        ({"type": "absolute_move", "threshold": 0}, "> 0"),
        ({"type": "absolute_move", "threshold": -0.1}, "> 0"),
        ({"type": "price_vs_benchmark", "threshold": 0.1}, "benchmark_id"),
        ({"type": "absolute_move", "threshold": 0.1, "benchmark_id": "CSI300"},
         "unknown settlement_rule fields"),
        ({"type": "absolute_move", "threshold": 0.1, "extra": 1},
         "unknown settlement_rule fields"),
    ):
        with pytest.raises(forecasts.ForecastError, match=why):
            parse(bad)

    # stored canonical: a JSON-string rule reads back as the normalized dict
    await _mk_thesis()
    await _mk_security("AAA.US")
    fc = await _forecast("AAA.US", rule='{"type": "absolute_move", "threshold": 0.02}')
    assert fc["settlement_rule"] == {"type": "absolute_move", "threshold": 0.02}


# ==== acceptance (b): hit / miss / partial =====================================

async def test_settle_absolute_move_hit_partial_miss_neutral():
    await _mk_thesis()
    await _mk_security("AAA.US")
    await _bar("AAA.US", "2026-06-01", 10.0)
    await _bar("AAA.US", "2026-06-11", 10.3)  # +3% over the window

    cases = [
        ("long", 0.02, "hit"),       # signed 0.03 >= 0.02
        ("long", 0.05, "partial"),   # 0 < 0.03 < 0.05
        ("short", 0.02, "miss"),     # signed -0.03 <= 0
        ("neutral", 0.05, "hit"),    # |0.03| <= 0.05
        ("neutral", 0.02, "miss"),   # |0.03| > 0.02
    ]
    for direction, threshold, expected in cases:
        fc = await _forecast(
            "AAA.US", direction=direction,
            rule={"type": "absolute_move", "threshold": threshold},
            claim=f"{direction}/{threshold}",
        )
        settled = await forecasts.settle_forecast(fc["id"])
        assert settled["status"] == "settled", (direction, threshold)
        s = settled["settlement"]
        assert s["verdict"] == expected, (direction, threshold)
        assert s["actual_return"] == pytest.approx(0.03)
        assert s["benchmark_return"] is None   # absolute_move never touches a benchmark
        assert s["note"]

    events = await bus.replay(0, types=["forecast.settled"])
    assert len(events) == len(cases)


async def test_settle_uses_adjusted_closes():
    # raw close halves but adj_factor doubles: adjusted +10% -> hit, not miss
    await _mk_thesis()
    await _mk_security("ADJ.US")
    await _bar("ADJ.US", "2026-06-01", 10.0)
    await _bar("ADJ.US", "2026-06-11", 5.5, adj=2.0)

    fc = await _forecast("ADJ.US", rule={"type": "absolute_move", "threshold": 0.05})
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "hit"
    assert settled["settlement"]["actual_return"] == pytest.approx(0.10)


async def test_settle_price_vs_benchmark_excess_return():
    await _mk_thesis()
    await market_data.upsert_benchmark({"id": "CSI300", "name_zh": "沪深300"})
    await _mark("CSI300", "2026-06-01", 4000.0)
    await _mark("CSI300", "2026-06-11", 4200.0)  # +5%

    rule = {"type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "CSI300"}
    for sid, exit_close, expected in (
        ("HIT.US", 11.0, "hit"),       # +10% vs +5% -> excess 0.05 >= 0.03
        ("PAR.US", 10.6, "partial"),   # +6%  vs +5% -> 0 < 0.01 < 0.03
        ("MIS.US", 10.2, "miss"),      # +2%  vs +5% -> excess -0.03 <= 0
    ):
        await _mk_security(sid)
        await _bar(sid, "2026-06-01", 10.0)
        await _bar(sid, "2026-06-11", exit_close)
        fc = await _forecast(sid, rule=rule, claim=f"跑赢基准 {sid}")
        settled = await forecasts.settle_forecast(fc["id"])
        s = settled["settlement"]
        assert s["verdict"] == expected, sid
        assert s["benchmark_return"] == pytest.approx(0.05)
        assert s["actual_return"] == pytest.approx(exit_close / 10.0 - 1.0)


# ==== knowledge-time semantics (REVIEW-B6 must-fix 1: no look-ahead) ==========

async def test_entry_frozen_at_made_at_correction_after_cannot_move_it():
    """The reviewer's probe: entry-day close 10 known at made_at; the same
    bar_date is corrected to 20 AFTER made_at; exit 11. The entry must stay
    10 (+10% -> hit), never 20 (-45% -> miss)."""
    await _mk_thesis()
    await _mk_security("LOOK.US")
    await _bar("LOOK.US", "2026-06-01", 10.0)                      # known 06-01 noon < MADE
    await _bar("LOOK.US", "2026-06-01", 20.0,                      # correction, known after MADE
               known="2026-06-02T09:00:00+00:00")
    await _bar("LOOK.US", "2026-06-11", 11.0)

    fc = await _forecast("LOOK.US", rule={"type": "absolute_move", "threshold": 0.05})
    settled = await forecasts.settle_forecast(fc["id"])
    s = settled["settlement"]
    assert s["verdict"] == "hit"
    assert s["actual_return"] == pytest.approx(0.10)               # basis 10, not 20


async def test_entry_close_published_after_made_at_falls_back_to_prior_known():
    """made_at lands before the entry-day close is published: the entry must
    be the last price the forecaster could actually know (the prior bar),
    not the later-published same-day close."""
    await _mk_thesis()
    await _mk_security("PREV.US")
    await _bar("PREV.US", "2026-05-29", 8.0)                       # known 05-29 noon
    await _bar("PREV.US", "2026-06-01", 10.0,                      # made_at-day close, published after MADE
               known="2026-06-02T02:00:00+00:00")
    await _bar("PREV.US", "2026-06-11", 11.0)

    fc = await _forecast("PREV.US", rule={"type": "absolute_move", "threshold": 0.05})
    settled = await forecasts.settle_forecast(fc["id"])
    s = settled["settlement"]
    assert s["actual_return"] == pytest.approx(11.0 / 8.0 - 1.0)   # basis 8, not 10
    assert s["verdict"] == "hit"


async def test_benchmark_entry_frozen_at_made_at():
    """Benchmark twin of the look-ahead probe: a mark restatement after
    made_at cannot rewrite the benchmark entry."""
    await _mk_thesis()
    await _mk_security("BMK.US")
    await _bar("BMK.US", "2026-06-01", 10.0)
    await _bar("BMK.US", "2026-06-11", 11.0)                       # security +10%
    await market_data.upsert_benchmark({"id": "IDX", "name_en": "Index"})
    await _mark("IDX", "2026-06-01", 4000.0)                       # known before MADE
    await _mark("IDX", "2026-06-01", 8000.0,                       # restated after MADE
                known="2026-06-03T00:00:00+00:00")
    await _mark("IDX", "2026-06-11", 4200.0)                       # +5% vs the true entry

    fc = await _forecast("BMK.US", rule={
        "type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "IDX"})
    settled = await forecasts.settle_forecast(fc["id"])
    s = settled["settlement"]
    assert s["benchmark_return"] == pytest.approx(0.05)            # basis 4000, not 8000
    assert s["verdict"] == "hit"                                   # excess 0.05 >= 0.03

    # benchmark entry published only after made_at, no prior mark -> fails closed
    await market_data.upsert_benchmark({"id": "LATEIDX", "name_en": "Late Index"})
    await _mark("LATEIDX", "2026-06-01", 4000.0, known="2026-06-02T09:00:00+00:00")
    await _mark("LATEIDX", "2026-06-11", 4200.0)
    fc = await _forecast("BMK.US", rule={
        "type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "LATEIDX"},
        claim="基准迟到")
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "invalid"
    assert "no entry value known at made_at" in settled["settlement"]["note"]


async def test_exit_leg_uses_settlement_time_knowledge():
    """The exit is deliberately NOT frozen at made_at: a post-period
    correction of the exit-day close is legitimate outcome knowledge."""
    await _mk_thesis()
    await _mk_security("EXITC.US")
    await _bar("EXITC.US", "2026-06-01", 10.0)
    await _bar("EXITC.US", "2026-06-11", 10.2)                     # first print: +2%
    await _bar("EXITC.US", "2026-06-11", 11.0,                     # corrected later: +10%
               known="2026-06-13T00:00:00+00:00")

    fc = await _forecast("EXITC.US", rule={"type": "absolute_move", "threshold": 0.05})
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["actual_return"] == pytest.approx(0.10)
    assert settled["settlement"]["verdict"] == "hit"

    # ...but an explicit as_of replays with only the knowledge of that time
    fc2 = await _forecast("EXITC.US", rule={"type": "absolute_move", "threshold": 0.05},
                          claim="回放旧知识")
    replayed = await forecasts.settle_forecast(fc2["id"], as_of="2026-06-12")
    assert replayed["settlement"]["actual_return"] == pytest.approx(0.02)
    assert replayed["settlement"]["verdict"] == "partial"          # 0 < 0.02 < 0.05


# ==== acceptance (c): invalid benchmark fails closed ===========================

async def test_settle_fails_closed_when_data_missing():
    await _mk_thesis()

    # no bars at all
    await _mk_security("EMPTY.US")
    fc = await _forecast("EMPTY.US")
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["status"] == "invalid"
    assert settled["settlement"]["verdict"] == "invalid"
    assert settled["settlement"]["actual_return"] is None
    assert "no entry value known at made_at" in settled["settlement"]["note"]

    # entry exists but nothing after it (no post-entry bar)
    await _mk_security("STALE.US")
    await _bar("STALE.US", "2026-06-01", 10.0)
    fc = await _forecast("STALE.US")
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "invalid"
    assert "no value after entry date" in settled["settlement"]["note"]

    # bars begin only after made_at: no entry price
    await _mk_security("LATE.US")
    await _bar("LATE.US", "2026-06-09", 10.0)
    await _bar("LATE.US", "2026-06-11", 10.5)
    fc = await _forecast("LATE.US")
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "invalid"
    assert "no entry value" in settled["settlement"]["note"]


async def test_settle_fails_closed_on_invalid_benchmark():
    await _mk_thesis()
    await _mk_security("GOOD.US")
    await _bar("GOOD.US", "2026-06-01", 10.0)
    await _bar("GOOD.US", "2026-06-11", 11.0)

    # benchmark id never registered -> invalid, even though the security priced fine
    fc = await _forecast("GOOD.US", rule={
        "type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "GHOST"})
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["status"] == "invalid"
    s = settled["settlement"]
    assert s["verdict"] == "invalid"
    assert s["benchmark_return"] is None
    assert "'GHOST' not found" in s["note"]

    # benchmark exists but has no usable marks -> same fails-closed verdict
    await market_data.upsert_benchmark({"id": "BARE", "name_en": "Bare Index"})
    fc = await _forecast("GOOD.US", rule={
        "type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "BARE"},
        claim="基准无数据")
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "invalid"
    assert "benchmark BARE" in settled["settlement"]["note"]

    # PIT replay: as_of before anything was known -> fails closed, no guessing
    fc = await _forecast("GOOD.US", claim="as-of 回放")
    settled = await forecasts.settle_forecast(fc["id"], as_of="2026-05-01")
    assert settled["settlement"]["verdict"] == "invalid"


async def test_settle_fails_closed_when_security_deleted():
    await _mk_thesis()
    await _mk_security("GONE.US")
    await _bar("GONE.US", "2026-06-01", 10.0)
    await _bar("GONE.US", "2026-06-11", 11.0)
    fc = await _forecast("GONE.US")
    await db.execute("DELETE FROM securities WHERE id = ?", ("GONE.US",))  # FK: SET NULL

    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "invalid"
    assert "no security_id" in settled["settlement"]["note"]


async def test_settle_fails_closed_on_unusable_endpoint_values():
    """REVIEW-B6 must-fix 2: zero/negative/non-finite endpoints are storable
    by 0006 design but must settle invalid, never produce a verdict."""
    await _mk_thesis()
    cases = [
        ("ZEXIT.US", 10.0, 0.0, "unusable exit value"),      # the reviewer's zero_exit probe
        ("NEXIT.US", 10.0, -5.0, "unusable exit value"),
        ("IEXIT.US", 10.0, float("inf"), "unusable exit value"),
        ("ZENTR.US", 0.0, 11.0, "unusable entry value"),
        ("NENTR.US", -10.0, 11.0, "unusable entry value"),
        ("IENTR.US", float("inf"), 11.0, "unusable entry value"),
    ]
    for sid, entry_close, exit_close, expected_note in cases:
        await _mk_security(sid)
        await _bar(sid, "2026-06-01", entry_close)
        await _bar(sid, "2026-06-11", exit_close)
        fc = await _forecast(sid, claim=f"坏端点 {sid}")
        settled = await forecasts.settle_forecast(fc["id"])
        assert settled["status"] == "invalid", sid
        assert settled["settlement"]["verdict"] == "invalid", sid
        assert settled["settlement"]["actual_return"] is None, sid
        assert expected_note in settled["settlement"]["note"], sid

    # benchmark twin: a zero exit mark must invalidate, not fake outperformance
    await _mk_security("OKSEC.US")
    await _bar("OKSEC.US", "2026-06-01", 10.0)
    await _bar("OKSEC.US", "2026-06-11", 11.0)
    await market_data.upsert_benchmark({"id": "ZMARK", "name_en": "Zero Mark"})
    await _mark("ZMARK", "2026-06-01", 4000.0)
    await _mark("ZMARK", "2026-06-11", 0.0)
    fc = await _forecast("OKSEC.US", rule={
        "type": "price_vs_benchmark", "threshold": 0.03, "benchmark_id": "ZMARK"})
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["settlement"]["verdict"] == "invalid"
    assert "benchmark ZMARK" in settled["settlement"]["note"]
    assert "unusable exit value" in settled["settlement"]["note"]


def test_usable_price_and_window_return_whitelist_units():
    """Unit gate for paths the domain writes cannot produce (SQLite binds NaN
    floats as NULL, so a stored NaN close is unreachable end-to-end) plus the
    computed-return overflow branch."""
    up = forecasts._usable_price
    assert up(10.0) and up(1e-300)
    for bad in (None, True, False, "10", 0.0, -1.0, float("nan"), float("inf"), float("-inf")):
        assert not up(bad), bad

    entry_rows = [{"bar_date": "2026-06-01", "close": 5e-324, "adj_factor": 1.0}]
    exit_rows = entry_rows + [{"bar_date": "2026-06-11", "close": 1e308, "adj_factor": 1.0}]
    ret, why = forecasts._window_return(
        entry_rows, exit_rows, "bar_date", forecasts._adj_close, "unit")
    assert ret is None
    assert "not finite" in why

    nan_exit = entry_rows + [{"bar_date": "2026-06-11", "close": float("nan"), "adj_factor": 1.0}]
    ret, why = forecasts._window_return(
        [{"bar_date": "2026-06-01", "close": 10.0, "adj_factor": 1.0}], nan_exit,
        "bar_date", forecasts._adj_close, "unit")
    assert ret is None
    assert "unusable exit value" in why


# ==== conditional claim / lifecycle ============================================

async def test_settle_conditional_claim_prevents_double_settlement():
    await _mk_thesis()
    await _mk_security("AAA.US")
    await _bar("AAA.US", "2026-06-01", 10.0)
    await _bar("AAA.US", "2026-06-11", 11.0)

    fc = await _forecast("AAA.US")
    settled = await forecasts.settle_forecast(fc["id"])
    assert settled["status"] == "settled"
    with pytest.raises(forecasts.TransitionConflict):
        await forecasts.settle_forecast(fc["id"])

    # racing settlers: exactly one wins the open->settled claim
    fc2 = await _forecast("AAA.US", claim="并发结算")
    results = await asyncio.gather(
        *(forecasts.settle_forecast(fc2["id"]) for _ in range(5)), return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1
    assert all(isinstance(e, forecasts.TransitionConflict) for e in losers)
    rows = await db.query(
        "SELECT id FROM forecast_settlements WHERE forecast_id = ?", (fc2["id"],)
    )
    assert len(rows) == 1

    assert await forecasts.settle_forecast("no-such") is None


async def test_settle_refuses_before_expiry():
    await _mk_thesis()
    await _mk_security("AAA.US")
    fc = await forecasts.create_forecast({
        "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "远期判断",
        "direction": "long", "horizon_days": 365,
        "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
    })  # made_at defaults to now -> expires a year out
    with pytest.raises(forecasts.ForecastError, match="not expired"):
        await forecasts.settle_forecast(fc["id"])
    fresh = await forecasts.get_forecast(fc["id"])
    assert fresh["status"] == "open"
    assert fresh["settlement"] is None


async def test_list_and_filters():
    await _mk_thesis("t-a")
    await _mk_thesis("t-b")
    await _mk_security("AAA.US")
    f1 = await _forecast("AAA.US", thesis="t-a")
    await _forecast("AAA.US", thesis="t-b", claim="另一条")

    assert {f["id"] for f in await forecasts.list_forecasts()} >= {f1["id"]}
    assert [f["thesis_id"] for f in await forecasts.list_forecasts(thesis_id="t-a")] == ["t-a"]
    assert len(await forecasts.list_forecasts(status="open")) == 2
    assert await forecasts.list_forecasts(status="settled") == []
    with pytest.raises(forecasts.ForecastError, match="unknown status"):
        await forecasts.list_forecasts(status="closed")


async def test_forecast_history_exports_managed_vault_note():
    from app.vault.writer import get_writer

    await _mk_thesis(name="出口增长论点")
    await _mk_security("VAULT.US", name="Vault Corp")
    await _bar("VAULT.US", "2026-06-01", 10.0)
    await _bar("VAULT.US", "2026-06-11", 11.0)
    fc = await _forecast("VAULT.US", claim="十日内显著上行并进入历史导出")
    await forecasts.settle_forecast(fc["id"])

    writer = get_writer()
    assert writer.root is not None
    book = writer.root / "Book"
    if book.exists():
        for old in book.glob("forecasts*.md"):
            old.unlink()

    exported = await forecasts.export_vault_history()
    assert exported == {"enabled": True, "path": "Book/forecasts.md", "count": 1}
    target = writer.root / exported["path"]
    text = target.read_text(encoding="utf-8")
    assert "managed: institute" in text
    assert "type: forecast-history" in text
    assert "%% institute:begin %%" in text and "%% institute:end %%" in text
    assert "# 预测历史" in text and fc["id"] in text
    assert "十日内显著上行并进入历史导出" in text
    assert "VAULT.US（Vault Corp）" in text
    assert "结算：**hit**" in text and "标的收益 +10.00%" in text
    ledger = await db.query_one(
        "SELECT artifact_kind, artifact_id, mode FROM vault_index WHERE path = ?",
        ("Book/forecasts.md",),
    )
    assert ledger == {
        "artifact_kind": "forecast-history",
        "artifact_id": "forecast-history",
        "mode": "region",
    }

    # The manual API refresh uses the same managed region and preserves notes
    # a human adds outside it.
    target.write_text(text + "\n人工复盘批注。\n", encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/forecasts/export-vault", json={"scope": "history"})
    assert response.status_code == 200
    assert response.json() == exported
    assert "人工复盘批注。" in target.read_text(encoding="utf-8")


# ==== API surface ==============================================================

def _make_app() -> FastAPI:
    # the router is not mounted in main.py yet (PATCH-NOTES-B6.md), so tests
    # mount it on a bare app; db/migrations come from the autouse fixture
    from app.api import forecasts as api_forecasts

    app = FastAPI()
    app.include_router(api_forecasts.router)
    return app


async def test_api_roundtrip():
    await _mk_thesis()
    await _mk_security("AAA.US")
    await _bar("AAA.US", "2026-06-01", 10.0)
    await _bar("AAA.US", "2026-06-11", 11.0)

    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.post("/api/forecasts", json={
            "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "十日上行",
            "direction": "long", "horizon_days": 10,
            "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
            "made_at": MADE,
        })
        assert r.status_code == 200
        fid = r.json()["id"]
        assert r.json()["status"] == "open"
        assert r.json()["expires_at"] == EXPIRES

        # domain validation maps to 400; typos map to 422 (extra=forbid)
        r = await client.post("/api/forecasts", json={
            "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "坏规则",
            "direction": "long", "horizon_days": 10,
            "settlement_rule": {"type": "coin_flip", "threshold": 0.05},
        })
        assert r.status_code == 400
        assert "unknown settlement_rule type" in r.json()["detail"]
        r = await client.post("/api/forecasts", json={
            "thesis_id": "t-macro", "claim": "缺字段", "direction": "long",
            "horizon_days": 10, "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
            "priorty": 1,
        })
        assert r.status_code == 422

        r = await client.get("/api/forecasts", params={"status": "open"})
        assert [f["id"] for f in r.json()] == [fid]
        r = await client.get(f"/api/forecasts/{fid}")
        assert r.status_code == 200
        assert r.json()["settlement"] is None
        assert (await client.get("/api/forecasts/nope")).status_code == 404

        r = await client.post(f"/api/forecasts/{fid}/settle", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "settled"
        assert r.json()["settlement"]["verdict"] == "hit"

        # double settlement is a lost conditional claim -> 409
        r = await client.post(f"/api/forecasts/{fid}/settle", json={})
        assert r.status_code == 409
        assert (await client.post("/api/forecasts/nope/settle", json={})).status_code == 404

        # malformed as_of is a readable 400, not a MarketDataError 500
        r = await client.post("/api/forecasts", json={
            "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "坏 as_of",
            "direction": "long", "horizon_days": 10,
            "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
            "made_at": MADE,
        })
        r2 = await client.post(f"/api/forecasts/{r.json()['id']}/settle", json={"as_of": "garbage"})
        assert r2.status_code == 400
        assert "not ISO-8601" in r2.json()["detail"]

        # settling an unexpired forecast is a 400, and it stays open
        r = await client.post("/api/forecasts", json={
            "thesis_id": "t-macro", "security_id": "AAA.US", "claim": "未到期",
            "direction": "long", "horizon_days": 365,
            "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
        })
        r2 = await client.post(f"/api/forecasts/{r.json()['id']}/settle", json={})
        assert r2.status_code == 400
        assert "not expired" in r2.json()["detail"]
