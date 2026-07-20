from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..institute import archive

# No prefix: this router carries both /api/archive/* and the Phase 1a
# semantic entry point POST /api/search (proposal §9) — main.py mounts one
# router per module, and /api/search must live outside the /api/archive tree.
router = APIRouter(tags=["archive"])


@router.get("/api/archive/search")
async def search(q: str, limit: int = 20):
    """Hybrid search: {"mode": "vector+fts"|"fts", "results": [...]}.

    Degrades to pure FTS5 rows (mode="fts") whenever the vector layer is
    unavailable (no Ollama, no sqlite-vec, vectors disabled).
    """
    return await archive.search_hybrid(q, limit=limit)


class SearchBody(BaseModel):
    query: str
    k: int = 10


@router.post("/api/search")
async def semantic_search(body: SearchBody):
    """Semantic search entry point (proposal §9). Same degradation contract."""
    return await archive.search_hybrid(body.query, limit=body.k)


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
