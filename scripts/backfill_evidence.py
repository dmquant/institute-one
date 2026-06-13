#!/usr/bin/env python3
"""Backfill evidence ledger from managed Obsidian notes.

Reads ``vault_index`` rows, extracts URLs from the corresponding vault files,
and records them in ``evidence_sources`` / ``claim_evidence_links``. The script
does not fetch remote pages; it only indexes cited URLs already present in
institute artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.institute import evidence  # noqa: E402
from app.institute.prompts import work_date  # noqa: E402

H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _topic_from_note(path: str, text: str) -> str:
    m = H1_RE.search(text)
    if m:
        return m.group(1).strip()
    return Path(path).stem


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="maximum notes to scan; 0 means all")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    await db.init()

    if not settings.vault_dir:
        print("vault disabled; nothing to backfill")
        await db.close()
        return

    rows = await db.query(
        "SELECT path, artifact_kind, artifact_id FROM vault_index ORDER BY written_at DESC"
    )
    if args.limit > 0:
        rows = rows[: args.limit]

    scanned = 0
    links = 0
    root = settings.vault_dir.expanduser()
    for row in rows:
        path = root / row["path"]
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        topic = _topic_from_note(row["path"], text)
        n = await evidence.ingest_text(
            text,
            artifact_kind=str(row["artifact_kind"]),
            artifact_id=str(row["artifact_id"]),
            artifact_path=str(row["path"]),
            topic=topic,
            work_date=work_date(),
        )
        scanned += 1
        links += n

    await db.close()
    print(f"scanned={scanned} new_links={links}")


if __name__ == "__main__":
    asyncio.run(_main())
