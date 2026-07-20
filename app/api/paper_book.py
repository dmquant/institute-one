from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from ..institute import paper_book

router = APIRouter(prefix="/api/book", tags=["paper-book"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409."""
    try:
        return await fn(*args, **kwargs)
    except paper_book.TransitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except paper_book.PaperBookError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/positions")
async def list_positions(status: str | None = None, limit: int = 200):
    return await _call(paper_book.list_positions, status=status, limit=limit)


@router.get("/positions/{position_id}")
async def get_position(position_id: str):
    pos = await paper_book.get_position(position_id)
    if pos is None:
        raise HTTPException(404, "position not found")
    return pos


@router.post("/positions/{position_id}/close")
async def close_position(position_id: str):
    """Manual close at the latest usable mark (fails closed on no price)."""
    pos = await _call(paper_book.close_position, position_id)
    if pos is None:
        raise HTTPException(404, "position not found")
    return pos


@router.get("/nav")
async def nav(days: int = 90):
    return await _call(paper_book.nav_series, days=days)
