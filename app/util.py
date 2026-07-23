"""Small shared helpers — id generation, clamped artifact reads, session
workspace resolution.

Neutral ground: imports only ``db`` (which itself imports only ``config``), so
``router/``, ``institute/`` and ``vault/`` can all use it without import cycles.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("institute.util")

ARTIFACT_READ_CAP = 512 * 1024  # bytes per session-workspace artifact file read
                                # (LOOP-P11c: a runaway report must not flood the
                                # INSTR scan / text assembly / vault export)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def read_text(path: Path | None, cap: int | None = None) -> str | None:
    """Clamped artifact read (LOOP-P11c): at most ``cap`` bytes (default
    ARTIFACT_READ_CAP), so a runaway report file cannot flood the INSTR
    backstop scan, the extraction text assembly, or the vault exporter. A
    multi-byte char split at the clamp boundary degrades to the U+FFFD
    replacement char, which no matcher depends on."""
    if path is None:
        return None
    try:
        if path.is_file():
            with path.open("rb") as fh:
                return fh.read(ARTIFACT_READ_CAP if cap is None else cap).decode("utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


async def session_workspace(session_id: Any) -> Path | None:
    if not session_id:
        return None
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (str(session_id),))
    if row and row["workspace_dir"]:
        ws = Path(row["workspace_dir"]).expanduser()
        if ws.is_dir():
            return ws
    return None
