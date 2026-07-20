from __future__ import annotations

from typing import Any, Awaitable, Callable

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
    made_at: str | None = None         # defaults to now — pass explicitly to backfill


class SettleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: str | None = None           # PIT replay: settle with only the data known then
    note: str = ""


@router.get("")
async def list_forecasts(status: str | None = None, thesis_id: str | None = None, limit: int = 100):
    return await _call(forecasts.list_forecasts, status=status, thesis_id=thesis_id, limit=limit)


@router.post("")
async def create_forecast(body: ForecastCreate):
    return await _call(forecasts.create_forecast, body.model_dump(exclude_unset=True))


@router.get("/{forecast_id}")
async def get_forecast(forecast_id: str):
    fc = await forecasts.get_forecast(forecast_id)
    if fc is None:
        raise HTTPException(404, "forecast not found")
    return fc


@router.post("/{forecast_id}/settle")
async def settle_forecast(forecast_id: str, body: SettleBody | None = None):
    body = body or SettleBody()
    fc = await _call(forecasts.settle_forecast, forecast_id, as_of=body.as_of, note=body.note)
    if fc is None:
        raise HTTPException(404, "forecast not found")
    return fc
