"""Multi-agent primitives: fan_out / join (Phase 7) + durable groups & runs (M8-012).

``fan_out`` runs ONE prompt across several analysts in parallel — each agent
gets the standard persona sandwich (``prompts.build_analyst_prompt``) and one
executor task, so every invocation stays on THE execution path (hard rule 1)
and is audited as a normal ``tasks`` row. ``join`` folds the finished tasks
into a single verdict under one of four modes (all / first_success /
majority_vote / best_effort).

The primitives stay stateless; the durable layer on top (M8-012, S4-P2-06)
persists named groups (member analysts + routing strategy) and one
``multi_agent_runs`` row per fan-out run: input snapshot, task ids in agents
order, and a STRUCTURED verdict — so a disconnected caller can reconnect via
``get_run_record`` (settle-on-read) and a crashed spawner leaves a recoverable
partial-spawn record instead of orphan tasks. The weekly committee workflow
bridges into the same tables through ``open_committee_run`` /
``finalize_committee_run`` (called from the vault exporter's bus handler).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from .. import bus, db
from ..config import get_settings
from ..router import executor
from .analysts import Analyst, get_analyst
from .prompts import build_analyst_prompt

log = logging.getLogger("institute.multi_agent")

JOIN_MODES = ("all", "first_success", "majority_vote", "best_effort")
MAX_AGENTS = 5


def _roster(agents: Sequence[str]) -> list[Analyst]:
    """Resolve agent ids against the roster; ValueError BEFORE anything spawns."""
    if not agents:
        raise ValueError("agents must not be empty")
    if len(agents) > MAX_AGENTS:
        raise ValueError(f"at most {MAX_AGENTS} agents per run (got {len(agents)})")
    if len(set(agents)) != len(agents):
        raise ValueError("duplicate agent ids")
    roster: list[Analyst] = []
    for aid in agents:
        analyst = get_analyst(aid)
        if analyst is None:
            raise ValueError(f"unknown analyst {aid!r}")
        roster.append(analyst)
    return roster


async def spawn_fan_out(
    agents: Sequence[str],
    prompt: str,
    *,
    hand: str | None = None,
    timeout_s: int = 1800,
) -> list[str]:
    """Spawn one executor task per agent; return the task ids immediately.

    Every agent id must exist in the roster (ValueError BEFORE anything is
    spawned — no chief-strategist fallback here: the caller named the agents
    explicitly). Each agent becomes one ``executor.spawn()`` with the prompt
    wrapped in that analyst's persona sandwich; ids come back in agents order.

    ``hand`` overrides every agent's preferred hand; None falls back per agent
    to ``analyst.hand`` or ``settings.default_hand``.

    Error boundary: a spawn-layer failure (task-row insert etc.) propagates —
    agents already spawned at that point keep running; nothing rolls back.
    Hand/model failures never propagate: the executor folds them into the
    task row (status failed/expired/rate_limited).
    """
    roster = _roster(agents)
    settings = get_settings()
    return [
        await executor.spawn(
            hand or analyst.hand or settings.default_hand,
            build_analyst_prompt(analyst, prompt),
            source="multi_agent",
            model=analyst.model,
            timeout_s=timeout_s,
        )
        for analyst in roster
    ]


async def wait_fan_out(
    task_ids: Sequence[str], *, timeout_s: float | None = None,
) -> list[executor.Task]:
    """Wait until every spawned task leaves the executor, then return the rows.

    ``timeout_s`` is a WALL-CLOCK budget on the wait, not on the tasks: when
    it elapses, ``asyncio.TimeoutError`` is raised and the tasks KEEP RUNNING
    to completion in the background (``asyncio.wait`` never cancels pending
    awaitables) — the caller can re-read them later by id. Rows are returned
    in task_ids order.

    Truth lives in the tasks table: if a driver crashed without writing a
    terminal state (infrastructure bug), the returned row stays 'queued' /
    'running' — join() then counts it as not-completed instead of raising.
    """
    # executor._running holds the in-flight _execute futures (same registry
    # the test teardown drains); a missing id means the task already left.
    live = [f for tid in task_ids if (f := executor._running.get(tid)) is not None]
    if live:
        # asyncio.wait (not gather): a timeout must NOT cancel the tasks, and
        # a driver exception must stay in its future (row state is the truth)
        done, pending = await asyncio.wait(live, timeout=timeout_s)
        for f in done:  # retrieve driver exceptions so they don't log as unretrieved
            if not f.cancelled() and f.exception() is not None:
                log.warning("fan-out driver raised %r; the task row state is the truth", f.exception())
        if pending:
            raise asyncio.TimeoutError(f"{len(pending)} of {len(task_ids)} tasks still running")
    out: list[executor.Task] = []
    for tid in task_ids:
        task = await executor.get_task(tid)
        if task is None:  # unreachable: spawn persisted the row
            raise RuntimeError(f"task row {tid} disappeared")
        out.append(task)
    return out


async def fan_out(
    agents: Sequence[str],
    prompt: str,
    *,
    hand: str | None = None,
    timeout_s: int = 1800,
) -> list[executor.Task]:
    """Run one prompt across several analysts in parallel; wait for them all.

    ``spawn_fan_out`` + unbounded ``wait_fan_out`` (callers wanting a wall
    clock budget use the two halves directly, like the API does). Real
    concurrency is bounded by the executor's global semaphore and per-hand
    mutex (one CLI = one task at a time): tasks on the same hand serialize,
    distinct hands genuinely overlap. Returns finished Task rows in agents
    order.
    """
    return await wait_fan_out(await spawn_fan_out(agents, prompt, hand=hand, timeout_s=timeout_s))


# A structured ballot line: the model states its vote as `VERDICT: <token>`
# (half- or full-width colon); the LAST such line wins (the final answer).
_BALLOT_LINE = re.compile(r"^\s*VERDICT\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def extract_ballot(text: str) -> str:
    """The structured ballot of one output: the last ``VERDICT: <token>`` line,
    falling back to the whole stripped text when no such line exists (the
    pre-M8-012 exact-match behaviour). Comparison stays EXACT (post-strip):
    normalization beyond whitespace would guess at vote equivalence."""
    matches = _BALLOT_LINE.findall(text or "")
    if matches:
        return matches[-1].strip()
    return (text or "").strip()


def join(tasks: Sequence[executor.Task], mode: str) -> dict[str, Any]:
    """Fold finished fan_out tasks into one verdict dict.

    Modes:

    - ``all``: ok iff EVERY task completed; no single output is elected.
    - ``first_success``: output = the first completed task in fan-out order
      (fan_out already awaited everything, so "first" means submission order,
      not wall-clock finish order).
    - ``majority_vote``: ballots are STRUCTURED (M8-012): each completed
      output votes with its last ``VERDICT: <token>`` line when present,
      else with its whole stripped text; only EXACT string equality counts
      as the same vote. ok iff one ballot takes a strict majority (> half)
      of ALL tasks — failures count against the quorum. Prompts that mandate
      a closing VERDICT line get convergent votes despite free-form prose;
      without the line, essays still virtually never match byte-for-byte —
      ties and split votes yield ok=False. The result carries the full
      ``ballots`` tally and each completed output's ``ballot``.
    - ``best_effort``: never fails the join on individual errors; ok iff at
      least one task completed, and everything usable is in ``outputs``.

    Returns ``{"mode", "ok", "output", "outputs"[, "votes", "ballots"]}``
    where ``outputs`` is the per-task projection in fan-out order and
    ``output`` is the elected text (None when the mode elects nothing or
    nothing qualifies).
    """
    if mode not in JOIN_MODES:
        raise ValueError(f"unknown join mode {mode!r} (expected one of: {', '.join(JOIN_MODES)})")
    outputs = [
        {"task_id": t.id, "status": t.status, "output": t.output or "", "error": t.error}
        for t in tasks
    ]
    completed = [t for t in tasks if t.status == "completed"]
    result: dict[str, Any] = {"mode": mode, "ok": False, "output": None, "outputs": outputs}

    if mode == "all":
        result["ok"] = bool(tasks) and len(completed) == len(tasks)
    elif mode == "first_success":
        if completed:
            result["ok"] = True
            result["output"] = completed[0].output or ""
    elif mode == "majority_vote":
        tally: dict[str, int] = {}
        for proj, t in zip(outputs, tasks):
            if t.status != "completed":
                proj["ballot"] = None
                continue
            ballot = extract_ballot(t.output or "")
            proj["ballot"] = ballot
            tally[ballot] = tally.get(ballot, 0) + 1
        result["votes"] = 0
        # structured tally, strongest first (stable sort keeps first-seen
        # order among equal counts — matching the max() election below)
        result["ballots"] = [
            {"ballot": b, "votes": n}
            for b, n in sorted(tally.items(), key=lambda kv: -kv[1])
        ]
        if tally:
            winner, votes = max(tally.items(), key=lambda kv: kv[1])
            result["votes"] = votes
            # a tie can never clear the strict-majority bar, so max() picking
            # the first-seen ballot on ties is safe (ok stays False)
            if votes * 2 > len(tasks):
                result["ok"] = True
                result["output"] = winner
    else:  # best_effort
        result["ok"] = bool(completed)

    return result


# ---- durable groups (M8-012) ------------------------------------------------
#
# multi_agent_groups (migrations/0027): a named standing panel — member
# analysts in fan-out order plus the routing strategy (join mode + optional
# hand override). Runs freeze their own agents/mode at spawn, so editing or
# deleting a group never rewrites history (group_id degrades to NULL).

GROUP_NAME_MAX = 200
GROUP_DESCRIPTION_MAX = 4000
MAX_GROUP_AGENTS = MAX_AGENTS

# multi_agent_runs.status enum — canonical code constant mirroring the CHECK
# in migrations/0027 (RUN_STATUSES idiom). 'completed' means every spawned
# task reached a terminal state and the verdict was recorded; whether the
# join converged is verdict["ok"].
RUN_RECORD_STATUSES = ("running", "completed", "failed")

# a 'running' row with fewer task ids than agents is a crashed spawner only
# after this long — younger rows may still be mid-spawn (spawning five tasks
# takes milliseconds; the window is generous on purpose)
RUN_SPAWN_STALE_S = 600

VERDICT_OUTPUT_CAP = 4000   # chars of elected text stored in the verdict
VERDICT_ERROR_CAP = 500


class RunSpawnError(RuntimeError):
    """A durable run whose executor fan-out failed during task creation.

    The exception keeps the durable identity and any successfully spawned task
    ids so non-HTTP callers can recover the same context as the API.  The
    original executor exception remains available through exception chaining.
    """

    def __init__(
        self, run_id: str, task_ids: Sequence[str], total_agents: int, cause: Exception,
    ) -> None:
        self.run_id = run_id
        self.task_ids = list(task_ids)
        self.total_agents = total_agents
        self.error = (
            f"spawn failed after {len(self.task_ids)} of {total_agents} agents: {cause}"
        )[:VERDICT_ERROR_CAP]
        super().__init__(self.error)


def _parse_group(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["agents"] = json.loads(row["agents"] or "[]")
    return row


def _parse_run_record(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["agents"] = json.loads(row["agents"] or "[]")
    row["task_ids"] = json.loads(row["task_ids"] or "[]")
    row["verdict"] = json.loads(row["verdict"]) if row["verdict"] else None
    return row


def _validate_group_agents(agents: Sequence[str]) -> list[str]:
    ids = [str(a).strip() for a in (agents or []) if str(a).strip()]
    if not ids:
        raise ValueError("agents must not be empty")
    if len(ids) > MAX_GROUP_AGENTS:
        raise ValueError(f"at most {MAX_GROUP_AGENTS} agents per group (got {len(ids)})")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate agent ids in group")
    unknown = [a for a in ids if get_analyst(a) is None]
    if unknown:
        raise ValueError(f"unknown analysts: {', '.join(unknown)}")
    return ids


def _validate_group_name(name: str) -> str:
    # structural metadata (headings, API keys): one plain line, never empty
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("group name must not be empty")
    if len(name) > GROUP_NAME_MAX:
        raise ValueError(f"group name exceeds {GROUP_NAME_MAX} chars ({len(name)})")
    return name


async def create_group(
    name: str, agents: Sequence[str], *,
    description: str = "", mode: str = "all", hand: str | None = None,
) -> dict[str, Any]:
    """Create a named group. Name is the human key: non-empty, unique."""
    name = _validate_group_name(name)
    ids = _validate_group_agents(agents)
    if mode not in JOIN_MODES:
        raise ValueError(f"unknown join mode {mode!r} (expected one of: {', '.join(JOIN_MODES)})")
    description = (description or "").strip()
    if len(description) > GROUP_DESCRIPTION_MAX:
        raise ValueError(f"description exceeds {GROUP_DESCRIPTION_MAX} chars ({len(description)})")
    group_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO multi_agent_groups (id, name, description, agents, mode, hand, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (group_id, name, description, json.dumps(ids, ensure_ascii=False), mode,
             (hand or None), now, now),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(name) — the INSERT is the arbiter (concurrent racers too)
        raise ValueError(f"group name {name!r} already exists") from exc
    return await get_group(group_id)  # type: ignore[return-value]


async def list_groups(limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.query(
        "SELECT * FROM multi_agent_groups ORDER BY created_at DESC, id LIMIT ?",
        (min(max(limit, 1), 500),),
    )
    return [_parse_group(r) for r in rows]


async def get_group(group_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM multi_agent_groups WHERE id = ?", (group_id,))
    return _parse_group(row) if row else None


async def update_group(group_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Partial update (only the provided keys change). Returns the fresh row,
    None for an unknown id; ValueError on any invalid field."""
    allowed = {"name", "description", "agents", "mode", "hand"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    if await get_group(group_id) is None:
        return None
    sets: list[str] = []
    params: list[Any] = []
    if "name" in changes:
        sets.append("name = ?")
        params.append(_validate_group_name(changes["name"]))
    if "description" in changes:
        description = (changes["description"] or "").strip()
        if len(description) > GROUP_DESCRIPTION_MAX:
            raise ValueError(f"description exceeds {GROUP_DESCRIPTION_MAX} chars ({len(description)})")
        sets.append("description = ?")
        params.append(description)
    if "agents" in changes:
        sets.append("agents = ?")
        params.append(json.dumps(_validate_group_agents(changes["agents"]), ensure_ascii=False))
    if "mode" in changes:
        if changes["mode"] not in JOIN_MODES:
            raise ValueError(f"unknown join mode {changes['mode']!r} (expected one of: {', '.join(JOIN_MODES)})")
        sets.append("mode = ?")
        params.append(changes["mode"])
    if "hand" in changes:
        sets.append("hand = ?")
        params.append(changes["hand"] or None)
    if sets:
        sets.append("updated_at = ?")
        params.append(bus.now_iso())
        params.append(group_id)
        try:
            await db.execute(
                f"UPDATE multi_agent_groups SET {', '.join(sets)} WHERE id = ?", params
            )
        except sqlite3.IntegrityError as exc:  # UNIQUE(name)
            raise ValueError("group name already exists") from exc
    return await get_group(group_id)


async def delete_group(group_id: str) -> bool:
    """Hard delete. Existing runs keep their frozen agents/mode; their
    group_id degrades to NULL (0027 ON DELETE SET NULL)."""
    return bool(await db.execute("DELETE FROM multi_agent_groups WHERE id = ?", (group_id,)))


# ---- durable runs (M8-012) ---------------------------------------------------
#
# One multi_agent_runs row per fan-out run: the intent row lands BEFORE any
# spawn, every spawned task carries parent_run_id = the run id (linkage is
# atomic with the task row itself), and the verdict is settled by conditional
# claim — so disconnected callers reconnect through get_run_record and a
# crashed spawner leaves a recoverable partial-spawn record.


def verdict_record(agents: Sequence[str], result: dict[str, Any]) -> dict[str, Any]:
    """Storage-lean structured projection of a join() result for
    multi_agent_runs.verdict: per-task rows keep status/ballot REFS only —
    full output text lives once, in the tasks rows (task_ids dereference)."""
    outputs: list[dict[str, Any]] = []
    for agent, proj in zip(agents, result.get("outputs") or []):
        item: dict[str, Any] = {
            "task_id": proj.get("task_id"), "agent": agent, "status": proj.get("status"),
        }
        if proj.get("error"):
            item["error"] = str(proj["error"])[:VERDICT_ERROR_CAP]
        if "ballot" in proj:
            item["ballot"] = proj["ballot"]
        outputs.append(item)
    record: dict[str, Any] = {
        "kind": "fan_out", "mode": result.get("mode"), "ok": bool(result.get("ok")),
        "outputs": outputs,
    }
    if result.get("output") is not None:
        record["output"] = str(result["output"])[:VERDICT_OUTPUT_CAP]
    if "votes" in result:
        record["votes"] = result["votes"]
        record["ballots"] = result.get("ballots") or []
    return record


async def start_run(
    agents: Sequence[str],
    prompt: str,
    *,
    mode: str = "all",
    hand: str | None = None,
    timeout_s: int = 1800,
    group_id: str | None = None,
    source: str = "multi_agent",
) -> dict[str, Any]:
    """Durable fan-out: intent row first, then one spawn per agent.

    Validation (mode / prompt / roster) happens BEFORE the row lands, so a
    rejected call leaves no record. A spawn-layer failure mid-loop claims the
    row 'failed' with the already-spawned ids recorded (they keep running —
    partial-spawn recovery data), then propagates. Returns the fresh row
    (status 'running', task_ids in agents order).
    """
    if mode not in JOIN_MODES:
        raise ValueError(f"unknown join mode {mode!r} (expected one of: {', '.join(JOIN_MODES)})")
    if not (prompt or "").strip():
        raise ValueError("prompt must not be empty")
    roster = _roster(agents)
    settings = get_settings()
    run_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO multi_agent_runs (id, group_id, agents, mode, prompt, status, task_ids, created_at) "
        "VALUES (?,?,?,?,?,'running','[]',?)",
        (run_id, group_id, json.dumps([a.id for a in roster], ensure_ascii=False),
         mode, prompt, bus.now_iso()),
    )
    task_ids: list[str] = []
    try:
        for analyst in roster:
            task_ids.append(await executor.spawn(
                hand or analyst.hand or settings.default_hand,
                build_analyst_prompt(analyst, prompt),
                source=source,
                model=analyst.model,
                timeout_s=timeout_s,
                parent_run_id=run_id,
            ))
    except Exception as exc:
        spawn_error = RunSpawnError(run_id, task_ids, len(roster), exc)
        await db.execute(
            "UPDATE multi_agent_runs SET status='failed', task_ids=?, error=?, finished_at=? "
            "WHERE id = ? AND status = 'running'",
            (json.dumps(task_ids), spawn_error.error, bus.now_iso(), run_id),
        )
        raise spawn_error from exc
    await db.execute(
        "UPDATE multi_agent_runs SET task_ids = ? WHERE id = ? AND status = 'running'",
        (json.dumps(task_ids), run_id),
    )
    return await get_run_record(run_id, settle=False)  # type: ignore[return-value]


def _spawn_stale(created_at: str, *, ttl_s: int = RUN_SPAWN_STALE_S) -> bool:
    try:
        created = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return True  # a corrupt timestamp must not wedge the row forever
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() > ttl_s


async def settle_run(run_id: str) -> dict[str, Any] | None:
    """Settle-on-read (the reconnect path): fold a 'running' row whose tasks
    all reached terminal states into its structured verdict.

    Idempotent and race-safe: the terminal write is a conditional claim
    (status='running' guard), so concurrent settlers collapse to one winner.
    Committee-bridged rows (workflow_run_id set) delegate to
    ``finalize_committee_run`` — their truth is the workflow run, not tasks.
    Crash recovery: an empty task_ids list is re-derived from the tasks table
    (spawns carry parent_run_id, written atomically with the task row); a row
    with FEWER tasks than agents is a crashed spawner and is failed once it
    is provably stale (RUN_SPAWN_STALE_S) and its spawned tasks are terminal.
    """
    row = await db.query_one("SELECT * FROM multi_agent_runs WHERE id = ?", (run_id,))
    if row is None:
        return None
    if row["workflow_run_id"]:
        settled = await finalize_committee_run(row["workflow_run_id"])
        return settled if settled is not None else _parse_run_record(row)
    if row["status"] != "running":
        return _parse_run_record(row)
    run = _parse_run_record(row)
    task_ids: list[str] = list(run["task_ids"])
    if not task_ids:
        # crash window: spawns landed (parent_run_id) but the task_ids write didn't
        task_ids = [r["id"] for r in await db.query(
            "SELECT id FROM tasks WHERE parent_run_id = ? ORDER BY rowid", (run_id,)
        )]

    tasks: list[executor.Task] = []
    for tid in task_ids:
        t = await executor.get_task(str(tid))
        if t is None:  # row surgically removed: settle it as a failure, not a wedge
            t = executor.Task(
                id=str(tid), status="failed", hand=None, requested_hand="", model=None,
                prompt="", source="multi_agent", session_id=None, parent_run_id=run_id,
                workspace_dir="", error="task row missing",
            )
        tasks.append(t)
    if any(t.status in executor.ACTIVE for t in tasks):
        return run  # still running — reconnect later

    if len(task_ids) < len(run["agents"]):
        if not _spawn_stale(str(run["created_at"])):
            return run  # the spawner may still be mid-loop; don't race it
        await db.execute(
            "UPDATE multi_agent_runs SET status='failed', task_ids=?, error=?, finished_at=? "
            "WHERE id = ? AND status = 'running'",
            (json.dumps(task_ids),
             f"partial spawn: {len(task_ids)} of {len(run['agents'])} tasks spawned",
             bus.now_iso(), run_id),
        )
        return await get_run_record(run_id, settle=False)

    record = verdict_record(run["agents"], join(tasks, run["mode"]))
    await db.execute(
        "UPDATE multi_agent_runs SET status='completed', task_ids=?, verdict=?, finished_at=? "
        "WHERE id = ? AND status = 'running'",
        (json.dumps(task_ids), json.dumps(record, ensure_ascii=False), bus.now_iso(), run_id),
    )
    return await get_run_record(run_id, settle=False)


async def get_run_record(run_id: str, *, settle: bool = True) -> dict[str, Any] | None:
    """One persisted run (parsed). ``settle=True`` (the default) folds a
    finished-but-unsettled row first — the reconnect read."""
    if settle:
        return await settle_run(run_id)
    row = await db.query_one("SELECT * FROM multi_agent_runs WHERE id = ?", (run_id,))
    return _parse_run_record(row) if row else None


async def list_run_records(
    *, group_id: str | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Run history, newest first. A cheap read: rows are returned as stored —
    settle happens on the single-run read, not here."""
    where, params = [], []
    if group_id:
        where.append("group_id = ?")
        params.append(group_id)
    if status:
        if status not in RUN_RECORD_STATUSES:
            raise ValueError(
                f"unknown status {status!r} (one of {', '.join(RUN_RECORD_STATUSES)})"
            )
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM multi_agent_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return [_parse_run_record(r) for r in await db.query(sql, params)]


async def run_outputs(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-task projections WITH output text for one persisted run — the
    reconnect read body. Task rows are the truth, re-read on every call;
    the stored verdict keeps refs only."""
    agents = list(run.get("agents") or [])
    out: list[dict[str, Any]] = []
    for i, tid in enumerate(run.get("task_ids") or []):
        t = await executor.get_task(str(tid))
        item: dict[str, Any] = {
            "task_id": tid,
            "agent": agents[i] if i < len(agents) else None,
            "status": t.status if t else "missing",
            "output": (t.output or "") if t else "",
            "error": t.error if t else "task row missing",
        }
        if run.get("mode") == "majority_vote":
            item["ballot"] = extract_ballot(t.output or "") if t and t.status == "completed" else None
        out.append(item)
    return out


# ---- committee bridge (M8-012) ------------------------------------------------
#
# The weekly committee workflow records into the SAME group/run tables:
# a system-maintained 'committee' group (members = committee.json step
# analysts) and one run row per workflow run, keyed by workflow_run_id
# (UNIQUE, NULLs distinct — the INSERT OR IGNORE idempotency arbiter).
# The input snapshot is the run's frozen ${WEEK_DISPUTES} whiteboard digest;
# per-step outputs stay in the step task rows; the verdict is the structured
# step map. Wiring: the vault exporter's workflow.* bus handler calls
# open_committee_run on workflow.started and finalize_committee_run on the
# terminal events (main.py stays untouched — the exporter is the one
# registered bus surface).

COMMITTEE_GROUP_ID = "committee"
COMMITTEE_SUMMARY_CAP = 300  # matches the workflow.completed payload cap


async def ensure_committee_group() -> dict[str, Any] | None:
    """Upsert the system-maintained committee group from the reconciled
    workflow definition (step analysts, first-appearance order). Returns the
    group row, or None when the committee workflow is not in the DB yet."""
    from . import workflows  # lazy: avoid an import cycle at module load

    wf = await workflows.get_workflow(workflows.COMMITTEE_WORKFLOW_ID)
    if wf is None:
        return None
    agents: list[str] = []
    for step in wf.get("steps") or []:
        aid = str(step.get("analyst_id") or "").strip() if isinstance(step, dict) else ""
        if aid and aid not in agents:
            agents.append(aid)
    now = bus.now_iso()
    try:
        await db.execute(
            "INSERT INTO multi_agent_groups (id, name, description, agents, mode, hand, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET agents = excluded.agents, updated_at = excluded.updated_at",
            (COMMITTEE_GROUP_ID, "committee",
             "每周委员会常设面板（系统自动维护：成员来自 workflows/committee.json 的步骤分析师）",
             json.dumps(agents, ensure_ascii=False), "all", None, now, now),
        )
    except sqlite3.IntegrityError:
        # an operator group squats the 'committee' name with a different id —
        # degrade to whatever exists under the fixed id (usually None)
        log.warning("could not upsert the committee group (name collision)")
    return await get_group(COMMITTEE_GROUP_ID)


async def open_committee_run(workflow_run_id: str) -> dict[str, Any] | None:
    """Durable committee record at kickoff (idempotent; never raises — record
    keeping must not break the debate). The input snapshot lands at finalize:
    ${WEEK_DISPUTES} is computed lazily inside the workflow driver."""
    try:
        from . import workflows  # lazy: avoid an import cycle at module load

        wf_run = await workflows.get_run(workflow_run_id)
        if wf_run is None:
            return None
        group = await ensure_committee_group()
        await db.execute(
            "INSERT OR IGNORE INTO multi_agent_runs "
            "(id, group_id, workflow_run_id, agents, mode, prompt, status, task_ids, created_at) "
            "VALUES (?,?,?,?,?,?, 'running', '[]', ?)",
            (uuid.uuid4().hex[:12],
             group["id"] if group else None,
             workflow_run_id,
             json.dumps((group or {}).get("agents") or [], ensure_ascii=False),
             "all",
             str((wf_run.get("variables") or {}).get("WEEK_DISPUTES") or ""),
             bus.now_iso()),
        )
        row = await db.query_one(
            "SELECT * FROM multi_agent_runs WHERE workflow_run_id = ?", (workflow_run_id,)
        )
        return _parse_run_record(row) if row else None
    except Exception:  # noqa: BLE001 - bus/scheduler callers must not blow up
        log.exception("could not open committee run record for %s", workflow_run_id)
        return None


async def finalize_committee_run(workflow_run_id: str) -> dict[str, Any] | None:
    """Settle the committee record from its workflow run row (event-driven
    from the exporter, or lazily from a run-record read).

    Upserts the record first so manual runs that bypassed the kickoff hook
    still land one. The claim (status='running' guard) writes: the input
    snapshot (frozen ${WEEK_DISPUTES}), the step task ids, the structured
    step-map verdict, and the mapped terminal status (completed → completed;
    failed/cancelled → failed). Returns the record (None only when the
    workflow run itself is unknown); a still-running workflow returns the
    open record unchanged.
    """
    from . import workflows  # lazy: avoid an import cycle at module load

    wf_run = await workflows.get_run(workflow_run_id)
    if wf_run is None:
        return None
    record = await open_committee_run(workflow_run_id)
    if wf_run["status"] == "running" or record is None:
        return record
    results = [r for r in (wf_run.get("results") or []) if isinstance(r, dict)]
    task_ids = [str(r["task_id"]) for r in results if r.get("task_id")]
    steps = [
        {
            "step_id": r.get("step_id"), "title": r.get("title"),
            "task_id": r.get("task_id"), "status": r.get("status"),
            "summary": str(r.get("summary") or "")[:COMMITTEE_SUMMARY_CAP],
        }
        for r in results
    ]
    verdict = {
        "kind": "committee",
        "workflow_status": wf_run["status"],
        "steps": steps,
        "summary": steps[-1]["summary"] if steps else "",
    }
    status = "completed" if wf_run["status"] == "completed" else "failed"
    error = wf_run.get("error") or ("cancelled" if wf_run["status"] == "cancelled" else None)
    await db.execute(
        "UPDATE multi_agent_runs SET status=?, prompt=?, task_ids=?, verdict=?, error=?, finished_at=? "
        "WHERE workflow_run_id = ? AND status = 'running'",
        (status,
         str((wf_run.get("variables") or {}).get("WEEK_DISPUTES") or ""),
         json.dumps(task_ids),
         json.dumps(verdict, ensure_ascii=False),
         error, bus.now_iso(), workflow_run_id),
    )
    row = await db.query_one(
        "SELECT * FROM multi_agent_runs WHERE workflow_run_id = ?", (workflow_run_id,)
    )
    return _parse_run_record(row) if row else None
