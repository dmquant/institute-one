"""Whiteboard — autonomous collaborative boards.

A board = topic + question. Analysts take turns writing cards (one card = one
analyst's take, a Markdown file in the board's session workspace). After each
card a HANDOFF picks the next analyst + question; the board completes at
max_cards or when the handoff says stop.

Driven by the scheduler: ``kickoff()`` opens boards from the topic pool,
``tick()`` advances running boards. Both never raise. All state transitions
use conditional claims so overlapping ticks can never double-run a card.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from .analysts import get_analyst, roster
from .prompts import (
    build_analyst_prompt,
    date_anchor,
    extract_summary,
    previous_steps_block,
    work_date,
)

log = logging.getLogger("institute.whiteboard")

MAX_ACTIVE_BOARDS = 2
DEFAULT_MAX_CARDS = 5
HANDOFF_TIMEOUT_S = 300

# Cards being driven by THIS process. A 'running' card not in here was orphaned
# by a restart (executor.recover_orphans already failed its task).
_active_cards: set[str] = set()
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro: Any) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


# ---- topic pool ----------------------------------------------------------

async def add_topic(topic: str, question: str = "", source: str = "manual", score: float = 1.0) -> dict[str, Any]:
    content_hash = hashlib.sha256((topic + question).encode("utf-8")).hexdigest()[:16]
    await db.execute(
        "INSERT OR IGNORE INTO topic_pool (topic, question, source, score, status, content_hash, created_at) "
        "VALUES (?,?,?,?, 'pending', ?, ?)",
        (topic, question, source, score, content_hash, bus.now_iso()),
    )
    row = await db.query_one("SELECT * FROM topic_pool WHERE content_hash = ?", (content_hash,))
    return row or {}


async def list_topics(status: str | None = "pending") -> list[dict[str, Any]]:
    if status:
        return await db.query(
            "SELECT * FROM topic_pool WHERE status = ? ORDER BY score DESC, created_at ASC", (status,)
        )
    return await db.query("SELECT * FROM topic_pool ORDER BY score DESC, created_at ASC")


async def expire_topic(topic_id: int) -> bool:
    n = await db.execute(
        "UPDATE topic_pool SET status='expired' WHERE id=? AND status='pending'", (topic_id,)
    )
    return n > 0


# ---- board lifecycle -----------------------------------------------------

def _match_root_analyst(text: str) -> str:
    """Simple keyword routing to the analyst who opens the board."""
    if any(k in text for k in ("科技", "AI", "芯片", "半导体", "算力")):
        rid = "tech-analyst"
    elif any(k in text for k in ("医药", "创新药", "生物", "医疗")):
        rid = "healthcare-analyst"
    elif any(k in text for k in ("消费", "零售", "白酒", "食品")):
        rid = "consumer-analyst"
    elif any(k in text for k in ("大宗", "原油", "有色", "黄金", "煤", "钢", "化工")):
        rid = "commodity-analyst"
    elif any(k in text for k in ("政策", "监管", "改革")):
        rid = "policy-analyst"
    elif any(k in text for k in ("债", "信用", "转债")):
        rid = "fixed-income-analyst"
    elif any(k in text for k in ("宏观", "利率", "汇率", "通胀")):
        rid = "macro-analyst"
    else:
        rid = "equity-analyst"
    if get_analyst(rid) is None:  # roster drift safety net
        everyone = roster()
        rid = everyone[0].id if everyone else rid
    return rid


async def _create_board_session(board_id: str, topic: str) -> str:
    title = f"WB {topic}"
    try:
        from . import sessions  # lazy: parallel module, soft coupling

        sess = await sessions.create_session(kind="whiteboard", title=title)
        return sess["id"] if isinstance(sess, dict) else sess.id
    except Exception:  # noqa: BLE001 - fall back to a direct row so boards still work
        log.warning("sessions.create_session unavailable; inserting session row directly", exc_info=True)
        session_id = uuid.uuid4().hex[:12]
        ws = get_settings().workspaces_dir / "whiteboard" / board_id
        ws.mkdir(parents=True, exist_ok=True)
        now = bus.now_iso()
        await db.execute(
            "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (session_id, title, "whiteboard", str(ws), now, now),
        )
        return session_id


async def _board_workspace(board: dict[str, Any]) -> Path:
    row = None
    if board.get("session_id"):
        row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (board["session_id"],))
    ws = Path(row["workspace_dir"]) if row and row["workspace_dir"] else (
        get_settings().workspaces_dir / "whiteboard" / board["id"]
    )
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def _open_board(topic: str, question: str = "", max_cards: int = DEFAULT_MAX_CARDS) -> dict[str, Any]:
    board_id = uuid.uuid4().hex[:12]
    session_id = await _create_board_session(board_id, topic)
    root = _match_root_analyst(f"{topic} {question}")
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, session_id, work_date, created_at, updated_at) "
        "VALUES (?,?,?,'active',?,?,?,?,?)",
        (board_id, topic, question, max_cards, session_id, work_date(), now, now),
    )
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, created_at) "
        "VALUES (?,?,1,?,'pending',?,?)",
        (uuid.uuid4().hex[:12], board_id, root, question, now),
    )
    await bus.emit("whiteboard.board_opened", "board", board_id, {"topic": topic})
    board = await db.query_one("SELECT * FROM whiteboard_boards WHERE id = ?", (board_id,))
    return board or {"id": board_id}


async def kickoff() -> str | None:
    """Open one board from the topic pool if capacity allows. Never raises."""
    try:
        row = await db.query_one("SELECT COUNT(*) AS n FROM whiteboard_boards WHERE status='active'")
        if row and row["n"] >= MAX_ACTIVE_BOARDS:
            return None
        top = await db.query_one(
            "SELECT * FROM topic_pool WHERE status='pending' ORDER BY score DESC, created_at ASC LIMIT 1"
        )
        if top is None:
            return None
        claimed = await db.execute(
            "UPDATE topic_pool SET status='used' WHERE id=? AND status='pending'", (top["id"],)
        )
        if not claimed:
            return None
        board = await _open_board(top["topic"], top["question"], max_cards=DEFAULT_MAX_CARDS)
        log.info("kicked off board %s: %s", board["id"], top["topic"])
        return board["id"]
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("whiteboard kickoff failed")
        return None


async def create_board(topic: str, question: str = "", max_cards: int = DEFAULT_MAX_CARDS) -> dict[str, Any]:
    board = await _open_board(topic, question, max_cards=max_cards)
    return await get_board(board["id"]) or board


# ---- the tick ------------------------------------------------------------

async def tick() -> None:
    """Advance every active board by at most one step. Never raises."""
    try:
        boards = await db.query("SELECT * FROM whiteboard_boards WHERE status='active' ORDER BY created_at")
        for board in boards:
            try:
                await _tick_board(board)
            except Exception:  # noqa: BLE001
                log.exception("tick failed for board %s", board["id"])
    except Exception:  # noqa: BLE001
        log.exception("whiteboard tick failed")


async def _tick_board(board: dict[str, Any]) -> None:
    cards = await db.query(
        "SELECT * FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board["id"],)
    )
    for c in cards:
        if c["status"] != "running":
            continue
        if c["id"] in _active_cards:
            return  # in flight, wait
        # orphaned by a restart: fail it and keep the board moving
        n = await db.execute(
            "UPDATE whiteboard_cards SET status='failed', finished_at=? WHERE id=? AND status='running'",
            (bus.now_iso(), c["id"]),
        )
        if n:
            log.warning("card %s on board %s orphaned by restart; marked failed", c["id"], board["id"])
            if c["idx"] < board["max_cards"]:
                await _handoff(board)
        return

    pending = next((c for c in cards if c["status"] == "pending"), None)
    if pending is not None:
        claimed = await db.execute(
            "UPDATE whiteboard_cards SET status='running' WHERE id=? AND status='pending'",
            (pending["id"],),
        )
        if claimed:
            _active_cards.add(pending["id"])
            _spawn_bg(_run_card(board, {**pending, "status": "running"}))
        return

    if cards:  # nothing pending, nothing running -> the board is done
        await _finalize(board)


# ---- card execution ------------------------------------------------------

async def _run_card(board: dict[str, Any], card: dict[str, Any]) -> None:
    settings = get_settings()
    board_id, card_id, idx = board["id"], card["id"], card["idx"]
    try:
        analyst = get_analyst(card["analyst_id"])
        if analyst is None:
            await db.execute(
                "UPDATE whiteboard_cards SET status='failed', finished_at=? WHERE id=? AND status='running'",
                (bus.now_iso(), card_id),
            )
            return

        ws = await _board_workspace(board)
        prev = await db.query(
            "SELECT * FROM whiteboard_cards WHERE board_id=? AND status='completed' AND idx<? ORDER BY idx",
            (board_id, idx),
        )
        pairs: list[tuple[str, str]] = []
        for p in prev:
            pa = get_analyst(p["analyst_id"])
            pairs.append((f"card {p['idx']} · {pa.name if pa else p['analyst_id']}", p["summary"] or ""))
        context = previous_steps_block(pairs)

        output_file = f"card-{idx:02d}-{analyst.id}.md"
        question = card["question"] or board["question"] or board["topic"]
        task_text = (
            "白板协作任务（多位分析师接力研讨）。\n"
            f"主题：{board['topic']}\n"
            f"总问题：{board['question'] or '（无，围绕主题展开）'}\n"
            f"本卡片要回答的问题：{question}\n"
            "协作要求：先明确表态你同意或反驳前面哪位同事的哪一个观点（你是第一张卡片则直接给出开局判断），"
            "再展开你自己的分析，最后以「## 核心结论」收尾。"
        )
        prompt = build_analyst_prompt(
            analyst, task_text,
            context_blocks=[context] if context else None,
            output_file=output_file,
        )
        task = await executor.submit(
            analyst.hand or settings.default_hand, prompt,
            source="whiteboard", model=analyst.model,
            session_id=board["session_id"], workspace=ws,
        )

        if task.status == "completed":
            content = task.output
            try:
                out_path = ws / output_file
                if out_path.exists():
                    content = out_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                log.warning("could not read %s; using task output", output_file)
            summary = extract_summary(content or "")
            n = await db.execute(
                "UPDATE whiteboard_cards SET status='completed', summary=?, output_file=?, task_id=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (summary, output_file, task.id, bus.now_iso(), card_id),
            )
            if n:
                await bus.emit(
                    "whiteboard.card_completed", "card", card_id,
                    {"board_id": board_id, "idx": idx, "analyst_id": analyst.id},
                )
        else:
            # a failed card still counts toward max_cards; the board continues
            await db.execute(
                "UPDATE whiteboard_cards SET status='failed', task_id=?, finished_at=? WHERE id=? AND status='running'",
                (task.id, bus.now_iso(), card_id),
            )

        await db.execute(
            "UPDATE whiteboard_boards SET updated_at=? WHERE id=?", (bus.now_iso(), board_id)
        )
        if idx < board["max_cards"]:
            await _handoff(board)
    except Exception:  # noqa: BLE001 - runs as a bare asyncio task
        log.exception("card %s on board %s crashed", card_id, board_id)
        try:
            await db.execute(
                "UPDATE whiteboard_cards SET status='failed', finished_at=? WHERE id=? AND status='running'",
                (bus.now_iso(), card_id),
            )
        except Exception:  # noqa: BLE001
            log.exception("could not mark card %s failed", card_id)
    finally:
        _active_cards.discard(card_id)


# ---- handoff (constrained pick) -------------------------------------------

def _parse_handoff(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            data, _ = decoder.raw_decode(text[idx:])
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        idx = text.find("{", idx + 1)
    raise ValueError("no JSON object in handoff output")


def _next_in_rotation(last_analyst_id: str | None) -> str:
    ids = [a.id for a in roster()]
    if not ids:
        return "equity-analyst"
    if last_analyst_id in ids and len(ids) > 1:
        return ids[(ids.index(last_analyst_id) + 1) % len(ids)]
    return ids[0]


async def _handoff(board: dict[str, Any]) -> None:
    """Pick the next analyst + question, or stop. Falls back deterministically."""
    settings = get_settings()
    fresh = await db.query_one("SELECT * FROM whiteboard_boards WHERE id = ?", (board["id"],))
    if fresh is None or fresh["status"] != "active":
        return
    cards = await db.query(
        "SELECT * FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board["id"],)
    )
    if any(c["status"] in ("pending", "running") for c in cards):
        return  # next card already queued
    next_idx = (cards[-1]["idx"] + 1) if cards else 1
    if next_idx > fresh["max_cards"]:
        return
    last_analyst = cards[-1]["analyst_id"] if cards else None

    ids = [a.id for a in roster()]
    catalog = "\n".join(f"- {a.id} · {a.name} · {a.focus}" for a in roster())
    summaries = "\n".join(
        f"[card {c['idx']} · {c['analyst_id']} · {c['status']}] {(c['summary'] or '（无摘要）')[:400]}"
        for c in cards
    )
    prompt = (
        f"{date_anchor()}\n\n"
        "你是研究所白板的主持人，负责决定下一张卡片。\n\n"
        f"分析师名册（封闭目录，analyst_id 只能从下列 id 中选择）：\n{catalog}\n\n"
        f"白板主题：{fresh['topic']}\n"
        f"总问题：{fresh['question'] or '（无）'}\n\n"
        f"已有卡片摘要：\n{summaries}\n\n"
        "请决定下一张卡片：选一位最合适的分析师（优先未发言或能提出不同视角的人），"
        "并给出该卡片要回答的具体问题。若讨论已收敛、不需要更多卡片，则把 stop 设为 true。\n"
        "只输出一段严格 JSON，不要任何其他文字：\n"
        '{"analyst_id": "<上述 id 之一>", "question": "<下一张卡片要回答的具体问题>", "stop": false}'
    )

    stop = False
    try:
        task = await executor.submit(
            settings.default_hand, prompt,
            source="whiteboard", session_id=board["session_id"], timeout_s=HANDOFF_TIMEOUT_S,
        )
        if task.status != "completed":
            raise ValueError(f"handoff task {task.id} ended {task.status}")
        data = _parse_handoff(task.output or "")
        if data.get("analyst_id") not in ids:
            raise ValueError(f"analyst_id {data.get('analyst_id')!r} not in roster")
        analyst_id = data["analyst_id"]
        question = str(data.get("question") or "").strip() or fresh["question"]
        stop = str(data.get("stop", False)).lower() == "true"
    except Exception as exc:  # noqa: BLE001 - ANY failure -> deterministic fallback
        log.warning("handoff fallback on board %s: %s", board["id"], exc)
        analyst_id = _next_in_rotation(last_analyst)
        question = fresh["question"]

    if stop:
        log.info("board %s: handoff says stop after %d cards", board["id"], len(cards))
        return

    exists = await db.query_one(
        "SELECT id FROM whiteboard_cards WHERE board_id=? AND idx=?", (board["id"], next_idx)
    )
    if exists:
        return
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, created_at) "
        "VALUES (?,?,?,?,'pending',?,?)",
        (uuid.uuid4().hex[:12], board["id"], next_idx, analyst_id, question or "", bus.now_iso()),
    )
    await db.execute(
        "UPDATE whiteboard_boards SET updated_at=? WHERE id=?", (bus.now_iso(), board["id"])
    )


# ---- finalize --------------------------------------------------------------

async def _finalize(board: dict[str, Any]) -> None:
    claimed = await db.execute(
        "UPDATE whiteboard_boards SET status='completed', updated_at=? WHERE id=? AND status='active'",
        (bus.now_iso(), board["id"]),
    )
    if not claimed:
        return
    cards = await db.query(
        "SELECT * FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board["id"],)
    )
    try:
        ws = await _board_workspace(board)
        lines = [
            f"# 白板：{board['topic']}",
            "",
            f"- 问题：{board['question'] or '—'}",
            f"- 日期：{board['work_date']}",
            f"- 卡片数：{len(cards)}",
            "",
            "| # | 分析师 | 状态 | 摘要 |",
            "|---|--------|------|------|",
        ]
        for c in cards:
            a = get_analyst(c["analyst_id"])
            name = a.name if a else c["analyst_id"]
            summary = (c["summary"] or "").replace("\n", " ").replace("|", "\\|")[:200]
            file_ref = f" → [{c['output_file']}]({c['output_file']})" if c["output_file"] else ""
            lines.append(f"| {c['idx']} | {name} | {c['status']} | {summary}{file_ref} |")
        (ws / "_board.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.exception("could not write _board.md for board %s", board["id"])

    try:
        from .archive import snapshot_session  # lazy: parallel module

        await snapshot_session(board["session_id"], "whiteboard", board["id"])
    except Exception:  # noqa: BLE001
        log.exception("archive snapshot failed for board %s", board["id"])

    await bus.emit(
        "whiteboard.board_completed", "board", board["id"],
        {"topic": board["topic"], "session_id": board["session_id"], "cards": len(cards)},
    )
    log.info("board %s completed with %d cards", board["id"], len(cards))


# ---- queries ---------------------------------------------------------------

async def list_boards(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT b.*, (SELECT COUNT(*) FROM whiteboard_cards c WHERE c.board_id = b.id) AS n_cards "
        "FROM whiteboard_boards b"
    )
    params: list[Any] = []
    if status:
        sql += " WHERE b.status = ?"
        params.append(status)
    sql += " ORDER BY b.updated_at DESC LIMIT ?"
    params.append(min(limit, 200))
    return await db.query(sql, params)


async def get_board(board_id: str) -> dict[str, Any] | None:
    board = await db.query_one("SELECT * FROM whiteboard_boards WHERE id = ?", (board_id,))
    if board is None:
        return None
    board["cards"] = await db.query(
        "SELECT * FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board_id,)
    )
    return board


async def stop_board(board_id: str) -> bool:
    n = await db.execute(
        "UPDATE whiteboard_boards SET status='stopped', updated_at=? WHERE id=? AND status='active'",
        (bus.now_iso(), board_id),
    )
    return n > 0
