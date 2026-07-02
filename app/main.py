"""institute-one — application factory.

One process: API + scheduler + all domain loops + static operator SPA + vault
exporter. Bind: 127.0.0.1, no auth (single operator, single machine).
"""
from __future__ import annotations

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    await db.init()
    init_registry(settings)
    await executor.recover_orphans()

    # domain modules register their bus handlers / load their config here
    from .institute import workflows as wf
    await wf.reconcile_from_disk()

    from .vault import exporter as vault_exporter
    vault_exporter.register()

    from .institute import scheduler as sched
    sched.start()

    log.info("institute-one ready on http://%s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        sched.shutdown()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="institute-one", version="0.1.0", lifespan=lifespan)

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
        events as api_events,
        hands as api_hands,
        mailbox as api_mailbox,
        meta as api_meta,
        research as api_research,
        roadmap as api_roadmap,
        sessions as api_sessions,
        tasks as api_tasks,
        vault as api_vault,
        whiteboard as api_whiteboard,
        workflows as api_workflows,
    )
    from . import mcp as api_mcp

    for r in (
        api_meta.router, api_tasks.router, api_hands.router, api_events.router,
        api_analysts.router, api_sessions.router, api_workflows.router,
        api_whiteboard.router, api_mailbox.router, api_research.router,
        api_roadmap.router, api_archive.router, api_vault.router, api_mcp.router,
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
