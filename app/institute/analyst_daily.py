"""Per-analyst daily reports — the institute's recursion seed.

Every working analyst (category != ops) writes a daily observation note into a
shared per-day session. Each note ends with the standard follow-ups JSON block;
applying it feeds the whiteboard topic pool and opens mailbox threads to other
analysts — which keeps boards and conversations spinning without operator input.

Recursion is bounded by design: dailies emit at most 2 topics + 1 mail each,
the topic pool dedups by content hash, active boards are capped, and mailbox
replies / whiteboard cards do NOT generate further follow-ups.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..hands.registry import get_registry
from ..router import executor
from ..util import new_id
from .analysts import Analyst, get_analyst, roster
from .claims import (
    claim_admin_state,
    heartbeat_admin_state,
    lease_stale_checker,
    release_admin_state,
)
from .prompts import work_date
from .research import parse_followups

log = logging.getLogger("institute.analyst_daily")

SOURCE = "analyst-daily"
SKIP_CATEGORIES = {"ops"}          # editors compile; they don't file field reports
MAX_TOPICS_PER_DAILY = 2
MAX_MAILS_PER_DAILY = 1
ROTATION_HANDS = ("claude", "codex", "gemini")
# The sweep claim is renewed by a heartbeat while run_all is alive, so the
# lease does NOT need to exceed the worst-case sweep duration (which is
# unbounded anyway: with one available hand the per-hand mutex serializes all
# analysts — 9 x (1800s timeout + 30s belt) ≈ 4h35m today, and it grows with
# the roster). The lease only bounds how long a HARD-KILLED sweep (heartbeat
# stopped, finally never ran) wedges the day: lease/heartbeat = 6 missed
# beats before a live-but-unlucky owner could be taken over.
SWEEP_LEASE_S = 30 * 60
SWEEP_HEARTBEAT_S = 5 * 60

_background: set[asyncio.Task] = set()

# per-loop lock for _today_session's check-then-create (asyncio.Lock binds to
# the running loop on first acquire, so tests — one loop per test — get a
# fresh lock instead of a cross-loop RuntimeError)
_session_lock: asyncio.Lock | None = None
_session_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_session_lock() -> asyncio.Lock:
    global _session_lock, _session_lock_loop
    loop = asyncio.get_running_loop()
    if _session_lock is None or _session_lock_loop is not loop:
        _session_lock = asyncio.Lock()
        _session_lock_loop = loop
    return _session_lock


# ---- per-day state ---------------------------------------------------------
# One admin_state row per analyst per day (analyst_daily:<date>:<analyst_id>).
# A single-row UPSERT is atomic, so concurrent finishes under asyncio.gather
# can't erase each other's marks (the old one-blob-per-day read-modify-write
# lost updates). _get_record still merges a legacy per-day blob, if present,
# so an upgrade mid-day doesn't forget already-completed analysts.

def _guard_prefix(date: str | None = None) -> str:
    return f"analyst_daily:{date or work_date()}"


def _guard_key(analyst_id: str, date: str | None = None) -> str:
    return f"{_guard_prefix(date)}:{analyst_id}"


async def _get_record(date: str | None = None) -> dict[str, Any]:
    """Aggregate {analyst_id: status} for the day; per-analyst rows win over the legacy blob."""
    prefix = _guard_prefix(date)
    record: dict[str, Any] = {}
    legacy = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (prefix,))
    if legacy is not None:
        try:
            data = json.loads(legacy["value"])
            if isinstance(data, dict):
                record.update(data)
        except ValueError:
            pass
    # literal prefix compare — GLOB/LIKE would treat metacharacters in an
    # externally supplied date (e.g. "2026-07-??" via the status API) as a
    # pattern and cross-match other days' rows
    rows = await db.query(
        "SELECT key, value FROM admin_state WHERE substr(key, 1, ?) = ?",
        (len(prefix) + 1, prefix + ":"),
    )
    for row in rows:
        try:
            record[row["key"][len(prefix) + 1:]] = json.loads(row["value"])
        except ValueError:
            record[row["key"][len(prefix) + 1:]] = row["value"]
    return record


async def _mark(analyst_id: str, status: str) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_guard_key(analyst_id), json.dumps(status, ensure_ascii=False)),
    )


async def _today_session() -> dict[str, Any]:
    """One shared session per day holds every analyst's note.

    check-then-create runs under a per-loop lock: a single sweep's
    asyncio.gather used to race every run_one through the SELECT window and
    create one session per analyst (F1-1), breaking the one-shared-session-
    per-day invariant. In-process mutual exclusion is sufficient — this is a
    single-process system and sessions has no UNIQUE(kind, title) to lean on
    (adding one now could wedge migration on already-duplicated legacy rows).
    """
    from . import sessions

    title = f"分析师日报 {work_date()}"
    async with _get_session_lock():
        row = await db.query_one(
            "SELECT * FROM sessions WHERE kind = 'daily' AND title = ? LIMIT 1", (title,)
        )
        if row:
            return row
        return await sessions.create_session(title, kind="daily")


# ---- sweep mutual exclusion --------------------------------------------------
# run_all used to double-run wholesale (F1-1 top1): the 19:00 cron overlapping
# a manual run-now (or two clicks) ran every analyst twice — 2x quota — and
# minted duplicate sessions. The shared conditional-claim idiom (claims.py) on
# an admin_state row makes one winner; the loser skips. While the winner runs,
# a heartbeat task renews claimed_at every SWEEP_HEARTBEAT_S, so a LIVE sweep
# is never taken over no matter how long it runs (REVIEW-B1 M1); only a
# hard-killed sweep (heartbeat stopped, finally never ran) goes stale and is
# taken over after SWEEP_LEASE_S. Escape hatches while a claim is held: the
# per-analyst force endpoint bypasses the sweep entirely; deleting the row
# frees it.

def _sweep_key(date: str | None = None) -> str:
    # deliberately NOT under _guard_prefix(): _get_record scans the
    # "analyst_daily:<date>:*" namespace and would read a sweep claim row
    # as per-analyst status for an analyst literally named "sweep"
    return f"analyst_daily_sweep:{date or work_date()}"


def _claim_token(owner: str) -> str:
    return json.dumps({"owner": owner, "claimed_at": bus.now_iso()})


async def _claim_sweep() -> tuple[str, str] | None:
    """Conditionally claim today's sweep; (key, token) for the winner, else None.

    The shared idiom (claims.claim_admin_state) picks one winner by rowcount
    and takes over stale claims via exact-value CAS. SWEEP_LEASE_S is read
    per call so tests can shrink it.
    """
    return await claim_admin_state(
        _sweep_key(),
        make_token=lambda: _claim_token(new_id()),
        is_stale=lease_stale_checker(SWEEP_LEASE_S, label="sweep claim"),
    )


async def _heartbeat_loop(key: str, holder: dict[str, str], stop: asyncio.Event) -> None:
    """Renew the sweep claim every SWEEP_HEARTBEAT_S until told to stop
    (claims.heartbeat_admin_state carries the renewal/loss semantics)."""
    await heartbeat_admin_state(
        key, holder, stop, interval_s=SWEEP_HEARTBEAT_S, renew_token=_claim_token,
    )


async def _release_sweep(key: str, token: str) -> None:
    # CAS delete — only our own claim (claims.release_admin_state)
    await release_admin_state(key, token)


# ---- prompt -----------------------------------------------------------------

def _catalog_excluding(analyst_id: str) -> str:
    return "\n".join(
        f"- {a.id}：{a.name}（{a.focus}）" for a in roster()
        if a.id != analyst_id and a.category not in SKIP_CATEGORIES
    )


def _daily_task(analyst: Analyst, filename: str) -> str:
    return (
        f"撰写你今天的《观察日报》。围绕你的覆盖领域（{analyst.focus}），"
        "写 3–5 条今天最重要的观察或变化。每一条包含：事实（必须给出来源链接或出处）、"
        "你的判断（明确标注为观点）、对市场或具体标的的影响。文末加一节「明日关注」（1–3 条）。\n\n"
        "然后提出后续跟进（这是日报的固定动作）：\n"
        f"1. 【白板议题】0–{MAX_TOPICS_PER_DAILY} 个值得多位分析师协作辩论的开放性问题（只提真正有分歧或跨领域的问题，没有就留空）。\n"
        f"2. 【信箱追问】0–{MAX_MAILS_PER_DAILY} 个需要某位**其他**分析师单独回答的追问（不要写给自己）。分析师从名册选择（用 id）：\n"
        f"{_catalog_excluding(analyst.id)}\n\n"
        f"文件 {filename} 的格式要求：正文在前；结尾附「## 后续跟进」一节，"
        "先用中文简述跟进理由，最后必须是一个 json 代码块（```json ... ```），严格遵循：\n"
        '{"whiteboard_topics": [{"topic": "主题", "question": "具体问题"}], '
        '"mailbox_followups": [{"analyst_id": "名册中的id", "subject": "追问标题", "body": "追问内容"}]}\n'
        "没有跟进项时对应数组留空 []。"
    )


def _pick_hand(analyst: Analyst, index: int) -> str:
    registry = get_registry()
    available = [h for h in ROTATION_HANDS if registry.is_available(h)]
    # opt-in weighted pick (settings.enable_hand_weights, default False): reorders
    # the SAME rotation pool by hand_weights 'daily' scope; off = byte-identical
    # rotation. An explicit analyst hand always wins (weights never override it).
    picked = registry.pick_weighted("daily", explicit=analyst.hand, pool=available)
    if picked:
        return picked
    if available:
        return available[index % len(available)]
    return get_settings().default_hand


# ---- follow-up application ---------------------------------------------------

async def _apply_followups(analyst: Analyst, ws: Path, filename: str) -> tuple[int, int]:
    path = ws / filename
    if not path.is_file():
        return 0, 0
    followups = parse_followups(path.read_text(encoding="utf-8", errors="replace"))
    topics = followups["whiteboard_topics"][:MAX_TOPICS_PER_DAILY]
    mails = [
        m for m in followups["mailbox_followups"]
        if m["analyst_id"] != analyst.id and get_analyst(m["analyst_id"]) is not None
    ][:MAX_MAILS_PER_DAILY]

    from . import mailbox, whiteboard  # lazy: domain peers

    n_topics = 0
    for t in topics:
        try:
            await whiteboard.add_topic(t["topic"], question=t["question"], source=SOURCE, score=1.2)
            n_topics += 1
        except Exception:  # noqa: BLE001
            log.exception("daily follow-up topic failed: %s", t["topic"])

    n_mails = 0
    for m in mails:
        subject = f"【日报跟进】{analyst.name}：{m['subject'] or '追问'}"
        body = f"来自 {analyst.name}（{analyst.id}）{work_date()} 观察日报的追问：\n\n{m['body']}"
        try:
            await mailbox.create_thread(subject, m["analyst_id"], body)
            n_mails += 1
        except Exception:  # noqa: BLE001
            log.exception("daily follow-up mail failed for %s", m["analyst_id"])

    return n_topics, n_mails


# ---- runners -----------------------------------------------------------------

async def run_one(analyst_id: str, *, force: bool = False, rotation_index: int = 0) -> dict[str, Any]:
    """Run one analyst's daily report end-to-end. Raises ValueError for unknown ids."""
    analyst = get_analyst(analyst_id)
    if analyst is None:
        raise ValueError(f"unknown analyst '{analyst_id}'")
    if not force:
        record = await _get_record()
        if record.get(analyst_id) == "completed":
            return {"analyst_id": analyst_id, "skipped": "already completed today"}

    session = await _today_session()
    ws = Path(session["workspace_dir"])
    filename = f"{analyst.id}.md"
    from . import memory
    prompt = await memory.prompt_with_memory(
        analyst, _daily_task(analyst, filename), output_file=filename,
    )

    task = await executor.submit(
        _pick_hand(analyst, rotation_index), prompt,
        source=SOURCE, model=analyst.model, session_id=session["id"], workspace=ws,
    )

    if task.status != "completed":
        await _mark(analyst_id, "failed")
        await bus.emit("analyst_daily.failed", "analyst", analyst_id, {
            "date": work_date(), "session_id": session["id"], "task_id": task.id,
            "status": task.status, "error": task.error,
        })
        return {"analyst_id": analyst_id, "status": task.status, "task_id": task.id}

    n_topics, n_mails = 0, 0
    try:
        n_topics, n_mails = await _apply_followups(analyst, ws, filename)
    except Exception:  # noqa: BLE001 - follow-ups never fail the daily
        log.exception("daily follow-ups failed for %s", analyst_id)

    await _mark(analyst_id, "completed")
    await bus.emit("analyst_daily.completed", "analyst", analyst_id, {
        "date": work_date(), "session_id": session["id"], "task_id": task.id,
        "file": filename, "whiteboard_topics": n_topics, "mailbox_threads": n_mails,
    })
    log.info("analyst daily done: %s (+%d topics, +%d mails)", analyst_id, n_topics, n_mails)
    return {
        "analyst_id": analyst_id, "status": "completed", "task_id": task.id,
        "whiteboard_topics": n_topics, "mailbox_threads": n_mails,
    }


async def run_all() -> dict[str, Any]:
    """Scheduler entry: run every working analyst's daily, skipping done ones. Never raises.

    Guarded by the per-day sweep claim: the 19:00 cron overlapping a manual
    run-now (or two overlapping run-nows) gets ONE running sweep; the loser
    returns a skip marker instead of double-spending quota (F1-1).
    """
    claim = await _claim_sweep()
    if claim is None:
        log.info("analyst dailies sweep already running today; skipping duplicate")
        return {"date": work_date(), "ran": 0, "skipped": "sweep already running"}
    key, token = claim
    holder = {"token": token}
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_loop(key, holder, stop_heartbeat))
    try:
        record = await _get_record()
        pending = [
            a for a in roster()
            if a.category not in SKIP_CATEGORIES and record.get(a.id) != "completed"
        ]
        if not pending:
            return {"date": work_date(), "ran": 0, "skipped": "all done"}

        log.info("analyst dailies starting: %s", [a.id for a in pending])

        async def _safe(analyst: Analyst, i: int) -> dict[str, Any]:
            try:
                return await run_one(analyst.id, rotation_index=i)
            except Exception as exc:  # noqa: BLE001
                log.exception("analyst daily crashed for %s", analyst.id)
                return {"analyst_id": analyst.id, "status": "crashed", "error": str(exc)[:200]}

        # Bounded fan-out: the executor caps each model call, but a wedged
        # hand mutex / lost driver would hang this gather forever while the
        # heartbeat keeps renewing the claim ("alive but not working"). The
        # budget assumes full serialization on one hand plus margin; a timeout
        # cancels the stragglers (their executor drivers mark rows cancelled).
        budget_s = len(pending) * (get_settings().default_timeout_s + 120) + 60
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(_safe(a, i) for i, a in enumerate(pending))),
                timeout=budget_s,
            )
        except asyncio.TimeoutError:
            log.error("analyst dailies sweep exceeded its %.0fs budget; stragglers cancelled", budget_s)
            return {"date": work_date(), "ran": len(pending),
                    "error": f"sweep timed out after {budget_s:.0f}s"}
        summary = {
            "date": work_date(),
            "ran": len(results),
            "completed": sum(1 for r in results if r.get("status") == "completed"),
            "results": results,
        }
        await bus.emit("analyst_daily.sweep_completed", "daily", work_date(), {
            "ran": summary["ran"], "completed": summary["completed"],
        })
        return summary
    finally:
        stop_heartbeat.set()
        try:
            await heartbeat  # brief: it exits at its next stop-check
        except Exception:  # noqa: BLE001 - the release below must still run
            log.exception("sweep heartbeat task errored during shutdown")
        await _release_sweep(key, holder["token"])


def spawn_all() -> None:
    """Fire-and-forget run_all (API hook)."""
    t = asyncio.create_task(run_all())
    _background.add(t)
    t.add_done_callback(_background.discard)


def spawn_one(analyst_id: str, *, force: bool = True) -> None:
    t = asyncio.create_task(run_one(analyst_id, force=force))
    _background.add(t)
    t.add_done_callback(_background.discard)


async def status(date: str | None = None) -> dict[str, Any]:
    record = await _get_record(date)
    title = f"分析师日报 {date or work_date()}"
    session = await db.query_one(
        "SELECT id, workspace_dir FROM sessions WHERE kind = 'daily' AND title = ? LIMIT 1", (title,)
    )
    working = [a.id for a in roster() if a.category not in SKIP_CATEGORIES]
    return {
        "date": date or work_date(),
        "analysts": {aid: record.get(aid, "pending") for aid in working},
        "session_id": session["id"] if session else None,
    }
