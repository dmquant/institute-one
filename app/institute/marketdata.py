"""Market and financial data adapters.

First pass:
- quotes: optional IBKR recent historical bar; do not use FMP for prices
- fundamentals: optional FMP statements, falling back to SEC EDGAR companyfacts

Every fetched object is cached in ``shared_data`` for the current work date so
research prompts can cite a stable local data bundle instead of relying only on
agent web search.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

import httpx

from .. import bus, db
from ..config import get_settings
from .prompts import work_date

log = logging.getLogger("institute.marketdata")

HTTP_TIMEOUT = 20.0
SEC_TICKERS_KEY = "__sec_company_tickers__"
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

FMP_STABLE_URL = "https://financialmodelingprep.com/stable/{endpoint}"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def normalize_ticker(topic: str) -> str | None:
    ticker = (topic or "").strip().upper()
    ticker = ticker.removeprefix("$")
    ticker = ticker.replace("/", "-")
    return ticker if TICKER_RE.match(ticker) else None


async def _get_cached(topic: str, data_type: str, *, wd: str | None = None) -> dict[str, Any] | None:
    row = await db.query_one(
        "SELECT * FROM shared_data WHERE topic=? AND data_type=? AND work_date=?",
        (topic, data_type, wd or work_date()),
    )
    if row is None:
        return None
    try:
        row["payload"] = json.loads(row["payload"] or "{}")
    except ValueError:
        row["payload"] = {}
    return row


async def _upsert(topic: str, data_type: str, provider: str, confidence: float, payload: dict[str, Any]) -> dict[str, Any]:
    wd = work_date()
    now = bus.now_iso()
    await db.execute(
        """INSERT INTO shared_data (topic, data_type, work_date, provider, confidence, payload, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(topic, data_type, work_date) DO UPDATE SET
             provider=excluded.provider,
             confidence=excluded.confidence,
             payload=excluded.payload,
             updated_at=excluded.updated_at""",
        (topic, data_type, wd, provider, confidence, json.dumps(payload, ensure_ascii=False), now, now),
    )
    return await _get_cached(topic, data_type, wd=wd) or {
        "topic": topic, "data_type": data_type, "work_date": wd,
        "provider": provider, "confidence": confidence, "payload": payload,
    }


async def latest(topic: str, data_type: str | None = None) -> list[dict[str, Any]]:
    topic_norm = normalize_ticker(topic) or (topic or "").strip()
    if not topic_norm:
        return []
    params: list[Any] = [topic_norm]
    sql = "SELECT * FROM shared_data WHERE topic=?"
    if data_type:
        sql += " AND data_type=?"
        params.append(data_type)
    sql += " ORDER BY updated_at DESC"
    rows = await db.query(sql, params)
    for row in rows:
        try:
            row["payload"] = json.loads(row["payload"] or "{}")
        except ValueError:
            row["payload"] = {}
    return rows


async def get_quote(ticker: str, *, refresh: bool = False) -> dict[str, Any]:
    ticker_norm = normalize_ticker(ticker)
    if ticker_norm is None:
        raise ValueError(f"invalid ticker: {ticker}")
    if not refresh:
        cached = await _get_cached(ticker_norm, "quote")
        if cached is not None:
            return cached

    settings = get_settings()
    if settings.price_provider.lower() == "ibkr":
        payload = await _ibkr_recent_bar_quote(ticker_norm)
        confidence = 0.75 if payload.get("available") else 0.0
        return await _upsert(ticker_norm, "quote", "ibkr", confidence, payload)

    payload = {
        "ticker": ticker_norm,
        "available": False,
        "reason": "price provider disabled; set INSTITUTE_PRICE_PROVIDER=ibkr to use recent IBKR historical bars",
    }
    return await _upsert(ticker_norm, "quote", "none", 0.0, payload)


async def _ibkr_recent_bar_quote(ticker: str) -> dict[str, Any]:
    """Fetch an approximate price from recent IBKR historical daily bars.

    This intentionally does NOT call market-data snapshot APIs, which can add
    per-request charges. The returned close is suitable for research context,
    not for execution or intraday precision.
    """
    settings = get_settings()
    try:
        from ib_async import IB, Stock  # type: ignore[import-not-found]
    except ImportError:
        return {
            "ticker": ticker,
            "available": False,
            "reason": "ib_async is not installed; install dependencies before enabling IBKR price provider",
        }

    ib = IB()
    try:
        await ib.connectAsync(
            settings.ibkr_host,
            settings.ibkr_port,
            clientId=settings.ibkr_client_id,
            timeout=settings.ibkr_connect_timeout_s,
            readonly=True,
        )
        contract = Stock(ticker, "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return {"ticker": ticker, "available": False, "reason": "IBKR could not qualify stock contract"}
        bars = await ib.reqHistoricalDataAsync(
            qualified[0],
            endDateTime="",
            durationStr=settings.ibkr_history_duration,
            barSizeSetting=settings.ibkr_history_bar_size,
            whatToShow=settings.ibkr_history_what_to_show,
            useRTH=settings.ibkr_history_use_rth,
            formatDate=1,
            keepUpToDate=False,
        )
    except Exception as exc:  # noqa: BLE001 - quote failures must degrade cleanly
        return {"ticker": ticker, "available": False, "reason": f"IBKR historical bar unavailable: {str(exc)[:240]}"}
    finally:
        if ib.isConnected():
            ib.disconnect()

    return _ibkr_bars_to_quote(ticker, bars)


def _ibkr_bars_to_quote(ticker: str, bars: Sequence[Any]) -> dict[str, Any]:
    if not bars:
        return {"ticker": ticker, "available": False, "reason": "IBKR returned no historical bars"}
    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None
    close = getattr(last, "close", None)
    if close is None:
        return {"ticker": ticker, "available": False, "reason": "IBKR latest bar has no close"}

    prev_close = getattr(prev, "close", None) if prev is not None else None
    change = None
    changes_pct = None
    if prev_close not in (None, 0):
        change = float(close) - float(prev_close)
        changes_pct = change / float(prev_close) * 100.0

    return {
        "ticker": ticker,
        "available": True,
        "mode": "historical_bar",
        "approximate": True,
        "price": close,
        "currency": "USD",
        "change": change,
        "changesPercentage": changes_pct,
        "bar_date": str(getattr(last, "date", "")),
        "bar_size": get_settings().ibkr_history_bar_size,
        "open": getattr(last, "open", None),
        "high": getattr(last, "high", None),
        "low": getattr(last, "low", None),
        "close": close,
        "volume": getattr(last, "volume", None),
        "source": "IBKR reqHistoricalData",
        "timestamp": bus.now_iso(),
    }


async def _fmp_json(endpoint: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]] | None:
    settings = get_settings()
    if not settings.fmp_api_key:
        return None
    params = {"symbol": ticker, "apikey": settings.fmp_api_key, "limit": limit}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(FMP_STABLE_URL.format(endpoint=endpoint), params=params)
        if resp.status_code >= 400:
            log.warning("FMP %s failed for %s: HTTP %s", endpoint, ticker, resp.status_code)
            return None
        data = resp.json()
    return data if isinstance(data, list) else None


async def _fmp_financials(ticker: str) -> dict[str, Any] | None:
    income, balance, cashflow, metrics = await _fetch_fmp_financials(ticker)
    if not any((income, balance, cashflow, metrics)):
        return None

    latest_income = income[0] if income else {}
    latest_balance = balance[0] if balance else {}
    latest_cashflow = cashflow[0] if cashflow else {}
    latest_metrics = metrics[0] if metrics else {}
    payload = {
        "ticker": ticker,
        "available": True,
        "company": latest_income.get("symbol") or ticker,
        "source_url": "https://site.financialmodelingprep.com/developer/docs/stable/income-statement",
        "statements": {
            "income": income,
            "balance": balance,
            "cashflow": cashflow,
            "key_metrics": metrics,
        },
        "facts": {
            "revenue": _fmp_fact(latest_income, "revenue"),
            "gross_profit": _fmp_fact(latest_income, "grossProfit"),
            "operating_income": _fmp_fact(latest_income, "operatingIncome"),
            "net_income": _fmp_fact(latest_income, "netIncome"),
            "eps": _fmp_fact(latest_income, "eps"),
            "diluted_eps": _fmp_fact(latest_income, "epsDiluted"),
            "operating_cash_flow": _fmp_fact(latest_cashflow, "operatingCashFlow"),
            "free_cash_flow": _fmp_fact(latest_cashflow, "freeCashFlow"),
            "assets": _fmp_fact(latest_balance, "totalAssets"),
            "liabilities": _fmp_fact(latest_balance, "totalLiabilities"),
            "equity": _fmp_fact(latest_balance, "totalStockholdersEquity"),
            "market_cap": _fmp_fact(latest_metrics, "marketCap"),
            "pe": _fmp_fact(latest_metrics, "peRatio"),
            "roe": _fmp_fact(latest_metrics, "roe"),
        },
    }
    payload["facts"] = {k: v for k, v in payload["facts"].items() if v is not None}
    return payload


async def _fetch_fmp_financials(ticker: str) -> tuple[list[dict[str, Any]] | None, ...]:
    return (
        await _fmp_json("income-statement", ticker, limit=5),
        await _fmp_json("balance-sheet-statement", ticker, limit=5),
        await _fmp_json("cash-flow-statement", ticker, limit=5),
        await _fmp_json("key-metrics", ticker, limit=5),
    )


def _fmp_fact(row: dict[str, Any], key: str) -> dict[str, Any] | None:
    if not row or row.get(key) is None:
        return None
    return {
        "value": row.get(key),
        "unit": row.get("reportedCurrency") or "USD",
        "fy": row.get("fiscalYear") or row.get("calendarYear"),
        "fp": row.get("period"),
        "form": row.get("form"),
        "end": row.get("date"),
        "filed": row.get("filingDate"),
        "accepted": row.get("acceptedDate"),
    }


async def _sec_headers() -> dict[str, str]:
    return {"User-Agent": get_settings().sec_user_agent, "Accept-Encoding": "gzip, deflate"}


async def _sec_ticker_map(refresh: bool = False) -> dict[str, dict[str, Any]]:
    if not refresh:
        cached = await _get_cached(SEC_TICKERS_KEY, "sec_tickers")
        if cached and isinstance(cached.get("payload"), dict):
            return cached["payload"].get("tickers", {})

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=await _sec_headers()) as client:
        resp = await client.get(SEC_TICKERS_URL)
        resp.raise_for_status()
        raw = resp.json()

    tickers: dict[str, dict[str, Any]] = {}
    for item in raw.values() if isinstance(raw, dict) else []:
        ticker = str(item.get("ticker", "")).upper()
        cik = item.get("cik_str")
        if ticker and cik is not None:
            tickers[ticker] = {
                "ticker": ticker,
                "cik": str(cik).zfill(10),
                "title": item.get("title"),
            }
    await _upsert(SEC_TICKERS_KEY, "sec_tickers", "sec", 0.95, {"tickers": tickers})
    return tickers


def _latest_fact(companyfacts: dict[str, Any], tags: list[str], units: list[str]) -> dict[str, Any] | None:
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    candidates: list[dict[str, Any]] = []
    for tag in tags:
        units_obj = facts.get(tag, {}).get("units", {})
        for unit in units:
            for item in units_obj.get(unit, []) or []:
                if item.get("val") is None:
                    continue
                candidates.append({
                    "tag": tag,
                    "unit": unit,
                    "value": item.get("val"),
                    "fy": item.get("fy"),
                    "fp": item.get("fp"),
                    "form": item.get("form"),
                    "end": item.get("end"),
                    "filed": item.get("filed"),
                    "frame": item.get("frame"),
                    "accn": item.get("accn"),
                })
    if not candidates:
        return None
    candidates.sort(key=lambda x: (str(x.get("filed") or ""), str(x.get("end") or "")), reverse=True)
    return candidates[0]


def _financial_snapshot(companyfacts: dict[str, Any]) -> dict[str, Any]:
    concepts = {
        "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"], ["USD"]),
        "net_income": (["NetIncomeLoss", "ProfitLoss"], ["USD"]),
        "operating_cash_flow": (["NetCashProvidedByUsedInOperatingActivities"], ["USD"]),
        "assets": (["Assets"], ["USD"]),
        "liabilities": (["Liabilities"], ["USD"]),
        "equity": (["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], ["USD"]),
        "diluted_eps": (["EarningsPerShareDiluted"], ["USD/shares"]),
        "shares_outstanding": (["EntityCommonStockSharesOutstanding"], ["shares"]),
    }
    out: dict[str, Any] = {}
    for name, (tags, units) in concepts.items():
        fact = _latest_fact(companyfacts, tags, units)
        if fact is not None:
            out[name] = fact
    return out


async def get_financials(ticker: str, *, refresh: bool = False) -> dict[str, Any]:
    ticker_norm = normalize_ticker(ticker)
    if ticker_norm is None:
        raise ValueError(f"invalid ticker: {ticker}")
    if not refresh:
        cached = await _get_cached(ticker_norm, "financials")
        if cached is not None:
            return cached

    fmp_payload = await _fmp_financials(ticker_norm)
    if fmp_payload is not None:
        return await _upsert(ticker_norm, "financials", "fmp", 0.85, fmp_payload)

    tickers = await _sec_ticker_map(refresh=False)
    sec = tickers.get(ticker_norm)
    if sec is None:
        payload = {"ticker": ticker_norm, "available": False, "reason": "ticker not found in SEC company_tickers.json"}
        return await _upsert(ticker_norm, "financials", "sec", 0.1, payload)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=await _sec_headers()) as client:
        resp = await client.get(SEC_COMPANYFACTS_URL.format(cik=sec["cik"]))
        resp.raise_for_status()
        companyfacts = resp.json()

    payload = {
        "ticker": ticker_norm,
        "available": True,
        "company": companyfacts.get("entityName") or sec.get("title"),
        "cik": sec["cik"],
        "facts": _financial_snapshot(companyfacts),
        "source_url": SEC_COMPANYFACTS_URL.format(cik=sec["cik"]),
    }
    return await _upsert(ticker_norm, "financials", "sec", 0.9, payload)


async def get_bundle(topic: str, *, refresh: bool = False) -> dict[str, Any]:
    ticker = normalize_ticker(topic)
    if ticker is None:
        return {
            "topic": topic,
            "available": False,
            "reason": "topic is not a simple ticker",
            "quote": None,
            "financials": None,
        }
    try:
        quote = await get_quote(ticker, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - data fetch must not break research
        log.warning("quote fetch failed for %s: %s", ticker, exc)
        quote = {
            "topic": ticker,
            "data_type": "quote",
            "provider": "error",
            "confidence": 0.0,
            "payload": {"ticker": ticker, "available": False, "reason": str(exc)[:300]},
        }
    try:
        financials = await get_financials(ticker, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        log.warning("financials fetch failed for %s: %s", ticker, exc)
        financials = {
            "topic": ticker,
            "data_type": "financials",
            "provider": "error",
            "confidence": 0.0,
            "payload": {"ticker": ticker, "available": False, "reason": str(exc)[:300]},
        }
    return {
        "topic": topic,
        "ticker": ticker,
        "available": bool((quote.get("payload") or {}).get("available") or (financials.get("payload") or {}).get("available")),
        "quote": quote,
        "financials": financials,
    }


def _fmt_money(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    return f"{v:,.2f}"


def bundle_markdown(bundle: dict[str, Any]) -> str:
    if not bundle.get("available"):
        return f"## 本地数据包\n\n本地 marketdata 暂无可用数据：{bundle.get('reason') or 'not available'}。"

    lines = [f"## 本地数据包：{bundle.get('ticker') or bundle.get('topic')}", ""]
    quote_payload = ((bundle.get("quote") or {}).get("payload") or {})
    if quote_payload.get("available"):
        lines.extend([
            "### Quote",
            f"- Provider: {(bundle.get('quote') or {}).get('provider')}",
            f"- Mode: {quote_payload.get('mode')}; approximate: {quote_payload.get('approximate')}",
            f"- Price: {quote_payload.get('price')}  Change: {quote_payload.get('change')} ({quote_payload.get('changesPercentage')}%)",
            f"- Bar date: {quote_payload.get('bar_date')}; source: {quote_payload.get('source')}",
            f"- Market cap: {_fmt_money(quote_payload.get('marketCap'))}; PE: {quote_payload.get('pe')}; EPS: {quote_payload.get('eps')}",
            f"- Timestamp: {quote_payload.get('timestamp')}",
            "",
        ])
    else:
        lines.extend(["### Quote", f"- Unavailable: {quote_payload.get('reason')}", ""])

    fin_payload = ((bundle.get("financials") or {}).get("payload") or {})
    if fin_payload.get("available"):
        lines.extend([
            "### SEC Financial Snapshot",
            f"- Company: {fin_payload.get('company')} (CIK {fin_payload.get('cik')})",
            f"- Source: {fin_payload.get('source_url')}",
        ])
        facts = fin_payload.get("facts") or {}
        for key in ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity", "diluted_eps", "shares_outstanding"):
            fact = facts.get(key)
            if not fact:
                continue
            lines.append(
                f"- {key}: {_fmt_money(fact.get('value'))} {fact.get('unit')} "
                f"({fact.get('form')} FY{fact.get('fy')} {fact.get('fp')}, end {fact.get('end')}, filed {fact.get('filed')})"
            )
    else:
        lines.extend(["### SEC Financial Snapshot", f"- Unavailable: {fin_payload.get('reason')}"])
    return "\n".join(lines)
