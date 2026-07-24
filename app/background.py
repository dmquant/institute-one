"""Single source of truth for the background-task registries.

``app.main._drain_background`` (shutdown drain) and the ``tests/conftest.py``
teardown both need "every asyncio.Task the domain modules spawned". Keeping
the union here means the two sweeps can never drift apart.
"""
from __future__ import annotations

import asyncio


def all_background_tasks() -> set[asyncio.Task]:
    """Snapshot the union of every module-level background-task registry.

    NOTE: when a module gains a new background-task registry, add it to THIS
    union only — main._drain_background and the conftest teardown both call
    here. Imports are lazy so pulling in this module never drags the domain
    modules along (tests must set env vars before app modules load).
    """
    from .institute import analyst_daily, archive, bilingual, mailbox, research, whiteboard, workflows
    from .router import executor

    return (
        set(executor._running.values())
        | set(workflows._driving)
        | set(whiteboard._bg_tasks)
        | set(mailbox._bg_tasks)
        | set(analyst_daily._background)
        | set(research._bg_tasks)
        | set(archive._bg_tasks)
        | set(bilingual._bg_tasks)
    )
