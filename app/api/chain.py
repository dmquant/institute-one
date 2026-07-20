from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..institute import chain

router = APIRouter(prefix="/api/chain", tags=["chain"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Map domain errors onto HTTP: validation -> 400, lost claim -> 409,
    unknown id -> 404."""
    try:
        return await fn(*args, **kwargs)
    except chain.PromoteConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except chain.ChainError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    security_id: str | None = None


class AliasBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str


class EdgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src_id: str
    dst_id: str
    relation: str            # open set; suggested: chain.RELATION_VOCABULARY
    confidence: float | None = None
    evidence_ref: str | None = None


class ReprojectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None = None  # one of chain.REPROJECT_KINDS; None = all source kinds
    cap: int = 50            # max notes rewritten per call (clamped to 1..500)


@router.get("/nodes")
async def list_nodes(q: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0):
    return await _call(chain.list_nodes, q=q, kind=kind, limit=limit, offset=offset)


@router.get("/nodes/{node_id}")
async def get_node(node_id: str):
    node = await chain.node_detail(node_id)
    if node is None:
        raise HTTPException(404, "chain node not found")
    return node


@router.post("/nodes/{node_id}/aliases")
async def add_alias(node_id: str, body: AliasBody):
    return await _call(chain.merge_aliases, node_id, body.alias)


@router.get("/candidates")
async def list_candidates(status: str = "pending", limit: int = 100, offset: int = 0):
    return await _call(chain.list_candidates, status=status, limit=limit, offset=offset)


@router.post("/candidates/{candidate_id}/promote")
async def promote_candidate(candidate_id: str, body: PromoteBody):
    return await _call(chain.promote_candidate, candidate_id, body.kind, body.security_id)


@router.post("/edges")
async def add_edge(body: EdgeBody):
    return await _call(
        chain.add_edge, body.src_id, body.dst_id, body.relation,
        confidence=body.confidence, evidence_ref=body.evidence_ref,
    )


@router.get("/graph")
async def graph(center: str, depth: int = 1):
    return await _call(chain.graph, center, depth)


@router.post("/reproject")
async def reproject(body: ReprojectBody | None = None):
    """Backfill ## Entities footers on already-exported source notes.
    Rewrites at most ``cap`` notes per call — repeat until reprojected == 0."""
    body = body or ReprojectBody()
    return await _call(chain.reproject_footers, kind=body.kind, cap=body.cap)
