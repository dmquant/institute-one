from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .. import db
from ..institute import evidence
from ..institute import marketdata

router = APIRouter(prefix="/api", tags=["data"])


@router.get("/quote/{ticker}")
async def quote(ticker: str, refresh: bool = False):
    try:
        return await marketdata.get_quote(ticker, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/data/{topic}/latest")
async def latest(topic: str, refresh: bool = False):
    if refresh:
        try:
            return await marketdata.get_bundle(topic, refresh=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return await marketdata.latest(topic)


@router.get("/data/{topic}/bundle")
async def bundle(topic: str, refresh: bool = False):
    try:
        return await marketdata.get_bundle(topic, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/evidence")
async def evidence_search(query: str = "", limit: int = 20):
    if query.strip():
        return await evidence.evidence_for_topic(query, limit=limit)
    return await db.query(
        """
        SELECT
          s.canonical_url, s.url, s.host, s.title, s.last_seen_at, s.source_count,
          l.topic, l.artifact_kind, l.artifact_id, l.artifact_path, l.analyst_id,
          l.work_date, l.claim_text, l.context_text
        FROM claim_evidence_links l
        JOIN evidence_sources s ON s.id = l.source_id
        ORDER BY l.created_at DESC
        LIMIT ?
        """,
        (min(max(limit, 1), 100),),
    )


@router.get("/claims")
async def claims(
    query: str = "",
    verdict: str = "",
    artifact_kind: str = "",
    limit: int = 50,
):
    where = []
    params: list[object] = []
    if query.strip():
        like = f"%{query.strip()}%"
        where.append("(topic LIKE ? OR claim_text LIKE ? OR context_text LIKE ?)")
        params.extend([like, like, like])
    if verdict.strip():
        where.append("verdict = ?")
        params.append(verdict.strip())
    if artifact_kind.strip():
        where.append("artifact_kind = ?")
        params.append(artifact_kind.strip())
    sql = """
        SELECT
          id, artifact_kind, artifact_id, artifact_path, topic, analyst_id, work_date,
          claim_text, category, verdict, confidence, rationale, source_urls,
          context_text, created_at, updated_at
        FROM fact_cards
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 200))
    rows = await db.query(sql, params)
    for row in rows:
        try:
            row["source_urls"] = json.loads(row.get("source_urls") or "[]")
        except ValueError:
            row["source_urls"] = []
    return rows
