"""Stable read/configuration API for bilingual briefing and daily twins."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ..institute import bilingual

router = APIRouter(prefix="/api/bilingual", tags=["bilingual"])

Locale = Literal["zh", "en"]


class LocalePreferencePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locale: Locale


@router.get("/preference")
async def get_preference():
    return {"locale": await bilingual.get_locale_preference()}


@router.put("/preference")
async def put_preference(body: LocalePreferencePut):
    return {"locale": await bilingual.set_locale_preference(body.locale)}


@router.get("/coverage")
async def coverage():
    return await bilingual.coverage_stats()


@router.get("/failures")
async def failures(permanent_only: bool = False):
    return {
        "items": await bilingual.list_translation_failures(
            permanent_only=permanent_only,
        )
    }


async def _read(document_ref: str, locale: Locale | None):
    result = await bilingual.read_twin(document_ref, locale=locale)
    if result is None:
        raise HTTPException(404, "bilingual document not found")
    return result


# Literal route must precede /twins/{document_id}.
@router.get("/twins/by-path")
async def get_twin_by_path(
    path: str = Query(min_length=1),
    locale: Locale | None = None,
):
    return await _read(path, locale)


@router.get("/twins/{document_id}")
async def get_twin(document_id: str, locale: Locale | None = None):
    return await _read(document_id, locale)
