from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..institute import theses

router = APIRouter(prefix="/api/theses", tags=["theses"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409."""
    try:
        return await fn(*args, **kwargs)
    except theses.TransitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except theses.ThesisError as exc:
        raise HTTPException(400, str(exc)) from exc


class ThesisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos 422, matching the domain's strictness

    # id defaults to slug (and vice versa); at least one is required
    id: str | None = None
    slug: str | None = None
    kind: str = "thesis"
    name_zh: str
    name_en: str | None = None
    status: str = "candidate"          # POST contract: candidate or active
    parent_id: str | None = None
    scope: str = ""
    exclusions: str = ""
    owner_analyst: str | None = None
    priority: float = 0
    confidence: str = "medium"
    view: str = "unknown"
    current_view: str | None = None    # column-name alias for view (domain folds them)
    conviction_score: float | None = None
    alpha_prior_score: float | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    source: str = "manual"
    source_href: str | None = None
    source_network_href: str | None = None
    metadata: dict = Field(default_factory=dict)
    # content fields seed thesis_versions version 1 when provided
    summary: str = ""
    run_id: str | None = None
    drivers: list = Field(default_factory=list)
    risks: list = Field(default_factory=list)
    kpis: list = Field(default_factory=list)
    catalysts: list = Field(default_factory=list)
    stock_map: list = Field(default_factory=list)


class ThesisPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos 422, matching the domain's strictness

    # projection fields
    slug: str | None = None
    parent_id: str | None = None
    name_zh: str | None = None
    name_en: str | None = None
    scope: str | None = None
    exclusions: str | None = None
    owner_analyst: str | None = None
    priority: float | None = None
    conviction_score: float | None = None
    alpha_prior_score: float | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    source: str | None = None
    source_href: str | None = None
    source_network_href: str | None = None
    metadata: dict | None = None
    # content fields (any of these appends a thesis_versions row)
    view: str | None = None
    current_view: str | None = None    # column-name alias for view (domain folds them)
    confidence: str | None = None
    summary: str | None = None
    run_id: str | None = None
    drivers: list | None = None
    risks: list | None = None
    kpis: list | None = None
    catalysts: list | None = None
    stock_map: list | None = None
    # lifecycle transition (conditional claim; stale expected_status -> 409)
    status: str | None = None
    expected_status: str | None = None
    reason: str = ""


@router.get("")
async def list_theses(
    flat: bool = False,
    status: str | None = None,
    kind: str | None = None,
    parent_id: str | None = None,
    search: str | None = None,
):
    """Tree (lanes→theses) by default; ``?flat=1`` returns the filterable flat list."""
    if flat or status or kind or parent_id or search:
        return await _call(
            theses.list_theses, status=status, kind=kind, parent_id=parent_id, search=search
        )
    return await theses.tree()


@router.post("")
async def create_thesis(body: ThesisCreate):
    return await _call(theses.create_thesis, body.model_dump(exclude_unset=True))


# ids are path-like ("ai/gpu"), so the param must swallow slashes
@router.get("/{thesis_id:path}")
async def get_thesis(thesis_id: str):
    thesis = await theses.get_thesis(thesis_id)
    if thesis is None:
        raise HTTPException(404, "thesis not found")
    return thesis


@router.patch("/{thesis_id:path}")
async def update_thesis(thesis_id: str, body: ThesisPatch):
    """Field updates OR a lifecycle transition — never both in one body.

    A transition is a conditional claim (all-or-nothing 409 on a lost claim);
    folding it into a field update would let the 409 land after the fields —
    and a version row — already committed, so mixed bodies are rejected."""
    fields = body.model_dump(exclude_unset=True)
    to_status = fields.pop("status", None)
    expected_status = fields.pop("expected_status", None)
    reason = fields.pop("reason", "")

    if to_status is not None:
        if fields:
            raise HTTPException(
                400, "send status changes alone: a transition cannot be combined with other fields"
            )
        thesis = await _call(
            theses.set_status, thesis_id, to_status,
            expected_status=expected_status, reason=reason,
        )
        if thesis is None:
            raise HTTPException(404, "thesis not found")
        return thesis

    if expected_status is not None or reason:
        raise HTTPException(400, "expected_status/reason only accompany a status change")
    thesis = await _call(theses.update_thesis, thesis_id, fields)
    if thesis is None:
        raise HTTPException(404, "thesis not found")
    return thesis
