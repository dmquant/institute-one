"""Research projects API (ROADMAP Phase 7).

Create/list/read projects, manage their lifecycle and links, and expose both
structured and markdown digests. Addressed resources 404 when the project is
unknown; deleting a link is idempotent and always returns 204.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
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


@router.post("/{project_id}/archive")
async def archive_project(project_id: str):
    project = await projects.archive(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


@router.post("/{project_id}/unarchive")
async def unarchive_project(project_id: str):
    project = await projects.unarchive(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


@router.post("/{project_id}/links")
async def add_link(project_id: str, body: LinkBody):
    try:
        return await projects.link(project_id, body.kind, body.ref_id)
    except ValueError as exc:  # unknown kind/ref, unknown or archived project
        status_code = 409 if str(exc).endswith(" is archived") else 400
        raise HTTPException(status_code, str(exc)) from exc


@router.delete("/{project_id}/links/{kind}/{ref_id}", status_code=204)
async def remove_link(project_id: str, kind: str, ref_id: str):
    try:
        await projects.unlink(project_id, kind, ref_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(status_code=204)


@router.get("/{project_id}/digest")
async def project_digest(project_id: str, limit: int = 10):
    summary = await projects.digest(project_id, limit=limit)
    if summary is None:
        raise HTTPException(404, "project not found")
    return summary


@router.get("/{project_id}/digest.md")
async def project_digest_md(project_id: str):
    text = await projects.digest_md(project_id)
    if text is None:
        raise HTTPException(404, "project not found")
    return PlainTextResponse(text, media_type=_MD)
