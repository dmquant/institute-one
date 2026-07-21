from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..institute import portfolios

# NB: the /proposals routes are declared BEFORE /{portfolio_id} — FastAPI
# matches in registration order, so the literal path must come first.
router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409."""
    try:
        return await fn(*args, **kwargs)
    except portfolios.TransitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except portfolios.PortfolioError as exc:
        raise HTTPException(400, str(exc)) from exc


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    note: str = ""


@router.get("")
async def list_portfolios(analyst_id: str | None = None):
    return await portfolios.list_portfolios(analyst_id=analyst_id)


@router.get("/proposals")
async def list_proposals(
    status: str | None = None,
    portfolio_id: str | None = None,
    analyst_id: str | None = None,
    limit: int = 100,
):
    return await _call(
        portfolios.list_proposals,
        status=status, portfolio_id=portfolio_id, analyst_id=analyst_id, limit=limit,
    )


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str):
    prop = await portfolios.get_proposal(proposal_id)
    if prop is None:
        raise HTTPException(404, "proposal not found")
    return prop


@router.post("/proposals/{proposal_id}/decide")
async def decide_proposal(proposal_id: str, body: DecideBody):
    """Adjudicate a pending proposal; a lost conditional claim is HTTP 409."""
    prop = await _call(
        portfolios.decide_proposal, proposal_id, body.decision, note=body.note)
    if prop is None:
        raise HTTPException(404, "proposal not found")
    return prop


@router.get("/{portfolio_id}")
async def get_portfolio(portfolio_id: str):
    pf = await portfolios.get_portfolio(portfolio_id)
    if pf is None:
        raise HTTPException(404, "portfolio not found")
    return pf


@router.get("/{portfolio_id}/valuation")
async def get_valuation(portfolio_id: str):
    """On-demand PIT valuation (unpriceable positions excluded + flagged)."""
    snap = await portfolios.valuation(portfolio_id)
    if snap is None:
        raise HTTPException(404, "portfolio not found")
    return snap
