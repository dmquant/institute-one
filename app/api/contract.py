"""Versioned API contract + artifact ref resolution (ROADMAP Phase 8).

GET /api/contract       — machine-readable status enums, field caps and the
                          artifact-ref grammar, so external clients (MCP
                          consumers, the Obsidian plugin, scripts) stop
                          hardcoding strings that can drift.
GET /api/artifacts?ref= — dereference ``task:<id> | note:<path> | fact_card:<id>``.

Status enums are IMPORTED from the owning state-machine modules (executor,
workflows, research, whiteboard) — single source of truth in code, nothing
restated here (REVIEW-C6 M4). The live schema's ``CHECK (status IN (...))``
constraints (migrations/0001_init.sql) are parsed as a cross-check: a
mismatch is reported per table in ``schema_cross_check`` and logged, so
code/DB drift is visible instead of silently served.

Mounted in app/main.py alongside the other API routers.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException

from .. import db
from ..config import get_settings
from ..institute import research, whiteboard, workflows
from ..router import executor

log = logging.getLogger("institute.api.contract")

router = APIRouter(tags=["contract"])

CONTRACT_VERSION = 1
NOTE_CAP_BYTES = 8 * 1024

# Status enums assembled from the canonical code constants of each state
# machine. Keys are the status-bearing tables the contract exposes.
CODE_ENUMS: dict[str, list[str]] = {
    "tasks": sorted(set(executor.ACTIVE) | executor.TERMINAL),
    "workflow_runs": sorted(workflows.RUN_STATUSES),
    "research_queue": sorted(research.QUEUE_STATUSES),
    "whiteboard_boards": sorted(whiteboard.BOARD_STATUSES),
}

_STATUS_CHECK_RE = re.compile(
    r"\bstatus\s+TEXT[^,()]*?CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


async def _live_status_enum(table: str) -> list[str] | None:
    """Parse the status CHECK constraint out of the live table DDL."""
    row = await db.query_one(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    if row is None or not row["sql"]:
        return None
    m = _STATUS_CHECK_RE.search(row["sql"])
    if m is None:
        return None
    values = [v.strip().strip("'\"") for v in m.group(1).split(",")]
    values = [v for v in values if v]
    return values or None


@router.get("/api/contract")
async def contract():
    settings = get_settings()
    statuses: dict[str, list[str]] = dict(CODE_ENUMS)
    cross_check: dict[str, str] = {}
    for table, enum in CODE_ENUMS.items():
        live = await _live_status_enum(table)
        if live is None:
            cross_check[table] = "no_live_check_constraint"
        elif sorted(live) == enum:
            cross_check[table] = "ok"
        else:
            cross_check[table] = "mismatch"
            log.warning(
                "contract enum drift on %s: code constants %s vs live CHECK %s",
                table, enum, sorted(live),
            )

    return {
        "version": CONTRACT_VERSION,
        "statuses": statuses,
        "status_source": "code_constants",
        "schema_cross_check": cross_check,
        "terminal_task_statuses": sorted(executor.TERMINAL),
        "caps": {
            "output_cap_bytes": settings.output_cap_bytes,
            "output_truncation_marker": executor.TRUNCATION_MARKER,
            "note_content_cap_bytes": NOTE_CAP_BYTES,
            "default_timeout_s": settings.default_timeout_s,
            "max_concurrent": settings.max_concurrent,
        },
        "refs": {
            "grammar": "task:<task_id> | note:<vault-relative-path> | fact_card:<fact_card_id>",
            "endpoint": "/api/artifacts?ref=",
            "kinds": {
                "task": "tasks row as JSON (the executor audit spine)",
                "note": f"vault note content, truncated at {NOTE_CAP_BYTES} bytes",
                "fact_card": "fact_cards row as JSON (501 on checkouts predating migration 0015)",
            },
        },
    }


# ---- artifact refs -------------------------------------------------------------

async def _task_artifact(ref: str, task_id: str) -> dict:
    row = await db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(404, f"task {task_id} not found")
    row["artifacts"] = json.loads(row["artifacts"] or "[]")
    row["tried"] = json.loads(row["tried"] or "[]")
    return {"ref": ref, "kind": "task", "task": row}


def _resolve_note_path(vault_dir: Path, relpath: str) -> tuple[str, Path]:
    """(vault-relative path, resolved file) — the ONLY way note refs touch disk.

    Two layers (REVIEW-C6 H2):
    1. lexical (mirrors VaultWriter._resolve): vault-relative POSIX path, no
       ``..``, not absolute → 400;
    2. physical: after following every symlink (file or intermediate
       directory), the real path must still live under the real vault root —
       a symlink pointing outside the vault reads out of jail otherwise → 403.
    """
    rel = PurePosixPath(str(relpath).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise HTTPException(400, f"unsafe vault path: {relpath!r}")
    root = vault_dir.expanduser()
    try:
        resolved = (root / rel).resolve(strict=True)  # follows ALL symlinks
    except OSError:
        raise HTTPException(404, f"note {rel} not found")
    if not resolved.is_relative_to(root.resolve()):
        raise HTTPException(403, f"note {rel} escapes the vault root (symlink)")
    if not resolved.is_file():
        raise HTTPException(404, f"note {rel} not found")
    return str(rel), resolved


async def _note_artifact(ref: str, relpath: str) -> dict:
    settings = get_settings()
    if settings.vault_dir is None:
        raise HTTPException(400, "vault_dir not configured")
    rel, target = _resolve_note_path(settings.vault_dir, relpath)
    raw = target.read_bytes()
    truncated = len(raw) > NOTE_CAP_BYTES
    # errors="ignore" drops the tail bytes of a code point split by the cut
    content = raw[:NOTE_CAP_BYTES].decode("utf-8", errors="ignore")
    ledger = await db.query_one("SELECT * FROM vault_index WHERE path = ?", (rel,))
    return {
        "ref": ref,
        "kind": "note",
        "path": rel,
        "size_bytes": len(raw),
        "truncated": truncated,
        "content": content,
        "ledger": ledger,  # None for a file the writer has never managed
    }


async def _fact_card_artifact(ref: str, card_id: str) -> dict:
    exists = await db.query_one(
        "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'fact_cards'"
    )
    if exists is None:
        raise HTTPException(
            501,
            "fact_cards is not enabled on this deployment — the table ships with "
            "migration 0015 (ROADMAP Phase 3); the ref grammar is reserved so "
            "clients can adopt it now",
        )
    row = await db.query_one("SELECT * FROM fact_cards WHERE id = ?", (card_id,))
    if row is None:
        raise HTTPException(404, f"fact_card {card_id} not found")
    return {"ref": ref, "kind": "fact_card", "fact_card": row}


@router.get("/api/artifacts")
async def get_artifact(ref: str):
    """Resolve one artifact ref: ``task:<id> | note:<path> | fact_card:<id>``."""
    kind, sep, rest = ref.partition(":")
    if not sep or not rest.strip():
        raise HTTPException(
            400, "ref must be task:<id> | note:<vault-relative-path> | fact_card:<id>"
        )
    if kind == "task":
        return await _task_artifact(ref, rest)
    if kind == "note":
        return await _note_artifact(ref, rest)
    if kind == "fact_card":
        return await _fact_card_artifact(ref, rest)
    raise HTTPException(400, f"unknown ref kind {kind!r} (expected task | note | fact_card)")
