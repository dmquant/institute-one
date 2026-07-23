"""Analyst standing memory — versioned compacts (ROADMAP Phase 2).

The flywheel: analysts stop restarting from zero. ``compact_one`` collects an
analyst's output since the previous memory version (analyst-daily tasks rows,
whiteboard cards, mailbox replies, plus the paper-book outcomes of the
analyst's own attributed calls — each source capped so the prompt cannot
explode), asks the model to compress it into a dense standing memory, and
stores the result as a new ``analyst_memory`` version row. ``memory_block``
renders the latest version as a context block; ``prompt_with_memory`` is THE
prompt-assembly entrypoint (M8-005) — it injects that block into
``prompts.build_analyst_prompt`` in exactly one place, so every analyst prompt
that opts into standing memory carries it between persona and task.

Exactly-once consumption (REVIEW-B3 B3-H1/B3-M3): material is NOT selected by
timestamp — second-precision timestamps plus a strict ``>`` lose whatever
arrives while the model is running or within the same second as the version
row. Instead each source is consumed by a monotonic integer cursor (events.id
for dailies, whiteboard cards and paper-book outcomes, mailbox_messages.id
for replies), ascending, oldest first. The version row stores the per-source high-water marks of the
rows it actually fetched (``cursors`` JSON), so the next compact resumes
strictly after them: material landing mid-compact has a higher id and is
picked up next round, and rows beyond a per-source LIMIT stay below the
high-water mark and are back-filled next round instead of being dropped.
A fetched row whose body proves unreadable is still consumed (there is
nothing of it to compress — skipping it forever loses nothing).

Concurrency (M8-005): a per-analyst conditional claim on an admin_state row
(``memory_compact:<analyst_id>``) is taken BEFORE the model call — two
overlapping compacts burn the model at most once; the loser skips without
collecting material (same claim/lease idiom as the analyst_daily sweep, minus
the heartbeat: one compact is ONE bounded model call, so a fixed lease longer
than the hand timeout covers it). A hard-killed compact frees after
COMPACT_LEASE_S via CAS takeover of the stale token; the pre-existing
UNIQUE(analyst_id, version) INSERT OR IGNORE stays as the last-line guard, so
even a taken-over zombie that wakes up cannot double-write a version (its
model output AND cursors are discarded, and its material is re-consumed
relative to the winner's cursors).

``scheduler.py`` mounts ``compact_all`` as the gated 23:30 SGT
``memory-compact`` job.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..util import new_id
from .analysts import Analyst, get_analyst, roster
from .claims import claim_admin_state, lease_stale_checker, release_admin_state
from .prompts import build_analyst_prompt, work_date

log = logging.getLogger("institute.memory")

SOURCE = "memory"
SKIP_CATEGORIES = {"ops"}  # mirrors analyst_daily: editors compile, they don't carry field memory

# ---- budgets (per-source caps keep the compact prompt bounded) --------------
MEMORY_MAX_CHARS = 6000     # target length the model is told to respect
MEMORY_INJECT_CAP = 8000    # hard cap on compact_md when injected into prompts
MAX_DAILY_ITEMS = 7
MAX_CARD_ITEMS = 10
MAX_MAIL_ITEMS = 10
MAX_OUTCOME_ITEMS = 10
DAILY_ITEM_CAP = 2000
CARD_ITEM_CAP = 800
MAIL_ITEM_CAP = 800
OUTCOME_ITEM_CAP = 300

# ---- prompt constants (verbatim-stable once written; CLAUDE.md rule 4) ------

MEMORY_COMPACT_TASK = """\
把你的常备记忆压缩到最新状态。常备记忆是你作为分析师跨日携带的工作记忆：它会注入你之后的每一次任务，是研究所「不再从零重启」的飞轮。上文语境中已依次给出你当前的常备记忆（若有）与自上一版以来你的新增产出，请把两者合并压缩成新一版常备记忆。

【压缩规则】
1. DENSITY > LENGTH：每一行都必须承载判断、证据或坐标；删除寒暄、过程性描述与重复表述。
2. 保留：仍然有效的核心观点（附最初形成日期）、关键事实与数据（附来源和时间点）、正在跟踪的线索、与其他分析师的重要分歧。
3. 强制撤回：凡已被后续事实证伪、或你已改变立场的观点，必须移入「已撤回」小节并注明一句撤回理由，禁止静默删除。
4. 结构：用「核心立场」「在跟踪」「已撤回」「教训」四个小节组织，无内容的小节写「（暂无）」。
5. 正文总长度不超过 {max_chars} 字符。宁少而密，不多而稀。

不要写文件，也不要任何前言或解释，直接输出新一版常备记忆的 Markdown 正文。\
"""

MEMORY_BLOCK_TEMPLATE = """\
## 常备记忆（第 {version} 版 · {work_date}）
（这是你此前工作的压缩记忆，可作为判断起点；若与最新事实冲突，以最新事实为准，并在下次压缩时修正。）

{compact_md}\
"""

MEMORY_MATERIAL_HEADER = "## 待压缩的新增产出（自上一版常备记忆以来；各来源均有截断）"


# ---- queries -----------------------------------------------------------------

async def latest(analyst_id: str) -> dict[str, Any] | None:
    """The newest memory version row for an analyst, or None."""
    return await db.query_one(
        "SELECT * FROM analyst_memory WHERE analyst_id = ? ORDER BY version DESC LIMIT 1",
        (analyst_id,),
    )


def _render_block(row: dict[str, Any]) -> str:
    md = (row["compact_md"] or "").strip()
    if len(md) > MEMORY_INJECT_CAP:
        md = md[:MEMORY_INJECT_CAP - 1] + "…"  # cap includes the ellipsis
    return MEMORY_BLOCK_TEMPLATE.format(
        version=row["version"], work_date=row["work_date"], compact_md=md
    )


async def memory_block(analyst_id: str) -> str:
    """Latest memory as a prompt context block. Empty string when no memory."""
    row = await latest(analyst_id)
    if row is None or not (row["compact_md"] or "").strip():
        return ""
    return _render_block(row)


async def prompt_with_memory(analyst: Analyst, task_text: str, **kwargs: Any) -> str:
    """THE analyst-prompt assembly entrypoint (M8-005): standing memory +
    ``build_analyst_prompt`` in one place.

    ``kwargs`` pass through to ``build_analyst_prompt`` (``context_blocks``,
    ``output_file``); ``memory_block`` is supplied here and must not be passed
    by callers. Byte-identical to the seven former call sites' manual
    ``memory_block=await memory_block(analyst.id)`` assembly.
    """
    return build_analyst_prompt(
        analyst, task_text,
        memory_block=await memory_block(analyst.id),
        **kwargs,
    )


# ---- material collection -------------------------------------------------------
#
# Each collector consumes its source oldest-first through a monotonic integer
# id cursor and reports the highest id it fetched. A fetched row with no
# readable body still advances the cursor via a placeholder item (there is
# nothing of it to compress, and letting it linger below the cursor would
# permanently clog the LIMIT window).

MISSING_BODY_NOTE = "（正文缺失，仅推进游标）"


def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def _daily_items(analyst_id: str, after_id: int) -> tuple[list[str], int]:
    """Analyst-daily bodies via the completed events (workspace file, else tasks.output)."""
    rows = await db.query(
        "SELECT * FROM events WHERE type = 'analyst_daily.completed' AND ref_id = ? AND id > ? "
        "ORDER BY id ASC LIMIT ?",
        (analyst_id, after_id, MAX_DAILY_ITEMS),
    )
    items: list[str] = []
    max_id = after_id
    for e in rows:
        max_id = max(max_id, e["id"])
        try:
            p = json.loads(e["payload"] or "{}")
        except ValueError:
            p = {}
        text: str | None = None
        if p.get("session_id"):
            row = await db.query_one(
                "SELECT workspace_dir FROM sessions WHERE id = ?", (str(p["session_id"]),)
            )
            if row and row["workspace_dir"]:
                f = Path(row["workspace_dir"]) / str(p.get("file") or f"{analyst_id}.md")
                try:
                    if f.is_file():
                        text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    log.warning("could not read daily file %s", f)
        if not (text and text.strip()) and p.get("task_id"):
            row = await db.query_one("SELECT output FROM tasks WHERE id = ?", (str(p["task_id"]),))
            text = (row or {}).get("output")
        date = str(p.get("date") or e["created_at"][:10])
        body = _cap(text or "", DAILY_ITEM_CAP) or MISSING_BODY_NOTE
        items.append(f"[{date} 观察日报] {body}")
    return items, max_id


async def _card_items(analyst_id: str, after_id: int) -> tuple[list[str], int]:
    """Completed whiteboard cards via their completion events (id-ordered)."""
    rows = await db.query(
        "SELECT e.id AS event_id, c.idx, c.summary, b.topic FROM events e "
        "LEFT JOIN whiteboard_cards c ON c.id = e.ref_id "
        "LEFT JOIN whiteboard_boards b ON b.id = c.board_id "
        "WHERE e.type = 'whiteboard.card_completed' "
        "AND json_extract(e.payload, '$.analyst_id') = ? AND e.id > ? "
        "ORDER BY e.id ASC LIMIT ?",
        (analyst_id, after_id, MAX_CARD_ITEMS),
    )
    items: list[str] = []
    max_id = after_id
    for r in rows:
        max_id = max(max_id, r["event_id"])
        body = _cap(r["summary"] or "", CARD_ITEM_CAP) or MISSING_BODY_NOTE
        items.append(f"[白板「{r['topic'] or '（已删除）'}」card-{r['idx'] or '?'}] {body}")
    return items, max_id


async def _mail_items(analyst_id: str, after_id: int) -> tuple[list[str], int]:
    rows = await db.query(
        "SELECT m.id AS msg_id, m.body, t.subject FROM mailbox_messages m "
        "LEFT JOIN mailbox_threads t ON t.id = m.thread_id "
        "WHERE m.author = ? AND m.kind = 'reply' AND m.id > ? "
        "ORDER BY m.id ASC LIMIT ?",
        (analyst_id, after_id, MAX_MAIL_ITEMS),
    )
    items: list[str] = []
    max_id = after_id
    for r in rows:
        max_id = max(max_id, r["msg_id"])
        body = _cap(r["body"] or "", MAIL_ITEM_CAP) or MISSING_BODY_NOTE
        items.append(f"[信箱「{r['subject'] or '（已删除）'}」回复] {body}")
    return items, max_id


async def _outcome_items(analyst_id: str, after_id: int) -> tuple[list[str], int]:
    """Closed paper-book positions attributed to this analyst (REVIEW-C3 M5):
    ``paper_book.closed`` events whose payload analyst_id matches, id-ordered
    like every other collector. The settled outcome of the analyst's own call
    — direction, close reason, realized return, the original claim — is
    exactly the feedback the flywheel was missing."""
    rows = await db.query(
        "SELECT e.id AS event_id, e.payload, e.created_at, f.claim FROM events e "
        "LEFT JOIN paper_positions p ON p.id = e.ref_id "
        "LEFT JOIN forecasts f ON f.id = p.forecast_id "
        "WHERE e.type = 'paper_book.closed' "
        "AND json_extract(e.payload, '$.analyst_id') = ? AND e.id > ? "
        "ORDER BY e.id ASC LIMIT ?",
        (analyst_id, after_id, MAX_OUTCOME_ITEMS),
    )
    items: list[str] = []
    max_id = after_id
    for r in rows:
        max_id = max(max_id, r["event_id"])
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            p = {}
        pnl = p.get("realized_pnl")
        pnl_s = f"{pnl:+.2%}" if isinstance(pnl, (int, float)) and not isinstance(pnl, bool) \
            else "未知（无可用价）"
        line = (
            f"{p.get('security_id') or '（已删标的）'} {p.get('direction') or '?'} · "
            f"{p.get('reason') or '?'} 平仓 · 盈亏 {pnl_s}"
        )
        claim = _cap(str(r.get("claim") or ""), OUTCOME_ITEM_CAP)
        if claim:
            line += f" · 原始观点：{claim}"
        items.append(f"[{str(r['created_at'] or '')[:10]} 纸面结果] {line}")
    return items, max_id


def _parse_cursors(raw: str | None) -> dict[str, int]:
    """Cursors JSON off a version row. Corrupt data degrades to 0 (re-consume,
    never lose): the compact prompt tolerates duplicate material, silence not."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("daily_event", "card_event", "mail_msg", "outcome_event"):
        v = data.get(key)
        if isinstance(v, (int, float)):
            out[key] = int(v)
    return out


async def _collect_material(analyst_id: str, prev_cursors: dict[str, int]) -> tuple[str, dict[str, int]]:
    """(material, new_cursors) — everything not yet consumed by a prior version."""
    dailies, daily_cur = await _daily_items(analyst_id, prev_cursors.get("daily_event", 0))
    cards, card_cur = await _card_items(analyst_id, prev_cursors.get("card_event", 0))
    mails, mail_cur = await _mail_items(analyst_id, prev_cursors.get("mail_msg", 0))
    outcomes, outcome_cur = await _outcome_items(analyst_id, prev_cursors.get("outcome_event", 0))
    sections: list[str] = []
    if dailies:
        sections.append("### 观察日报\n\n" + "\n\n".join(dailies))
    if cards:
        sections.append("### 白板卡片\n\n" + "\n\n".join(cards))
    if mails:
        sections.append("### 信箱回复\n\n" + "\n\n".join(mails))
    if outcomes:
        sections.append("### 纸面账本结果（你此前观点的实盘化验证）\n\n" + "\n\n".join(outcomes))
    cursors = {
        "daily_event": daily_cur, "card_event": card_cur,
        "mail_msg": mail_cur, "outcome_event": outcome_cur,
    }
    return "\n\n".join(sections), cursors


# ---- compact mutual exclusion -------------------------------------------------
# The version-row UNIQUE alone let two overlapping compacts BOTH run the model
# and discard one output (2x quota, M8-005). A per-analyst conditional claim on
# an admin_state row makes one winner BEFORE any model call; the loser skips.
# The shared claims.py claim/lease/CAS-takeover idiom, without the heartbeat:
# a compact is one bounded model call (settings-default 30 min hand timeout),
# so a fixed lease with slack covers a live compact end-to-end, and only a
# hard-killed one (finally never ran) goes stale and is taken over.
# Escape hatch while a claim is held: deleting the row frees it.

COMPACT_LEASE_S = 45 * 60


def _claim_key(analyst_id: str) -> str:
    return f"memory_compact:{analyst_id}"


def _claim_token(owner: str) -> str:
    return json.dumps({"owner": owner, "claimed_at": bus.now_iso()})


async def _claim_compact(analyst_id: str) -> tuple[str, str] | None:
    """Conditionally claim one analyst's compact; (key, token) for the winner.

    The shared idiom (claims.claim_admin_state) picks one winner by rowcount
    and takes over stale claims via exact-value CAS.
    """
    return await claim_admin_state(
        _claim_key(analyst_id),
        make_token=lambda: _claim_token(new_id()),
        is_stale=lease_stale_checker(COMPACT_LEASE_S, label="compact claim"),
    )


async def _release_compact(key: str, token: str) -> None:
    # CAS delete — only our own claim (claims.release_admin_state)
    await release_admin_state(key, token)


# ---- compaction -----------------------------------------------------------------

async def compact_one(analyst_id: str) -> dict[str, Any]:
    """Compress one analyst's recent output into a new memory version.

    Skips (no model call) when nothing new landed since the previous version,
    or when a concurrent compact holds this analyst's claim (cross-process
    single-burn, M8-005). Raises ValueError for unknown analyst ids; other
    failures return a dict.

    The material window is fixed BEFORE the model runs: whatever the cursors
    fetched is what this version consumed, and the stored cursors say exactly
    that. Output landing while the model runs sits above the cursors and is
    consumed by the next compact — nothing depends on wall-clock timestamps.
    """
    analyst = get_analyst(analyst_id)
    if analyst is None:
        raise ValueError(f"unknown analyst '{analyst_id}'")

    claim = await _claim_compact(analyst_id)
    if claim is None:
        log.info("memory compact for %s already claimed; skipping", analyst_id)
        return {"analyst_id": analyst_id, "skipped": "compact already running"}
    key, token = claim
    try:
        return await _compact_claimed(analyst_id, analyst)
    finally:
        # every exit releases (success, no-material skip, failure, crash): the
        # claim guards ONE compact run, not a cadence
        await _release_compact(key, token)


async def _compact_claimed(analyst_id: str, analyst: Analyst) -> dict[str, Any]:
    """The compact body — caller already holds this analyst's claim."""
    prev = await latest(analyst_id)
    prev_cursors = _parse_cursors(prev["cursors"]) if prev else {}
    material, cursors = await _collect_material(analyst_id, prev_cursors)
    if not material:
        return {"analyst_id": analyst_id, "skipped": "no new material"}

    context_blocks: list[str] = []
    if prev:
        context_blocks.append(_render_block(prev))
    context_blocks.append(f"{MEMORY_MATERIAL_HEADER}\n\n{material}")
    prompt = build_analyst_prompt(
        analyst,
        MEMORY_COMPACT_TASK.format(max_chars=MEMORY_MAX_CHARS),
        context_blocks=context_blocks,
    )

    settings = get_settings()
    task = await executor.submit(
        settings.default_hand, prompt, source=SOURCE, model=analyst.model
    )
    compact_md = (task.output or "").strip()
    if task.status != "completed" or not compact_md:
        # cursors are NOT persisted: the material stays unconsumed for a retry
        log.warning("memory compact failed for %s: task %s %s", analyst_id, task.id, task.status)
        return {
            "analyst_id": analyst_id, "status": task.status or "failed",
            "task_id": task.id, "error": task.error,
        }

    version = (prev["version"] if prev else 0) + 1
    memory_id = new_id()
    wd = work_date()
    claimed = await db.execute(
        "INSERT OR IGNORE INTO analyst_memory "
        "(id, analyst_id, version, work_date, compact_md, supersedes, cursors, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (memory_id, analyst_id, version, wd, compact_md,
         prev["id"] if prev else None, json.dumps(cursors), bus.now_iso()),
    )
    if not claimed:  # a concurrent compact already claimed this version
        log.info("memory version %d for %s claimed concurrently; discarding", version, analyst_id)
        return {
            "analyst_id": analyst_id, "task_id": task.id,
            "skipped": f"version {version} already claimed by a concurrent compact",
        }

    await bus.emit("memory.compacted", "analyst", analyst_id, {
        "version": version, "work_date": wd, "memory_id": memory_id, "task_id": task.id,
    })
    log.info("memory compacted: %s v%d (%d chars)", analyst_id, version, len(compact_md))
    return {
        "analyst_id": analyst_id, "status": "completed",
        "version": version, "memory_id": memory_id, "task_id": task.id,
    }


async def compact_all() -> dict[str, Any]:
    """Serial sweep over every working analyst. One failure never breaks the chain."""
    results: list[dict[str, Any]] = []
    for a in roster():
        if a.category in SKIP_CATEGORIES:
            continue
        try:
            results.append(await compact_one(a.id))
        except Exception as exc:  # noqa: BLE001 - the loop must survive any single analyst
            log.exception("memory compact crashed for %s", a.id)
            results.append({"analyst_id": a.id, "status": "crashed", "error": str(exc)[:200]})
    summary = {
        "date": work_date(),
        "ran": len(results),
        "completed": sum(1 for r in results if r.get("status") == "completed"),
        "results": results,
    }
    log.info("memory compact sweep: %d ran, %d completed", summary["ran"], summary["completed"])
    return summary
