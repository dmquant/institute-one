from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..institute import forecasts

router = APIRouter(prefix="/api/forecasts", tags=["forecasts"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409."""
    try:
        return await fn(*args, **kwargs)
    except forecasts.TransitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except forecasts.ForecastError as exc:
        raise HTTPException(400, str(exc)) from exc


class ForecastCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos 422, matching the domain's strictness

    thesis_id: str
    security_id: str | None = None
    claim: str
    direction: str
    conviction: float | None = None
    horizon_days: int
    # {"type": "absolute_move"|"price_vs_benchmark", "threshold": >0, "benchmark_id"?}
    settlement_rule: dict | str
    made_at: str | None = None         # defaults to now; beyond now±24h needs backfill=true
    # privileged historical import: persists origin='backfill' — the row is an
    # accountability record excluded from the default list scope (hit-rate
    # consumers) and from the paper book
    backfill: bool = False


class SettleBody(BaseModel):
    """State-changing settlement: the knowledge cutoff is FIXED BY THE SYSTEM
    (no caller-supplied as_of — historical replays are the read-only
    GET /{id}/settlement-preview)."""

    model_config = ConfigDict(extra="forbid")

    note: str = ""


class ExportVaultBody(BaseModel):
    """Explicitly select the one rolling export supported by this endpoint."""

    model_config = ConfigDict(extra="forbid")
    scope: Literal["history"]


@router.get("")
async def list_forecasts(
    status: str | None = None,
    thesis_id: str | None = None,
    limit: int = 100,
    origin: str | None = None,
):
    """Default scope excludes origin='backfill' (performance view — the SPA/
    plugin hit-rate aggregations read this); origin='all' lists everything."""
    return await _call(
        forecasts.list_forecasts, status=status, thesis_id=thesis_id, limit=limit,
        origin=origin,
    )


@router.post("")
async def create_forecast(body: ForecastCreate):
    return await _call(forecasts.create_forecast_public, body.model_dump(exclude_unset=True))


@router.post("/export-vault")
async def export_vault_history(body: ExportVaultBody):
    """Manually refresh the managed rolling forecast-history note."""
    return await _call(forecasts.export_vault_history)


@router.get("/stats")
async def forecast_stats():
    """Settled hit/miss/partial counts over the performance scope — registered
    BEFORE /{forecast_id} so 'stats' is never captured as an id."""
    return await forecasts.hit_rate_stats()


@router.get("/{forecast_id}")
async def get_forecast(forecast_id: str):
    fc = await forecasts.get_forecast(forecast_id)
    if fc is None:
        raise HTTPException(404, "forecast not found")
    return fc


@router.post("/{forecast_id}/settle")
async def settle_forecast(forecast_id: str, body: SettleBody | None = None):
    body = body or SettleBody()
    fc = await _call(forecasts.settle_forecast, forecast_id, note=body.note)
    if fc is None:
        raise HTTPException(404, "forecast not found")
    return fc


@router.get("/{forecast_id}/settlement-preview")
async def settlement_preview(forecast_id: str, as_of: str | None = None):
    """Read-only settlement replay: what would settlement say with the
    knowledge available at ``as_of`` (omit for now). Never writes, never
    changes status — pass a settlement row's recorded knowledge_as_of to
    re-derive its verdict from the immutable PIT version store."""
    out = await _call(forecasts.preview_settlement, forecast_id, as_of=as_of)
    if out is None:
        raise HTTPException(404, "forecast not found")
    return out
