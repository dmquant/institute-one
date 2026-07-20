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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..hands.registry import get_registry
from ..router import executor
from . import sessions
from .analysts import get_analyst
from .prompts import (
    extract_summary,
    previous_steps_block,
    substitute_variables,
    work_date,
)

log = logging.getLogger("institute.workflows")

# workflow_runs.status enum — canonical code constant mirroring the CHECK in
# migrations/0001_init.sql. Import point for API surfaces (/api/contract).
RUN_STATUSES = ("running", "completed", "failed", "cancelled")

# keep strong references to fire-and-forget drivers
_driving: set[asyncio.Task] = set()

# ${WEEK_DISPUTES} rendering caps (committee workflow, Phase 7)
WEEK_DISPUTES_DAYS = 7
WEEK_DISPUTES_MAX_BYTES = 3072      # UTF-8 bytes, like the ${DATA_BUNDLE} cap
WEEK_DISPUTES_MAX_BOARDS = 50       # backstop; the byte cap bites first


# ---- definitions ---------------------------------------------------------

def _normalize_steps(workflow_id: str, steps: list[Any]) -> list[Any]:
    """Fold the legacy ``analyst`` step key into the canonical ``analyst_id``.

    Unknown analyst ids get a loud warning (runs fall back to chief-strategist,
    documented behaviour) but never raise — reconcile happens at boot.
    """
    out: list[Any] = []
    for step in steps:
        if not isinstance(step, dict):  # malformed step: keep as-is, _drive deals with it
            out.append(step)
            continue
        step = dict(step)
        legacy = step.pop("analyst", None)
        # canonical analyst_id wins over the legacy alias when both are present
        # (matches the runtime lookup order in _drive)
        aid = str(step.get("analyst_id") or legacy or "").strip()
        if aid:
            step["analyst_id"] = aid
            if get_analyst(aid) is None:
                log.warning(
                    "workflow %s step %s: unknown analyst %r — runs will fall back to chief-strategist",
                    workflow_id, step.get("id"), aid,
                )
        out.append(step)
    return out


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
            steps = _normalize_steps(data["id"], data["steps"])
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
                    json.dumps(steps, ensure_ascii=False),
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


# ---- lazy prompt variables --------------------------------------------------

def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[: max_bytes - len("…".encode("utf-8"))].decode("utf-8", errors="ignore")
    return cut + "…"


async def week_disputes_variable() -> str:
    """${WEEK_DISPUTES} value: topics + closing summaries of whiteboard boards
    completed in the last WEEK_DISPUTES_DAYS days (newest first), ≤3KB UTF-8.

    Reads the whiteboard tables directly (read-only projection; whiteboard.py
    owns the write path and stays untouched). The closing summary is the
    highest-idx completed card's summary — the board's wrap-up card. Never
    raises — no boards / no data renders as "" so the committee prompt
    degrades to its documented「材料为空」branch.
    """
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=WEEK_DISPUTES_DAYS)
        ).isoformat(timespec="seconds")
        boards = await db.query(
            "SELECT b.topic, b.work_date, "
            "       (SELECT c.summary FROM whiteboard_cards c "
            "         WHERE c.board_id = b.id AND c.status = 'completed' "
            "           AND c.summary IS NOT NULL AND c.summary != '' "
            "         ORDER BY c.idx DESC LIMIT 1) AS closing_summary "
            "FROM whiteboard_boards b "
            "WHERE b.status = 'completed' AND b.updated_at >= ? "
            "ORDER BY b.updated_at DESC LIMIT ?",
            (cutoff, WEEK_DISPUTES_MAX_BOARDS),
        )
        lines = []
        for b in boards:
            summary = (b["closing_summary"] or "").replace("\n", " ").strip() or "（无收尾摘要）"
            lines.append(f"- 「{b['topic']}」（{b['work_date']} 研讨）：{summary}")
        return _truncate_utf8("\n".join(lines), WEEK_DISPUTES_MAX_BYTES)
    except Exception:  # noqa: BLE001 - the prompt path must never break on data
        log.exception("week disputes render failed")
        return ""


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


# ---- committee (Phase 7): once-per-week idempotent kickoff -------------------

COMMITTEE_WORKFLOW_ID = "committee"
COMMITTEE_CLAIM_PREFIX = "committee"
# a claim that never recorded a run_id (kickoff crashed between claim and
# insert) is retryable after this long
COMMITTEE_CLAIM_STALE_S = 3600


def committee_week(wd: str | None = None) -> str:
    """ISO-week key for the committee claim, from the SGT work date."""
    y, w, _ = date.fromisoformat(wd or work_date()).isocalendar()
    return f"{y}-W{w:02d}"


async def run_committee_once(*, source: str = "scheduler") -> str | None:
    """Idempotent committee kickoff: at most ONE run per ISO week (SGT).

    Durable atomic claim (hard rule 2): INSERT ... ON CONFLICT DO NOTHING on
    the admin_state row ``committee:<iso-week>`` makes one winner per week —
    scheduler misfires/coalesce replays, restarts and manual run-now triggers
    all collapse into it. Retry semantics: if the claimed week's run ended
    'failed'/'cancelled' (or the claim never recorded a run_id and is older
    than COMMITTEE_CLAIM_STALE_S), the claim is taken over via CAS UPDATE and
    the week reruns; a running/completed run keeps the week closed. Returns
    the new run id, or None when the week is already taken.

    Escape hatch: POST /api/workflows/committee/run bypasses this guard (the
    operator's explicit intent), same as the generic run endpoint vs
    daily.py's _ran_today guard.
    """
    week = committee_week()
    key = f"{COMMITTEE_CLAIM_PREFIX}:{week}"
    token = json.dumps({"status": "claimed", "claimed_at": bus.now_iso()})
    won = await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (key, token),
    )
    if not won:
        row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
        if row is None:  # released between INSERT and SELECT — don't race it, just skip
            return None
        stale = False
        try:
            held = json.loads(row["value"])
            run_id = held.get("run_id")
            if run_id:
                run = await db.query_one(
                    "SELECT status FROM workflow_runs WHERE id = ?", (run_id,)
                )
                stale = run is None or run["status"] in ("failed", "cancelled")
            else:
                claimed_at = datetime.fromisoformat(held["claimed_at"])
                stale = (
                    datetime.now(timezone.utc) - claimed_at
                ).total_seconds() > COMMITTEE_CLAIM_STALE_S
        except (ValueError, KeyError, TypeError):
            stale = True  # a corrupt claim must not wedge the week forever
        if not stale:
            log.info("committee already ran/running for %s; skipping", week)
            return None
        taken = await db.execute(
            "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
            (token, key, row["value"]),
        )
        if not taken:
            return None  # lost the takeover race — exactly one retryer wins
    try:
        run_id = await run_workflow(
            COMMITTEE_WORKFLOW_ID, variables={"WORK_DATE": work_date()}, source=source,
        )
    except Exception:
        # release our own claim (CAS) so the week stays retryable
        await db.execute("DELETE FROM admin_state WHERE key = ? AND value = ?", (key, token))
        raise
    recorded = await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
        (json.dumps({"status": "started", "run_id": run_id, "claimed_at": bus.now_iso()}),
         key, token),
    )
    if not recorded:  # taken over mid-kickoff (only possible after the stale window)
        log.warning("committee claim %s changed under us; run %s continues anyway", key, run_id)
    return run_id


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
        # opt-in weighted pick (settings.enable_hand_weights, default False = the
        # round-robin below): the pool is intersected with research_hand_names
        # (hard rule 10 — weights only reorder INSIDE the chain); an explicit
        # step hand still wins and the fallback chain is unchanged either way.
        if settings.enable_hand_weights and not step.get("hand"):
            live = [h for h in hands if get_registry().is_available(h)]
            picked = get_registry().pick_weighted_hand("research", live)
            if picked:
                return picked, hands
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
        # ${DATA_BUNDLE}: local market-data digest for ${TOPIC} (Phase 1b).
        # Computed lazily — only when some step prompt references it and the
        # caller did not pass an explicit value; missing/failed data renders
        # as "" so prompts degrade without a trace (never raises).
        if "DATA_BUNDLE" not in variables and any(
            isinstance(s, dict) and "${DATA_BUNDLE}" in str(s.get("prompt", "")) for s in wf["steps"]
        ):
            from . import market_fetchers  # lazy: engine stays importable without the fetcher stack
            variables["DATA_BUNDLE"] = await market_fetchers.data_bundle_variable(variables)
            # persist the computed value on the run row so what the prompts
            # actually saw stays inspectable. json_set touches ONLY this key —
            # a whole-blob write would clobber keys landed by a concurrent
            # writer (REVIEW-C5 P2 lost update); the status guard still keeps
            # terminal rows immutable (cancel-safety, not concurrency safety).
            await db.execute(
                "UPDATE workflow_runs SET variables = json_set(variables, '$.DATA_BUNDLE', ?) "
                "WHERE id = ? AND status = 'running'",
                (variables["DATA_BUNDLE"], run_id),
            )
        # ${WEEK_DISPUTES}: last-7-days completed whiteboard digest (Phase 7
        # committee). Same lazy contract as ${DATA_BUNDLE}: only computed when
        # a step prompt references it and no explicit value was passed; empty
        # data renders as "" so the prompt degrades without a trace.
        if "WEEK_DISPUTES" not in variables and any(
            isinstance(s, dict) and "${WEEK_DISPUTES}" in str(s.get("prompt", "")) for s in wf["steps"]
        ):
            variables["WEEK_DISPUTES"] = await week_disputes_variable()
            await db.execute(
                "UPDATE workflow_runs SET variables = json_set(variables, '$.WEEK_DISPUTES', ?) "
                "WHERE id = ? AND status = 'running'",
                (variables["WEEK_DISPUTES"], run_id),
            )
        prior: list[tuple[str, str]] = []
        results: list[dict[str, Any]] = []

        for i, step in enumerate(wf["steps"]):
            current = await db.query_one("SELECT status FROM workflow_runs WHERE id = ?", (run_id,))
            if current is None or current["status"] != "running":
                return  # cancelled between steps

            prompt = substitute_variables(step.get("prompt", ""), variables)
            # canonical key first; 'analyst' tolerated for rows predating normalization
            aid = str(step.get("analyst_id") or step.get("analyst") or "").strip()
            analyst = get_analyst(aid) if aid else None
            if analyst is None and aid:
                log.warning(
                    "run %s step %s: unknown analyst %r; falling back to chief-strategist",
                    run_id, step.get("id"), aid,
                )
            analyst = analyst or get_analyst("chief-strategist")
            if analyst is None:
                await _finish_run(run_id, "failed", error=f"step {step.get('id')}: no analyst available")
                return
            from . import memory
            full_prompt = await memory.prompt_with_memory(
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
