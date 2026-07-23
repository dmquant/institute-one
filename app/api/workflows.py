from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..institute import daily, workflows

log = logging.getLogger("institute.api.workflows")

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("")
async def list_workflows():
    return await workflows.list_workflows()


# ---- runs (declared before /{workflow_id} so the literal paths win) --------

@router.get("/runs/recent")
async def recent_runs(workflow_id: str | None = None, status: str | None = None, limit: int = 50):
    return await workflows.list_runs(workflow_id=workflow_id, status=status, limit=limit)


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = await workflows.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    return {"cancelled": await workflows.cancel_run(run_id)}


# ---- daily products ---------------------------------------------------------

@router.post("/daily/briefing/run-now")
async def run_briefing_now():
    """Runs to completion (idempotent per SGT work date); fast under the echo hand."""
    run_id = await daily.run_briefing()
    return {"run_id": run_id, "skipped": run_id is None}


@router.post("/daily/daily/run-now")
async def run_daily_now():
    run_id = await daily.run_daily()
    return {"run_id": run_id, "skipped": run_id is None}


# ---- workflow by id ---------------------------------------------------------

@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str):
    wf = await workflows.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(404, "workflow not found")
    return wf


class RunBody(BaseModel):
    variables: dict[str, str] | None = None


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, body: RunBody | None = None):
    if await workflows.get_workflow(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    try:
        run_id = await workflows.run_workflow(
            workflow_id, variables=(body.variables if body else None), source="api",
        )
    except ValueError as exc:
        # missing/blank declared variables (e.g. research without TOPIC) — the
        # engine refuses to feed a literal ${NAME} placeholder to the model
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": run_id}


@router.get("/{workflow_id}/runs")
async def workflow_runs(workflow_id: str, status: str | None = None, limit: int = 50):
    return await workflows.list_runs(workflow_id=workflow_id, status=status, limit=limit)
