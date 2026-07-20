"""BFS research tree / Explore mode (ROADMAP Phase 7, proposal §6.2).

One tree = one root topic broken down breadth-first by single-prompt explore
calls. The legacy ``research-worker`` (prompt.ts + defensive parser.ts) does
not exist on this machine — the behavior here is a from-scratch port of the
DESCRIPTION: per node, ask for (a) a short research conclusion and (b) up to
:data:`MAX_CHILDREN_PER_NODE` drill-down sub-questions in a strict line
protocol, parse defensively, and grow the tree under per-tree caps.

The loop (house rules throughout):

1. **create_tree()** books one slot of the SGT daily budget atomically
   (admin_state counter row ``research_tree_booked:<date>`` — the factcheck
   ``_reserve_attempt`` idiom: booked BEFORE the tree lands, no refunds, so
   the counter reads "attempts booked", not "trees created"; REVIEW-D4 N2),
   then inserts the tree + its root node (depth 0, pending) in one
   transaction.
2. **tick()** is the scheduler entrypoint (5-min gated interval; the mount
   lives in PATCH-NOTES-D4.md) and never raises. It first sweeps strays
   (pending rows stranded under terminal trees are pruned; stalled trees are
   settled/announced), then conditional-claims up to :data:`NODES_PER_TICK`
   pending nodes in BFS order (``ORDER BY depth, created_at`` — same layer
   first) and runs them concurrently. The claim UPDATE embeds three guards
   as subqueries — tree still live, per-tree running count under
   ``node_concurrency``, and **parent completed** (REVIEW-D4 H1
   defense-in-depth) — so the database is the arbiter and overlapping ticks
   can never claim a child whose parent conclusion is not yet durable.
3. **Explore call**: one ``executor.submit`` per node (the global semaphore
   bounds real parallelism — this module owns NO pool of its own). Research
   stays on the research hands (hard rule 10): round-robin over
   ``settings.research_hand_names`` with the fallback chain confined to it.
4. **parse_explore()** is canonical-line extraction (the C1 VERDICT / C4
   precedent): only line-anchored ``CONCLUSION:``, ``SCORE:`` and
   ``CHILD: <topic> | <question>`` lines count; code fences and blockquotes
   are quoted material;
   placeholder mimics (``<子主题>``, and the ``<一段结论>`` conclusion form —
   REVIEW-D4 N1) are dropped; children are deduped and capped. Echo immunity
   is structural: the prompt never places a protocol token at line start
   (the format spec keeps them mid-line) and every interpolated material
   (topic/question/ancestor summaries) is neutralized by ``_quote_material``
   — a mirrored prompt parses to zero children.
5. **One transaction per completion** (REVIEW-D4 H1): the parent's terminal
   write (status/summary/task_id) and ALL of its children (pending or
   pruned) commit together, parent first. A crash can only land before the
   whole batch (node re-runs, no children exist) or after it (layer fully
   consistent) — a claimable pending child ALWAYS has a completed parent
   with a durable summary. Losing the running claim mid-flight discards the
   result batch entirely (no half layers). Child budget: pending inserts are
   count-guarded (non-pruned rows < max_nodes) and tree-status-guarded
   INSIDE the same statement; depth/budget overflow lands as 'pruned' rows
   so the viewer shows what was cut.
6. **Finality & the announce arbiter** (REVIEW-D4 H2/M2): ``stop_tree()``
   flips the tree to 'stopped' and prunes its pending nodes in ONE
   transaction — SQLite's single writer means a concurrently completing
   parent either commits its children first (they get pruned here) or reads
   the stopped tree and inserts nothing; no stranded pending rows either
   way. Running nodes finish naturally. The ``tree.completed`` event is a
   FINAL SNAPSHOT: emitted only when the tree is terminal
   (completed/failed/stopped) AND drained (no pending/running nodes), via a
   conditional claim on the ``announced_at`` column — exactly one event per
   drain generation (a manual failed-node retry starts a new generation),
   payload read from the database rows after the terminal state is durable.
   Crash recovery: ``recover_orphans()`` at boot prunes running
   nodes under terminal trees and requeues the rest (mount in
   PATCH-NOTES-D4.md); the tick sweep settles/announces whatever a crash
   left unflipped or unannounced.

Bus events (SSE-visible via /api/events/stream?types=tree.):
``tree.node_completed`` (every node reaching completed/failed),
``tree.node_retried`` (an operator requeues one failed node), and
``tree.completed`` (the drained terminal snapshot described above).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import re
import uuid
from typing import Any

import aiosqlite

from .. import bus, db
from ..config import get_settings
from ..router import executor
from .prompts import date_anchor, work_date

log = logging.getLogger("institute.research_tree")

SOURCE = "research_tree"

# Canonical status enums mirroring the CHECKs in migrations/0020_research_tree.sql.
# Import points for API surfaces (/api/contract) — do not restate.
TREE_STATUSES = ("pending", "exploring", "completed", "stopped", "failed")
NODE_STATUSES = ("pending", "running", "completed", "failed", "pruned")
TREE_TERMINAL = ("completed", "stopped", "failed")
NODE_LIVE = ("pending", "running")

NODE_COMPLETED_EVENT = "tree.node_completed"
NODE_RETRIED_EVENT = "tree.node_retried"
TREE_COMPLETED_EVENT = "tree.completed"

MAX_CHILDREN_PER_NODE = 3      # line-protocol children accepted per node
MAX_TOPIC_LEN = 200            # parse_followups' topic cap (house convention)
MAX_QUESTION_LEN = 500         # parse_followups' question cap
SUMMARY_CAP = 800              # extract_summary's cap (house convention)
ANCESTRY_SUMMARY_CAP = 300     # per-ancestor slice in the prompt chain block
NODES_PER_TICK = 3             # one tick claims at most this many nodes (== global semaphore width)
CLAIM_SCAN_LIMIT = 10          # BFS candidates examined per claim attempt
MAX_DEPTH_LIMIT = 4            # create_tree hard bound on max_depth
MAX_NODES_LIMIT = 50           # create_tree hard bound on max_nodes

# ---- exploration limits (admin_state row over in-code defaults; 0015 idiom) --
LIMITS_KEY = "research_tree_limits"
DEFAULT_DAILY_TREE_CAP = 3     # NEW trees per SGT work date
DEFAULT_NODE_CONCURRENCY = 2   # concurrently running nodes per tree

# Daily tree budget: one admin_state counter row per SGT work date, booked by
# a conditional UPDATE BEFORE the tree row lands. No refunds — the counter
# counts BOOKED attempts, not durable trees (REVIEW-D4 N2 naming).
TREES_BOOKED_KEY_PREFIX = "research_tree_booked:"

# ---- prompt constant (verbatim-stable once written; CLAUDE.md rule 4) --------
# Echo immunity by construction: no line of this template starts with a
# protocol token (CONCLUSION:/SCORE:/CHILD: appear only mid-line, inside 「」), and
# every {placeholder} is filled with _quote_material-neutralized text — so a
# hand that mirrors the prompt back can never produce a canonical line.

EXPLORE_PROMPT = """\
你是研究所的探索研究员，正在对一个研究主题做广度优先（BFS）的树状拆解。你负责其中一个节点：先给出本节点的简要研究结论，再提出最值得继续下钻的子问题。

【当前主题】{topic}
【核心问题】{question}
【父节点结论链】
{ancestry}

【任务】
1. 结合父节点结论链的已有认识，针对当前主题与核心问题给出一段简要研究结论（200 字以内，讲清关键事实、你的判断与依据）；
2. 对本节点结论相对根主题的研究价值打 0–100 分（越高越值得优先阅读）；
3. 提出最多 {max_children} 个最值得下钻的子问题——只提能实质推进整体研究、且与父节点已覆盖内容不重复的方向；没有值得下钻的方向就一个都不提。

【输出格式】只输出以下行协议，不要任何其他文字：
第一行输出结论行，格式是「CONCLUSION: <一段结论>」（独占一行）；
第二行输出评分行，格式是「SCORE: <0-100>」（独占一行，只写数字）；
随后每个子问题独占一行，格式是「CHILD: <子主题> | <子问题>」——子主题不超过 30 字，子问题是一句可直接研究的问题。\
"""

NO_ANCESTRY_LABEL = "（当前为根节点，无父链）"
NO_QUESTION_LABEL = "（未指定，围绕主题自由探索）"

# ---- defensive line-protocol parser (C1 canonical-line precedent) ------------

_FENCE_LINE = re.compile(r"^\s*(?:```|~~~)")
_CONCLUSION_LINE = re.compile(r"^\s*\**\s*CONCLUSION\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_SCORE_LINE = re.compile(r"^\s*\**\s*SCORE\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_SCORE_VALUE = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:(?:/|／)\s*100|[%％]|分)?$"
)
_CHILD_LINE = re.compile(r"^\s*\**\s*CHILD\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_PROTOCOL_GUARD = re.compile(r"(CONCLUSION|SCORE|CHILD)(\s*[:：])", re.IGNORECASE)


def _quote_material(text: str) -> str:
    """Neutralize untrusted material before it enters the explore prompt
    (the C1 ``_quote_material`` / C4 ``_quote_detail`` precedent). Collapsing
    whitespace keeps the material inline after its 【标签】/list label so
    nothing from it can sit at line start; breaking the ``CONCLUSION:`` /
    ``SCORE:`` / ``CHILD:`` patterns is belt and braces for hands that
    re-wrap lines."""
    flat = " ".join((text or "").split())
    return _PROTOCOL_GUARD.sub(r"\1 -\2", flat)


def _is_placeholder(text: str) -> bool:
    # a format-spec mimic (`CHILD: <子主题> | <子问题>` / `CONCLUSION: <一段结论>`)
    # is not an answer
    return text.startswith("<") and text.endswith(">")


def _parse_score_payload(payload: str) -> float | None:
    """Parse the canonical score payload, or return None without raising.

    Score contract: ``SCORE: N`` is the model's 0–100 self-assessment of this
    node conclusion's research value relative to the root topic. ``N%``,
    ``N/100`` and ``N分`` are tolerated, but prose, non-finite values and
    out-of-range numbers are rejected. The database therefore stores either
    a comparable 0–100 REAL or NULL — malformed model output never fails the
    node.
    """
    m = _SCORE_VALUE.fullmatch(payload.strip())
    if m is None:
        return None
    try:
        score = float(m.group(1))
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) and 0 <= score <= 100 else None


def parse_explore(text: str) -> dict[str, Any]:
    """Model output -> conclusion + optional score + child questions.

    Canonical-line extraction only: line-anchored ``CONCLUSION:`` /
    ``SCORE:`` / ``CHILD:`` (markdown bold and full-width colons tolerated),
    lines inside code fences or blockquotes are quoted material and are
    skipped. The conclusion and score are the FIRST valid canonical lines;
    malformed/missing scores stay ``None``. Children split on the first
    ``|``/``｜`` (a missing separator degrades to a topic-only child);
    placeholder mimics are dropped, topics are deduped case-insensitively
    and capped at MAX_CHILDREN_PER_NODE. Any failure mode returns what was
    salvageable — never raises.
    """
    out: dict[str, Any] = {"conclusion": "", "score": None, "children": []}
    if not (text or "").strip():
        return out
    seen: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        m = _CONCLUSION_LINE.match(line)
        if m:
            candidate = m.group(1).strip("* ").strip()
            if not out["conclusion"] and candidate and not _is_placeholder(candidate):
                out["conclusion"] = candidate[:SUMMARY_CAP]
            continue
        m = _SCORE_LINE.match(line)
        if m:
            if out["score"] is None:
                out["score"] = _parse_score_payload(m.group(1).strip("* ").strip())
            continue
        m = _CHILD_LINE.match(line)
        if m is None:
            continue
        payload = m.group(1).strip("* ").strip()
        parts = re.split(r"[|｜]", payload, maxsplit=1)
        topic = " ".join(parts[0].split()).strip()
        question = " ".join((parts[1] if len(parts) > 1 else "").split()).strip()
        if not topic or _is_placeholder(topic) or (question and _is_placeholder(question)):
            continue
        key = topic.casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(out["children"]) < MAX_CHILDREN_PER_NODE:
            out["children"].append({
                "topic": topic[:MAX_TOPIC_LEN],
                "question": question[:MAX_QUESTION_LEN],
            })
    return out


# ---- limits & daily budget ----------------------------------------------------

async def get_limits() -> dict[str, int]:
    """{daily_tree_cap, node_concurrency} from admin_state, merged over the
    in-code defaults. A broken/missing row degrades to the defaults."""
    limits = {
        "daily_tree_cap": DEFAULT_DAILY_TREE_CAP,
        "node_concurrency": DEFAULT_NODE_CONCURRENCY,
    }
    try:
        row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (LIMITS_KEY,))
        if row:
            stored = json.loads(row["value"])
            if isinstance(stored, dict):
                for knob in limits:
                    val = stored.get(knob)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        limits[knob] = int(val)
    except Exception:  # noqa: BLE001 - a corrupt config row must not break the loop
        log.warning("could not read %s config; using defaults", LIMITS_KEY, exc_info=True)
    limits["daily_tree_cap"] = max(0, limits["daily_tree_cap"])
    limits["node_concurrency"] = max(1, limits["node_concurrency"])
    return limits


def _trees_booked_key(date: str | None = None) -> str:
    return TREES_BOOKED_KEY_PREFIX + (date or work_date())


async def trees_booked_today() -> int:
    """Create attempts already booked against today's SGT budget (a booked
    slot whose tree insert failed still counts — no refunds)."""
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (_trees_booked_key(),)
    )
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


async def _reserve_tree_slot(cap: int) -> bool:
    """Atomically book ONE slot of today's tree budget (factcheck
    ``_reserve_attempt`` idiom): INSERT OR IGNORE seeds the counter row, then
    a conditional UPDATE (value < cap) is the arbiter — the rowcount says
    whether THIS caller got the slot, so concurrent creates can never jointly
    exceed the cap. Booked slots are never refunded."""
    if cap <= 0:
        return False
    key = _trees_booked_key()
    await db.execute(
        "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, '0')", (key,)
    )
    n = await db.execute(
        "UPDATE admin_state SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = ? AND CAST(value AS INTEGER) < ?",
        (key, cap),
    )
    return bool(n)


# ---- tree creation --------------------------------------------------------------

async def create_tree(root_topic: str, *, max_depth: int = 2, max_nodes: int = 12) -> dict[str, Any]:
    """Create one explore tree with a pending root node.

    Validation errors raise ValueError (explainable caps, not silent
    truncation — the research.enqueue precedent); a spent daily budget
    returns ``{"refused": "daily_cap", ...}`` instead of a row (the
    cooldown-refusal shape). The tree + root insert in one transaction, so a
    crash can never leave a rootless tree behind.
    """
    topic = " ".join(str(root_topic or "").split())
    if not topic:
        raise ValueError("root_topic must not be empty")
    if len(topic) > MAX_TOPIC_LEN:
        raise ValueError(f"root_topic exceeds {MAX_TOPIC_LEN} chars ({len(topic)}); shorten it")
    max_depth, max_nodes = int(max_depth), int(max_nodes)
    if not 0 <= max_depth <= MAX_DEPTH_LIMIT:
        raise ValueError(f"max_depth must be between 0 and {MAX_DEPTH_LIMIT}")
    if not 1 <= max_nodes <= MAX_NODES_LIMIT:
        raise ValueError(f"max_nodes must be between 1 and {MAX_NODES_LIMIT}")

    limits = await get_limits()
    if not await _reserve_tree_slot(limits["daily_tree_cap"]):
        return {
            "refused": "daily_cap",
            "cap": limits["daily_tree_cap"],
            "booked_today": await trees_booked_today(),
            "root_topic": topic,
        }

    tree_id, root_id, now = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12], bus.now_iso()
    # NB: transaction() holds the db write lock — use the yielded conn
    # directly (db.execute in here would deadlock; the factcheck precedent)
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO research_trees (id, root_topic, status, max_depth, max_nodes, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (tree_id, topic, "pending", max_depth, max_nodes, now),
        )
        await conn.execute(
            "INSERT INTO research_tree_nodes (id, tree_id, parent_id, depth, topic, question, status, created_at) "
            "VALUES (?,?,NULL,0,?,?,?,?)",
            (root_id, tree_id, topic, "", "pending", now),
        )
    log.info("research tree %s created (topic=%s depth<=%d nodes<=%d)",
             tree_id, topic, max_depth, max_nodes)
    return await get_tree(tree_id)  # type: ignore[return-value]


# ---- prompt assembly -------------------------------------------------------------

async def _ancestry_block(node: dict[str, Any]) -> str:
    """Root-first chain of ancestor conclusions for the explore prompt.
    Bounded: depth is capped at creation, every summary slices at
    ANCESTRY_SUMMARY_CAP and is neutralized (see _quote_material). The H1
    transaction invariant guarantees every ancestor of a claimable node is
    completed with a durable summary."""
    chain: list[dict[str, Any]] = []
    parent_id = node.get("parent_id")
    for _ in range(MAX_DEPTH_LIMIT + 2):  # belt: parent links cannot cycle, walk is bounded anyway
        if not parent_id:
            break
        parent = await db.query_one(
            "SELECT id, parent_id, depth, topic, summary FROM research_tree_nodes WHERE id = ?",
            (parent_id,),
        )
        if parent is None:
            break
        chain.append(parent)
        parent_id = parent["parent_id"]
    if not chain:
        return NO_ANCESTRY_LABEL
    lines = []
    for p in reversed(chain):  # root first
        summary = _quote_material(str(p["summary"] or ""))[:ANCESTRY_SUMMARY_CAP] or "（无结论）"
        lines.append(f"- 第 {p['depth']} 层「{_quote_material(p['topic'])[:MAX_TOPIC_LEN]}」：{summary}")
    return "\n".join(lines)


def _build_prompt(node: dict[str, Any], ancestry: str) -> str:
    question = _quote_material(node.get("question") or "") or NO_QUESTION_LABEL
    return f"{date_anchor()}\n\n" + EXPLORE_PROMPT.format(
        topic=_quote_material(node["topic"]),
        question=question,
        ancestry=ancestry,
        max_children=MAX_CHILDREN_PER_NODE,
    )


# ---- hand policy (hard rule 10: research stays on the research hands) -----------

_rr = itertools.count()


def _pick_hand() -> tuple[str, tuple[str, ...]]:
    hands = get_settings().research_hand_names
    return hands[next(_rr) % len(hands)], hands


# ---- the BFS drain ----------------------------------------------------------------

async def _claim_next_node(node_concurrency: int) -> dict[str, Any] | None:
    """Conditional-claim ONE pending node in BFS order (depth, created_at).

    The claim UPDATE is the arbiter and carries three guards as subqueries in
    the ONE atomic statement: the tree is still live (a stop between the
    candidate scan and the claim loses — REVIEW-D4 H2), the per-tree running
    count is under ``node_concurrency``, and the parent (if any) is
    'completed' (REVIEW-D4 H1 defense-in-depth: the completion transaction
    already guarantees this for rows it created; the guard also covers
    operator-edited rows). Candidates whose tree is at capacity are skipped
    in favor of the next tree's front node.
    """
    candidates = await db.query(
        "SELECT n.id, n.tree_id, n.parent_id FROM research_tree_nodes n "
        "JOIN research_trees t ON t.id = n.tree_id "
        "WHERE n.status = 'pending' AND t.status IN ('pending','exploring') "
        "AND (n.parent_id IS NULL OR EXISTS (SELECT 1 FROM research_tree_nodes p "
        "     WHERE p.id = n.parent_id AND p.status = 'completed')) "
        "ORDER BY n.depth ASC, n.created_at ASC, n.id ASC LIMIT ?",
        (CLAIM_SCAN_LIMIT,),
    )
    for cand in candidates:
        claimed = await db.execute(
            "UPDATE research_tree_nodes SET status='running' "
            "WHERE id = ? AND status = 'pending' "
            "AND (SELECT status FROM research_trees t WHERE t.id = ?) IN ('pending','exploring') "
            "AND (? IS NULL OR EXISTS (SELECT 1 FROM research_tree_nodes p "
            "     WHERE p.id = ? AND p.status = 'completed')) "
            "AND (SELECT COUNT(*) FROM research_tree_nodes r "
            "     WHERE r.tree_id = ? AND r.status = 'running') < ?",
            (cand["id"], cand["tree_id"], cand["parent_id"], cand["parent_id"],
             cand["tree_id"], node_concurrency),
        )
        if not claimed:
            continue  # lost the race, tree stopped, or the tree is at capacity
        await db.execute(
            "UPDATE research_trees SET status='exploring' WHERE id = ? AND status = 'pending'",
            (cand["tree_id"],),
        )
        return await db.query_one("SELECT * FROM research_tree_nodes WHERE id = ?", (cand["id"],))
    return None


async def _insert_children(
    conn: aiosqlite.Connection, node: dict[str, Any], children: list[dict[str, str]],
) -> tuple[int, int]:
    """Insert parsed children INSIDE the caller's completion transaction
    (REVIEW-D4 H1 — ``conn`` is the open transaction; db.execute here would
    deadlock on the write lock). Returns (added, pruned).

    The transaction serializes us against stop_tree(), so the tree-status
    read is stable for the whole batch: a tree no longer exploring gets NO
    new rows at all (stop means stop — REVIEW-D4 H2). Pending inserts are
    additionally count-guarded and tree-status-guarded in the statement
    itself (the budget arbiter against earlier committed completions); depth
    or budget overflow lands as 'pruned' rows (also tree-status-guarded) so
    the viewer shows what was cut. The (tree_id, parent_id, topic) unique
    index makes exact replays idempotent.
    """
    if not children:
        return 0, 0
    cur = await conn.execute(
        "SELECT status, max_depth, max_nodes FROM research_trees WHERE id = ?",
        (node["tree_id"],),
    )
    tree = await cur.fetchone()
    await cur.close()
    if tree is None or tree["status"] != "exploring":
        return 0, 0
    added = pruned = 0
    child_depth = int(node["depth"]) + 1
    parent_key = " ".join(str(node["topic"]).split()).casefold()
    for child in children:
        if " ".join(child["topic"].split()).casefold() == parent_key:
            continue  # a child mirroring its parent would recurse for free (rule 8 spirit)
        now = bus.now_iso()
        if child_depth <= tree["max_depth"]:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO research_tree_nodes "
                "(id, tree_id, parent_id, depth, topic, question, status, created_at) "
                "SELECT ?,?,?,?,?,?, 'pending', ? "
                "WHERE (SELECT status FROM research_trees WHERE id = ?) = 'exploring' "
                "AND (SELECT COUNT(*) FROM research_tree_nodes "
                "     WHERE tree_id = ? AND status != 'pruned') < ?",
                (uuid.uuid4().hex[:12], node["tree_id"], node["id"], child_depth,
                 child["topic"], child["question"], now,
                 node["tree_id"], node["tree_id"], tree["max_nodes"]),
            )
            landed = cur.rowcount
            await cur.close()
            if landed:
                added += 1
                continue
            cur = await conn.execute(
                "SELECT 1 FROM research_tree_nodes "
                "WHERE tree_id = ? AND parent_id = ? AND topic = ?",
                (node["tree_id"], node["id"], child["topic"]),
            )
            dup = await cur.fetchone()
            await cur.close()
            if dup:
                continue  # exact replay of a re-driven parent: row already exists
        # depth or node budget cut: keep a terminal 'pruned' row for the viewer
        cur = await conn.execute(
            "INSERT OR IGNORE INTO research_tree_nodes "
            "(id, tree_id, parent_id, depth, topic, question, status, created_at, finished_at) "
            "SELECT ?,?,?,?,?,?, 'pruned', ?, ? "
            "WHERE (SELECT status FROM research_trees WHERE id = ?) = 'exploring'",
            (uuid.uuid4().hex[:12], node["tree_id"], node["id"], child_depth,
             child["topic"], child["question"], now, now, node["tree_id"]),
        )
        pruned += cur.rowcount
        await cur.close()
    return added, pruned


async def _maybe_finish_tree(tree_id: str) -> bool:
    """Flip an exploring tree terminal once every node is (no event here —
    the announce arbiter below owns the event). 'completed' needs at least
    one completed node; a tree whose every node failed/pruned lands
    'failed'. Stopped trees are already terminal and never flip."""
    live = await db.query_one(
        "SELECT COUNT(*) AS n FROM research_tree_nodes "
        "WHERE tree_id = ? AND status IN ('pending','running')",
        (tree_id,),
    )
    if live and live["n"]:
        return False
    done = await db.query_one(
        "SELECT COUNT(*) AS n FROM research_tree_nodes "
        "WHERE tree_id = ? AND status = 'completed'",
        (tree_id,),
    )
    final = "completed" if done and done["n"] else "failed"
    n = await db.execute(
        "UPDATE research_trees SET status = ?, finished_at = ? WHERE id = ? AND status = 'exploring'",
        (final, bus.now_iso(), tree_id),
    )
    if n:
        log.info("research tree %s finished: %s", tree_id, final)
    return bool(n)


async def _announce_if_drained(tree_id: str) -> bool:
    """Emit one ``tree.completed`` final snapshot per drain generation.

    The conditional claim on ``announced_at`` is the generation's single-shot
    arbiter (``retry_node`` deliberately clears it when reopening a terminal
    tree):
    it only fires when the tree is terminal AND drained (no pending/running
    nodes left — a stopped tree with naturally-finishing running nodes waits
    for its last finisher). The payload is read from the database AFTER the
    terminal state is durable, so SSE viewers may disconnect on receipt and
    the vault exporter projects a settled tree, never a half-running one.
    """
    n = await db.execute(
        "UPDATE research_trees SET announced_at = ? "
        "WHERE id = ? AND status IN ('completed','stopped','failed') "
        "AND announced_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM research_tree_nodes n "
        "                WHERE n.tree_id = research_trees.id "
        "                AND n.status IN ('pending','running'))",
        (bus.now_iso(), tree_id),
    )
    if not n:
        return False
    tree = await db.query_one("SELECT * FROM research_trees WHERE id = ?", (tree_id,))
    counts = await db.query(
        "SELECT status, COUNT(*) AS n FROM research_tree_nodes WHERE tree_id = ? GROUP BY status",
        (tree_id,),
    )
    await bus.emit(TREE_COMPLETED_EVENT, "research_tree", tree_id, {
        "tree_id": tree_id,
        "root_topic": (tree or {}).get("root_topic", ""),
        "status": (tree or {}).get("status", ""),
        "nodes": {r["status"]: r["n"] for r in counts},
        "finished_at": (tree or {}).get("finished_at"),
    })
    log.info("research tree %s announced terminal snapshot (%s)",
             tree_id, (tree or {}).get("status"))
    return True


async def _settle_tree(tree_id: str) -> bool:
    """Post-transition bookkeeping: flip an all-terminal exploring tree, then
    announce once drained. Safe to call after ANY node/tree state change."""
    flipped = await _maybe_finish_tree(tree_id)
    announced = await _announce_if_drained(tree_id)
    return flipped or announced


async def _run_node(node: dict[str, Any]) -> str:
    """Explore ONE node this caller already claimed (status='running').

    One executor.submit under the global semaphore. The terminal write and
    the children batch commit in ONE transaction, parent first (REVIEW-D4
    H1): losing the running claim mid-flight (operator reset / recovery in
    another process) discards the whole batch — no children without a
    durable parent conclusion, no half layers after a crash.
    """
    node_id, tree_id = node["id"], node["tree_id"]
    task = None
    try:
        prompt = _build_prompt(node, await _ancestry_block(node))
        hand, chain = _pick_hand()
        task = await executor.submit(
            hand, prompt, source=SOURCE, fallback_chain=chain,
        )
    except Exception:  # noqa: BLE001 - one node must never break the drain
        log.exception("explore call crashed for node %s", node_id)

    if task is None or task.status != "completed":
        n = await db.execute(
            "UPDATE research_tree_nodes SET status='failed', task_id=?, finished_at=? "
            "WHERE id = ? AND status = 'running'",
            (getattr(task, "id", None), bus.now_iso(), node_id),
        )
        if n:
            await bus.emit(NODE_COMPLETED_EVENT, "research_tree", tree_id, {
                "tree_id": tree_id, "node_id": node_id, "depth": node["depth"],
                "topic": node["topic"], "status": "failed",
                "task_id": getattr(task, "id", None),
                "children_added": 0, "children_pruned": 0,
            })
        await _settle_tree(tree_id)
        return "failed"

    parsed = parse_explore(task.output or "")
    summary = parsed["conclusion"] or " ".join((task.output or "").split())[:SUMMARY_CAP]
    score = parsed["score"]
    added = pruned = 0
    completed = False
    # one transaction: parent terminal write FIRST, then its children — the
    # whole layer commits or vanishes together (REVIEW-D4 H1). Events after
    # commit (bus.emit inside the transaction would deadlock on the write lock).
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE research_tree_nodes SET status='completed', task_id=?, summary=?, score=?, finished_at=? "
            "WHERE id = ? AND status = 'running'",
            (task.id, summary, score, bus.now_iso(), node_id),
        )
        claimed_completion = cur.rowcount
        await cur.close()
        if claimed_completion:
            completed = True
            added, pruned = await _insert_children(conn, node, parsed["children"])
    if completed:
        await bus.emit(NODE_COMPLETED_EVENT, "research_tree", tree_id, {
            "tree_id": tree_id, "node_id": node_id, "depth": node["depth"],
            "topic": node["topic"], "status": "completed", "task_id": task.id,
            "children_added": added, "children_pruned": pruned,
            "summary": summary[:300], "score": score,
        })
    else:
        log.info("node %s no longer running; discarding result batch (no children inserted)",
                 node_id)
    await _settle_tree(tree_id)
    return "completed" if completed else "lost_claim"


async def _sweep_trees() -> int:
    """Tick prologue: repair whatever a crash / race / operator edit left
    behind. (a) pending nodes stranded under terminal trees are pruned (the
    REVIEW-D4 H2 backstop — the transactional stop makes new strays
    impossible, this sweeps history and manual edits); (b) exploring trees
    with every node terminal are flipped (crash between the last completion
    and the flip); (c) terminal trees never announced are announced once
    drained (crash between the flip and the announce)."""
    stranded = await db.execute(
        "UPDATE research_tree_nodes SET status='pruned', finished_at=? "
        "WHERE status = 'pending' AND tree_id IN "
        "(SELECT id FROM research_trees WHERE status IN ('completed','stopped','failed'))",
        (bus.now_iso(),),
    )
    if stranded:
        log.warning("pruned %d pending nodes stranded under terminal trees", stranded)
    settled = 0
    rows = await db.query(
        "SELECT id FROM research_trees t "
        "WHERE (t.status = 'exploring' "
        "       OR (t.status IN ('completed','stopped','failed') AND t.announced_at IS NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM research_tree_nodes n "
        "                WHERE n.tree_id = t.id AND n.status IN ('pending','running'))"
    )
    for r in rows:
        if await _settle_tree(r["id"]):
            settled += 1
    return settled


async def tick() -> dict[str, Any]:
    """Scheduler job body (5-min gated interval — it starts model work;
    mount in PATCH-NOTES-D4.md). Never raises."""
    out: dict[str, Any] = {"claimed": 0, "completed": 0, "failed": 0, "finalized": 0}
    try:
        out["finalized"] = await _sweep_trees()
        limits = await get_limits()
        nodes: list[dict[str, Any]] = []
        while len(nodes) < NODES_PER_TICK:
            node = await _claim_next_node(limits["node_concurrency"])
            if node is None:
                break
            nodes.append(node)
        out["claimed"] = len(nodes)
        if not nodes:
            return out
        results = await asyncio.gather(*(_run_node(n) for n in nodes), return_exceptions=True)
        for res in results:
            if isinstance(res, BaseException):  # _run_node guards internally; belt only
                log.error("run_node raised despite guards: %r", res)
            elif res == "completed":
                out["completed"] += 1
            elif res == "failed":
                out["failed"] += 1
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("research tree tick failed")
    return out


async def recover_orphans() -> int:
    """Boot-time sweep (the research.py idiom; lifespan mount line in
    PATCH-NOTES-D4.md). Running nodes under TERMINAL trees are pruned — a
    stopped/finished tree must never re-run work, and a requeued pending row
    there would be unclaimable by design (REVIEW-D4 H2). Running nodes under
    live trees requeue as pending. Unannounced terminal trees are settled by
    the next tick's sweep — deliberately NOT here, so their tree.completed
    events fire after the vault exporter has registered."""
    now = bus.now_iso()
    stranded = await db.execute(
        "UPDATE research_tree_nodes SET status='pruned', task_id=NULL, finished_at=? "
        "WHERE status = 'running' AND tree_id IN "
        "(SELECT id FROM research_trees WHERE status IN ('completed','stopped','failed'))",
        (now,),
    )
    if stranded:
        log.warning("pruned %d running nodes stranded under terminal trees by restart", stranded)
    n = await db.execute(
        "UPDATE research_tree_nodes SET status='pending', task_id=NULL WHERE status='running'"
    )
    if n:
        log.warning("requeued %d research tree nodes orphaned by restart", n)
    return stranded + n


# ---- failed-node retry -------------------------------------------------------

class TransitionConflict(RuntimeError):
    """A research-tree state transition is not currently allowed (API: 409)."""


async def retry_node(tree_id: str, node_id: str) -> dict[str, Any]:
    """Requeue exactly one failed node for the normal BFS drain.

    This is deliberately a bounded manual retry, not an automatic retry loop:
    only ``failed -> pending`` is accepted, so a duplicate/concurrent request
    sees the already-pending node and conflicts. A stopped tree remains final.
    Retrying a node from a completed/failed tree atomically reopens the tree as
    ``exploring`` and clears its final-snapshot markers; after the new attempt
    drains, the normal arbiter emits a fresh ``tree.completed`` snapshot.
    """
    previous_tree_status = ""
    depth = 0
    topic = ""
    async with db.transaction() as conn:
        # This conditional UPDATE is the arbiter and the first mutable
        # statement, so concurrent callers cannot both win.
        cur = await conn.execute(
            "UPDATE research_tree_nodes "
            "SET status='pending', task_id=NULL, summary=NULL, score=NULL, finished_at=NULL "
            "WHERE id = ? AND tree_id = ? AND status = 'failed' "
            "AND EXISTS (SELECT 1 FROM research_trees t "
            "            WHERE t.id = ? AND t.status != 'stopped')",
            (node_id, tree_id, tree_id),
        )
        claimed = cur.rowcount
        await cur.close()
        if not claimed:
            cur = await conn.execute(
                "SELECT status FROM research_trees WHERE id = ?", (tree_id,)
            )
            tree = await cur.fetchone()
            await cur.close()
            if tree is None:
                raise LookupError(f"research tree {tree_id} not found")
            cur = await conn.execute(
                "SELECT status FROM research_tree_nodes WHERE id = ? AND tree_id = ?",
                (node_id, tree_id),
            )
            node = await cur.fetchone()
            await cur.close()
            if node is None:
                raise LookupError(f"research tree node {node_id} not found in tree {tree_id}")
            if node["status"] != "failed":
                raise TransitionConflict(
                    f"only failed research tree nodes can be retried (status: {node['status']})"
                )
            raise TransitionConflict(f"research tree {tree_id} is stopped and cannot be retried")

        cur = await conn.execute(
            "SELECT depth, topic FROM research_tree_nodes WHERE id = ?", (node_id,)
        )
        node = await cur.fetchone()
        await cur.close()
        cur = await conn.execute(
            "SELECT status FROM research_trees WHERE id = ?", (tree_id,)
        )
        tree = await cur.fetchone()
        await cur.close()
        depth = int(node["depth"])
        topic = str(node["topic"])
        previous_tree_status = str(tree["status"])
        cur = await conn.execute(
            "UPDATE research_trees "
            "SET status='exploring', finished_at=NULL, announced_at=NULL "
            "WHERE id = ? AND status IN ('completed','failed')",
            (tree_id,),
        )
        await cur.close()

    await bus.emit(NODE_RETRIED_EVENT, "research_tree", tree_id, {
        "tree_id": tree_id,
        "node_id": node_id,
        "depth": depth,
        "topic": topic,
        "status": "pending",
        "previous_tree_status": previous_tree_status,
    })
    tree = await get_tree(tree_id)
    assert tree is not None  # protected by the node FK and transaction above
    return tree


# ---- stop ---------------------------------------------------------------------

async def stop_tree(tree_id: str) -> dict[str, Any] | None:
    """Stop exploring. The tree flip and the pending-node prune commit in
    ONE transaction (REVIEW-D4 H2): a concurrently completing parent either
    commits its children before us (we prune them here) or reads the stopped
    tree inside its own transaction and inserts nothing — stranded pending
    rows cannot exist either way. Running nodes finish naturally (results
    kept, children blocked); the ``tree.completed`` snapshot fires when the
    last of them settles — immediately, when nothing is running. Idempotent
    on terminal trees (no second event: the announce arbiter)."""
    tree = await db.query_one("SELECT id, status FROM research_trees WHERE id = ?", (tree_id,))
    if tree is None:
        return None
    now = bus.now_iso()
    stopped_now = pruned = 0
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE research_trees SET status='stopped', finished_at=? "
            "WHERE id = ? AND status IN ('pending','exploring')",
            (now, tree_id),
        )
        stopped_now = cur.rowcount
        await cur.close()
        if stopped_now:
            cur = await conn.execute(
                "UPDATE research_tree_nodes SET status='pruned', finished_at=? "
                "WHERE tree_id = ? AND status = 'pending'",
                (now, tree_id),
            )
            pruned = cur.rowcount
            await cur.close()
    if stopped_now:
        log.info("research tree %s stopped (%d pending nodes pruned)", tree_id, pruned)
        await _announce_if_drained(tree_id)
    return await get_tree(tree_id)


# ---- queries (API read surface) --------------------------------------------------

async def get_tree(tree_id: str) -> dict[str, Any] | None:
    """Tree row + flat node list with parent references (cycle-proof; the
    viewer rebuilds nesting from parent_id)."""
    tree = await db.query_one("SELECT * FROM research_trees WHERE id = ?", (tree_id,))
    if tree is None:
        return None
    tree["nodes"] = await db.query(
        "SELECT * FROM research_tree_nodes WHERE tree_id = ? "
        "ORDER BY depth ASC, created_at ASC, id ASC",
        (tree_id,),
    )
    return tree


async def list_trees(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT t.*, "
        "(SELECT COUNT(*) FROM research_tree_nodes n WHERE n.tree_id = t.id) AS nodes_total, "
        "(SELECT COUNT(*) FROM research_tree_nodes n WHERE n.tree_id = t.id "
        " AND n.status = 'completed') AS nodes_completed "
        "FROM research_trees t"
    )
    params: list[Any] = []
    if status:
        sql += " WHERE t.status = ?"
        params.append(status)
    sql += " ORDER BY t.created_at DESC, t.id DESC LIMIT ?"
    params.append(min(max(int(limit), 1), 200))
    return await db.query(sql, params)
