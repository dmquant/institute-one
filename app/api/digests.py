"""Curl-back digest endpoints (ROADMAP Phase 2, proposal §6.1).

Every route returns **plain markdown** (``text/markdown; charset=utf-8``) —
no JSON envelope — because the consumer is a CLI hand running a Step-0
``curl 127.0.0.1:8100/api/institute/....md`` block inside its prompt. That
also makes the context auditable: run the same curl by hand to see exactly
what the model saw. Bodies are clamped to 8KB with an explicit truncation
marker (institute/digests.py owns the rendering rules).

Robustness stance: these endpoints always answer 200 with markdown — unknown
analyst ids, empty tables, or tables owned by later phases all degrade to a
stable placeholder document rather than a 4xx/5xx, so a failing curl never
poisons a prompt with an error page.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ..institute import digests

router = APIRouter(prefix="/api/institute", tags=["digests"])

_MD = "text/markdown; charset=utf-8"


def _md(text: str) -> PlainTextResponse:
    return PlainTextResponse(text, media_type=_MD)


@router.get("/recent-reports.md")
async def recent_reports(days: int = 7):
    """Titles + one-line summaries of recent briefings/dailies/research (newest first)."""
    return _md(await digests.recent_reports_md(days))


@router.get("/analyst-memory/{analyst_id}.md")
async def analyst_memory(analyst_id: str):
    """Latest standing-memory compact for the analyst; '# no memory yet' before the first compact."""
    return _md(await digests.analyst_memory_md(analyst_id))


@router.get("/analyst-disputes/{analyst_id}.md")
async def analyst_disputes(analyst_id: str):
    """Disputed claims for the analyst — stable placeholder until fact-check v2 (Phase 3)."""
    return _md(await digests.analyst_disputes_md(analyst_id))


@router.get("/operator-actions-digest.md")
async def operator_actions_digest():
    """Operator-actions digest — stable placeholder until the operator console (Phase 6)."""
    return _md(await digests.operator_actions_md())
