"""Deep research queue.

A durable priority queue (priority DESC, created_at ASC) over the ``research``
workflow. ``tick()`` is the scheduler entrypoint: it claims at most one pending
item (respecting the daily cap and the one-at-a-time rule), runs the workflow
to completion, writes ``research_log``, and archives the session workspace.

Thesis-aware tasks (card M3-001): ``enqueue`` also accepts structured context
(thesis_id / security_id / question / output_type / priority_reason, columns
from migrations/0012_research_thesis.sql). Dedup and cooldown run on two
independent rails — the old topic-string rail for topic-only items and
``structured_dedup_key()`` (thesis + security + normalized question) for
structured ones. Claim/cap/orphan-recovery semantics are rail-agnostic and
unchanged; structured context reaches the workflow through the ``${TOPIC}``
variable value only (the prompt templates in workflows/research.json stay
byte-identical). ``seed_from_theses()`` turns imported theses carrying
``practical.actionCode`` into structured candidates.

Projects (ROADMAP Phase 7): ``enqueue`` also accepts an optional
``project_id`` (column from migrations/0021_projects.sql) tagging the item to
a named long-running project. It is a grouping label ONLY — dedup, cooldown,
claim and cap semantics ignore it entirely on both rails.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .. import bus, db
from ..config import get_settings
from ..util import new_id
from . import archive
from .prompts import work_date

log = logging.getLogger("institute.research")

# research_queue.status enum — canonical code constant mirroring the CHECK in
# migrations/0001_init.sql. Import point for API surfaces (/api/contract).
QUEUE_STATUSES = ("pending", "running", "completed", "failed", "cancelled")

# Serializes the cap-check + claim section across concurrent ticks (scheduler
# job overlapping a manual POST /tick). The workflow run itself happens outside.
_claim_lock = asyncio.Lock()

# One-shot flag for the "queue disabled by research_daily_cap <= 0" log:
# without it the tick loop would repeat the line every research_tick_minutes.
_cap_disabled_logged = False

# Tick tasks created for asyncio.shield callers (POST /api/research/tick).
# Shielded tasks outlive their HTTP request, so they must be registered for
# the lifespan shutdown drain like every other background-task set.
_bg_tasks: set[asyncio.Task] = set()


def shielded_tick() -> asyncio.Task:
    """A tick() task that survives client disconnects but not shutdown.

    Callers wrap it in ``asyncio.shield`` — the shield protects it from the
    request being cancelled, while the registry lets ``_drain_background``
    cancel it when the process stops.
    """
    t = asyncio.create_task(tick(), name="research-shielded-tick")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


# Structured-field caps (REVIEW-B6): question mirrors parse_followups' 500-char
# question cap; output_type/priority_reason are labels, not prose. The context
# suffix rides EVERY ${TOPIC} occurrence across the 7-step research workflow
# (~20 substitution sites), so bounding it bounds total prompt growth: a
# 700-char suffix caps the blowup at ~14KB — negligible for a CLI context —
# while a max-length question plus display labels still fits untruncated.
MAX_QUESTION_LEN = 500
MAX_ANNOTATION_LEN = 200        # output_type / priority_reason
_CTX_NAME_CAP = 80              # defensive slice: DB display names are unbounded
_CTX_SUFFIX_CAP = 700


def normalize_question(question: str | None) -> str:
    """Question normalization for the structured dedup key: NFKC (full/half
    width, compatibility forms), casefold, whitespace collapsed to single
    spaces. Rewordings that survive this are different questions on purpose."""
    text = unicodedata.normalize("NFKC", str(question or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def structured_dedup_key(thesis_id: str, security_id: str | None, question: str | None) -> str:
    """Dedup/cooldown key for the structured rail (card M3-001): the
    (thesis, security, normalized question) triple, hashed. \\x1f separators
    keep field boundaries unambiguous."""
    payload = "\x1f".join([str(thesis_id or ""), str(security_id or ""), normalize_question(question)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def enqueue(
    topic: str,
    priority: int = 0,
    source: str = "api",
    *,
    thesis_id: str | None = None,
    security_id: str | None = None,
    question: str | None = None,
    output_type: str | None = None,
    priority_reason: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Queue one research task. Backward compatible: topic-only calls keep the
    exact pre-0012 behavior (dedup on the topic string, cooldown on
    research_log.topic). Passing ``thesis_id`` switches the row to the
    structured rail: dedup and cooldown run on ``structured_dedup_key()``
    instead, so the same thesis+security with a differently-normalized
    question is a distinct task. ``security_id``/``question`` require a
    ``thesis_id`` anchor; ``output_type``/``priority_reason`` are plain
    annotations valid on either rail. ``project_id`` (0021) tags the item to
    an ACTIVE project — a grouping label, invisible to dedup/cooldown; a
    dedup hit returns the existing row untagged rather than retagging it.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic must not be empty")
    thesis_id = (thesis_id or "").strip() or None
    security_id = (security_id or "").strip() or None
    question = (question or "").strip() or None
    output_type = (output_type or "").strip() or None
    priority_reason = (priority_reason or "").strip() or None
    project_id = (project_id or "").strip() or None
    if (security_id or question) and not thesis_id:
        raise ValueError("structured enqueue needs a thesis_id (security_id/question anchor)")
    # explainable caps, not silent truncation: a truncated question would change
    # the dedup triple behind the caller's back (limits documented at MAX_*)
    if question and len(question) > MAX_QUESTION_LEN:
        raise ValueError(f"question exceeds {MAX_QUESTION_LEN} chars ({len(question)}); shorten it")
    for label, val in (("output_type", output_type), ("priority_reason", priority_reason)):
        if val and len(val) > MAX_ANNOTATION_LEN:
            raise ValueError(f"{label} exceeds {MAX_ANNOTATION_LEN} chars ({len(val)})")
    if thesis_id and not await db.query_one("SELECT id FROM theses WHERE id = ?", (thesis_id,)):
        raise ValueError(f"thesis {thesis_id!r} not found")
    if security_id and not await db.query_one("SELECT id FROM securities WHERE id = ?", (security_id,)):
        raise ValueError(f"security {security_id!r} not found")
    if project_id:
        proj = await db.query_one("SELECT status FROM projects WHERE id = ?", (project_id,))
        if proj is None:
            raise ValueError(f"project {project_id!r} not found")
        if proj["status"] != "active":
            raise ValueError(f"project {project_id!r} is archived")

    dedup_key = structured_dedup_key(thesis_id, security_id, question) if thesis_id else None
    if dedup_key:
        existing = await db.query_one(
            "SELECT * FROM research_queue WHERE dedup_key = ? AND status IN ('pending','running') "
            "ORDER BY created_at LIMIT 1",
            (dedup_key,),
        )
    else:
        # dedup_key IS NULL keeps the rails independent: a pending structured
        # item never swallows a broader topic-only request for the same string
        # (pre-0012 rows all have dedup_key NULL, so their behavior is unchanged)
        existing = await db.query_one(
            "SELECT * FROM research_queue WHERE topic = ? AND dedup_key IS NULL "
            "AND status IN ('pending','running') ORDER BY created_at LIMIT 1",
            (topic,),
        )
    if existing:
        return {**existing, "deduped": True}

    settings = get_settings()
    # research_log.completed_at is bus.now_iso() format, so compare in the same format
    threshold = (
        datetime.now(timezone.utc) - timedelta(days=settings.research_cooldown_days)
    ).isoformat(timespec="seconds")
    if dedup_key:
        # structured cooldown rail: research_log.dedup_key is written when a
        # structured item completes; legacy/topic rows keep it NULL and never match
        last = await db.query_one(
            "SELECT completed_at FROM research_log WHERE dedup_key = ? AND completed_at >= ? "
            "ORDER BY completed_at DESC LIMIT 1",
            (dedup_key, threshold),
        )
    else:
        # topic rail cooldown: dedup_key IS NULL for the same rail independence
        # as the pending dedup above (a structured completion on this topic
        # string does not block a broader topic-only request)
        last = await db.query_one(
            "SELECT completed_at FROM research_log WHERE topic = ? AND dedup_key IS NULL "
            "AND completed_at >= ? ORDER BY completed_at DESC LIMIT 1",
            (topic, threshold),
        )
    if last and priority <= 0:
        return {"refused": "cooldown", "topic": topic, "last_completed_at": last["completed_at"]}

    item_id = new_id()
    _cols = ("id, topic, priority, status, source, created_at, thesis_id, security_id, "
             "question, output_type, priority_reason, dedup_key, project_id")
    _vals = (item_id, topic, priority, "pending", source, bus.now_iso(),
             thesis_id, security_id, question, output_type, priority_reason, dedup_key, project_id)
    try:
        if project_id:
            # archived freeze is ATOMIC (REVIEW-D5 H1): the row is selected FROM
            # projects WHERE status='active', so the active check and the INSERT
            # are one statement — an archive() between the pre-read above and
            # this write can no longer slip a tagged row in. rowcount 0 = the
            # project was archived (or deleted) inside that window.
            inserted = await db.execute(
                f"INSERT INTO research_queue ({_cols}) "
                "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,? FROM projects WHERE id = ? AND status = 'active'",
                (*_vals, project_id),
            )
            if not inserted:
                raise ValueError(f"project {project_id!r} was archived concurrently; retry")
        else:
            await db.execute(
                f"INSERT INTO research_queue ({_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _vals,
            )
    except sqlite3.IntegrityError as exc:
        # the INSERT is the arbiter (A2 spirit: the database decides, not the
        # pre-read). Racers that slipped past the dedup SELECT land here.
        if "FOREIGN KEY" in str(exc):
            raise ValueError("thesis, security or project disappeared concurrently; retry") from exc
        # partial unique index idx_research_queue_dedup_active: a concurrent
        # enqueue of the same structured triple won — return it as the dedup
        winner = await db.query_one(
            "SELECT * FROM research_queue WHERE dedup_key = ? AND status IN ('pending','running') "
            "ORDER BY created_at LIMIT 1",
            (dedup_key,),
        )
        if winner:
            return {**winner, "deduped": True}
        raise  # winner vanished in the gap (cancelled/completed): surface the conflict
    await bus.emit("research.queued", "research", item_id, {
        "topic": topic, "thesis_id": thesis_id, "security_id": security_id,
    })
    return await db.query_one("SELECT * FROM research_queue WHERE id = ?", (item_id,))  # type: ignore[return-value]


MAX_SEED_CAP = 100  # one seeding sweep never floods the queue past this


async def seed_from_theses(
    action_codes: Sequence[str] = ("deep_research_candidate",),
    cap: int = 10,
    source: str = "thesis_seed",
) -> dict[str, Any]:
    """Seed structured research candidates from imported theses (card M3-001).

    Scans live theses (kind='thesis', status candidate/active/watch — dormant
    and retired are shelved) whose metadata practical.actionCode matches
    ``action_codes`` and enqueues one structured item per thesis
    (thesis_id anchor, no security/question). Idempotent through the normal
    enqueue rails: an existing pending/running item with the same triple
    dedups, a completion inside the cooldown window refuses — both count as
    "not enqueued" here, never as duplicates. ``cap`` bounds how many NEW
    items this sweep may enqueue: cap=0 is a dry sweep (count matches, enqueue
    nothing) — never reinterpreted — and caps beyond MAX_SEED_CAP are refused.
    """
    codes = {str(c).strip() for c in (action_codes or ()) if str(c).strip()}
    if not codes:
        raise ValueError("seed_from_theses needs at least one action code")
    cap = int(cap)
    if cap < 0:
        raise ValueError("cap must be >= 0 (0 = dry sweep, enqueue nothing)")
    if cap > MAX_SEED_CAP:
        raise ValueError(f"cap exceeds MAX_SEED_CAP ({MAX_SEED_CAP})")
    rows = await db.query(
        "SELECT id, name_zh, metadata_json FROM theses "
        "WHERE kind = 'thesis' AND status IN ('candidate','active','watch') "
        "ORDER BY priority DESC, id"
    )
    scanned = 0
    enqueued: list[dict[str, Any]] = []
    deduped = refused = 0
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            metadata = {}
        practical = metadata.get("practical") if isinstance(metadata, dict) else None
        code = str((practical or {}).get("actionCode") or "").strip()
        if code not in codes:
            continue
        scanned += 1
        if len(enqueued) >= cap:
            continue  # keep counting matches, stop enqueueing
        item = await enqueue(
            row["name_zh"] or row["id"],
            source=source,
            thesis_id=row["id"],
            priority_reason=f"practical.actionCode={code}",
        )
        if item.get("refused"):
            refused += 1
        elif item.get("deduped"):
            deduped += 1
        else:
            enqueued.append({"id": item["id"], "thesis_id": row["id"], "topic": item["topic"]})
    return {
        "matched": scanned, "enqueued": enqueued,
        "deduped": deduped, "refused_cooldown": refused, "cap": cap,
    }


async def recover_orphans() -> int:
    """Boot-time sweep: requeue 'running' rows left behind by a dead process.

    Nothing else recovers them — executor.recover_orphans() sweeps only
    ``tasks`` and the janitor only ``workflow_runs`` — and ``_claim_next()``
    refuses to claim while any row is 'running', so a stale one deadlocks the
    whole research pipeline. Called from the app lifespan at boot.

    Offline twin: app/cli.py's check_orphans() counts this same 'running'
    residue (plus tasks') over a read-only connection — a status-vocabulary
    change must land in both.
    """
    n = await db.execute(
        "UPDATE research_queue SET status='pending', started_at=NULL WHERE status='running'"
    )
    if n:
        log.warning("requeued %d research items orphaned by restart", n)
    return n


async def tick() -> str | None:
    """Scheduler job: process at most one queue item. Never raises."""
    try:
        claimed = await _claim_next()
        if claimed is None:
            return None
        await _run_item(claimed)
        return claimed["id"]
    except Exception:  # noqa: BLE001 - scheduler jobs must never raise
        log.exception("research tick failed")
        return None


async def _claim_next() -> dict[str, Any] | None:
    settings = get_settings()
    async with _claim_lock:
        if settings.research_daily_cap <= 0:
            # 0/negative disables the queue (documented kill switch). Log once
            # per process so the tick job doesn't look healthy while the
            # feature silently does nothing.
            global _cap_disabled_logged
            if not _cap_disabled_logged:
                log.info("research queue disabled: research_daily_cap=%d (<=0)",
                         settings.research_daily_cap)
                _cap_disabled_logged = True
            return None
        # daily cap counts SGT work days: work_date is written at insert time.
        # Legacy pre-0005 rows keep work_date NULL and never match this
        # equality — they are deliberately excluded from every day's cap.
        done_today = await db.query_one(
            "SELECT COUNT(*) AS n FROM research_log WHERE work_date = ?",
            (work_date(),),
        )
        if done_today and done_today["n"] >= settings.research_daily_cap:
            return None
        if await db.query_one("SELECT 1 AS x FROM research_queue WHERE status = 'running' LIMIT 1"):
            return None
        # full row: _run_item needs the structured columns (0012) when present
        top = await db.query_one(
            "SELECT * FROM research_queue WHERE status = 'pending' "
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


async def _topic_with_context(item: dict[str, Any]) -> str:
    """The ``${TOPIC}`` value for a structured item: the topic string plus an
    inline thesis/security/question context suffix. Injection happens here, at
    the variable-value layer research.py owns — the prompt templates in
    workflows/research.json stay byte-identical. Topic-only rows (all NULL
    columns, including every pre-0012 row) pass through unchanged.

    The suffix is bounded: display names slice at _CTX_NAME_CAP (DB names are
    unbounded) and the whole suffix hard-caps at _CTX_SUFFIX_CAP. ${TOPIC}
    recurs at every substitution site across the 7-step workflow, so an
    unbounded suffix would multiply into the full prompt chain (see the cap
    rationale at the constants).
    """
    topic = item["topic"]
    if not item.get("thesis_id"):
        return topic
    parts: list[str] = []
    thesis = await db.query_one(
        "SELECT name_zh, current_view FROM theses WHERE id = ?", (item["thesis_id"],)
    )
    if thesis:
        name = str(thesis["name_zh"] or "")[:_CTX_NAME_CAP]
        label = f"{item['thesis_id']}：{name}（当前观点 {thesis['current_view']}）"
    else:
        label = str(item["thesis_id"])
    parts.append(f"所属论点 {label}")
    if item.get("security_id"):
        sec = await db.query_one(
            "SELECT name_zh, name_en FROM securities WHERE id = ?", (item["security_id"],)
        )
        name = str((sec and (sec["name_zh"] or sec["name_en"])) or "")[:_CTX_NAME_CAP]
        parts.append(f"聚焦标的 {item['security_id']}{f'（{name}）' if name else ''}")
    if item.get("question"):
        parts.append(f"核心研究问题：{item['question']}")
    suffix = "；".join(parts)
    if len(suffix) > _CTX_SUFFIX_CAP:
        suffix = suffix[: _CTX_SUFFIX_CAP - 1] + "…"
    return f"{topic}【论点上下文】{suffix}"


async def _run_item(item: dict[str, Any]) -> None:
    from . import workflows  # deferred: keeps this module importable standalone

    item_id, topic = item["id"], item["topic"]
    try:
        run = _as_dict(await workflows.run_workflow_and_wait(
            "research",
            variables={
                "TOPIC": await _topic_with_context(item), "WORK_DATE": work_date(),
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
        # dedup_key lands in the log so the structured cooldown rail can match
        # completions; topic-only items keep it NULL (old rail untouched)
        await db.execute(
            "INSERT INTO research_log (topic, run_id, summary, completed_at, work_date, dedup_key) "
            "VALUES (?,?,?,?,?,?)",
            (topic, run_id, summary, bus.now_iso(), work_date(), item.get("dedup_key")),
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
