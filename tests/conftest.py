"""Test bootstrap.

The environment MUST be configured before any ``app`` module is imported:
settings are an lru-cached pydantic object reading ``INSTITUTE_*`` env vars,
so we point INSTITUTE_HOME / INSTITUTE_VAULT_DIR at a throwaway tmp tree,
disable every real CLI hand, and pin the default hand to the built-in echo
hand. Then ``reset_settings_cache()`` guarantees the first ``get_settings()``
sees this environment.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="institute-one-tests-"))
atexit.register(shutil.rmtree, _TMP_ROOT, True)

os.environ["INSTITUTE_HOME"] = str(_TMP_ROOT / "home")
os.environ["INSTITUTE_VAULT_DIR"] = str(_TMP_ROOT / "vault")
for _flag in ("CLAUDE", "CODEX", "GEMINI", "OPENCODE", "OLLAMA", "AGY"):
    os.environ[f"INSTITUTE_ENABLE_{_flag}"] = "false"
os.environ["INSTITUTE_ENABLE_ECHO"] = "true"
os.environ["INSTITUTE_DEFAULT_HAND"] = "echo"
os.environ["INSTITUTE_RESEARCH_HANDS"] = "echo"

from app import config  # noqa: E402

config.reset_settings_cache()

import asyncio  # noqa: E402

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.hands import registry as registry_mod  # noqa: E402
from app.institute import analyst_daily as analyst_daily_mod  # noqa: E402
from app.institute import archive as archive_mod  # noqa: E402
from app.institute import bilingual as bilingual_mod  # noqa: E402
from app.institute import mailbox as mailbox_mod  # noqa: E402
from app.institute import research as research_mod  # noqa: E402
from app.institute import whiteboard as whiteboard_mod  # noqa: E402
from app.institute import workflows as workflows_mod  # noqa: E402
from app.router import executor  # noqa: E402
from app.vault import writer as vault_writer_mod  # noqa: E402


@pytest.fixture(autouse=True)
async def app_runtime():
    """Fresh DB + registry per test, torn down with db.close().

    Module-level asyncio primitives (locks, semaphores) are recreated so they
    bind to THIS test's event loop (pytest-asyncio uses one loop per test).
    """
    settings = get_settings()
    settings.ensure_dirs()

    # wipe durable state so every test sees a fresh institute
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
    settings.rate_limits_path.unlink(missing_ok=True)
    shutil.rmtree(settings.archive_dir, ignore_errors=True)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)

    # rebind module-level primitives to the current event loop
    db._write_lock = asyncio.Lock()
    executor._global_sem = None
    executor._hand_locks.clear()
    executor._running.clear()
    research_mod._claim_lock = asyncio.Lock()
    whiteboard_mod._active_cards.clear()
    mailbox_mod._inflight.clear()
    vault_writer_mod.reset_writer()

    await db.init()
    registry_mod.init_registry(settings)

    yield

    # stop any background work before closing the connection
    # (keep in sync with the 8 registries app.main._drain_background sweeps)
    pending: set[asyncio.Task] = set()
    pending |= set(whiteboard_mod._bg_tasks)
    pending |= set(mailbox_mod._bg_tasks)
    pending |= set(workflows_mod._driving)
    pending |= set(executor._running.values())
    pending |= set(analyst_daily_mod._background)
    pending |= set(research_mod._bg_tasks)
    pending |= set(archive_mod._bg_tasks)
    pending |= set(bilingual_mod._bg_tasks)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await db.close()
