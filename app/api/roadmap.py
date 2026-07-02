from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import get_settings
from ..institute import roadmap

router = APIRouter(prefix="/api/roadmap", tags=["roadmap"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409."""
    try:
        return await fn(*args, **kwargs)
    except roadmap.MoveConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except roadmap.RoadmapError as exc:
        raise HTTPException(400, str(exc)) from exc


class ImportBody(BaseModel):
    path: str | None = None  # defaults to roadmap/backlog.json in the repo
    force: bool = False      # apply seed status over local status


class CardPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    problem: str | None = None
    implementation: str | None = None
    agent_prompt: str | None = None
    owner: str | None = None
    phase: str | None = None
    type: str | None = None
    priority: str | None = None
    risk: str | None = None
    blocked_reason: str | None = None
    sort_order: float | None = None
    design_links: list[str] | None = None
    expected_files: list[str] | None = None
    verification: list[str] | None = None
    tags: list[str] | None = None


class MoveBody(BaseModel):
    status: str
    override: bool = False
    reason: str = ""
    owner: str | None = None
    sort_order: float | None = None
    expected_status: str | None = None  # optimistic concurrency: fail with 409 if stale


class EvidenceBody(BaseModel):
    kind: str
    title: str
    body: str = ""
    status: str = "info"
    artifact_ref: str | None = None


class SessionCreate(BaseModel):
    actor: str
    goal: str
    planned_files: list[str] = Field(default_factory=list)


class SessionPatch(BaseModel):
    status: str | None = None
    goal: str | None = None
    summary: str | None = None
    planned_files: list[str] | None = None
    touched_files: list[str] | None = None


class CommandBody(BaseModel):
    command_label: str
    command_text: str
    exit_code: int | None = None
    output_excerpt: str | None = None


# ---- cards -------------------------------------------------------------------

@router.get("/cards")
async def list_cards(
    status: str | None = None,
    phase: str | None = None,
    type_: str | None = Query(None, alias="type"),
    priority: str | None = None,
    search: str | None = None,
):
    return await roadmap.list_cards(
        status=status, phase=phase, type=type_, priority=priority, search=search
    )


@router.get("/cards/{card_id}")
async def get_card(card_id: str):
    card = await roadmap.get_card(card_id)
    if card is None:
        raise HTTPException(404, "roadmap card not found")
    return card


@router.post("/import")
async def import_backlog(body: ImportBody):
    path = body.path
    if path:  # the seed contract only covers files inside the repo — no arbitrary reads
        root = get_settings().repo_root.resolve()
        resolved = (Path(path) if Path(path).is_absolute() else root / path).resolve()
        if not resolved.is_relative_to(root):
            raise HTTPException(400, "import path must live inside the repository")
        path = str(resolved)
    return await _call(roadmap.import_backlog, path, force=body.force)


@router.patch("/cards/{card_id}")
async def update_card(card_id: str, body: CardPatch):
    card = await _call(roadmap.update_card, card_id, body.model_dump(exclude_unset=True))
    if card is None:
        raise HTTPException(404, "roadmap card not found")
    return card


@router.post("/cards/{card_id}/move")
async def move_card(card_id: str, body: MoveBody):
    card = await _call(
        roadmap.move, card_id, body.status,
        override=body.override, reason=body.reason, owner=body.owner,
        sort_order=body.sort_order, expected_status=body.expected_status,
    )
    if card is None:
        raise HTTPException(404, "roadmap card not found")
    return card


@router.post("/cards/{card_id}/evidence")
async def add_evidence(card_id: str, body: EvidenceBody):
    evidence = await _call(
        roadmap.add_evidence, card_id, body.kind, body.title,
        body=body.body, status=body.status, artifact_ref=body.artifact_ref,
    )
    if evidence is None:
        raise HTTPException(404, "roadmap card not found")
    return evidence


# ---- coding sessions -----------------------------------------------------------

@router.post("/cards/{card_id}/sessions")
async def create_session(card_id: str, body: SessionCreate):
    sess = await _call(
        roadmap.create_session, card_id, body.actor, body.goal, planned_files=body.planned_files
    )
    if sess is None:
        raise HTTPException(404, "roadmap card not found")
    return sess


@router.get("/sessions")
async def list_sessions(card_id: str | None = None, status: str | None = None, limit: int = 100):
    return await roadmap.list_sessions(card_id=card_id, status=status, limit=limit)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    sess = await roadmap.get_session(session_id)
    if sess is None:
        raise HTTPException(404, "roadmap session not found")
    return sess


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionPatch):
    sess = await _call(roadmap.update_session, session_id, body.model_dump(exclude_unset=True))
    if sess is None:
        raise HTTPException(404, "roadmap session not found")
    return sess


@router.post("/sessions/{session_id}/commands")
async def append_command(session_id: str, body: CommandBody):
    cmd = await _call(
        roadmap.append_command, session_id, body.command_label, body.command_text,
        exit_code=body.exit_code, output_excerpt=body.output_excerpt,
    )
    if cmd is None:
        raise HTTPException(404, "roadmap session not found")
    return cmd


# ---- release gates ---------------------------------------------------------------

@router.get("/release-gates")
async def release_gates():
    return await roadmap.release_gates()
