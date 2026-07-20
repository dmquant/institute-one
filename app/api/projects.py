"""Research projects API (ROADMAP Phase 7).

Four routes per the card: create/list projects, attach links, read one
project (attachments expanded), and the markdown digest. Archive/unlink stay
domain-level for now (MCP / SPA follow-ups — PATCH-NOTES-D5.md). The digest
route answers ``text/markdown`` like the /api/institute/*.md family, but 404s
on an unknown id — a project digest is an addressed resource, not a Step-0
curl placeholder.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..institute import projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

_MD = "text/markdown; charset=utf-8"


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=projects.MAX_NAME_LEN)
    description: str = Field(default="", max_length=projects.MAX_DESCRIPTION_LEN)


class LinkBody(BaseModel):
    kind: str
    ref_id: str = Field(min_length=1)


@router.post("")
async def create_project(body: CreateBody):
    try:
        return await projects.create(body.name, body.description)
    except ValueError as exc:  # empty/duplicate name
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def list_projects(status: str | None = None, limit: int = 100):
    try:
        return await projects.list_projects(status=status, limit=limit)
    except ValueError as exc:  # unknown status filter
        raise HTTPException(400, str(exc)) from exc


@router.get("/{project_id}")
async def get_project(project_id: str):
    project = await projects.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


@router.post("/{project_id}/links")
async def add_link(project_id: str, body: LinkBody):
    try:
        return await projects.link(project_id, body.kind, body.ref_id)
    except ValueError as exc:  # unknown kind/ref, unknown or archived project
        raise HTTPException(400, str(exc)) from exc


@router.get("/{project_id}/digest.md")
async def project_digest(project_id: str):
    text = await projects.digest_md(project_id)
    if text is None:
        raise HTTPException(404, "project not found")
    return PlainTextResponse(text, media_type=_MD)
