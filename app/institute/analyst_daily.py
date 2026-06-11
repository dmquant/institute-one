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
from .analysts import Analyst, get_analyst, roster
from .prompts import build_analyst_prompt, work_date
from .research import parse_followups

log = logging.getLogger("institute.analyst_daily")

SOURCE = "analyst-daily"
SKIP_CATEGORIES = {"ops"}          # editors compile; they don't file field reports
MAX_TOPICS_PER_DAILY = 2
MAX_MAILS_PER_DAILY = 1
ROTATION_HANDS = ("claude", "codex", "gemini")

_background: set[asyncio.Task] = set()


# ---- per-day state ---------------------------------------------------------

def _guard_key(date: str | None = None) -> str:
    return f"analyst_daily:{date or work_date()}"


async def _get_record(date: str | None = None) -> dict[str, Any]:
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (_guard_key(date),))
    if row is None:
        return {}
    try:
        return json.loads(row["value"])
    except ValueError:
        return {}


async def _mark(analyst_id: str, status: str) -> None:
    record = await _get_record()
    record[analyst_id] = status
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_guard_key(), json.dumps(record, ensure_ascii=False)),
    )


async def _today_session() -> dict[str, Any]:
    """One shared session per day holds every analyst's note."""
    from . import sessions

    title = f"分析师日报 {work_date()}"
    row = await db.query_one(
        "SELECT * FROM sessions WHERE kind = 'daily' AND title = ? LIMIT 1", (title,)
    )
    if row:
        return row
    return await sessions.create_session(title, kind="daily")


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
    if analyst.hand:
        return analyst.hand
    registry = get_registry()
    available = [h for h in ROTATION_HANDS if registry.is_available(h)]
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
    prompt = build_analyst_prompt(analyst, _daily_task(analyst, filename), output_file=filename)

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
    """Scheduler entry: run every working analyst's daily, skipping done ones. Never raises."""
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

    results = await asyncio.gather(*(_safe(a, i) for i, a in enumerate(pending)))
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
