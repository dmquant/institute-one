from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from ..institute import archive

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("/search")
async def search(q: str, limit: int = 20):
    return await archive.search(q, limit=limit)


@router.get("/files")
async def list_files(ref_kind: str | None = None, ref_id: str | None = None, limit: int = 200):
    return await archive.list_files(ref_kind=ref_kind, ref_id=ref_id, limit=limit)


@router.get("/file", response_class=PlainTextResponse)
async def read_file(path: str):
    try:
        return await archive.read_file(path)
    except ValueError:
        raise HTTPException(400, "invalid path") from None
    except FileNotFoundError:
        raise HTTPException(404, "file not found") from None
