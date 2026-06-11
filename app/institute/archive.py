"""Workspace archive + full-text search.

``snapshot_session`` copies text artifacts out of a session workspace into the
archive tree (``archive_dir/<ref_kind>/<ref_id>/<relpath>``), records them in
``archive_files`` (path relative to archive_dir, sha256-deduped), and indexes
.md/.txt content into the ``archive_fts`` FTS5 table.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings

log = logging.getLogger("institute.archive")

TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv"}
FTS_SUFFIXES = {".md", ".txt"}
MAX_FILE_BYTES = 2 * 1024 * 1024


def _write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


async def snapshot_session(session_id: str, ref_kind: str, ref_id: str | int) -> list[str]:
    """Archive every text file in the session workspace. Returns the relative
    paths archived in this call (unchanged files are skipped)."""
    settings = get_settings()
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (session_id,))
    if row is None:
        log.warning("snapshot: unknown session %s", session_id)
        return []
    workspace = Path(row["workspace_dir"])
    if not workspace.is_dir():
        log.warning("snapshot: workspace missing for session %s: %s", session_id, workspace)
        return []

    ref_id = str(ref_id)
    archived: list[str] = []
    candidates = sorted(await asyncio.to_thread(lambda: list(workspace.rglob("*"))))

    for src in candidates:
        if not src.is_file() or src.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = src.relative_to(workspace)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if src.stat().st_size > MAX_FILE_BYTES:
            log.info("snapshot: skipping oversized file %s", rel)
            continue

        data = await asyncio.to_thread(src.read_bytes)
        sha = hashlib.sha256(data).hexdigest()
        rel_archive = str(Path(ref_kind) / ref_id / rel)

        unchanged = await db.query_one(
            "SELECT id FROM archive_files WHERE path = ? AND sha256 = ?", (rel_archive, sha)
        )
        if unchanged:
            continue

        await asyncio.to_thread(_write_bytes, settings.archive_dir / rel_archive, data)
        await db.execute(
            """INSERT INTO archive_files (session_id, ref_kind, ref_id, path, size, sha256, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 session_id=excluded.session_id, ref_kind=excluded.ref_kind, ref_id=excluded.ref_id,
                 size=excluded.size, sha256=excluded.sha256, created_at=excluded.created_at""",
            (session_id, ref_kind, ref_id, rel_archive, len(data), sha, bus.now_iso()),
        )
        if src.suffix.lower() in FTS_SUFFIXES:
            await db.execute("DELETE FROM archive_fts WHERE path = ?", (rel_archive,))
            await db.execute(
                "INSERT INTO archive_fts (content, path, ref_kind, ref_id, session_id) VALUES (?,?,?,?,?)",
                (data.decode("utf-8", errors="replace"), rel_archive, ref_kind, ref_id, session_id),
            )
        archived.append(rel_archive)

    if archived:
        await bus.emit("archive.snapshot", ref_kind, ref_id,
                       {"session_id": session_id, "files": len(archived)})
    return archived


def _sanitize_match(query: str) -> str:
    """Quote each whitespace token so user punctuation can't break FTS5 MATCH."""
    tokens = [t.replace('"', '""') for t in (query or "").split()]
    return " ".join(f'"{t}"' for t in tokens)


async def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    match = _sanitize_match(query)
    if not match:
        return []
    try:
        return await db.query(
            "SELECT path, ref_kind, ref_id, "
            "snippet(archive_fts, 0, '<b>', '</b>', '…', 20) AS snippet "
            "FROM archive_fts WHERE archive_fts MATCH ? LIMIT ?",
            (match, min(max(limit, 1), 100)),
        )
    except Exception as exc:  # noqa: BLE001 - MATCH parse errors -> empty result
        log.warning("archive search failed for %r: %s", query, exc)
        return []


async def read_file(relpath: str) -> str:
    base = get_settings().archive_dir.resolve()
    target = (base / relpath).resolve()
    if not target.is_relative_to(base):
        raise ValueError("path escapes the archive")
    if not target.is_file():
        raise FileNotFoundError(relpath)
    return await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")


async def list_files(
    ref_kind: str | None = None, ref_id: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    where, params = [], []
    if ref_kind:
        where.append("ref_kind = ?")
        params.append(ref_kind)
    if ref_id:
        where.append("ref_id = ?")
        params.append(str(ref_id))
    sql = "SELECT id, session_id, ref_kind, ref_id, path, size, sha256, created_at FROM archive_files"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(min(max(limit, 1), 1000))
    return await db.query(sql, params)
