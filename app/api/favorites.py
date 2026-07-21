"""Operator favorites API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..institute import favorites


router = APIRouter(prefix="/api/favorites", tags=["favorites"])


class FavoriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_kind: str = Field(min_length=1, max_length=64)
    ref_id: str = Field(min_length=1, max_length=favorites.MAX_REF_ID_LEN)
    note: str = Field(default="", max_length=favorites.MAX_NOTE_LEN)


@router.post("")
async def add_favorite(body: FavoriteBody):
    try:
        return await favorites.add(body.ref_kind, body.ref_id, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{ref_kind}/{ref_id:path}")
async def remove_favorite(ref_kind: str, ref_id: str):
    try:
        removed = await favorites.remove(ref_kind, ref_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"removed": removed}


@router.get("")
async def list_favorites(kind: str | None = None):
    try:
        return await favorites.list_favorites(kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
