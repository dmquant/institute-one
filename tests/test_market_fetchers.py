"""Fetcher ladder + research data bundle (Phase 1b, card B5).

Network is always mocked (httpx.MockTransport swapped in for
market_fetchers._client) — no test touches the real internet. One optional
real-Sina smoke test exists at the bottom, skipped unless
INSTITUTE_NET_TESTS=1.

Covers: symbol dialect tables (the quirk maps), ladder success / failure /
degradation, the confidence gate (refuse-to-write), PIT-friendly re-ingest
(unchanged bars don't stack versions), bundle rendering incl. the empty
degradation, and the ${DATA_BUNDLE} substitution end-to-end on the echo hand.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import market_data, market_fetchers, sessions, workflows


# ---- fixtures / helpers -----------------------------------------------------

async def _mk_security(
    sid: str, *, market: str, name_zh: str | None = None, name_en: str | None = None,
    currency: str | None = None, status: str = "active",
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_zh, name_en, currency, listing_status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, sid.split(".")[0], market, name_zh, name_en or (None if name_zh else sid), currency,
         status, now, now),
    )


async def _mk_alias(security_id: str, alias: str, kind: str) -> None:
    await db.execute(
        "INSERT INTO security_aliases (id, security_id, alias, kind, created_at) VALUES (?,?,?,?,?)",
        (f"al-{alias}-{kind}"[:40], security_id, alias, kind, bus.now_iso()),
    )


def _mock_net(monkeypatch, handler) -> None:
    """Swap _client for one driven by httpx.MockTransport(handler)."""
    def client(timeout: float = 12.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(market_fetchers, "_client", client)


def _sina_a_share_payload(
    *, price=1710.5, prev_close=1690.0, open_=1700.0, high=1720.0, low=1695.0,
    volume=3_000_000, day="2026-07-17",
) -> bytes:
    cells = [""] * 33
    cells[0], cells[1], cells[2], cells[3] = "贵州茅台", str(open_), str(prev_close), str(price)
    cells[4], cells[5], cells[8] = str(high), str(low), str(volume)
    cells[30], cells[31] = day, "15:00:03"
    return f'var hq_str_sh600519="{",".join(cells)}";\n'.encode("gb18030")


def _sina_hk_payload(*, price=505.0, prev_close=500.0, day="2026/07/17") -> bytes:
    cells = [""] * 20
    cells[0], cells[1] = "TENCENT", "腾讯控股"
    cells[2], cells[3], cells[4], cells[5], cells[6] = "501.0", str(prev_close), "508.0", "498.0", str(price)
    cells[12], cells[17], cells[18] = "12000000", day, "16:08:11"
    return f'var hq_str_hk00700="{",".join(cells)}";\n'.encode("gb18030")


def _stooq_quote_csv(*, close=180.5, date="2026-07-17") -> str:
    return (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        f"NVDA.US,{date},22:00:00,178.0,182.0,177.5,{close},210000000\n"
    )


def _stooq_daily_csv(rows: list[tuple[str, float, float, float, float]]) -> str:
    out = ["Date,Open,High,Low,Close,Volume"]
    out += [f"{d},{o},{h},{lo},{c},1000000" for d, o, h, lo, c in rows]
    return "\n".join(out) + "\n"


def _sina_kline(rows: list[tuple[str, float, float, float, float]]) -> str:
    return json.dumps([
        {"day": d, "open": str(o), "high": str(h), "low": str(lo), "close": str(c), "volume": "5000000"}
        for d, o, h, lo, c in rows
    ])


# ==== symbol dialect tables (the quirk maps) ==================================

def test_sina_symbol_dialect():
    assert market_fetchers.to_sina_symbol("600519.SH") == "sh600519"
    assert market_fetchers.to_sina_symbol("000001.SZ") == "sz000001"
    assert market_fetchers.to_sina_symbol("830799.BJ") == "bj830799"
    assert market_fetchers.to_sina_symbol("0700.HK") == "hk00700"     # 5-digit pad
    assert market_fetchers.to_sina_symbol("9988.HK") == "hk09988"
    assert market_fetchers.to_sina_symbol("NVDA.US") == "gb_nvda"     # lowercase + gb_
    assert market_fetchers.to_sina_symbol("BRK.B.US") == "gb_brk$b"   # share class dot -> $
    assert market_fetchers.to_sina_symbol("005930.KS") is None        # GLOBAL_CONTEXT: no dialect
    assert market_fetchers.to_sina_symbol("600519") is None           # not canonical
    assert market_fetchers.to_sina_symbol("") is None


def test_stooq_symbol_dialect():
    assert market_fetchers.to_stooq_symbol("NVDA.US") == "nvda.us"
    assert market_fetchers.to_stooq_symbol("BRK.B.US") == "brk-b.us"  # share class dot -> dash
    assert market_fetchers.to_stooq_symbol("0700.HK") == "0700.hk"
    assert market_fetchers.to_stooq_symbol("09988.HK") == "9988.hk"   # 4-digit normalization
    assert market_fetchers.to_stooq_symbol("600519.SH") is None       # no A-share coverage
    assert market_fetchers.to_stooq_symbol("830799.BJ") is None
    assert market_fetchers.to_stooq_symbol("6954.T") is None


def test_fmp_symbol_dialect():
    assert market_fetchers.to_fmp_symbol("NVDA.US") == "NVDA"
    assert market_fetchers.to_fmp_symbol("BRK.B.US") == "BRK-B"
    assert market_fetchers.to_fmp_symbol("600519.SH") == "600519.SS"  # Shanghai is .SS, not .SH
    assert market_fetchers.to_fmp_symbol("000001.SZ") == "000001.SZ"
    assert market_fetchers.to_fmp_symbol("0700.HK") == "0700.HK"
    assert market_fetchers.to_fmp_symbol("700.HK") == "0700.HK"
    assert market_fetchers.to_fmp_symbol("830799.BJ") is None         # no BSE coverage
    assert market_fetchers.to_fmp_symbol("005930.KS") is None


def test_available_sources_respects_key_and_dialects(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    assert market_fetchers._available_sources("600519.SH") == [("sina", "sh600519")]
    assert market_fetchers._available_sources("NVDA.US") == [
        ("stooq", "nvda.us"), ("sina", "gb_nvda"),
    ]
    assert market_fetchers._available_sources("005930.KS") == []
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: "k")
    assert market_fetchers._available_sources("NVDA.US")[0] == ("fmp", "NVDA")
    assert market_fetchers._available_sources("830799.BJ") == [("sina", "bj830799")]  # FMP no BSE


def test_settings_bridge_reads_env(monkeypatch):
    # the bridge prefers Settings fields once config.py grows them
    # (PATCH-NOTES-B5) and falls back to the PROCESS environment until then
    # (.env alone is NOT os.environ — pydantic never writes it back). This
    # test sets whichever layer is live, so it holds before AND after the
    # main agent applies the config patch.
    from app.config import get_settings

    settings = get_settings()

    def _set(attr: str, env: str, val: str) -> None:
        if hasattr(settings, attr):
            monkeypatch.setattr(settings, attr, val)
        else:
            monkeypatch.setenv(env, val)

    if not hasattr(settings, "fmp_api_key"):
        monkeypatch.delenv("INSTITUTE_FMP_API_KEY", raising=False)
        assert market_fetchers.fmp_api_key() is None
    _set("fmp_api_key", "INSTITUTE_FMP_API_KEY", "secret-k")
    assert market_fetchers.fmp_api_key() == "secret-k"
    _set("fetch_proxy", "INSTITUTE_FETCH_PROXY", "http://127.0.0.1:7897")
    assert market_fetchers.fetch_proxy() == "http://127.0.0.1:7897"
    assert market_fetchers.market_fetch_enabled() is True   # default on
    _set("market_fetch_enabled", "INSTITUTE_MARKET_FETCH_ENABLED", "false")
    assert market_fetchers.market_fetch_enabled() is False


# ==== quote ladder: success / failure / degradation ===========================

async def test_quote_a_share_via_sina(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hq.sinajs.cn"
        assert request.headers.get("Referer", "").startswith("https://finance.sina.com.cn")
        return httpx.Response(200, content=_sina_a_share_payload())

    _mock_net(monkeypatch, handler)
    quote = await market_fetchers.fetch_quote("600519.SH")
    assert quote["source"] == "sina"
    assert quote["price"] == 1710.5
    assert quote["prev_close"] == 1690.0
    assert quote["quote_date"] == "2026-07-17"
    assert quote["change_pct"] == pytest.approx(1.21, abs=0.01)


async def test_quote_hk_via_sina(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stooq.com":  # ladder tries stooq first for HK
            return httpx.Response(500)
        return httpx.Response(200, content=_sina_hk_payload())

    _mock_net(monkeypatch, handler)
    quote = await market_fetchers.fetch_quote("0700.HK")
    assert quote["source"] == "sina"
    assert quote["price"] == 505.0
    assert quote["quote_date"] == "2026-07-17"  # slashes normalized


async def test_quote_ladder_fmp_first_when_key_present(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: "k")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        assert request.url.params["apikey"] == "k"
        return httpx.Response(200, json=[{
            "symbol": "NVDA", "price": 181.2, "previousClose": 179.0,
            "open": 179.5, "dayHigh": 183.0, "dayLow": 178.9,
            "volume": 2.1e8, "timestamp": 1784200000,  # 2026-07-16 UTC (not future)
        }])

    _mock_net(monkeypatch, handler)
    quote = await market_fetchers.fetch_quote("NVDA.US")
    assert quote["source"] == "fmp"
    assert quote["price"] == 181.2
    assert seen == ["financialmodelingprep.com"]


async def test_quote_degrades_down_the_ladder(monkeypatch, caplog):
    # FMP 500s, Stooq answers — the ladder must degrade silently (warn only)
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: "k")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "financialmodelingprep.com":
            return httpx.Response(500)
        assert request.url.host == "stooq.com"
        return httpx.Response(200, text=_stooq_quote_csv())

    _mock_net(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger="institute.market_fetchers"):
        quote = await market_fetchers.fetch_quote("NVDA.US")
    assert quote["source"] == "stooq"
    assert quote["price"] == 180.5
    assert any("fmp" in r.getMessage() for r in caplog.records)


async def test_quote_all_sources_down_returns_none(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    _mock_net(monkeypatch, lambda request: httpx.Response(503))
    assert await market_fetchers.fetch_quote("NVDA.US") is None
    assert await market_fetchers.fetch_quote("005930.KS") is None  # no dialect at all


async def test_quote_stooq_nd_payload_degrades(monkeypatch):
    # Stooq answers 200 with N/D placeholders (unknown symbol) -> next source
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stooq.com":
            return httpx.Response(200, text="Symbol,Date,Time,Open,High,Low,Close,Volume\nX,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n")
        return httpx.Response(200, content=_sina_hk_payload())

    _mock_net(monkeypatch, handler)
    quote = await market_fetchers.fetch_quote("0700.HK")
    assert quote["source"] == "sina"


async def test_insane_quote_is_refused(monkeypatch, caplog):
    # +60% vs prev close: the gate refuses the only source -> None
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    _mock_net(monkeypatch, lambda request: httpx.Response(
        200, content=_sina_a_share_payload(price=2704.0, prev_close=1690.0, high=2710.0)
    ))
    with caplog.at_level(logging.WARNING, logger="institute.market_fetchers"):
        assert await market_fetchers.fetch_quote("600519.SH") is None
    assert any("refusing quote" in r.getMessage() for r in caplog.records)


async def test_quote_ladder_degrades_on_non_finite_source(monkeypatch):
    # FMP answers 200 with a NaN price (json literal): the parse layer maps it
    # to None and the ladder must fall through to Stooq (M1 + ladder semantics)
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: "k")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "financialmodelingprep.com":
            return httpx.Response(
                200,
                text='[{"price": NaN, "previousClose": Infinity, "open": 1.0, '
                     '"dayHigh": 1.0, "dayLow": 1.0, "volume": 1, "timestamp": 1784200000}]',
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, text=_stooq_quote_csv())

    _mock_net(monkeypatch, handler)
    quote = await market_fetchers.fetch_quote("NVDA.US")
    assert quote["source"] == "stooq"
    assert market_fetchers._finite(quote["price"])


# ==== daily bars ladder ========================================================

async def test_daily_bars_a_share_via_sina(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    rows = [("2026-07-15", 100, 104, 99, 103), ("2026-07-16", 103, 106, 102, 105)]
    _mock_net(monkeypatch, lambda request: httpx.Response(200, text=_sina_kline(rows)))
    bars = await market_fetchers.fetch_daily_bars("600519.SH", days=30)
    assert [(b["bar_date"], b["close"]) for b in bars] == [("2026-07-15", 103.0), ("2026-07-16", 105.0)]
    assert all(b["source"] == "sina" for b in bars)


async def test_daily_bars_us_via_stooq_and_full_failure(monkeypatch):
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    rows = [("2026-07-16", 178, 182, 177, 181), ("2026-07-17", 181, 183, 179, 180.5)]
    _mock_net(monkeypatch, lambda request: httpx.Response(
        200, text=_stooq_daily_csv(rows)) if request.url.host == "stooq.com" else httpx.Response(500))
    bars = await market_fetchers.fetch_daily_bars("NVDA.US", days=30)
    assert len(bars) == 2 and bars[-1]["close"] == 180.5

    # stooq down; sina gb_ has no daily wiring -> [] (documented degradation)
    _mock_net(monkeypatch, lambda request: httpx.Response(500))
    assert await market_fetchers.fetch_daily_bars("NVDA.US", days=30) == []


async def test_daily_ladder_degrades_when_first_source_fully_refused(monkeypatch, caplog):
    # M2: FMP answers with FORMAT-valid bars whose OHLC is all NaN (json
    # literals) — outcome 2 (pass rate == 0): the source is untrustworthy and
    # the ladder must degrade to Stooq instead of adopting FMP
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: "k")
    fmp_nan = '{"historical": [{"date": "2026-07-16", "open": NaN, "high": NaN, "low": NaN, "close": NaN, "volume": 100}]}'
    good = [("2026-07-16", 178, 182, 177, 181), ("2026-07-17", 181, 183, 179, 180.5)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "financialmodelingprep.com":
            return httpx.Response(200, text=fmp_nan, headers={"content-type": "application/json"})
        assert request.url.host == "stooq.com"
        return httpx.Response(200, text=_stooq_daily_csv(good))

    _mock_net(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger="institute.market_fetchers"):
        bars = await market_fetchers.fetch_daily_bars("NVDA.US", days=30)
    assert [b["source"] for b in bars] == ["stooq", "stooq"]
    assert all(market_fetchers._finite(b["close"]) for b in bars)
    assert any("all 1 rows failed the confidence gate" in r.getMessage() for r in caplog.records)

    # outcome 3: a PARTIALLY refused source is adopted (pass rate > 0), its
    # refused rows dropped — the ladder does not skip a usable source
    mixed = [("2026-07-16", 178, 182, 177, 181), ("2026-07-17", 181, 320, 179, 300)]  # 2nd insane

    def handler2(request: httpx.Request) -> httpx.Response:
        if request.url.host == "financialmodelingprep.com":
            return httpx.Response(500)
        if request.url.host == "stooq.com":
            return httpx.Response(200, text=_stooq_daily_csv(mixed))
        raise AssertionError("ladder must stop at stooq (pass rate > 0)")

    _mock_net(monkeypatch, handler2)
    bars = await market_fetchers.fetch_daily_bars("NVDA.US", days=30)
    assert [(b["source"], b["bar_date"]) for b in bars] == [("stooq", "2026-07-16")]


async def test_refresh_ingests_nothing_when_all_sources_are_nan(monkeypatch):
    # M1 end-to-end: non-finite bars never reach the PIT store, and the
    # exhausted ladder reports source=None
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    nan_csv = "Date,Open,High,Low,Close,Volume\n2026-07-16,NaN,Infinity,NaN,NaN,1000\n"
    _mock_net(monkeypatch, lambda request: httpx.Response(200, text=nan_csv)
              if request.url.host == "stooq.com" else httpx.Response(500))
    stats = await market_fetchers.refresh_security("NVDA.US")
    assert (stats["written"], stats["source"]) == (0, None)
    assert await db.query("SELECT id FROM price_bars") == []


# ==== confidence gate ==========================================================

def test_check_bar_rules():
    good = {"bar_date": "2026-07-16", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 100}
    assert market_fetchers.check_bar(good) == []
    assert market_fetchers.check_bar({**good, "close": 0})            # non-positive
    assert market_fetchers.check_bar({**good, "close": None})         # missing
    assert market_fetchers.check_bar({**good, "high": 9.0})           # high < low
    assert market_fetchers.check_bar({**good, "high": 10.2, "close": 10.5})  # close above high
    assert market_fetchers.check_bar({**good, "bar_date": "9999-01-01"})     # future
    assert market_fetchers.check_bar({**good, "bar_date": "2026/07/16"})     # bad shape
    assert market_fetchers.check_bar({**good, "volume": -1})
    assert market_fetchers.check_bar({**good, "high": 16.0, "close": 15.0})  # 68% intraday range
    # threshold uses >=: an exactly-50% intraday range (or close move) is refused
    assert market_fetchers.check_bar({**good, "open": 10.0, "high": 15.0, "low": 10.0, "close": 15.0})
    assert market_fetchers.check_bar(good, prev_close=7.0)            # exactly 50% vs prev
    assert market_fetchers.check_bar(good, prev_close=5.0)            # 110% vs prev
    assert market_fetchers.check_bar(good, prev_close=10.4) == []


def test_check_bar_refuses_non_finite():
    # NaN compares False against everything, so `v <= 0` alone waves it
    # through — every numeric field must hit the isfinite whitelist (M1)
    good = {"bar_date": "2026-07-16", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 100}
    nan, inf = float("nan"), float("inf")
    for field in ("open", "high", "low", "close", "volume"):
        for bad in (nan, inf, -inf):
            assert market_fetchers.check_bar({**good, field: bad}), f"{field}={bad} passed the gate"
    assert market_fetchers.check_bar(good, prev_close=nan) == []  # non-finite prev: comparison skipped
    assert market_fetchers.check_bar(good, prev_close=inf) == []


def test_check_quote_rules():
    assert market_fetchers.check_quote({"price": 10.0, "prev_close": 9.8}) == []
    assert market_fetchers.check_quote({"price": 0})
    assert market_fetchers.check_quote({"price": None})
    assert market_fetchers.check_quote({"price": 15.0, "prev_close": 10.0})   # >=50% move
    assert market_fetchers.check_quote({"price": 10.0, "quote_date": "9999-01-01"})
    assert market_fetchers.check_quote({"price": 10.0, "volume": -5})
    assert market_fetchers.check_quote({"price": 10.0, "open": 12.0, "high": 11.0, "low": 9.0})  # open>high
    assert market_fetchers.check_quote(
        {"price": 10.0, "open": 10.0, "high": 11.0, "low": 9.0, "volume": 100.0, "prev_close": 9.9}
    ) == []


def test_check_quote_refuses_non_finite():
    nan, inf = float("nan"), float("inf")
    for bad in (nan, inf, -inf):
        assert market_fetchers.check_quote({"price": bad}), f"price={bad} passed the gate"
    for field in ("prev_close", "open", "high", "low", "volume"):
        for bad in (nan, inf):
            assert market_fetchers.check_quote({"price": 10.0, field: bad}), f"{field}={bad} passed"


def test_parse_layer_maps_non_finite_to_none():
    # upstream CSV/JSON can carry "NaN"/"Infinity" strings and json.loads
    # even accepts bare NaN/Infinity literals — _f() must not let them out
    for raw in ("NaN", "nan", "Infinity", "-Infinity", "inf", float("nan"), float("inf"), None, "N/D", ""):
        assert market_fetchers._f(raw) is None, raw
    assert market_fetchers._f("1,234.5") == 1234.5
    assert market_fetchers._f(json.loads("NaN")) is None  # json.loads accepts the literal


async def test_refresh_refuses_bad_bars_and_writes_good_ones(monkeypatch, caplog):
    await _mk_security("600519.SH", market="CN_A", name_zh="贵州茅台")
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    rows = [
        ("2026-07-15", 100, 104, 99, 103),
        ("2026-07-16", 103, 260, 102, 250),   # +142% vs prev close: refused
        ("2026-07-17", 103, 106, 102, 104),
    ]
    _mock_net(monkeypatch, lambda request: httpx.Response(200, text=_sina_kline(rows)))
    with caplog.at_level(logging.WARNING, logger="institute.market_fetchers"):
        stats = await market_fetchers.refresh_security("600519.SH")
    assert (stats["fetched"], stats["written"], stats["rejected"]) == (3, 2, 1)
    assert any("refuse-to-write" in r.getMessage() for r in caplog.records)
    bars = await market_data.get_bars_pit("600519.SH")
    assert [b["bar_date"] for b in bars] == ["2026-07-15", "2026-07-17"]  # bad bar never landed
    assert all(b["source"] == "sina" for b in bars)

    # replaying identical data must not stack PIT versions
    stats2 = await market_fetchers.refresh_security("600519.SH")
    assert (stats2["written"], stats2["unchanged"], stats2["rejected"]) == (0, 2, 1)
    assert len(await db.query("SELECT id FROM price_bars")) == 2

    # a real upstream correction appends a new version (later as_known_at)
    rows[2] = ("2026-07-17", 103, 106, 102, 105)
    _mock_net(monkeypatch, lambda request: httpx.Response(200, text=_sina_kline(rows)))
    stats3 = await market_fetchers.refresh_security("600519.SH")
    assert stats3["corrected"] == 1
    versions = await db.query(
        "SELECT close FROM price_bars WHERE bar_date = '2026-07-17' ORDER BY as_known_at"
    )
    assert [v["close"] for v in versions] == [104.0, 105.0]
    latest = await market_data.get_bars_pit("600519.SH")
    assert latest[-1]["close"] == 105.0

    # a volume-only change is a correction too (_same_bar covers OHLC + volume)
    rows[2] = ("2026-07-17", 103, 106, 102, 105)
    _mock_net(monkeypatch, lambda request: httpx.Response(200, text=_sina_kline(rows)))
    await db.execute(
        "UPDATE price_bars SET volume = 999.0 "
        "WHERE bar_date = '2026-07-17' AND as_known_at = "
        "(SELECT MAX(as_known_at) FROM price_bars WHERE bar_date = '2026-07-17')"
    )  # simulate a differing known volume without changing prices
    stats4 = await market_fetchers.refresh_security("600519.SH")
    assert stats4["corrected"] == 1

    # adj_factor is deliberately OUTSIDE _same_bar: a corporate-action card
    # stamping a real factor must not be shadowed by a raw re-fetch
    latest = (await market_data.get_bars_pit("600519.SH", start="2026-07-17"))[-1]
    await market_data.upsert_bar({
        "security_id": "600519.SH", "bar_date": "2026-07-17",
        "open": latest["open"], "high": latest["high"], "low": latest["low"],
        "close": latest["close"], "volume": latest["volume"], "adj_factor": 2.0,
        "source": "corporate-actions",
    })
    stats5 = await market_fetchers.refresh_security("600519.SH")
    assert stats5["written"] == 0 and stats5["corrected"] == 0  # unchanged, factor preserved
    latest = (await market_data.get_bars_pit("600519.SH", start="2026-07-17"))[-1]
    assert latest["adj_factor"] == 2.0

    assert await market_fetchers.refresh_security("no-such-id") is None


async def test_refresh_all_selects_fetchable_and_can_be_disabled(monkeypatch):
    await _mk_security("600519.SH", market="CN_A", name_zh="贵州茅台")
    await _mk_security("005930.KS", market="GLOBAL_CONTEXT", name_en="Samsung")
    await _mk_security("0700.HK", market="HK", name_zh="腾讯控股", status="delisted")
    calls = []

    async def fake_refresh(security_id: str, *, days: int = 30):
        calls.append(security_id)
        return {"security_id": security_id, "fetched": 1, "written": 1, "corrected": 0,
                "unchanged": 0, "rejected": 0, "source": "sina"}

    monkeypatch.setattr(market_fetchers, "refresh_security", fake_refresh)
    summary = await market_fetchers.refresh_all(limit=10)
    assert summary["enabled"] is True
    assert calls == ["600519.SH"]  # GLOBAL_CONTEXT and delisted rows excluded
    assert summary["written"] == 1
    events = await bus.replay(0, types=["market.refreshed"])
    assert len(events) == 1

    from app.config import get_settings
    settings = get_settings()
    if hasattr(settings, "market_fetch_enabled"):
        monkeypatch.setattr(settings, "market_fetch_enabled", "0")
    else:
        monkeypatch.setenv("INSTITUTE_MARKET_FETCH_ENABLED", "0")
    assert (await market_fetchers.refresh_all())["enabled"] is False


# ==== topic -> securities resolution ==========================================

async def test_resolve_security_id_symbol_alias():
    await _mk_security("600519.SH", market="CN_A", name_zh="贵州茅台")
    await _mk_security("0700.HK", market="HK", name_zh="腾讯控股")
    await _mk_alias("600519.SH", "茅台", "abbreviation")
    assert (await market_fetchers.resolve_security("600519.SH"))["id"] == "600519.SH"
    assert (await market_fetchers.resolve_security("600519"))["id"] == "600519.SH"   # bare symbol
    assert (await market_fetchers.resolve_security("茅台"))["id"] == "600519.SH"     # alias
    assert (await market_fetchers.resolve_security("0700.hk"))["id"] == "0700.HK"    # case-folded id
    assert await market_fetchers.resolve_security("does-not-exist") is None
    assert await market_fetchers.resolve_security("") is None


async def test_match_topic_securities():
    await _mk_security("600519.SH", market="CN_A", name_zh="贵州茅台")
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    await _mk_security("0700.HK", market="HK", name_zh="腾讯控股")
    await _mk_alias("0700.HK", "腾讯", "abbreviation")

    got = await market_fetchers.match_topic_securities("贵州茅台 2026 年提价逻辑与渠道库存")
    assert [s["id"] for s in got] == ["600519.SH"]
    got = await market_fetchers.match_topic_securities("NVIDIA vs AMD: the GB300 supply chain")
    assert [s["id"] for s in got] == ["NVDA.US"]
    got = await market_fetchers.match_topic_securities("600519.SH 与 0700.HK 的南向资金对比")
    assert {s["id"] for s in got} == {"600519.SH", "0700.HK"}
    got = await market_fetchers.match_topic_securities("腾讯游戏版号进展")   # alias substring
    assert [s["id"] for s in got] == ["0700.HK"]
    got = await market_fetchers.match_topic_securities("宏观：美债利率与流动性")  # no match
    assert got == []


# ==== data bundle ==============================================================

async def _seed_maotai_with_bars(n: int = 6, *, base: float = 100.0) -> None:
    from datetime import date, timedelta
    await _mk_security("600519.SH", market="CN_A", name_zh="贵州茅台", currency="CNY")
    d0 = date.fromisoformat(market_fetchers.work_date()) - timedelta(days=n)
    for i in range(n):
        px = base + i
        await market_data.upsert_bar({
            "security_id": "600519.SH", "bar_date": (d0 + timedelta(days=i)).isoformat(),
            "open": px, "high": px + 1, "low": px - 1, "close": px + 0.5,
            "volume": 1000 + i, "source": "sina",
        })


async def test_bundle_renders_summary_and_benchmark(caplog):
    await _seed_maotai_with_bars(6)
    await market_data.upsert_benchmark({"id": "CSI300", "name_zh": "沪深300", "market": "CN_A"})
    from datetime import date, timedelta
    d0 = date.fromisoformat(market_fetchers.work_date()) - timedelta(days=6)
    for i, val in enumerate((4000.0, 4100.0)):
        await market_data.upsert_benchmark_mark(
            "CSI300", {"mark_date": (d0 + timedelta(days=i * 5)).isoformat(), "value": val}
        )

    bundle = await market_fetchers.build_data_bundle("贵州茅台提价逻辑")
    assert "600519.SH" in bundle and "贵州茅台" in bundle
    assert "最新日线" in bundle and "105.5" in bundle
    assert "CSI300" in bundle and "基准对比" in bundle
    assert "近5日收盘" in bundle
    assert len(bundle.encode("utf-8")) <= market_fetchers.BUNDLE_MAX_BYTES

    # persisted into shared_data (topic, work_date) and re-rendering upserts
    row = await db.query_one("SELECT * FROM shared_data WHERE topic = ?", ("贵州茅台提价逻辑",))
    assert row is not None and row["work_date"] == market_fetchers.work_date()
    assert json.loads(row["metadata_json"])["securities"] == ["600519.SH"]
    await market_fetchers.build_data_bundle("贵州茅台提价逻辑")
    assert len(await db.query("SELECT id FROM shared_data")) == 1


async def test_bundle_empty_degradations():
    # unknown topic -> ""
    assert await market_fetchers.build_data_bundle("完全无关的宏观主题") == ""
    # matched security but zero bars -> "" (and nothing stored)
    await _mk_security("0700.HK", market="HK", name_zh="腾讯控股")
    assert await market_fetchers.build_data_bundle("腾讯控股回购") == ""
    assert await db.query("SELECT id FROM shared_data") == []
    assert await market_fetchers.build_data_bundle("") == ""


async def test_bundle_caps_at_4kb():
    await _seed_maotai_with_bars(6)
    long_name = "超长主题" * 800
    # a topic that matches but renders long is capped, not rejected
    bundle = await market_fetchers.build_data_bundle(f"贵州茅台 {long_name}")
    assert bundle and len(bundle.encode("utf-8")) <= market_fetchers.BUNDLE_MAX_BYTES


async def test_latest_bundle_cache_and_miss():
    await _seed_maotai_with_bars(4)
    got = await market_fetchers.latest_bundle("贵州茅台估值")   # renders on miss
    assert got is not None and "600519.SH" in got["content"]
    assert got["metadata"]["securities"] == ["600519.SH"]
    assert await market_fetchers.latest_bundle("无关主题") is None


# ==== ${DATA_BUNDLE} end-to-end on the echo hand ===============================

async def _mk_bundle_workflow(wf_id: str = "wf-bundle") -> None:
    steps = [{
        "id": "s1", "title": "数据检查", "prompt":
            "研究对象：${TOPIC}\n\n【已注入行情】\n${DATA_BUNDLE}\n\nWRITE_FILE: out.md",
        "output_file": "out.md",
    }]
    await db.execute(
        "INSERT INTO workflows (id, name, description, variables, steps, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (wf_id, "bundle wf", "", json.dumps(["TOPIC", "DATA_BUNDLE"]),
         json.dumps(steps, ensure_ascii=False), bus.now_iso()),
    )


async def _run_and_read(wf_id: str, variables: dict) -> tuple[dict, str]:
    run = await workflows.run_workflow_and_wait(wf_id, variables=variables, source="test")
    assert run["status"] == "completed"
    session = await sessions.get_session(run["session_id"])
    out = (sessions.workspace_path(session) / "out.md").read_text(encoding="utf-8")
    return run, out


async def test_data_bundle_substitution_end_to_end():
    await _seed_maotai_with_bars(6)
    await _mk_bundle_workflow()
    run, out = await _run_and_read("wf-bundle", {"TOPIC": "贵州茅台提价逻辑"})
    assert "${DATA_BUNDLE}" not in out          # substituted, no residue
    assert "600519.SH" in out and "最新日线" in out
    # the computed value is persisted on the run row (like every variable)
    assert "600519.SH" in run["variables"]["DATA_BUNDLE"]


async def test_data_bundle_renders_empty_when_no_data():
    await _mk_bundle_workflow("wf-bundle-empty")
    run, out = await _run_and_read("wf-bundle-empty", {"TOPIC": "无行情覆盖的主题"})
    assert "${DATA_BUNDLE}" not in out
    assert "【已注入行情】\n\n" in out           # empty string, prompt otherwise intact
    assert run["variables"]["DATA_BUNDLE"] == ""


async def test_data_bundle_explicit_value_wins():
    await _seed_maotai_with_bars(4)
    await _mk_bundle_workflow("wf-bundle-explicit")
    _, out = await _run_and_read(
        "wf-bundle-explicit", {"TOPIC": "贵州茅台", "DATA_BUNDLE": "OPERATOR-SUPPLIED"}
    )
    assert "OPERATOR-SUPPLIED" in out and "最新日线" not in out


async def test_workflows_without_the_variable_never_compute_it(monkeypatch):
    # research.json steps do not reference ${DATA_BUNDLE}: no bundle work runs
    called = []

    async def boom(variables):  # pragma: no cover - must not be called
        called.append(1)
        return ""

    monkeypatch.setattr(market_fetchers, "data_bundle_variable", boom)
    await workflows.reconcile_from_disk()
    run = await workflows.run_workflow_and_wait("briefing", source="test")
    assert run["status"] == "completed"
    assert called == []
    assert "DATA_BUNDLE" not in run["variables"]


# ==== API surface ===============================================================

def _make_app() -> FastAPI:
    from app.api import market_data as api_market_data

    app = FastAPI()
    app.include_router(api_market_data.router)
    return app


async def test_api_quote_bundle_refresh(monkeypatch):
    await _seed_maotai_with_bars(5)
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    _mock_net(monkeypatch, lambda request: httpx.Response(200, content=_sina_a_share_payload())
              if request.url.host == "hq.sinajs.cn"
              else httpx.Response(200, text=_sina_kline([("2026-07-17", 103, 106, 102, 105)])))

    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # quote resolves bare symbols / aliases to the canonical id
        r = await client.get("/api/quote/600519")
        assert r.status_code == 200
        body = r.json()
        assert (body["security_id"], body["source"], body["price"]) == ("600519.SH", "sina", 1710.5)
        assert body["name_zh"] == "贵州茅台"
        assert (await client.get("/api/quote/NOPE.US")).status_code == 404

        r = await client.get("/api/data/贵州茅台估值/latest")
        assert r.status_code == 200
        assert "600519.SH" in r.json()["content"]
        assert (await client.get("/api/data/无关主题/latest")).status_code == 404

        r = await client.post("/api/market/refresh/600519.SH")
        assert r.status_code == 200
        assert r.json()["fetched"] == 1
        assert (await client.post("/api/market/refresh/9999.HK")).status_code == 404

        # pre-existing /api/market/* routes still answer on their old paths
        assert (await client.get("/api/market/bars", params={"security_id": "600519.SH"})).status_code == 200


async def test_api_quote_502_when_all_sources_down(monkeypatch):
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    monkeypatch.setattr(market_fetchers, "fmp_api_key", lambda: None)
    _mock_net(monkeypatch, lambda request: httpx.Response(503))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/quote/NVDA.US")
        assert r.status_code == 502


# ==== optional real-network smoke (skipped by default) =========================

@pytest.mark.skipif(
    os.environ.get("INSTITUTE_NET_TESTS") != "1",
    reason="real-network smoke; set INSTITUTE_NET_TESTS=1 to run",
)
async def test_real_sina_quote_smoke():
    quote = await market_fetchers.fetch_quote("600519.SH")  # needs securities row? no: pure fetch
    assert quote is not None and quote["price"] > 0
