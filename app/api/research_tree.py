"""BFS research tree API (ROADMAP Phase 7 Explore mode).

Same /api/research prefix as the research-queue router (FastAPI merges
routers; the /tree* paths are disjoint from /queue*). The SSE viewer contract
(events + polling shape for the /research/tree/:id SPA page) is documented in
PATCH-NOTES-D4.md — the frontend itself is another partition.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..institute import research_tree

router = APIRouter(prefix="/api/research", tags=["research-tree"])


class CreateTreeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos 422 (the SeedBody precedent)

    root_topic: str
    # bounds mirror the domain's hard limits (create_tree re-validates)
    max_depth: int = Field(default=2, ge=0, le=research_tree.MAX_DEPTH_LIMIT)
    max_nodes: int = Field(default=12, ge=1, le=research_tree.MAX_NODES_LIMIT)


@router.post("/tree")
async def create_tree(body: CreateTreeBody):
    """Create one explore tree (root node pending; the gated 5-min tick
    drains it). A spent daily budget returns {"refused": "daily_cap", ...}
    with 200 — the research-queue cooldown-refusal shape."""
    try:
        return await research_tree.create_tree(
            body.root_topic, max_depth=body.max_depth, max_nodes=body.max_nodes,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/trees")
async def list_trees(status: str | None = None, limit: int = 50):
    return await research_tree.list_trees(status=status, limit=limit)


@router.get("/tree/{tree_id}")
async def get_tree(tree_id: str):
    """Tree JSON: the tree row plus a FLAT `nodes` list (BFS order) carrying
    `parent_id` references — the viewer rebuilds nesting client-side."""
    tree = await research_tree.get_tree(tree_id)
    if tree is None:
        raise HTTPException(404, "research tree not found")
    return tree


@router.post("/tree/{tree_id}/node/{node_id}/retry")
async def retry_node(tree_id: str, node_id: str):
    """Requeue one failed node. Duplicate/non-failed retries and retries on a
    stopped tree are conflicts; a node id from another tree is not found."""
    try:
        return await research_tree.retry_node(tree_id, node_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except research_tree.TransitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tree/{tree_id}/stop")
async def stop_tree(tree_id: str):
    """Prune pending nodes and terminal the tree to 'stopped'; running nodes
    finish naturally without spawning children. Idempotent on terminal trees."""
    tree = await research_tree.stop_tree(tree_id)
    if tree is None:
        raise HTTPException(404, "research tree not found")
    return tree
