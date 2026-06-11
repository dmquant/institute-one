"""Sessions — every conversation/workflow/board gets a session row + workspace dir.

The workspace is the artifact surface: hands write files there, the API serves
them read-only. ``read_workspace_file`` resolves paths and refuses anything
outside the workspace prefix.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings

log = logging.getLogger("institute.sessions")


async def create_session(title: str, kind: str = "chat", analyst_id: str | None = None) -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    workspace = get_settings().workspaces_dir / "sessions" / session_id
    workspace.mkdir(parents=True, exist_ok=True)
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO sessions (id, title, kind, analyst_id, workspace_dir, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (session_id, title, kind, analyst_id, str(workspace), now, now),
    )
    return {
        "id": session_id, "title": title, "kind": kind, "analyst_id": analyst_id,
        "workspace_dir": str(workspace), "created_at": now, "updated_at": now,
    }


async def get_session(session_id: str) -> dict[str, Any] | None:
    return await db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))


async def list_sessions(kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sessions"
    params: list[Any] = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(min(limit, 500))
    return await db.query(sql, params)


async def touch(session_id: str) -> None:
    await db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (bus.now_iso(), session_id))


async def add_message(
    session_id: str, role: str, content: str, hand: str | None = None, task_id: str | None = None,
) -> int:
    message_id = await db.insert(
        "INSERT INTO messages (session_id, role, content, hand, task_id, created_at) VALUES (?,?,?,?,?,?)",
        (session_id, role, content, hand, task_id, bus.now_iso()),
    )
    await touch(session_id)
    return message_id


async def get_message(message_id: int) -> dict[str, Any] | None:
    return await db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))


async def list_messages(session_id: str) -> list[dict[str, Any]]:
    return await db.query("SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,))


# ---- workspace ----------------------------------------------------------

def workspace_path(session: dict[str, Any]) -> Path:
    return Path(session["workspace_dir"])


async def list_workspace_files(session_id: str) -> list[dict[str, Any]]:
    session = await get_session(session_id)
    if session is None:
        raise ValueError(f"unknown session {session_id}")
    ws = workspace_path(session)

    def _scan() -> list[dict[str, Any]]:
        if not ws.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(ws.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ws)
            if any(part.startswith(".") for part in rel.parts):
                continue
            st = p.stat()
            out.append({
                "path": str(rel),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            })
        return out

    return await asyncio.to_thread(_scan)


async def read_workspace_file(session_id: str, relpath: str) -> str:
    session = await get_session(session_id)
    if session is None:
        raise ValueError(f"unknown session {session_id}")
    ws = workspace_path(session).resolve()
    target = (ws / relpath).resolve()
    if not target.is_relative_to(ws):
        raise ValueError("path escapes the session workspace")
    if not target.is_file():
        raise FileNotFoundError(relpath)
    return await asyncio.to_thread(lambda: target.read_text(encoding="utf-8", errors="replace"))
