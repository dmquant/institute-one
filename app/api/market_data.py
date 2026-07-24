from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..institute import market_data, market_fetchers

# Prefix is /api (not /api/market): the fetcher card B5 adds /api/quote/* and
# /api/data/* next to /api/market/* per ROADMAP Phase 1b, and main.py mounts
# exactly ONE router from this module. Every pre-existing route spells the
# /market segment out, so all URLs are unchanged.
router = APIRouter(prefix="/api", tags=["market"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409."""
    try:
        return await fn(*args, **kwargs)
    except market_data.TransitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except market_data.MarketDataError as exc:
        raise HTTPException(400, str(exc)) from exc


class CalendarDayUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos 422, matching the domain's strictness

    market: str
    cal_date: str
    is_open: bool
    note: str | None = None


class SuspensionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_id: str
    start_date: str
    end_date: str | None = None
    reason: str = ""
    source: str | None = None


class SuspensionClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    end_date: str


class BarUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_id: str
    freq: str = "1d"
    bar_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    adj_factor: float = 1.0
    valid_time: str | None = None      # defaults to bar_date (00:00 UTC)
    as_known_at: str | None = None     # defaults to now — pass explicitly to backfill a revision stream
    source: str | None = None
    metadata: dict = Field(default_factory=dict)


class BenchmarkUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name_zh: str | None = None
    name_en: str | None = None
    market: str | None = None
    currency: str | None = None
    source: str = "manual"
    metadata: dict = Field(default_factory=dict)


class BenchmarkMarkUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mark_date: str
    value: float
    valid_time: str | None = None
    as_known_at: str | None = None
    source: str | None = None
    metadata: dict = Field(default_factory=dict)


# ---- calendar & suspensions --------------------------------------------------

@router.get("/market/calendar")
async def get_calendar(market: str, start: str | None = None, end: str | None = None):
    return await _call(market_data.get_calendar, market, start=start, end=end)


@router.post("/market/calendar")
async def set_calendar_day(body: CalendarDayUpsert):
    return await _call(
        market_data.set_calendar_day, body.market, body.cal_date, body.is_open, note=body.note
    )


@router.get("/market/suspensions")
async def list_suspensions(security_id: str | None = None, active_on: str | None = None):
    return await _call(market_data.list_suspensions, security_id=security_id, active_on=active_on)


@router.post("/market/suspensions")
async def add_suspension(body: SuspensionCreate):
    return await _call(
        market_data.add_suspension, body.security_id, body.start_date, body.end_date,
        reason=body.reason, source=body.source,
    )


@router.post("/market/suspensions/{suspension_id}/close")
async def close_suspension(suspension_id: str, body: SuspensionClose):
    row = await _call(market_data.close_suspension, suspension_id, body.end_date)
    if row is None:
        raise HTTPException(404, "suspension not found")
    return row


@router.get("/market/status/{security_id:path}")
async def get_trading_status(security_id: str, cal_date: str):
    """Closed vs suspended, combined, for one security on one day."""
    status = await _call(market_data.get_trading_status, security_id, cal_date)
    if status is None:
        raise HTTPException(404, "security not found")
    return status


# ---- bars (PIT) ----------------------------------------------------------------

@router.get("/market/bars")
async def get_bars(
    security_id: str,
    as_of: str | None = None,
    freq: str = "1d",
    start: str | None = None,
    end: str | None = None,
):
    """Bars as known at ``as_of`` (omit for latest known)."""
    return await _call(
        market_data.get_bars_pit, security_id, as_of, freq=freq, start=start, end=end
    )


@router.post("/market/bars")
async def upsert_bar(body: BarUpsert):
    return await _call(market_data.upsert_bar, body.model_dump(exclude_unset=True))


# ---- benchmarks -----------------------------------------------------------------

@router.get("/market/benchmarks")
async def list_benchmarks():
    return await market_data.list_benchmarks()


@router.post("/market/benchmarks")
async def upsert_benchmark(body: BenchmarkUpsert):
    return await _call(market_data.upsert_benchmark, body.model_dump(exclude_unset=True))


@router.get("/market/benchmarks/{benchmark_id}")
async def get_benchmark(benchmark_id: str):
    bench = await market_data.get_benchmark(benchmark_id)
    if bench is None:
        raise HTTPException(404, "benchmark not found")
    return bench


@router.get("/market/benchmarks/{benchmark_id}/marks")
async def get_marks(
    benchmark_id: str,
    as_of: str | None = None,
    start: str | None = None,
    end: str | None = None,
):
    return await _call(market_data.get_marks_pit, benchmark_id, as_of, start=start, end=end)


@router.post("/market/benchmarks/{benchmark_id}/marks")
async def upsert_mark(benchmark_id: str, body: BenchmarkMarkUpsert):
    return await _call(
        market_data.upsert_benchmark_mark, benchmark_id, body.model_dump(exclude_unset=True)
    )


# ---- fetcher ladder (Phase 1b, card B5) ------------------------------------------

@router.get("/quote/{ticker:path}")
async def get_quote(ticker: str):
    """Realtime quote via the FMP -> Stooq -> Sina ladder. ``ticker`` accepts a
    canonical id, an unsuffixed symbol, or an alias (resolve_security)."""
    sec = await market_fetchers.resolve_security(ticker)
    if sec is None:
        raise HTTPException(404, f"security {ticker!r} not found")
    quote = await _call(market_fetchers.fetch_quote, sec["id"])
    if quote is None:
        raise HTTPException(502, f"no source produced a sane quote for {sec['id']}")
    return {**quote, "name_zh": sec.get("name_zh"), "name_en": sec.get("name_en"),
            "market": sec.get("market")}


@router.get("/data/{topic:path}/latest")
async def get_latest_bundle(topic: str):
    """Newest rendered ${DATA_BUNDLE} for a topic (renders one on cache miss)."""
    bundle = await _call(market_fetchers.latest_bundle, topic)
    if bundle is None:
        raise HTTPException(404, f"no data bundle available for topic {topic!r}")
    return bundle


@router.post("/market/refresh/{security_id:path}")
async def refresh_security(security_id: str):
    """Manually run the fetcher ladder for one security (confidence-gated PIT ingest)."""
    stats = await _call(market_fetchers.refresh_security, security_id)
    if stats is None:
        raise HTTPException(404, f"security {security_id!r} not found")
    return stats
