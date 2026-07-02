"""Linear workflow engine.

A workflow is an ordered list of steps; each step is one analyst prompt run as
one executor task inside the run's session workspace. State lives in the
``workflow_runs`` row (conditional-claim updates), so a crashed driver leaves a
``running`` row for the janitor and never double-runs a step.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from . import sessions
from .analysts import get_analyst
from .prompts import (
    build_analyst_prompt,
    extract_summary,
    previous_steps_block,
    substitute_variables,
    work_date,
)

log = logging.getLogger("institute.workflows")

# keep strong references to fire-and-forget drivers
_driving: set[asyncio.Task] = set()


# ---- definitions ---------------------------------------------------------

async def reconcile_from_disk() -> int:
    """Upsert every workflows/*.json into the workflows table. Never raises."""
    wf_dir = get_settings().workflows_dir
    if not wf_dir.is_dir():
        log.warning("workflows dir %s missing; nothing to reconcile", wf_dir)
        return 0
    count = 0
    for path in sorted(wf_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            await db.execute(
                """INSERT INTO workflows (id, name, description, variables, steps, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, description = excluded.description,
                     variables = excluded.variables, steps = excluded.steps,
                     updated_at = excluded.updated_at""",
                (
                    data["id"], data["name"], data.get("description", ""),
                    json.dumps(data.get("variables", []), ensure_ascii=False),
                    json.dumps(data["steps"], ensure_ascii=False),
                    bus.now_iso(),
                ),
            )
            count += 1
        except Exception:  # noqa: BLE001 - one bad file must not break boot
            log.exception("could not reconcile workflow file %s", path.name)
    log.info("reconciled %d workflow definitions from %s", count, wf_dir)
    return count


def _parse_workflow(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["variables"] = json.loads(row["variables"] or "[]")
    row["steps"] = json.loads(row["steps"] or "[]")
    return row


async def list_workflows() -> list[dict[str, Any]]:
    rows = await db.query("SELECT * FROM workflows ORDER BY id")
    return [_parse_workflow(r) for r in rows]


async def get_workflow(workflow_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
    return _parse_workflow(row) if row else None


# ---- runs -----------------------------------------------------------------

async def _create_run(workflow_id: str, variables: dict[str, str] | None, source: str) -> str:
    wf = await get_workflow(workflow_id)
    if wf is None:
        raise ValueError(f"unknown workflow {workflow_id}")
    variables = dict(variables or {})
    variables.setdefault("WORK_DATE", work_date())
    run_id = uuid.uuid4().hex[:12]
    session = await sessions.create_session(f"{wf['name']} {work_date()}", kind="workflow")
    await db.execute(
        """INSERT INTO workflow_runs (id, workflow_id, session_id, status, variables, source, started_at)
           VALUES (?,?,?, 'running', ?, ?, ?)""",
        (run_id, workflow_id, session["id"], json.dumps(variables, ensure_ascii=False), source, bus.now_iso()),
    )
    await bus.emit(
        "workflow.started", "workflow_run", run_id,
        {"workflow_id": workflow_id, "session_id": session["id"], "variables": variables},
    )
    return run_id


async def run_workflow(
    workflow_id: str, *, variables: dict[str, str] | None = None, source: str = "api",
) -> str:
    run_id = await _create_run(workflow_id, variables, source)
    task = asyncio.create_task(_drive(run_id), name=f"workflow-run-{run_id}")
    _driving.add(task)
    task.add_done_callback(_driving.discard)
    return run_id


async def run_workflow_and_wait(
    workflow_id: str, *, variables: dict[str, str] | None = None, source: str = "api",
) -> dict[str, Any]:
    run_id = await _create_run(workflow_id, variables, source)
    await _drive(run_id)
    return await get_run(run_id)  # type: ignore[return-value]


async def _finish_run(run_id: str, status: str, *, error: str | None = None) -> None:
    claimed = await db.execute(
        "UPDATE workflow_runs SET status = ?, error = ?, finished_at = ? WHERE id = ? AND status = 'running'",
        (status, error, bus.now_iso(), run_id),
    )
    if claimed == 0:  # already terminal (e.g. cancelled mid-step)
        return
    run = await get_run(run_id)
    if run is None:
        return
    await bus.emit(
        f"workflow.{status}", "workflow_run", run_id,
        {
            "workflow_id": run["workflow_id"],
            "session_id": run["session_id"],
            "variables": run["variables"],
            "results": [{**r, "summary": (r.get("summary") or "")[:300]} for r in run["results"]],
        },
    )


def _workflow_hand_policy(
    workflow_id: str,
    step: dict[str, Any],
    analyst_hand: str | None,
    step_index: int,
) -> tuple[str, tuple[str, ...] | None]:
    settings = get_settings()
    if workflow_id == "research":
        hands = settings.research_hand_names
        return str(step.get("hand") or hands[step_index % len(hands)]), hands
    return str(step.get("hand") or analyst_hand or settings.default_hand), None


async def _drive(run_id: str) -> None:
    """Run all steps in order. Must never raise (spawned via create_task)."""
    settings = get_settings()
    try:
        run = await db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if run is None or run["status"] != "running":
            return
        wf = await get_workflow(run["workflow_id"])
        if wf is None:
            await _finish_run(run_id, "failed", error=f"workflow {run['workflow_id']} not found")
            return
        session = await sessions.get_session(run["session_id"])
        if session is None:
            await _finish_run(run_id, "failed", error="run session missing")
            return
        workspace = sessions.workspace_path(session)
        variables: dict[str, str] = json.loads(run["variables"] or "{}")
        prior: list[tuple[str, str]] = []
        results: list[dict[str, Any]] = []

        for i, step in enumerate(wf["steps"]):
            current = await db.query_one("SELECT status FROM workflow_runs WHERE id = ?", (run_id,))
            if current is None or current["status"] != "running":
                return  # cancelled between steps

            prompt = substitute_variables(step.get("prompt", ""), variables)
            analyst = get_analyst(step.get("analyst") or step.get("analyst_id") or "") or get_analyst("chief-strategist")
            if analyst is None:
                await _finish_run(run_id, "failed", error=f"step {step.get('id')}: no analyst available")
                return
            full_prompt = build_analyst_prompt(
                analyst, prompt,
                context_blocks=[previous_steps_block(prior)],
                output_file=step.get("output_file"),
            )
            hand, fallback_chain = _workflow_hand_policy(run["workflow_id"], step, analyst.hand, i)
            task = await executor.submit(
                hand, full_prompt,
                source=run["source"], model=analyst.model,
                session_id=session["id"], parent_run_id=run_id, workspace=workspace,
                timeout_s=step.get("timeout_s") or settings.default_timeout_s,
                fallback_chain=fallback_chain,
            )

            output_file = step.get("output_file")
            if output_file and (workspace / output_file).is_file():
                summary = extract_summary((workspace / output_file).read_text(encoding="utf-8", errors="replace"))
            else:
                summary = extract_summary(task.output or "")
            title = step.get("title", step.get("id", f"step-{i + 1}"))
            results.append({
                "step_id": step.get("id", f"step-{i + 1}"), "title": title,
                "task_id": task.id, "status": task.status,
                "summary": summary, "output_file": output_file,
            })
            prior.append((title, summary))

            claimed = await db.execute(
                "UPDATE workflow_runs SET results = ?, current_step = ? WHERE id = ? AND status = 'running'",
                (json.dumps(results, ensure_ascii=False), i + 1, run_id),
            )
            if claimed == 0:
                return  # cancelled while the step ran
            if task.status != "completed":
                await _finish_run(
                    run_id, "failed",
                    error=f"step {step.get('id')} {task.status}: {task.error or ''}".strip(),
                )
                return

        await _finish_run(run_id, "completed")
    except asyncio.CancelledError:
        log.info("workflow run %s driver cancelled", run_id)
    except Exception as exc:  # noqa: BLE001 - the driver must never raise
        log.exception("workflow run %s crashed", run_id)
        try:
            await _finish_run(run_id, "failed", error=f"engine error: {exc}")
        except Exception:  # noqa: BLE001
            log.exception("could not mark run %s failed", run_id)


# ---- run queries ----------------------------------------------------------

def _parse_run(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["variables"] = json.loads(row["variables"] or "{}")
    row["results"] = json.loads(row["results"] or "[]")
    return row


async def get_run(run_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
    return _parse_run(row) if row else None


async def list_runs(
    workflow_id: str | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    where, params = [], []
    if workflow_id:
        where.append("workflow_id = ?")
        params.append(workflow_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM workflow_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(min(limit, 500))
    return [_parse_run(r) for r in await db.query(sql, params)]


async def cancel_run(run_id: str) -> bool:
    claimed = await db.execute(
        "UPDATE workflow_runs SET status = 'cancelled', error = 'cancelled by operator', finished_at = ? "
        "WHERE id = ? AND status = 'running'",
        (bus.now_iso(), run_id),
    )
    if claimed == 0:
        return False
    # best effort: also stop the in-flight step task
    for t in await db.query(
        "SELECT id FROM tasks WHERE parent_run_id = ? AND status IN ('queued','running')", (run_id,)
    ):
        await executor.cancel(t["id"])
    await bus.emit("workflow.cancelled", "workflow_run", run_id, {})
    return True
