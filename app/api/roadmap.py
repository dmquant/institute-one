from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, Header, HTTPException, Query
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
    dry_run: bool = False    # return the reconciliation plan; zero writes/events
    new_card_status_policy: Literal["seed", "inbox"] = "seed"


class CardCreate(BaseModel):
    id: str | None = None    # server-generated when omitted
    title: str
    type: str = "feature"
    phase: str = ""
    status: str = "inbox"    # inbox|ready only — further stages go through /move
    priority: str = "P2"
    risk: str = "medium"
    owner: str | None = None
    summary: str = ""
    problem: str = ""
    implementation: str = ""
    agent_prompt: str = ""
    design_links: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    sort_order: float = 0


class ClaimBody(BaseModel):
    owner: str


class ChecklistCreate(BaseModel):
    kind: str = "acceptance"
    text: str
    sort_order: float | None = None


class ChecklistPatch(BaseModel):
    checked: bool | None = None
    text: str | None = None
    sort_order: float | None = None


class DependencyBody(BaseModel):
    depends_on_id: str
    relation: str = "blocks"


class DecisionCreate(BaseModel):
    title: str
    question: str
    card_id: str | None = None
    options: list[str] = Field(default_factory=list)


class DecisionPatch(BaseModel):
    status: str | None = None    # only 'resolved' is accepted
    decision: str | None = None
    title: str | None = None
    question: str | None = None
    options: list[str] | None = None


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
    as_evidence: bool = False  # also attach to the card as command evidence (M7-005)


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


@router.get("/cards/{card_id}/prompt")
async def card_prompt(card_id: str):
    """Deterministic agent prompt from the card's current state (M7-007)."""
    prompt = await roadmap.generate_agent_prompt(card_id)
    if prompt is None:
        raise HTTPException(404, "roadmap card not found")
    return {"prompt": prompt}


@router.post("/cards")
async def create_card(
    body: CardCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    return await _call(
        roadmap.create_card,
        body.model_dump(exclude_unset=True),
        idempotency_key=idempotency_key,
    )


@router.post("/cards/{card_id}/claim")
async def claim_card(card_id: str, body: ClaimBody):
    card = await _call(roadmap.claim_card, card_id, body.owner)
    if card is None:
        raise HTTPException(404, "roadmap card not found")
    return card


@router.get("/export")
async def export_backlog():
    return await roadmap.export_backlog()


@router.post("/import")
async def import_backlog(body: ImportBody):
    path = body.path
    if path:  # the seed contract only covers files inside the repo — no arbitrary reads
        root = get_settings().repo_root.resolve()
        resolved = (Path(path) if Path(path).is_absolute() else root / path).resolve()
        if not resolved.is_relative_to(root):
            raise HTTPException(400, "import path must live inside the repository")
        path = str(resolved)
    return await _call(
        roadmap.import_backlog,
        path,
        force=body.force,
        dry_run=body.dry_run,
        new_card_status_policy=body.new_card_status_policy,
    )


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
async def add_evidence(
    card_id: str,
    body: EvidenceBody,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    evidence = await _call(
        roadmap.add_evidence, card_id, body.kind, body.title,
        body=body.body, status=body.status, artifact_ref=body.artifact_ref,
        idempotency_key=idempotency_key,
    )
    if evidence is None:
        raise HTTPException(404, "roadmap card not found")
    return evidence


# ---- checklists ------------------------------------------------------------------

@router.post("/cards/{card_id}/checklists")
async def add_checklist_item(
    card_id: str,
    body: ChecklistCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    item = await _call(
        roadmap.add_checklist_item, card_id, body.kind, body.text,
        sort_order=body.sort_order, idempotency_key=idempotency_key,
    )
    if item is None:
        raise HTTPException(404, "roadmap card not found")
    return item


@router.patch("/checklists/{item_id}")
async def update_checklist_item(item_id: str, body: ChecklistPatch):
    item = await _call(roadmap.update_checklist_item, item_id, body.model_dump(exclude_unset=True))
    if item is None:
        raise HTTPException(404, "checklist item not found")
    return item


@router.delete("/checklists/{item_id}")
async def delete_checklist_item(item_id: str):
    ok = await roadmap.delete_checklist_item(item_id)
    if not ok:
        raise HTTPException(404, "checklist item not found")
    return {"deleted": item_id}


# ---- dependencies ------------------------------------------------------------------

@router.post("/cards/{card_id}/dependencies")
async def add_dependency(
    card_id: str,
    body: DependencyBody,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    dep = await _call(
        roadmap.add_dependency, card_id, body.depends_on_id,
        relation=body.relation, idempotency_key=idempotency_key,
    )
    if dep is None:
        raise HTTPException(404, "roadmap card not found")
    return dep


@router.delete("/dependencies/{dep_id}")
async def remove_dependency(dep_id: str):
    ok = await roadmap.remove_dependency(dep_id)
    if not ok:
        raise HTTPException(404, "dependency not found")
    return {"deleted": dep_id}


# ---- decisions ------------------------------------------------------------------

@router.post("/decisions")
async def open_decision(
    body: DecisionCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    return await _call(
        roadmap.open_decision, body.title, body.question,
        card_id=body.card_id, options=body.options, idempotency_key=idempotency_key,
    )


@router.get("/decisions")
async def list_decisions(card_id: str | None = None, status: str | None = None, limit: int = 100):
    return await _call(roadmap.list_decisions, card_id=card_id, status=status, limit=limit)


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str):
    dec = await roadmap.get_decision(decision_id)
    if dec is None:
        raise HTTPException(404, "roadmap decision not found")
    return dec


@router.patch("/decisions/{decision_id}")
async def update_decision(decision_id: str, body: DecisionPatch):
    dec = await _call(roadmap.update_decision, decision_id, body.model_dump(exclude_unset=True))
    if dec is None:
        raise HTTPException(404, "roadmap decision not found")
    return dec


# ---- coding sessions -----------------------------------------------------------

@router.post("/cards/{card_id}/sessions")
async def create_session(
    card_id: str,
    body: SessionCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    sess = await _call(
        roadmap.create_session, card_id, body.actor, body.goal,
        planned_files=body.planned_files, idempotency_key=idempotency_key,
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
async def append_command(
    session_id: str,
    body: CommandBody,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    cmd = await _call(
        roadmap.append_command, session_id, body.command_label, body.command_text,
        exit_code=body.exit_code, output_excerpt=body.output_excerpt,
        as_evidence=body.as_evidence, idempotency_key=idempotency_key,
    )
    if cmd is None:
        raise HTTPException(404, "roadmap session not found")
    return cmd


# ---- release gates ---------------------------------------------------------------

@router.get("/release-gates")
async def release_gates():
    return await roadmap.release_gates()


# ---- global process view (M7-006) --------------------------------------------------

@router.get("/process")
async def process_overview():
    """Sessions + decisions + release-gate readiness + blocked cards in one payload."""
    return await roadmap.process_overview()
