"""Market data fetcher ladder + research data bundle (Phase 1b, card B5).

Ladder: FMP (needs INSTITUTE_FMP_API_KEY) -> Stooq (free, US + some intl) ->
Sina (free, CN A-share/HK realtime + CN daily). Per security the ladder walks
sources in order, skipping any source that has no symbol dialect for that
market (see the ``to_*_symbol`` functions — the explicit "symbol-quirk
tables" from the ROADMAP) or whose fetch/parse fails, and returns the first
usable result.

HTTP: every request goes through ``_client()`` — ``httpx.AsyncClient(
trust_env=False)`` (this machine has a global SOCKS proxy that httpx must not
inherit). When INSTITUTE_FETCH_PROXY is set (e.g. mihomo at
http://127.0.0.1:7897) the client uses it explicitly; unset = direct.

Confidence gate (refuse-to-write): fetched rows pass ``check_bar`` /
``check_quote`` sanity (prices > 0, OHLC consistency, real non-future dates,
day-over-day move < MAX_MOVE_PCT). Failing rows are NEVER written — a warning
is logged and the row is dropped; passing bars go through the A7 PIT store
(``market_data.upsert_bar``, default microsecond as_known_at clock). To keep
the immutable version history from bloating, ``refresh_security`` first reads
the latest-known version per bar_date and only writes when the facts actually
changed (a real upstream correction) or the bar is new.

The research data bundle (``build_data_bundle``) renders LOCAL data only —
latest known bars, 30-day summary, benchmark comparison — never network IO:
it runs on the prompt-rendering path (${DATA_BUNDLE} in workflows), which
must stay fast and deterministic; freshness is the hourly refresh job's
problem. Rendered bundles are upserted into ``shared_data`` keyed
``(topic, work_date)`` (migrations/0014) as audit trail + API cache. Missing
data renders as "" so prompts degrade without a trace.

Settings bridge: these fields are declared in ``config.py``. The helpers below
read the settings object first and retain a raw-process-environment fallback
for compatibility:
  INSTITUTE_FMP_API_KEY           -> settings.fmp_api_key
  INSTITUTE_FETCH_PROXY           -> settings.fetch_proxy
  INSTITUTE_MARKET_FETCH_ENABLED  -> settings.market_fetch_enabled
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx

from .. import bus, db
from ..config import get_settings
from . import market_data
from .prompts import work_date

log = logging.getLogger("institute.market_fetchers")

SOURCES = ("fmp", "stooq", "sina")  # ladder order
MAX_MOVE_PCT = 0.5          # refuse a >=50% day-over-day (or vs prev_close) move
BUNDLE_MAX_BYTES = 4096     # rendered ${DATA_BUNDLE} cap (UTF-8 bytes)
BUNDLE_MAX_SECURITIES = 3
BUNDLE_BAR_DAYS = 30
MARKET_BENCHMARKS = {"CN_A": "CSI300", "HK": "HSI", "US": "SPX"}

FMP_BASE = "https://financialmodelingprep.com"
STOOQ_BASE = "https://stooq.com"
SINA_HQ_BASE = "https://hq.sinajs.cn"
SINA_KLINE_BASE = "https://money.finance.sina.com.cn"
# hq.sinajs.cn rejects requests without a finance.sina referer
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
# GB18030 is a strict superset of GBK for the legacy quote endpoint and has a
# canonical codec module across supported Python builds.  Some long-running
# Python 3.14 test/process states have returned a transient failed lookup for
# the ``gbk`` alias even though the payload was valid; the superset avoids an
# alias-specific availability/cache failure without changing decoded bytes.
SINA_TEXT_ENCODING = "gb18030"
USER_AGENT = "Mozilla/5.0 (institute-one market fetcher)"


# ---- settings bridge --------------------------------------------------------

def _setting(attr: str, env: str) -> str:
    """Configured settings field, with a raw process-environment fallback."""
    val = getattr(get_settings(), attr, None)
    if val is not None and str(val).strip():
        return str(val).strip()
    return (os.environ.get(env) or "").strip()


def fmp_api_key() -> str | None:
    return _setting("fmp_api_key", "INSTITUTE_FMP_API_KEY") or None


def fetch_proxy() -> str | None:
    return _setting("fetch_proxy", "INSTITUTE_FETCH_PROXY") or None


def market_fetch_enabled() -> bool:
    raw = _setting("market_fetch_enabled", "INSTITUTE_MARKET_FETCH_ENABLED")
    return raw.lower() not in {"false", "0", "no", "off"}  # default: enabled


# ---- symbol dialects (the "symbol-quirk tables") ----------------------------
#
# Canonical ids (migrations/0004): 600519.SH / 000001.SZ / 830799.BJ (CN_A),
# 0700.HK (HK), NVDA.US (US). GLOBAL_CONTEXT ids keep native vendor suffixes
# (005930.KS) — no fetcher source covers them, every dialect returns None.

_CANONICAL_RE = re.compile(r"^(?P<sym>[A-Za-z0-9.\-]+)\.(?P<suffix>SH|SZ|BJ|HK|US)$")


def _parse_canonical(security_id: str) -> tuple[str, str] | None:
    """canonical id -> (unsuffixed symbol, suffix), else None."""
    m = _CANONICAL_RE.match((security_id or "").strip())
    if not m:
        return None
    sym = m.group("sym")
    if sym.upper().endswith((".SH", ".SZ", ".BJ", ".HK", ".US")):
        return None  # double-suffixed garbage
    return sym, m.group("suffix")


def to_sina_symbol(security_id: str) -> str | None:
    """Sina dialect: sh600519 / sz000001 / bj830799 / hk00700 / gb_nvda.

    Quirks: HK is zero-padded to 5 digits; US is lowercased with 'gb_' prefix
    and share-class dots become '$' (BRK.B -> gb_brk$b).
    """
    parsed = _parse_canonical(security_id)
    if parsed is None:
        return None
    sym, suffix = parsed
    if suffix in {"SH", "SZ", "BJ"}:
        return suffix.lower() + sym
    if suffix == "HK":
        return "hk" + sym.zfill(5)
    return "gb_" + sym.lower().replace(".", "$")


def to_stooq_symbol(security_id: str) -> str | None:
    """Stooq dialect: aapl.us / 0700.hk. No CN A-share coverage.

    Quirks: US share-class dots become '-' (BRK.B -> brk-b.us); HK symbols are
    exactly 4 digits (09988 -> 9988.hk, 700 -> 0700.hk).
    """
    parsed = _parse_canonical(security_id)
    if parsed is None:
        return None
    sym, suffix = parsed
    if suffix == "US":
        return sym.lower().replace(".", "-") + ".us"
    if suffix == "HK":
        return (sym.lstrip("0") or "0").zfill(4) + ".hk"
    return None  # SH/SZ/BJ: Stooq has no A-share data


def to_fmp_symbol(security_id: str) -> str | None:
    """FMP dialect: AAPL / 0700.HK / 600519.SS (NOT .SH!). No BSE coverage.

    Quirks: Shanghai is Yahoo-style '.SS'; US share-class dots become '-'
    (BRK.B -> BRK-B); HK symbols are 4-digit padded.
    """
    parsed = _parse_canonical(security_id)
    if parsed is None:
        return None
    sym, suffix = parsed
    if suffix == "US":
        return sym.upper().replace(".", "-")
    if suffix == "HK":
        return (sym.lstrip("0") or "0").zfill(4) + ".HK"
    if suffix == "SH":
        return sym + ".SS"
    if suffix == "SZ":
        return sym + ".SZ"
    return None  # BJ: FMP has no Beijing Stock Exchange data


SYMBOL_DIALECTS: dict[str, Callable[[str], str | None]] = {
    "fmp": to_fmp_symbol,
    "stooq": to_stooq_symbol,
    "sina": to_sina_symbol,
}


# ---- http -------------------------------------------------------------------

def _client(timeout: float = 12.0) -> httpx.AsyncClient:
    """trust_env=False is mandatory (global SOCKS proxy on this machine must
    not leak in); proxy comes only from the explicit INSTITUTE_FETCH_PROXY.
    Tests monkeypatch this function with a MockTransport client."""
    return httpx.AsyncClient(
        trust_env=False,
        proxy=fetch_proxy(),
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _f(val: Any) -> float | None:
    """Lenient float ('1,234.5' ok); None on anything unparsable OR non-finite.

    Upstream payloads can carry "NaN"/"Infinity" strings (CSV) and even bare
    NaN/Infinity literals (json.loads accepts them), and NaN compares False
    against everything — a numeric gate alone can never catch it — so the
    parse layer already maps non-finite to None (REVIEW-B5 M1).
    """
    try:
        f = float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _finite(val: Any) -> bool:
    """Gate whitelist: a real, finite number (bool excluded). The gate checks
    this even though _f already sanitizes — check_bar/check_quote are public
    and must hold for hand-built payloads too (defense in depth)."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


def _norm_date(raw: str) -> str | None:
    """'2026/07/18' or '2026-07-18[ hh:mm:ss]' -> '2026-07-18'; None if bad."""
    txt = (raw or "").strip().replace("/", "-").split(" ")[0].split("T")[0]
    try:
        return date.fromisoformat(txt).isoformat()
    except ValueError:
        return None


# ---- per-source quote fetchers ------------------------------------------------
# Each returns a quote dict or None (unusable payload); network/HTTP errors
# raise and are caught by the ladder.

async def _fmp_quote(client: httpx.AsyncClient, security_id: str, sym: str) -> dict[str, Any] | None:
    r = await client.get(f"{FMP_BASE}/api/v3/quote/{sym}", params={"apikey": fmp_api_key() or ""})
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    q = rows[0]
    quote_date = None
    ts = _f(q.get("timestamp"))
    if ts:
        quote_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return {
        "security_id": security_id,
        "price": _f(q.get("price")),
        "prev_close": _f(q.get("previousClose")),
        "open": _f(q.get("open")),
        "high": _f(q.get("dayHigh")),
        "low": _f(q.get("dayLow")),
        "volume": _f(q.get("volume")),
        "quote_date": quote_date,
        "quote_time": None,
        "source": "fmp",
    }


async def _stooq_quote(client: httpx.AsyncClient, security_id: str, sym: str) -> dict[str, Any] | None:
    r = await client.get(f"{STOOQ_BASE}/q/l/", params={"s": sym, "f": "sd2t2ohlcv", "h": "", "e": "csv"})
    r.raise_for_status()
    lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    cells = lines[1].split(",")
    if len(cells) < 8 or "N/D" in cells[3:8]:
        return None
    return {
        "security_id": security_id,
        "price": _f(cells[6]),          # Close
        "prev_close": None,             # the light quote endpoint has none
        "open": _f(cells[3]),
        "high": _f(cells[4]),
        "low": _f(cells[5]),
        "volume": _f(cells[7]),
        "quote_date": _norm_date(cells[1]),
        "quote_time": cells[2].strip() or None,
        "source": "stooq",
    }


async def _sina_quote(client: httpx.AsyncClient, security_id: str, sym: str) -> dict[str, Any] | None:
    r = await client.get(f"{SINA_HQ_BASE}/list={sym}", headers=SINA_HEADERS)
    r.raise_for_status()
    text = r.content.decode(SINA_TEXT_ENCODING, errors="replace")
    m = re.search(r'="([^"]*)"', text)
    if not m or not m.group(1).strip():
        return None
    cells = m.group(1).split(",")
    if sym.startswith(("sh", "sz", "bj")) and len(cells) >= 32:
        return {
            "security_id": security_id,
            "price": _f(cells[3]),
            "prev_close": _f(cells[2]),
            "open": _f(cells[1]),
            "high": _f(cells[4]),
            "low": _f(cells[5]),
            "volume": _f(cells[8]),
            "quote_date": _norm_date(cells[30]),
            "quote_time": cells[31].strip() or None,
            "source": "sina",
        }
    if sym.startswith("hk") and len(cells) >= 19:
        return {
            "security_id": security_id,
            "price": _f(cells[6]),
            "prev_close": _f(cells[3]),
            "open": _f(cells[2]),
            "high": _f(cells[4]),
            "low": _f(cells[5]),
            "volume": _f(cells[12]),
            "quote_date": _norm_date(cells[17]),
            "quote_time": cells[18].strip() or None,
            "source": "sina",
        }
    if sym.startswith("gb_") and len(cells) >= 11:
        return {
            "security_id": security_id,
            "price": _f(cells[1]),
            "prev_close": _f(cells[26]) if len(cells) > 26 else None,
            "open": _f(cells[5]),
            "high": _f(cells[6]),
            "low": _f(cells[7]),
            "volume": _f(cells[10]),
            "quote_date": _norm_date(cells[3]),
            "quote_time": None,
            "source": "sina",
        }
    return None


# ---- per-source daily-bar fetchers --------------------------------------------
# Each returns a list of bar dicts (ascending bar_date) — raw parse only, the
# confidence gate happens at ingest time (refresh_security).

async def _fmp_daily(client: httpx.AsyncClient, security_id: str, sym: str, days: int) -> list[dict[str, Any]]:
    r = await client.get(
        f"{FMP_BASE}/api/v3/historical-price-full/{sym}",
        params={"timeseries": str(days), "apikey": fmp_api_key() or ""},
    )
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("historical") if isinstance(payload, dict) else None
    bars = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        bars.append({
            "bar_date": _norm_date(str(row.get("date", ""))),
            "open": _f(row.get("open")), "high": _f(row.get("high")),
            "low": _f(row.get("low")), "close": _f(row.get("close")),
            "volume": _f(row.get("volume")), "source": "fmp",
        })
    return sorted(bars, key=lambda b: b["bar_date"] or "")[-days:]


async def _stooq_daily(client: httpx.AsyncClient, security_id: str, sym: str, days: int) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days * 2 + 10)  # calendar padding over trading days
    r = await client.get(
        f"{STOOQ_BASE}/q/d/l/",
        params={"s": sym, "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d"), "i": "d"},
    )
    r.raise_for_status()
    lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        return []
    bars = []
    for ln in lines[1:]:
        cells = ln.split(",")
        if len(cells) < 5:
            continue
        bars.append({
            "bar_date": _norm_date(cells[0]),
            "open": _f(cells[1]), "high": _f(cells[2]), "low": _f(cells[3]), "close": _f(cells[4]),
            "volume": _f(cells[5]) if len(cells) > 5 else None, "source": "stooq",
        })
    return sorted(bars, key=lambda b: b["bar_date"] or "")[-days:]


async def _sina_daily(client: httpx.AsyncClient, security_id: str, sym: str, days: int) -> list[dict[str, Any]]:
    if not sym.startswith(("sh", "sz", "bj")):
        return []  # Sina daily klines wired for CN A-shares only (HK daily rides FMP/Stooq)
    r = await client.get(
        f"{SINA_KLINE_BASE}/quotes_service/api/json_v2.php/CN_MarketDataService.getKLineData",
        params={"symbol": sym, "scale": "240", "ma": "no", "datalen": str(days)},
        headers=SINA_HEADERS,
    )
    r.raise_for_status()
    try:
        rows = r.json()
    except ValueError:
        return []
    bars = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        bars.append({
            "bar_date": _norm_date(str(row.get("day", ""))),
            "open": _f(row.get("open")), "high": _f(row.get("high")),
            "low": _f(row.get("low")), "close": _f(row.get("close")),
            "volume": _f(row.get("volume")), "source": "sina",
        })
    return sorted(bars, key=lambda b: b["bar_date"] or "")[-days:]


_SOURCE_QUOTE: dict[str, Callable[..., Awaitable[dict[str, Any] | None]]] = {
    "fmp": _fmp_quote, "stooq": _stooq_quote, "sina": _sina_quote,
}
_SOURCE_DAILY: dict[str, Callable[..., Awaitable[list[dict[str, Any]]]]] = {
    "fmp": _fmp_daily, "stooq": _stooq_daily, "sina": _sina_daily,
}


# ---- confidence gate ----------------------------------------------------------

def _future_date(d: str) -> bool:
    """+1 day of slack: exchanges east of UTC print 'tomorrow' near midnight."""
    return d > (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def check_bar(bar: dict[str, Any], prev_close: float | None = None) -> list[str]:
    """Sanity problems for one daily bar; empty list = safe to write.

    Every numeric field must pass the _finite whitelist FIRST: NaN compares
    False against everything, so ``v <= 0``-style checks alone would wave
    NaN/Infinity straight through (REVIEW-B5 M1).
    """
    problems = []
    d = bar.get("bar_date")
    if not d or _norm_date(d) != d:
        problems.append(f"bad bar_date {d!r}")
    elif _future_date(d):
        problems.append(f"bar_date {d} is in the future")
    o, h, lo, c = (bar.get(k) for k in ("open", "high", "low", "close"))
    if any(not _finite(v) or v <= 0 for v in (o, h, lo, c)):
        problems.append("non-finite, non-positive or missing OHLC")
    else:
        if h < lo or h < max(o, c) or lo > min(o, c):
            problems.append(f"inconsistent OHLC o={o} h={h} l={lo} c={c}")
        if (h / lo - 1) >= MAX_MOVE_PCT:
            problems.append(f"intraday range {h}/{lo} exceeds {MAX_MOVE_PCT:.0%}")
        if _finite(prev_close) and prev_close > 0 and abs(c / prev_close - 1) >= MAX_MOVE_PCT:
            problems.append(f"close {c} moved >={MAX_MOVE_PCT:.0%} vs prev close {prev_close}")
    vol = bar.get("volume")
    if vol is not None and (not _finite(vol) or vol < 0):
        problems.append(f"non-finite or negative volume {vol}")
    return problems


def check_quote(quote: dict[str, Any]) -> list[str]:
    """Sanity problems for one realtime quote; empty list = usable.

    Same _finite whitelist as bars, over EVERY numeric field the payload
    carries (price/prev_close/open/high/low/volume — REVIEW-B5 M1). OHLC
    consistency is only enforced when o/h/l are all present and positive:
    pre-open snapshots legitimately carry 0 placeholders, and the live price
    itself is deliberately not range-checked against a possibly-lagging
    high/low.
    """
    problems = []
    price = quote.get("price")
    if not _finite(price) or price <= 0:
        problems.append(f"non-finite or non-positive price {price!r}")
    for key in ("prev_close", "open", "high", "low", "volume"):
        val = quote.get(key)
        if val is not None and not _finite(val):
            problems.append(f"non-finite {key} {val!r}")
    vol = quote.get("volume")
    if _finite(vol) and vol < 0:
        problems.append(f"negative volume {vol}")
    o, h, lo = (quote.get(k) for k in ("open", "high", "low"))
    if all(_finite(v) and v > 0 for v in (o, h, lo)) and (h < lo or not (lo <= o <= h)):
        problems.append(f"inconsistent OHLC o={o} h={h} l={lo}")
    prev = quote.get("prev_close")
    if _finite(price) and _finite(prev) and prev > 0 and abs(price / prev - 1) >= MAX_MOVE_PCT:
        problems.append(f"price {price} moved >={MAX_MOVE_PCT:.0%} vs prev close {prev}")
    d = quote.get("quote_date")
    if d is not None:
        if _norm_date(d) != d:
            problems.append(f"bad quote_date {d!r}")
        elif _future_date(d):
            problems.append(f"quote_date {d} is in the future")
    return problems


# ---- ladder -------------------------------------------------------------------

def _available_sources(security_id: str) -> list[tuple[str, str]]:
    """(source, dialect_symbol) pairs the ladder should try, in order."""
    out = []
    for source in SOURCES:
        if source == "fmp" and not fmp_api_key():
            continue
        sym = SYMBOL_DIALECTS[source](security_id)
        if sym:
            out.append((source, sym))
    return out


async def fetch_quote(security_id: str) -> dict[str, Any] | None:
    """Walk the ladder for a realtime quote. Insane quotes are refused (the
    gate also protects reads); every failure degrades to the next source.
    Returns None when no source produced a sane quote."""
    ladder = _available_sources(security_id)
    if not ladder:
        return None
    async with _client() as client:
        for source, sym in ladder:
            try:
                quote = await _SOURCE_QUOTE[source](client, security_id, sym)
            except Exception as exc:  # noqa: BLE001 - any source failure degrades
                log.warning("quote %s via %s (%s) failed: %s", security_id, source, sym, exc)
                continue
            if quote is None or quote.get("price") is None:
                continue
            problems = check_quote(quote)
            if problems:
                log.warning("refusing quote %s from %s: %s", security_id, source, "; ".join(problems))
                continue
            if quote.get("price") and quote.get("prev_close"):
                quote["change_pct"] = round((quote["price"] / quote["prev_close"] - 1) * 100, 2)
            return quote
    return None


def _gate_bars(security_id: str, bars: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Confidence gate over one source's parsed bars (ascending bar_date,
    sequential prev-close chain where prev = last PASSING bar's close).
    Returns (kept, rejected_count); refused rows are logged and dropped."""
    kept: list[dict[str, Any]] = []
    rejected = 0
    prev_close: float | None = None
    for bar in bars:
        problems = check_bar(bar, prev_close=prev_close)
        if problems:
            rejected += 1
            log.warning(
                "refuse-to-write bar %s %s from %s: %s",
                security_id, bar.get("bar_date"), bar.get("source"), "; ".join(problems),
            )
            continue
        prev_close = bar["close"]
        kept.append(bar)
    return kept, rejected


async def _fetch_daily_ladder(
    security_id: str, days: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Walk the ladder for daily bars. Source outcomes are THREE-way
    (REVIEW-B5 M2 — a format-valid but untrustworthy source must not end the
    ladder):

    1. network/parse failure, or zero rows parsed  -> degrade to next source;
    2. rows parsed but ALL refused by the confidence gate (pass rate == 0)
       -> the source's data is untrustworthy: degrade to next source too;
    3. at least one row passes (pass rate > 0) -> ADOPT this source; its
       refused rows are dropped (already logged), never written.

    Returns (kept_bars, meta); meta = {source, parsed, rejected} describes the
    adopted source, or {source: None, parsed: 0, rejected: 0} when the whole
    ladder is exhausted.
    """
    meta = {"source": None, "parsed": 0, "rejected": 0}
    ladder = _available_sources(security_id)
    if not ladder:
        return [], meta
    async with _client() as client:
        for source, sym in ladder:
            try:
                bars = await _SOURCE_DAILY[source](client, security_id, sym, days)
            except Exception as exc:  # noqa: BLE001 - outcome 1: degrade
                log.warning("bars %s via %s (%s) failed: %s", security_id, source, sym, exc)
                continue
            bars = [b for b in bars if b.get("bar_date")]
            if not bars:
                continue  # outcome 1: nothing parsed, degrade
            kept, rejected = _gate_bars(security_id, bars)
            if not kept:  # outcome 2: all rows refused — source untrusted, degrade
                log.warning(
                    "bars %s via %s: all %d rows failed the confidence gate; trying next source",
                    security_id, source, rejected,
                )
                continue
            meta = {"source": source, "parsed": len(bars), "rejected": rejected}
            return kept, meta  # outcome 3: adopt
    return [], meta


async def fetch_daily_bars(security_id: str, days: int = BUNDLE_BAR_DAYS) -> list[dict[str, Any]]:
    """Daily bars from the first ladder source with at least one bar that
    passes the confidence gate (refused rows are dropped); [] when every
    source fails, parses nothing, or is fully refused."""
    bars, _meta = await _fetch_daily_ladder(security_id, days)
    return bars


# ---- ingest (the hourly job's execution body) -----------------------------------

def _same_bar(known: dict[str, Any], bar: dict[str, Any]) -> bool:
    """Same facts as the latest known version, over the fields the fetcher
    asserts: OHLC + volume. Deliberately NOT compared: ``adj_factor`` (the
    fetchers write raw bars and never produce one — if a later corporate-
    action card stamps a real factor onto the newest version, re-fetching the
    same raw OHLCV must NOT append an adj_factor=1.0 version that would
    shadow it) and ``source`` (the same facts arriving via another source is
    not a correction)."""
    if any(known.get(k) != bar.get(k) for k in ("open", "high", "low", "close")):
        return False
    kv, bv = known.get("volume"), bar.get("volume")
    return kv == bv or (kv is None and bv is None)


async def refresh_security(security_id: str, *, days: int = BUNDLE_BAR_DAYS) -> dict[str, Any] | None:
    """Fetch recent daily bars and ingest them through the PIT store.

    The confidence gate runs INSIDE the ladder (_fetch_daily_ladder): a source
    whose rows are all refused degrades to the next source; the adopted
    source's refused rows are already logged and dropped. Here every kept bar
    is safe to write. To keep immutable version rows meaningful, a bar
    identical to the latest known version is skipped; only new bars / real
    corrections append a version (default microsecond as_known_at clock).
    Returns stats (fetched = rows parsed from the adopted source), or None
    when the security does not exist.
    """
    sec = await db.query_one("SELECT * FROM securities WHERE id = ?", (security_id,))
    if sec is None:
        return None
    bars, meta = await _fetch_daily_ladder(security_id, days)
    stats = {"security_id": security_id, "fetched": meta["parsed"], "written": 0, "corrected": 0,
             "unchanged": 0, "rejected": meta["rejected"], "source": meta["source"]}
    if not bars:
        return stats

    dates = [b["bar_date"] for b in bars]
    known = {
        b["bar_date"]: b
        for b in await market_data.get_bars_pit(security_id, start=min(dates), end=max(dates))
    }
    for bar in bars:  # ascending bar_date, all gate-passed
        existing = known.get(bar["bar_date"])
        if existing is not None and _same_bar(existing, bar):
            stats["unchanged"] += 1
            continue
        await market_data.upsert_bar({
            "security_id": security_id, "bar_date": bar["bar_date"],
            "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "volume": bar["volume"], "source": bar["source"],
        })
        stats["corrected" if existing is not None else "written"] += 1
    return stats


async def refresh_all(limit: int = 20) -> dict[str, Any]:
    """Refresh the stalest active securities (fetchable markets only). The
    scheduler mounts this as ``market-refresh``; it no-ops when fetching is
    disabled or there is nothing to refresh, and never raises per item."""
    if not market_fetch_enabled():
        return {"enabled": False, "refreshed": 0, "items": []}
    rows = await db.query(
        "SELECT s.id FROM securities s WHERE s.listing_status = 'active' "
        "AND s.market IN ('CN_A','HK','US') "
        "ORDER BY COALESCE((SELECT MAX(b.as_known_at) FROM price_bars b "
        "WHERE b.security_id = s.id), '') ASC, s.id LIMIT ?",
        (max(1, limit),),
    )
    items = []
    for row in rows:
        try:
            stats = await refresh_security(row["id"])
        except Exception as exc:  # noqa: BLE001 - one bad security must not stop the sweep
            log.exception("refresh failed for %s", row["id"])
            stats = {"security_id": row["id"], "error": str(exc)}
        if stats:
            items.append(stats)
    summary = {
        "enabled": True, "refreshed": len(items),
        "written": sum(i.get("written", 0) + i.get("corrected", 0) for i in items),
        "rejected": sum(i.get("rejected", 0) for i in items),
        "items": items,
    }
    if items:
        await bus.emit("market.refreshed", "market", work_date(), {
            k: v for k, v in summary.items() if k != "items"
        })
    return summary


# ---- topic -> securities resolution ---------------------------------------------

_ID_IN_TEXT_RE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)|[0-9]{4,5}\.HK|[A-Z][A-Z.\-]{0,9}\.US")
_BARE_A_SHARE_RE = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")
_MARKET_ORDER = "CASE market WHEN 'CN_A' THEN 0 WHEN 'HK' THEN 1 WHEN 'US' THEN 2 ELSE 3 END"


async def resolve_security(text: str) -> dict[str, Any] | None:
    """Exact lookup: canonical id -> unsuffixed symbol -> alias (0004 tables).
    A-share-first tie-break on ambiguous symbols (operator intent: A股为主)."""
    t = (text or "").strip()
    if not t:
        return None
    for cand in dict.fromkeys((t, t.upper())):
        row = await db.query_one("SELECT * FROM securities WHERE id = ?", (cand,))
        if row:
            return row
    for cand in dict.fromkeys((t, t.upper())):
        row = await db.query_one(
            f"SELECT * FROM securities WHERE symbol = ? ORDER BY {_MARKET_ORDER}, id LIMIT 1",
            (cand,),
        )
        if row:
            return row
    for cand in dict.fromkeys((t, t.upper())):
        row = await db.query_one(
            "SELECT s.* FROM security_aliases a JOIN securities s ON s.id = a.security_id "
            f"WHERE a.alias = ? ORDER BY {_MARKET_ORDER}, s.id LIMIT 1",
            (cand,),
        )
        if row:
            return row
    return None


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _name_in_topic(name: str, topic: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    if _has_cjk(name):
        return len(name) >= 2 and name in topic
    if len(name) < 3:
        return False  # 'MU'-style tickers false-positive inside English words
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", topic, re.I) is not None


async def match_topic_securities(topic: str, cap: int = BUNDLE_MAX_SECURITIES) -> list[dict[str, Any]]:
    """Securities a research topic is about: whole-topic exact match, canonical
    ids / bare A-share codes inside the text, then name/alias substring scan."""
    t = (topic or "").strip()
    if not t:
        return []
    hits: dict[str, tuple[float, dict[str, Any]]] = {}

    def _add(row: dict[str, Any] | None, score: float) -> None:
        if row and (row["id"] not in hits or hits[row["id"]][0] < score):
            hits[row["id"]] = (score, row)

    _add(await resolve_security(t), 1000)
    for m in _ID_IN_TEXT_RE.finditer(t.upper()):
        _add(await db.query_one("SELECT * FROM securities WHERE id = ?", (m.group(0),)), 500)
    for m in _BARE_A_SHARE_RE.finditer(t):
        _add(await db.query_one(
            "SELECT * FROM securities WHERE symbol = ? AND market = 'CN_A' LIMIT 1", (m.group(0),)
        ), 400)

    named: list[tuple[str, str]] = [
        (r["alias"], r["security_id"]) for r in await db.query("SELECT alias, security_id FROM security_aliases")
    ]
    named += [
        (r[k], r["id"])
        for r in await db.query("SELECT id, name_zh, name_en FROM securities")
        for k in ("name_zh", "name_en") if r.get(k)
    ]
    for name, sid in named:
        if _name_in_topic(name, t):
            row = await db.query_one("SELECT * FROM securities WHERE id = ?", (sid,))
            _add(row, min(len(name), 100))

    ranked = sorted(hits.values(), key=lambda pair: (-pair[0], pair[1]["id"]))
    return [row for _, row in ranked[:cap]]


# ---- research data bundle --------------------------------------------------------

def _fmt(val: float | None, nd: int = 2) -> str:
    if val is None:
        return "—"
    return f"{val:,.{nd}f}".rstrip("0").rstrip(".") if nd else f"{val:,.0f}"


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


async def _benchmark_line(market: str, start: str, sec_pct: float | None) -> str | None:
    bench_id = MARKET_BENCHMARKS.get(market)
    if not bench_id:
        return None
    marks = await market_data.get_marks_pit(bench_id, start=start)
    if len(marks) < 2 or not marks[0]["value"]:
        return None
    bench_pct = _pct(marks[-1]["value"], marks[0]["value"])
    line = f"基准对比：{bench_id} 同期 {bench_pct:+.2f}%"
    if sec_pct is not None:
        line += f"，相对超额 {sec_pct - bench_pct:+.2f}pct"
    return line


async def _security_section(sec: dict[str, Any], start: str) -> str | None:
    """One bundle section from LOCAL PIT data (latest known); None = no data."""
    bars = await market_data.get_bars_pit(sec["id"], start=start)
    if not bars:
        return None
    last = bars[-1]
    name = sec.get("name_zh") or sec.get("name_en") or sec["id"]
    lines = [f"== {name}（{sec['id']} · {sec['market']} · {sec.get('currency') or '—'}）=="]
    lines.append(
        f"最新日线：{last['bar_date']} 收 {_fmt(last['close'])}，开 {_fmt(last['open'])}，"
        f"高 {_fmt(last['high'])}，低 {_fmt(last['low'])}，量 {_fmt(last['volume'], 0)}"
        f"（来源 {last.get('source') or '本地'}）"
    )
    sec_pct: float | None = None
    if len(bars) >= 2 and bars[0]["close"]:
        sec_pct = _pct(last["close"], bars[0]["close"])
        hi = max(bars, key=lambda b: b["high"])
        lo = min(bars, key=lambda b: b["low"])
        lines.append(
            f"近{BUNDLE_BAR_DAYS}天（{len(bars)} 根K线）：区间涨跌 {sec_pct:+.2f}%，"
            f"最高 {_fmt(hi['high'])}（{hi['bar_date']}），最低 {_fmt(lo['low'])}（{lo['bar_date']}）"
        )
        tail = bars[-5:]
        lines.append("近5日收盘：" + " → ".join(_fmt(b["close"]) for b in tail))
    bench = await _benchmark_line(sec["market"], start, sec_pct)
    if bench:
        lines.append(bench)
    if await market_data.is_suspended(sec["id"], work_date()):
        lines.append("状态：当前处于停牌区间（详见停牌记录）")
    return "\n".join(lines)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[: max_bytes - len("…".encode("utf-8"))].decode("utf-8", errors="ignore")
    return cut + "…"


async def build_data_bundle(topic: str) -> str:
    """Render the ≤4KB plain-text data bundle for a research topic.

    Local reads only (PIT latest known) — no network on the prompt path.
    Non-empty renders are upserted into shared_data (topic, work_date).
    Returns "" when the topic matches nothing or nothing has data (the
    ${DATA_BUNDLE} substitution then degrades without a trace).
    """
    topic = (topic or "").strip()
    if not topic:
        return ""
    matched = await match_topic_securities(topic)
    if not matched:
        return ""
    start = (date.fromisoformat(work_date()) - timedelta(days=BUNDLE_BAR_DAYS)).isoformat()
    sections, used = [], []
    for sec in matched:
        section = await _security_section(sec, start)
        if section:
            sections.append(section)
            used.append(sec["id"])
    if not sections:
        return ""
    header = (
        f"【行情数据注入】以下为研究所本地行情库（point-in-time 最新已知值）自动生成的数据摘要，"
        f"生成于 {work_date()}（SGT）。数据可能滞后于实时行情，引用请注明数据时点。"
    )
    bundle = _truncate_utf8("\n\n".join([header, *sections]), BUNDLE_MAX_BYTES)
    await _store_bundle(topic, bundle, {"securities": used, "bytes": len(bundle.encode("utf-8"))})
    return bundle


async def _store_bundle(topic: str, content: str, metadata: dict[str, Any]) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO shared_data (id, topic, work_date, content, metadata_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(topic, work_date) DO UPDATE SET content = excluded.content, "
        "metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
        (uuid.uuid4().hex[:12], topic, work_date(), content, json.dumps(metadata, ensure_ascii=False), now, now),
    )


async def latest_bundle(topic: str) -> dict[str, Any] | None:
    """Newest stored bundle for a topic; renders one on miss (may still be None)."""
    topic = (topic or "").strip()
    if not topic:
        return None
    row = await db.query_one(
        "SELECT * FROM shared_data WHERE topic = ? ORDER BY work_date DESC LIMIT 1", (topic,)
    )
    if row is None:
        if await build_data_bundle(topic):
            row = await db.query_one(
                "SELECT * FROM shared_data WHERE topic = ? ORDER BY work_date DESC LIMIT 1", (topic,)
            )
    if row is None:
        return None
    out = dict(row)
    try:
        out["metadata"] = json.loads(out.pop("metadata_json", None) or "{}")
    except ValueError:
        out["metadata"] = {}
    return out


async def data_bundle_variable(variables: dict[str, Any]) -> str:
    """${DATA_BUNDLE} value for a workflow run. Never raises — any failure
    (or a topic with no data) renders as "" so prompts degrade silently."""
    topic = str(variables.get("TOPIC") or "").strip()
    if not topic:
        return ""
    try:
        return await build_data_bundle(topic)
    except Exception:  # noqa: BLE001 - the prompt path must never break on data
        log.exception("data bundle failed for topic %r", topic)
        return ""
