"""Deep research queue.

A durable priority queue (priority DESC, created_at ASC) over the ``research``
workflow. ``tick()`` is the scheduler entrypoint: it claims at most one pending
item (respecting the daily cap and the one-at-a-time rule), runs the workflow
to completion, writes ``research_log``, and archives the session workspace.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from . import archive
from .prompts import work_date

log = logging.getLogger("institute.research")

# Serializes the cap-check + claim section across concurrent ticks (scheduler
# job overlapping a manual POST /tick). The workflow run itself happens outside.
_claim_lock = asyncio.Lock()


async def enqueue(topic: str, priority: int = 0, source: str = "api") -> dict[str, Any]:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic must not be empty")

    existing = await db.query_one(
        "SELECT * FROM research_queue WHERE topic = ? AND status IN ('pending','running') "
        "ORDER BY created_at LIMIT 1",
        (topic,),
    )
    if existing:
        return {**existing, "deduped": True}

    settings = get_settings()
    # research_log.completed_at is bus.now_iso() format, so compare in the same format
    threshold = (
        datetime.now(timezone.utc) - timedelta(days=settings.research_cooldown_days)
    ).isoformat(timespec="seconds")
    last = await db.query_one(
        "SELECT completed_at FROM research_log WHERE topic = ? AND completed_at >= ? "
        "ORDER BY completed_at DESC LIMIT 1",
        (topic, threshold),
    )
    if last and priority <= 0:
        return {"refused": "cooldown", "topic": topic, "last_completed_at": last["completed_at"]}

    item_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO research_queue (id, topic, priority, status, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, topic, priority, "pending", source, bus.now_iso()),
    )
    await bus.emit("research.queued", "research", item_id, {"topic": topic})
    return await db.query_one("SELECT * FROM research_queue WHERE id = ?", (item_id,))  # type: ignore[return-value]


async def tick() -> str | None:
    """Scheduler job: process at most one queue item. Never raises."""
    try:
        claimed = await _claim_next()
        if claimed is None:
            return None
        await _run_item(claimed["id"], claimed["topic"])
        return claimed["id"]
    except Exception:  # noqa: BLE001 - scheduler jobs must never raise
        log.exception("research tick failed")
        return None


async def _claim_next() -> dict[str, Any] | None:
    settings = get_settings()
    async with _claim_lock:
        done_today = await db.query_one(
            "SELECT COUNT(*) AS n FROM research_log WHERE substr(completed_at, 1, 10) = ?",
            (work_date(),),
        )
        if done_today and done_today["n"] >= settings.research_daily_cap:
            return None
        if await db.query_one("SELECT 1 AS x FROM research_queue WHERE status = 'running' LIMIT 1"):
            return None
        top = await db.query_one(
            "SELECT id, topic FROM research_queue WHERE status = 'pending' "
            "ORDER BY priority DESC, created_at ASC LIMIT 1"
        )
        if top is None:
            return None
        claimed = await db.execute(
            "UPDATE research_queue SET status='running', started_at=? WHERE id=? AND status='pending'",
            (bus.now_iso(), top["id"]),
        )
        return top if claimed else None


def _as_dict(run: Any) -> dict[str, Any]:
    """Normalize run_workflow_and_wait's return (row dict or dataclass-like)."""
    if isinstance(run, dict):
        d = dict(run)
    else:
        d = {k: getattr(run, k, None) for k in ("id", "session_id", "status", "results", "error")}
    if isinstance(d.get("results"), str):
        try:
            d["results"] = json.loads(d["results"] or "[]")
        except ValueError:
            d["results"] = []
    return d


def _analyst_catalog() -> str:
    """Closed-list roster catalog injected into the follow-ups step prompt."""
    from .analysts import roster

    return "\n".join(f"- {a.id}：{a.name}（{a.focus}）" for a in roster())


async def _run_item(item_id: str, topic: str) -> None:
    from . import workflows  # deferred: keeps this module importable standalone

    try:
        run = _as_dict(await workflows.run_workflow_and_wait(
            "research",
            variables={
                "TOPIC": topic, "WORK_DATE": work_date(),
                "ANALYST_CATALOG": _analyst_catalog(),
            },
            source="research",
        ))
    except Exception as exc:  # noqa: BLE001
        log.exception("research run crashed for item %s", item_id)
        await db.execute(
            "UPDATE research_queue SET status='failed', error=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (str(exc)[:1000], bus.now_iso(), item_id),
        )
        return

    run_id = run.get("id")
    if run.get("status") == "completed":
        results = run.get("results") or []
        # the research summary comes from the compiled report step, not the last step
        report_steps = [r for r in results if str(r.get("step_id", "")).startswith("06")]
        summary = ((report_steps or results[-1:]) or [{}])[-1].get("summary") or ""
        n = await db.execute(
            "UPDATE research_queue SET status='completed', run_id=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (run_id, bus.now_iso(), item_id),
        )
        if n == 0:  # cancelled while the run was finishing
            log.warning("research item %s no longer running; skipping log/archive", item_id)
            return
        await db.execute(
            "INSERT INTO research_log (topic, run_id, summary, completed_at) VALUES (?,?,?,?)",
            (topic, run_id, summary, bus.now_iso()),
        )
        session_id = run.get("session_id")
        if session_id:
            try:
                await archive.snapshot_session(session_id, "research", item_id)
            except Exception:  # noqa: BLE001 - archiving must not fail the item
                log.exception("archive snapshot failed for research %s", item_id)
        try:
            await _apply_followups(item_id, topic, session_id)
        except Exception:  # noqa: BLE001 - follow-ups must not fail the item
            log.exception("follow-ups failed for research %s", item_id)
        await bus.emit("research.completed", "research", item_id, {
            "topic": topic, "run_id": run_id, "session_id": session_id, "summary": summary[:500],
        })
    else:
        error = (run.get("error") or f"workflow run {run.get('status') or 'failed'}")[:1000]
        await db.execute(
            "UPDATE research_queue SET status='failed', run_id=?, error=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (run_id, error, bus.now_iso(), item_id),
        )


# ---- follow-ups (07_后续跟进.md → whiteboard topic pool + mailbox threads) ----

FOLLOWUPS_FILE = "07_后续跟进.md"
MAX_FOLLOWUP_TOPICS = 3
MAX_FOLLOWUP_MAILS = 2

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_followups(text: str) -> dict[str, list[dict[str, str]]]:
    """Extract the follow-ups JSON block. Defensive: any failure -> empty lists."""
    out: dict[str, list[dict[str, str]]] = {"whiteboard_topics": [], "mailbox_followups": []}
    if not text:
        return out
    for match in reversed(_JSON_BLOCK.findall(text)):  # last block wins
        try:
            data = json.loads(match)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        for t in data.get("whiteboard_topics") or []:
            if isinstance(t, dict) and str(t.get("topic", "")).strip():
                out["whiteboard_topics"].append({
                    "topic": str(t["topic"]).strip()[:200],
                    "question": str(t.get("question", "")).strip()[:500],
                })
        for m in data.get("mailbox_followups") or []:
            if isinstance(m, dict) and str(m.get("analyst_id", "")).strip() and str(m.get("body", "")).strip():
                out["mailbox_followups"].append({
                    "analyst_id": str(m["analyst_id"]).strip(),
                    "subject": str(m.get("subject", "")).strip()[:200],
                    "body": str(m.get("body", "")).strip()[:4000],
                })
        break
    out["whiteboard_topics"] = out["whiteboard_topics"][:MAX_FOLLOWUP_TOPICS]
    out["mailbox_followups"] = out["mailbox_followups"][:MAX_FOLLOWUP_MAILS]
    return out


async def _apply_followups(item_id: str, topic: str, session_id: str | None) -> None:
    """Feed the follow-ups step output into the whiteboard topic pool and mailbox."""
    if not session_id:
        return
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (session_id,))
    if not row or not row["workspace_dir"]:
        return
    path = Path(row["workspace_dir"]) / FOLLOWUPS_FILE
    if not path.is_file():
        log.info("research %s: no %s — skipping follow-ups", item_id, FOLLOWUPS_FILE)
        return
    followups = parse_followups(path.read_text(encoding="utf-8", errors="replace"))

    from .analysts import get_analyst
    from . import mailbox, whiteboard  # lazy: domain peers

    n_topics = 0
    for t in followups["whiteboard_topics"]:
        try:
            await whiteboard.add_topic(
                t["topic"], question=t["question"], source="research", score=1.5,
            )
            n_topics += 1
        except Exception:  # noqa: BLE001
            log.exception("follow-up topic failed: %s", t["topic"])

    n_mails = 0
    for m in followups["mailbox_followups"]:
        if get_analyst(m["analyst_id"]) is None:
            log.warning("follow-up mail dropped: unknown analyst %s", m["analyst_id"])
            continue
        subject = f"【研究跟进】{topic}：{m['subject'] or '追问'}"
        body = f"来自深度研究《{topic}》（{work_date()}）的跟进追问：\n\n{m['body']}"
        try:
            await mailbox.create_thread(subject, m["analyst_id"], body)
            n_mails += 1
        except Exception:  # noqa: BLE001
            log.exception("follow-up mail failed for %s", m["analyst_id"])

    if n_topics or n_mails:
        await bus.emit("research.followups", "research", item_id, {
            "topic": topic, "whiteboard_topics": n_topics, "mailbox_threads": n_mails,
        })
        log.info("research %s follow-ups: %d topics, %d mail threads", item_id, n_topics, n_mails)


async def list_queue(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM research_queue"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return await db.query(sql, params)


async def get_item(item_id: str) -> dict[str, Any] | None:
    item = await db.query_one("SELECT * FROM research_queue WHERE id = ?", (item_id,))
    if item is None:
        return None
    item["run"] = None
    if item.get("run_id"):
        run = await db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (item["run_id"],))
        if run:
            for col, empty in (("variables", "{}"), ("results", "[]")):
                try:
                    run[col] = json.loads(run[col] or empty)
                except ValueError:
                    pass
            item["run"] = run
    return item


async def cancel_item(item_id: str) -> dict[str, Any] | None:
    item = await db.query_one("SELECT id, status, run_id FROM research_queue WHERE id = ?", (item_id,))
    if item is None:
        return None
    if item["status"] == "pending":
        await db.execute(
            "UPDATE research_queue SET status='cancelled', finished_at=? WHERE id=? AND status='pending'",
            (bus.now_iso(), item_id),
        )
    elif item["status"] == "running":
        if item["run_id"]:
            from . import workflows
            try:
                await workflows.cancel_run(item["run_id"])
            except Exception:  # noqa: BLE001
                log.exception("cancel_run failed for run %s", item["run_id"])
        await db.execute(
            "UPDATE research_queue SET status='cancelled', finished_at=? WHERE id=? AND status='running'",
            (bus.now_iso(), item_id),
        )
    return await db.query_one("SELECT * FROM research_queue WHERE id = ?", (item_id,))
