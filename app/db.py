"""SQLite access layer.

One aiosqlite connection for the process (aiosqlite serializes statements on a
worker thread).  Helpers commit per call; multi-statement work uses
``transaction()`` which holds the write lock.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .config import get_settings

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


async def init() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    settings = get_settings()
    settings.ensure_dirs()
    _conn = await aiosqlite.connect(settings.db_path, isolation_level=None)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA busy_timeout=5000")
    await _conn.execute("PRAGMA foreign_keys=ON")
    await migrate(_conn)
    return _conn


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db.init() has not been called")
    return _conn


async def migrate(c: aiosqlite.Connection) -> None:
    await c.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    applied = {r["name"] for r in await (await c.execute("SELECT name FROM schema_migrations")).fetchall()}
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        await c.executescript(sql)
        await c.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))


# ---- helpers -----------------------------------------------------------

async def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    cur = await conn().execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def query_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = await query(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: tuple | list = ()) -> int:
    """Run a write statement. Returns rowcount (useful for conditional claims)."""
    async with _write_lock:
        cur = await conn().execute(sql, params)
        await cur.close()
        return cur.rowcount


async def insert(sql: str, params: tuple | list = ()) -> int:
    """Run an INSERT. Returns lastrowid."""
    async with _write_lock:
        cur = await conn().execute(sql, params)
        await cur.close()
        return cur.lastrowid or 0


@asynccontextmanager
async def transaction():
    async with _write_lock:
        await conn().execute("BEGIN")
        try:
            yield conn()
        except BaseException:
            await conn().execute("ROLLBACK")
            raise
        else:
            await conn().execute("COMMIT")
