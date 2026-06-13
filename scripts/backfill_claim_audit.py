#!/usr/bin/env python3
"""Backfill fact_cards from existing managed vault notes.

This does not rewrite Obsidian notes. It only populates SQLite so /api/claims
can inspect older artifacts generated before claim audit was installed.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.institute import claim_audit  # noqa: E402


async def main() -> None:
    settings = get_settings()
    if not settings.vault_dir:
        raise SystemExit("INSTITUTE_VAULT_DIR is not set")
    vault_dir = settings.vault_dir.expanduser()
    if not vault_dir.is_dir():
        raise SystemExit(f"vault dir not found: {vault_dir}")

    await db.init()
    try:
        rows = await db.query(
            """
            SELECT path, artifact_kind, artifact_id
            FROM vault_index
            WHERE state IN ('clean','conflict')
            ORDER BY written_at
            """
        )
        scanned = 0
        claims = 0
        for row in rows:
            rel = str(row["path"] or "")
            path = vault_dir / rel
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            report = await claim_audit.audit_and_store_text(
                text,
                artifact_kind=str(row["artifact_kind"] or ""),
                artifact_id=str(row["artifact_id"] or rel),
                artifact_path=rel,
                topic=_topic_from_path(rel),
                work_date=_date_from_path(rel),
            )
            scanned += 1
            claims += report.total
        print(f"scanned={scanned} claims={claims}")
    finally:
        await db.close()


def _topic_from_path(rel: str) -> str:
    stem = Path(rel).stem
    if " " in stem:
        return stem.split(" ", 1)[1]
    return stem


def _date_from_path(rel: str) -> str:
    stem = Path(rel).stem
    head = stem.split(" ", 1)[0]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return head
    return ""


if __name__ == "__main__":
    asyncio.run(main())
