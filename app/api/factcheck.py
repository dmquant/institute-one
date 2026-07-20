from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..institute import factcheck

# No prefix: the writing-time check lives under /api/meta/* per ROADMAP
# Phase 3 ("claim-check-before-write"), not under a /api/factcheck tree.
# Mounted in app/main.py (one-line include; see PATCH-NOTES-C1.md).
router = APIRouter(tags=["factcheck"])

CARD_STATUSES = ("pending", "verified", "disputed", "unverifiable", "reused", "self_contradicted")


class ClaimCheckBody(BaseModel):
    # max_length mirrors the domain cap so oversized drafts fail loudly at the
    # boundary instead of being silently truncated (MCP callers still get the
    # domain-side truncation).
    text: str = Field(max_length=factcheck.CLAIM_CHECK_TEXT_CAP)
    k: int = Field(default=factcheck.CLAIM_CHECK_MAX_HITS, ge=1, le=20)


@router.post("/api/meta/claim_check_before_write")
async def claim_check_before_write(body: ClaimCheckBody):
    """Check a draft against verified/disputed facts while writing.

    Returns ``{"mode": ..., "hits": [{fact_card_id, claim, category, verdict,
    similarity, source}]}``. Vector near-neighbors over the stored claim
    embeddings when the vector layer is live; degraded == keyword-overlap
    fallback (mode="keyword"). Empty text returns no hits, never an error.
    """
    return await factcheck.claim_check(body.text, k=body.k)


@router.get("/api/factcheck/cards")
async def list_cards(
    status: str | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Fact cards, newest first, optionally filtered by status/category."""
    if status is not None and status not in CARD_STATUSES:
        raise HTTPException(422, f"unknown status {status!r}")
    if category is not None and category not in factcheck.CATEGORIES:
        raise HTTPException(422, f"unknown category {category!r}")
    return await factcheck.list_cards(status=status, category=category, limit=limit)


@router.get("/api/factcheck/cards/{card_id}")
async def get_card(card_id: str):
    """One card + its verdict row (or the verdict a reused card points at)."""
    card = await factcheck.get_card(card_id)
    if card is None:
        raise HTTPException(404, f"unknown fact card {card_id!r}")
    return card


@router.get("/api/factcheck/outbox")
async def get_outbox(
    limit: int = Query(default=50, ge=1, le=200),
):
    """Dispute-delivery backlog counts and newest rows."""
    return await factcheck.outbox_overview(limit=limit)
