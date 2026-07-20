"""institute-one — application factory.

One process: API + scheduler + all domain loops + static operator SPA + vault
exporter. The default bind is 127.0.0.1; optional bearer auth is enforced when
``INSTITUTE_TOKEN`` is configured.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import get_settings
from .hands.registry import init_registry
from .router import executor

log = logging.getLogger("institute")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


SHUTDOWN_DRAIN_TIMEOUT_S = 15.0


def _scheduler_inflight() -> set[asyncio.Task]:
    """Snapshot APScheduler's in-flight job tasks (must run BEFORE shutdown).

    Delegates to ``scheduler.inflight_jobs()`` — the one place that touches
    APScheduler private internals — so the drain can await job cancellation
    before ``db.close()``. The outer try/except keeps shutdown alive even if
    the accessor itself breaks (import-time drift, unexpected errors).
    """
    from .institute import scheduler as sched

    try:
        return sched.inflight_jobs()
    except Exception:  # noqa: BLE001 - shutdown must not fail on internals drift
        log.exception("could not snapshot scheduler in-flight jobs")
        return set()


async def _drain_background(
    timeout_s: float = SHUTDOWN_DRAIN_TIMEOUT_S, *, extra: set[asyncio.Task] | None = None,
) -> None:
    """Shutdown hook: cancel every background-task registry, wait (bounded).

    Must run BEFORE db.close(): cancellation paths persist final state (the
    executor marks rows 'cancelled'), and cancelling an executor task makes
    the hand kill its CLI process group (hands/base.run_subprocess) — skipping
    this leaks detached CLI processes past a restart. ``extra`` carries tasks
    outside the module registries (the pre-shutdown scheduler job snapshot).

    Two rounds: work spawned while round 1 was cancelling (e.g. an in-flight
    scheduler job submitting one last executor task before it hits its next
    await) is picked up and cancelled by round 2.
    """
    from .institute import analyst_daily, archive, bilingual, mailbox, research, whiteboard, workflows

    def _registered() -> set[asyncio.Task]:
        return (
            set(executor._running.values())
            | set(workflows._driving)
            | set(whiteboard._bg_tasks)
            | set(mailbox._bg_tasks)
            | set(analyst_daily._background)
            | set(research._bg_tasks)
            | set(archive._bg_tasks)
            | set(bilingual._bg_tasks)
            | (extra or set())
        )

    seen: set[asyncio.Task] = set()
    for sweep in (1, 2):
        pending = {t for t in _registered() if not t.done() and t not in seen}
        if not pending:
            return
        seen |= pending
        log.info("shutdown: draining %d background tasks (sweep %d)", len(pending), sweep)
        for t in pending:
            t.cancel()
        done, alive = await asyncio.wait(pending, timeout=timeout_s)
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()  # consume it: never let shutdown drop errors silently
            if exc is not None:
                log.warning(
                    "shutdown: task %s finished with %s: %s",
                    t.get_name(), type(exc).__name__, exc,
                )
        if alive:
            log.warning(
                "shutdown: %d background tasks still alive after %.0fs (sweep %d)",
                len(alive), timeout_s, sweep,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    await db.init()
    init_registry(settings)
    # load persisted hand weights into the registry's process cache — without
    # this, saved weights silently degrade to neutral 1.0 until a weights API
    # call happens (registry is sync and never reads the DB itself)
    from .api.hands import refresh_weights_cache
    await refresh_weights_cache()
    await executor.recover_orphans()

    from .institute import research
    await research.recover_orphans()

    from .institute import research_tree as research_tree_mod
    await research_tree_mod.recover_orphans()

    # domain modules register their bus handlers / load their config here
    from .institute import workflows as wf
    await wf.reconcile_from_disk()

    from .vault import exporter as vault_exporter
    vault_exporter.register()

    from .institute import chain as chain_graph
    chain_graph.register()

    from .institute import factcheck as factcheck_mod
    factcheck_mod.register()

    from .institute import forecast_extract
    forecast_extract.register()

    from .institute import bilingual as bilingual_twins
    bilingual_twins.register()

    from .institute import operator as operator_loop
    operator_loop.register()

    from .institute import scheduler as sched
    sched.start()

    log.info("institute-one ready on http://%s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        # snapshot in-flight scheduler jobs BEFORE shutdown clears its future
        # set, so the drain can await their cancellation too
        inflight_jobs = _scheduler_inflight()
        sched.shutdown()
        try:
            await _drain_background(extra=inflight_jobs)
        finally:
            await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="institute-one", version="0.1.0", lifespan=lifespan)

    from .api.auth import install_auth
    install_auth(app)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):  # noqa: ANN001
        log.exception("unhandled error on %s", request.url.path)
        transient = "locked" in str(exc).lower() or "busy" in str(exc).lower()
        return JSONResponse(
            status_code=500,
            content={"error": type(exc).__name__, "message": str(exc), "path": request.url.path, "transient": transient},
        )

    from .api import (
        analysts as api_analysts,
        archive as api_archive,
        ask_stream as api_ask_stream,
        bilingual as api_bilingual,
        chain as api_chain,
        contract as api_contract,
        digests as api_digests,
        events as api_events,
        factcheck as api_factcheck,
        forecasts as api_forecasts,
        hands as api_hands,
        mailbox as api_mailbox,
        market_data as api_market_data,
        meta as api_meta,
        multi_agent as api_multi_agent,
        operator as api_operator,
        paper_book as api_paper_book,
        projects as api_projects,
        research as api_research,
        research_tree as api_research_tree,
        roadmap as api_roadmap,
        sessions as api_sessions,
        tasks as api_tasks,
        theses as api_theses,
        vault as api_vault,
        whiteboard as api_whiteboard,
        workflows as api_workflows,
    )
    from . import mcp as api_mcp

    for r in (
        api_meta.router, api_tasks.router, api_ask_stream.router, api_digests.router,
        api_hands.router, api_events.router,
        api_analysts.router, api_sessions.router, api_workflows.router,
        api_whiteboard.router, api_mailbox.router, api_research.router,
        api_research_tree.router, api_projects.router,
        api_roadmap.router, api_theses.router, api_market_data.router,
        api_forecasts.router, api_chain.router, api_paper_book.router,
        api_factcheck.router, api_operator.router, api_multi_agent.router,
        api_archive.router, api_vault.router, api_bilingual.router,
        api_contract.router, api_mcp.router,
    ):
        app.include_router(r)

    dist = get_settings().frontend_dist
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        # SPA fallback: client-side routes (/analysts, /research, …) get index.html.
        # Registered last, so every /api route above wins first.
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            target = (dist / full_path).resolve()
            if full_path and target.is_file() and target.is_relative_to(dist.resolve()):
                return FileResponse(target)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
