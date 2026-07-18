from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..institute import market_thesis_import, securities, theses

router = APIRouter(prefix="/api/theses", tags=["theses"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    try:
        return await fn(*args, **kwargs)
    except (theses.ThesisError, securities.SecurityError, market_thesis_import.BundleError) as exc:
        raise HTTPException(400, str(exc)) from exc


class ThesisCreate(BaseModel):
    title: str
    slug: str | None = None
    kind: str = "thesis"
    parent_id: str | None = None
    view: str = ""
    direction: str = "neutral"
    status: str = "candidate"
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class ThesisPatch(BaseModel):
    title: str | None = None
    view: str | None = None
    direction: str | None = None
    status: str | None = None
    parent_id: str | None = None
    tags: list[str] | None = None
    meta: dict[str, Any] | None = None


class EdgeBody(BaseModel):
    security_id: str
    role: str = "exposure"
    exposure: str = ""
    confidence: float | None = None
    rationale: str = ""


class ImportBody(BaseModel):
    path: str | None = None  # defaults to market-thesis-data/ in the repo
    apply: bool = False      # dry-run unless set


# static routes are declared before /{thesis_id} so they never match as an id

@router.post("/import-market-data")
async def import_market_data(body: ImportBody):
    path = body.path
    if path:  # bundle contract only covers data inside the repository
        root = get_settings().repo_root.resolve()
        resolved = (Path(path) if Path(path).is_absolute() else root / path).resolve()
        if not resolved.is_relative_to(root):
            raise HTTPException(400, "bundle path must live inside the repository")
        path = str(resolved)
    return await _call(market_thesis_import.import_bundle, path, apply=body.apply)


@router.get("/import-batches")
async def import_batches(limit: int = 20):
    return await market_thesis_import.list_batches(limit=limit)


@router.get("")
async def thesis_tree(flat: bool = False, status: str | None = None, kind: str | None = None):
    if flat or status or kind:
        return await theses.list_theses(status=status, kind=kind)
    return await theses.tree()


@router.get("/{thesis_id}")
async def get_thesis(thesis_id: str):
    thesis = await theses.get_thesis(thesis_id)
    if thesis is None:
        raise HTTPException(404, "thesis not found")
    return thesis


@router.post("")
async def create_thesis(body: ThesisCreate):
    return await _call(theses.create_thesis, body.model_dump())


@router.patch("/{thesis_id}")
async def update_thesis(thesis_id: str, body: ThesisPatch):
    thesis = await _call(theses.update_thesis, thesis_id, body.model_dump(exclude_unset=True))
    if thesis is None:
        raise HTTPException(404, "thesis not found")
    return thesis


@router.post("/{thesis_id}/securities")
async def link_security(thesis_id: str, body: EdgeBody):
    thesis = await theses.get_thesis(thesis_id)
    if thesis is None:
        raise HTTPException(404, "thesis not found")
    return await _call(
        securities.upsert_edge, thesis["id"], body.security_id,
        role=body.role, exposure=body.exposure,
        confidence=body.confidence, rationale=body.rationale,
    )


@router.delete("/{thesis_id}/securities/{security_id}")
async def unlink_security(thesis_id: str, security_id: str, role: str | None = None):
    thesis = await theses.get_thesis(thesis_id)
    if thesis is None:
        raise HTTPException(404, "thesis not found")
    removed = await securities.remove_edge(thesis["id"], security_id, role)
    return {"removed": removed}
