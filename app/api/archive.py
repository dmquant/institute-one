from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..institute import archive, vectors

# No prefix: this router carries both /api/archive/* and the Phase 1a
# semantic entry point POST /api/search (proposal §9) — main.py mounts one
# router per module, and /api/search must live outside the /api/archive tree.
router = APIRouter(tags=["archive"])


@router.get("/api/archive/search")
async def search(q: str, limit: int = 20):
    """Hybrid search with a backward-compatible top-level health reason.

    Degrades to pure FTS5 rows (mode="fts") whenever the vector layer is
    unavailable (no Ollama, no sqlite-vec, vectors disabled).
    """
    result = await archive.search_hybrid(q, limit=limit)
    return {**result, "reason": vectors.last_search_reason()}


class SearchBody(BaseModel):
    query: str
    k: int = 10


@router.post("/api/search")
async def semantic_search(body: SearchBody):
    """Semantic search entry point (proposal §9). Same degradation contract."""
    result = await archive.search_hybrid(body.query, limit=body.k)
    return {**result, "reason": vectors.last_search_reason()}


@router.get("/api/vectors/health")
async def vector_health():
    return await vectors.get_health()


class VectorGCBody(BaseModel):
    keep_model: str


@router.post("/api/vectors/gc")
async def vector_gc(body: VectorGCBody):
    keep_model = body.keep_model.strip()
    if not keep_model:
        raise HTTPException(400, "keep_model must not be empty")
    return await vectors.gc_stale_models(keep_model)


@router.get("/api/archive/files")
async def list_files(ref_kind: str | None = None, ref_id: str | None = None, limit: int = 200):
    return await archive.list_files(ref_kind=ref_kind, ref_id=ref_id, limit=limit)


@router.get("/api/archive/file", response_class=PlainTextResponse)
async def read_file(path: str):
    try:
        return await archive.read_file(path)
    except ValueError:
        raise HTTPException(400, "invalid path") from None
    except FileNotFoundError:
        raise HTTPException(404, "file not found") from None
