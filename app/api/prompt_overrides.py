"""Prompt-overrides operations API (ROADMAP Phase 2).

CRUD over ``prompt_overrides`` rows plus the lifecycle transitions
(shadow → active → retired) and a diff preview against the code default.
Domain rules live in ``app/institute/prompt_overrides.py``; this router only
maps them onto HTTP: ValueError → 400, LookupError → 404,
OverrideConflict (lost conditional claim / immutable history) → 409.

The router is mounted by ``app.main.create_app`` and lifespan pre-warms the
active-override cache before any boot recovery can assemble model work.
"""
from __future__ import annotations

import difflib

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from ..institute import prompt_overrides as po

router = APIRouter(prefix="/api/prompt-overrides", tags=["prompt-overrides"])


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=po.MAX_CONTENT_LEN)
    note: str = Field(default="", max_length=po.MAX_NOTE_LEN)


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=po.MAX_CONTENT_LEN)
    note: str | None = Field(default=None, max_length=po.MAX_NOTE_LEN)


# ---- collection ------------------------------------------------------------
# Static paths ("/scopes") registered before the /{override_id} routes on
# principle (the id routes are int-typed, so there is no actual collision).

@router.get("")
async def list_overrides(
    scope: str | None = None, status: str | None = None, limit: int = 100,
):
    try:
        rows = await po.list_overrides(scope=scope, status=status, limit=limit)
    except ValueError as exc:  # unknown status filter
        raise HTTPException(400, str(exc)) from exc
    # opportunistic lazy-load: any read re-syncs the resolve cache, so a
    # missed boot pre-warm heals on first inspection (the hand-weights idiom)
    await po.refresh_cache()
    return rows


@router.get("/scopes")
async def list_scopes():
    """Every registered mount point: description, fields, code default, live state."""
    return await po.scopes_overview()


@router.post("", status_code=201)
async def create_override(body: CreateBody):
    """Record a new shadow draft — never affects prompts until activated."""
    try:
        return await po.create(body.scope, body.content, body.note)
    except ValueError as exc:  # unknown scope / bad placeholders / caps
        raise HTTPException(400, str(exc)) from exc


# ---- item --------------------------------------------------------------------

@router.get("/{override_id}")
async def get_override(override_id: int):
    row = await po.get(override_id)
    if row is None:
        raise HTTPException(404, "prompt override not found")
    return row


@router.put("/{override_id}")
async def update_override(override_id: int, body: UpdateBody):
    """Edit a shadow draft (content/note). Active/retired rows are immutable."""
    try:
        return await po.update_draft(override_id, content=body.content, note=body.note)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except po.OverrideConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{override_id}", status_code=204)
async def delete_override(override_id: int):
    """Discard a shadow draft. History (active/retired) cannot be deleted."""
    try:
        await po.delete_draft(override_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except po.OverrideConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@router.get("/{override_id}/diff")
async def diff_override(override_id: int):
    """Preview: unified diff of the override content vs the code default."""
    row = await po.get(override_id)
    if row is None:
        raise HTTPException(404, "prompt override not found")
    try:
        default = po.default_text(row["scope"])
    except ValueError as exc:  # scope no longer registered — nothing to diff against
        raise HTTPException(400, str(exc)) from exc
    diff = "\n".join(difflib.unified_diff(
        default.splitlines(), str(row["content"]).splitlines(),
        fromfile=f"default:{row['scope']}", tofile=f"override:{override_id}",
        lineterm="",
    ))
    return {
        "id": row["id"], "scope": row["scope"], "status": row["status"],
        "default": default, "content": row["content"],
        "changed": row["content"] != default, "diff": diff,
    }


# ---- lifecycle -----------------------------------------------------------------

@router.post("/{override_id}/activate")
async def activate_override(override_id: int):
    """shadow → active; atomically retires the scope's previous active."""
    try:
        return await po.activate(override_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except po.OverrideConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:  # registry tightened since the draft was recorded
        raise HTTPException(400, str(exc)) from exc


@router.post("/{override_id}/retire")
async def retire_override(override_id: int):
    """active → retired; the scope falls back to its code default."""
    try:
        return await po.retire(override_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except po.OverrideConflict as exc:
        raise HTTPException(409, str(exc)) from exc
