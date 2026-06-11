"""Vault status / ledger / doctor / manual export endpoints."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import db
from ..config import get_settings
from ..vault import exporter
from ..vault.writer import get_writer

log = logging.getLogger("institute.api.vault")

router = APIRouter(prefix="/api/vault", tags=["vault"])


@router.get("/status")
async def status():
    settings = get_settings()
    rows = await db.query("SELECT state, COUNT(*) AS n FROM vault_index GROUP BY state")
    counts = {r["state"]: r["n"] for r in rows}
    return {
        "configured": settings.vault_dir is not None,
        "vault_dir": str(settings.vault_dir) if settings.vault_dir else None,
        "counts": counts,
        "total": sum(counts.values()),
    }


@router.get("/index")
async def index(state: str | None = None, limit: int = 200):
    if state is not None and state not in ("clean", "conflict"):
        raise HTTPException(400, "state must be 'clean' or 'conflict'")
    sql = "SELECT path, artifact_kind, artifact_id, sha256, state, written_at FROM vault_index"
    params: list[Any] = []
    if state:
        sql += " WHERE state = ?"
        params.append(state)
    sql += " ORDER BY written_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 1000))
    return await db.query(sql, params)


@router.post("/doctor")
async def doctor():
    report = await get_writer().doctor()
    if report is None:
        raise HTTPException(400, "vault_dir not configured")
    return report


@router.post("/export/research/{queue_id}")
async def export_research(queue_id: str):
    """Manually re-export a completed research queue item into the vault."""
    if not get_writer().enabled:
        raise HTTPException(400, "vault_dir not configured")
    try:
        rel = await exporter.export_research_queue_item(queue_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if rel is None:
        raise HTTPException(422, "nothing to export (no report or step summaries found)")
    return {"exported": rel}
