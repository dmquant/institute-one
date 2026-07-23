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
import math
import shutil
import struct
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..hands.registry import get_registry
from ..router import executor
from ..util import new_id
from . import vectors
from .analysts import get_analyst, roster
from .claims import claim_admin_state, lease_stale_checker, release_admin_state
from .prompts import (
    date_anchor,
    extract_summary,
    previous_steps_block,
    work_date,
)

log = logging.getLogger("institute.whiteboard")

# whiteboard_boards.status enum — canonical code constant mirroring the CHECK
# in migrations/0001_init.sql. Import point for API surfaces (/api/contract).
BOARD_STATUSES = ("active", "completed", "stopped", "failed")

MAX_ACTIVE_BOARDS = 2
DEFAULT_MAX_CARDS = 5
HANDOFF_TIMEOUT_S = 300
TOPIC_CLAIM_LEASE_S = 45 * 60
ORPHAN_SESSION_GRACE_S = 60 * 60
TOPIC_CLAIM_PREFIX = "whiteboard_topic_claim:"

# ---- similarity gate + diversity pick (Phase 1a) --------------------------
# Thresholds/knobs live in admin_state (key below) as one JSON row seeded by
# migration 0011; these defaults are the in-code fallback if the row is gone.
#
# Threshold provenance: the 0.85/0.65 defaults come from the proposal (§6.3),
# NOT from a measured bge-m3 calibration — no Ollama on this machine yet. The
# ROADMAP 1a "~50 known pairs" sanity check exists as a synthetic equivalent:
# tests/test_similarity_calibration.py runs _classify_prior over 60 hand-
# written CJK finance pairs (paraphrase/related/unrelated tiers) under a
# deterministic char-n-gram proxy embedder and asserts the tier→verdict
# distribution. Real calibration once Ollama is up = the SAME test with the
# real embedder: INSTITUTE_CALIBRATION_REAL=1 pytest
# tests/test_similarity_calibration.py -rP — it prints the measured
# distribution under 0.85/0.65 plus suggested cut points; apply tuned values
# via PUT /api/whiteboard/similarity-config (the admin_state row), not here.
SIMILARITY_CONFIG_KEY = "whiteboard_similarity"
SIMILARITY_DEFAULTS: dict[str, Any] = {
    "skip_threshold": 0.85,      # cosine ≥ this + a board within skip_window_days → skip topic
    "skip_window_days": 14,
    "augment_threshold": 0.65,   # cosine ≥ this within augment_window_days → BUILD ON prior work
    "augment_window_days": 30,
    "diversity_penalty": 0.15,   # per recent same-category board, subtracted from the score
    "diversity_window_days": 7,
    "rotation_max_streak": 3,    # this many consecutive same-category boards force a switch
}
SIMILARITY_CACHE_TTL_H = 24     # gate verdicts are cached on the topic row this long
KICKOFF_CANDIDATES = 10         # how many pool topics one kickoff considers

# New constant (NOT a modification of any existing prompt string): joined into
# the card prompt as an extra context block when the board augments prior work.
BUILD_ON_PRIOR_BLOCK = """\
## 延续先前白板（BUILD ON prior work）
研究所近期已就相近主题完成过白板研讨：「{prior_topic}」（{prior_date}）。
先前讨论的收尾结论摘要：
{prior_summaries}
本次讨论必须在先前工作的基础上推进：优先补充增量信息、验证或修正先前结论，避免从零重复已有共识。\
"""

# Cards being driven by THIS process. A 'running' card not in here was orphaned
# by a restart (executor.recover_orphans already failed its task).
_active_cards: set[str] = set()
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro: Any) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


# ---- topic pool ----------------------------------------------------------

async def add_topic(
    topic: str, question: str = "", source: str = "manual", score: float = 1.0,
    category: str | None = None,
) -> dict[str, Any]:
    content_hash = hashlib.sha256((topic + question).encode("utf-8")).hexdigest()[:16]
    # rowcount is the one authoritative "did THIS call insert" signal: hash
    # aliasing and concurrent same-args calls make any pre-check/read-back lie
    n = await db.execute(
        "INSERT OR IGNORE INTO topic_pool (topic, question, source, score, status, content_hash, category, created_at) "
        "VALUES (?,?,?,?, 'pending', ?, ?, ?)",
        (topic, question, source, score, content_hash, category or None, bus.now_iso()),
    )
    row = await db.query_one("SELECT * FROM topic_pool WHERE content_hash = ?", (content_hash,))
    if n:
        # emitted here in the domain layer so EVERY add path (API, MCP, daily /
        # research follow-ups) publishes exactly one topic_pool.added per real
        # insert, keyed off the same atomic INSERT OR IGNORE rowcount verdict —
        # never a phantom event for a deduped call.
        await bus.emit(
            "topic_pool.added", "topic", str((row or {}).get("id", "")),
            {"topic": topic, "source": source},
        )
    return {**(row or {}), "inserted": bool(n)}


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


# ---- topic claim lease + orphan reaper ------------------------------------

def _topic_claim_key(topic_id: int) -> str:
    return f"{TOPIC_CLAIM_PREFIX}{topic_id}"


def _topic_claim_token(owner: str) -> str:
    return json.dumps({"owner": owner, "claimed_at": bus.now_iso()})


async def _topic_claim_stale(value: str, key: str) -> bool:
    """Stale = lease expired / future claimed_at / corrupt token (shared
    claims.py checker); the clock stays bus.now_iso-based, as this module's
    timestamps all are."""
    return await lease_stale_checker(
        TOPIC_CLAIM_LEASE_S,
        now=lambda: datetime.fromisoformat(bus.now_iso()),
        label="topic claim",
    )(value, key)


async def _claim_topic(topic_id: int) -> tuple[str, str] | None:
    """Claim a pool topic, taking over an expired claim with an exact-value CAS
    (the shared claims.claim_admin_state idiom)."""
    return await claim_admin_state(
        _topic_claim_key(topic_id),
        make_token=lambda: _topic_claim_token(new_id()),
        is_stale=_topic_claim_stale,
    )


async def _release_topic_claim(key: str, token: str) -> None:
    # A timed-out zombie must not erase the newer owner's takeover claim.
    await release_admin_state(key, token)


async def reap_orphans() -> dict[str, int]:
    """Reap stale topic claims and old whiteboard sessions with no board.

    The session has no status column, so a board row is its durable lifecycle
    marker. A board-less session is reaped only after one hour, when it has
    also seen no recent touch and owns no live task. Never raises.
    """
    stats = {
        "claims_reaped": 0,
        "topics_requeued": 0,
        "sessions_reaped": 0,
        "workspaces_reaped": 0,
    }
    try:
        claims = await db.query(
            "SELECT key, value FROM admin_state WHERE substr(key, 1, ?) = ?",
            (len(TOPIC_CLAIM_PREFIX), TOPIC_CLAIM_PREFIX),
        )
        for row in claims:
            if not await _topic_claim_stale(row["value"], row["key"]):
                continue
            deleted = await db.execute(
                "DELETE FROM admin_state WHERE key = ? AND value = ?",
                (row["key"], row["value"]),
            )
            if not deleted:
                continue
            stats["claims_reaped"] += 1
            try:
                topic_id = int(row["key"][len(TOPIC_CLAIM_PREFIX):])
            except ValueError:
                log.warning("reaped malformed whiteboard topic claim key %r", row["key"])
                continue
            try:
                claimed_board_id = str(json.loads(row["value"])["owner"])
            except (ValueError, KeyError, TypeError):
                claimed_board_id = ""
            # Compatibility repair for crashes under the old status='used'
            # claim. For new claims, owner is the reserved board id, giving the
            # reaper an exact no-migration link from claim to landed board.
            stats["topics_requeued"] += await db.execute(
                "UPDATE topic_pool SET status='pending' "
                "WHERE id=? AND status='used' "
                "AND NOT EXISTS (SELECT 1 FROM whiteboard_boards WHERE id=?)",
                (topic_id, claimed_board_id),
            )
    except Exception:  # noqa: BLE001 - cleanup must never block kickoff
        log.exception("whiteboard topic-claim reaper failed")

    try:
        cutoff = _iso_ago(hours=ORPHAN_SESSION_GRACE_S / 3600)
        sessions = await db.query(
            "SELECT id, workspace_dir FROM sessions s "
            "WHERE kind='whiteboard' AND created_at < ? AND updated_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM whiteboard_boards b WHERE b.session_id=s.id) "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.session_id=s.id "
            "                AND t.status IN ('queued','running'))",
            (cutoff, cutoff),
        )
        root = get_settings().workspaces_dir.resolve()
        for session in sessions:
            deleted = await db.execute(
                "DELETE FROM sessions WHERE id=? AND kind='whiteboard' "
                "AND created_at < ? AND updated_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM whiteboard_boards b "
                "                WHERE b.session_id=sessions.id) "
                "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.session_id=sessions.id "
                "                AND t.status IN ('queued','running'))",
                (session["id"], cutoff, cutoff),
            )
            if not deleted:
                continue
            stats["sessions_reaped"] += 1
            workspace = Path(session["workspace_dir"])
            try:
                resolved = workspace.resolve()
                if resolved == root or not resolved.is_relative_to(root):
                    log.error("refusing to remove unsafe orphan workspace %s", workspace)
                elif workspace.exists():
                    await asyncio.to_thread(shutil.rmtree, workspace)
                    stats["workspaces_reaped"] += 1
            except (OSError, RuntimeError):
                log.exception("could not remove orphan whiteboard workspace %s", workspace)
    except Exception:  # noqa: BLE001 - cleanup must never block kickoff
        log.exception("whiteboard session reaper failed")

    if any(stats.values()):
        log.info("whiteboard reaper: %s", stats)
    return stats


# ---- similarity config + category weights ---------------------------------

async def get_similarity_config() -> dict[str, Any]:
    """Thresholds/knobs from admin_state, merged over the in-code defaults."""
    cfg = dict(SIMILARITY_DEFAULTS)
    try:
        row = await db.query_one(
            "SELECT value FROM admin_state WHERE key = ?", (SIMILARITY_CONFIG_KEY,)
        )
        if row:
            stored = json.loads(row["value"])
            if isinstance(stored, dict):
                for key in SIMILARITY_DEFAULTS:
                    if key in stored and isinstance(stored[key], (int, float)):
                        cfg[key] = stored[key]
    except Exception:  # noqa: BLE001 - a broken config row must not break kickoff
        log.warning("could not read %s config; using defaults", SIMILARITY_CONFIG_KEY, exc_info=True)
    return cfg


async def set_similarity_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge known keys into the config row; returns the effective config."""
    cfg = await get_similarity_config()
    for key, value in patch.items():
        if key in SIMILARITY_DEFAULTS and isinstance(value, (int, float)):
            cfg[key] = type(SIMILARITY_DEFAULTS[key])(value)
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SIMILARITY_CONFIG_KEY, json.dumps(cfg, ensure_ascii=False)),
    )
    return cfg


async def list_category_weights() -> list[dict[str, Any]]:
    return await db.query("SELECT * FROM topic_category_weights ORDER BY category")


async def set_category_weight(category: str, weight: float) -> dict[str, Any]:
    await db.execute(
        "INSERT INTO topic_category_weights (category, weight, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
        (category, weight, bus.now_iso()),
    )
    return await db.query_one(
        "SELECT * FROM topic_category_weights WHERE category = ?", (category,)
    ) or {"category": category, "weight": weight}


async def _category_weight_map() -> dict[str, float]:
    return {r["category"]: r["weight"] for r in await list_category_weights()}


# ---- similarity gate (pre-claim; inherits the vectors degradation contract) -

def _iso_ago(*, days: float = 0, hours: float = 0) -> str:
    """UTC ISO cutoff, same format as bus.now_iso() so string order == time order.

    Derives "now" from bus.now_iso() — the project's single clock source
    (hard rule 7: never datetime.now() raw).
    """
    now = datetime.fromisoformat(bus.now_iso())
    return (now - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def _topic_embed_text(topic: str, question: str) -> str:
    return f"{topic}\n{question}".strip()


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vec(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _similarity_fingerprint(cfg: dict[str, Any]) -> str:
    """Pin cached verdicts to the embedding model + the gate knobs that
    produced them (REVIEW-B4 M2). Any change to the model or to a gate
    threshold/window changes the fingerprint, which lazily invalidates every
    cached verdict (candidate SQL + gate cache both match on it) — no TTL
    wait, no bulk UPDATE. Diversity/rotation knobs are deliberately NOT part
    of the fingerprint: they order candidates but never produce verdicts.
    """
    gate_keys = ("skip_threshold", "skip_window_days", "augment_threshold", "augment_window_days")
    payload = vectors.model_name() + "|" + "|".join(f"{k}={cfg[k]}" for k in gate_keys)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _classify_prior(
    sim: float, board_created_at: str, cfg: dict[str, Any], *,
    skip_cutoff: str, augment_cutoff: str,
) -> str:
    """One prior board → the strongest verdict it can justify (REVIEW-B4 M1).

    The two windows are INDEPENDENT knobs — each verdict checks only its own
    cutoff, so no ordering between the windows is assumed:

      sim ≥ skip_threshold    AND created_at ≥ skip_cutoff    → 'skip'
      sim ≥ augment_threshold AND created_at ≥ augment_cutoff → 'augment'
      otherwise                                               → 'pass'

    Boundaries are inclusive on both axes: a board exactly at the cutoff
    instant or exactly at a threshold still counts as inside.
    """
    if sim >= cfg["skip_threshold"] and board_created_at >= skip_cutoff:
        return "skip"
    if sim >= cfg["augment_threshold"] and board_created_at >= augment_cutoff:
        return "augment"
    return "pass"


async def _store_board_vector(
    board_id: str, topic: str, question: str, vec: list[float] | None = None,
) -> None:
    """Persist the board's topic embedding for future gates. Never raises.

    Runs AFTER the board transaction committed — a failure here degrades the
    gate (this board is invisible to future similarity checks), never the
    board itself. Not vector_chunks: that table's index/search semantics are
    keyed to archive_files' current sha (rebuild-by-source), which would
    silently drop non-archive sources like a board topic.
    """
    try:
        if vec is None:
            vec = await vectors.embed(_topic_embed_text(topic, question))
        if vec is None:
            return  # vectors degraded: the gate simply won't see this board
        await db.execute(
            "INSERT OR REPLACE INTO whiteboard_topic_vectors (board_id, model, dim, embedding, created_at) "
            "VALUES (?,?,?,?,?)",
            (board_id, vectors.model_name(), len(vec), _pack_vec(vec), bus.now_iso()),
        )
    except Exception:  # noqa: BLE001 - projection only, board rows are truth
        log.warning("could not store topic vector for board %s", board_id, exc_info=True)


async def _similarity_gate(top: dict[str, Any]) -> tuple[str, str | None, list[float] | None]:
    """Compare a candidate topic against recent boards, BEFORE it is claimed.

    Returns ``(verdict, prior_board_id, topic_vec)`` with verdict one of:
    - ``skip``    — cosine ≥ skip_threshold vs a board within skip_window_days
    - ``augment`` — cosine ≥ augment_threshold within augment_window_days
    - ``pass``    — fresh topic (or vectors unavailable: degrade == open)

    The verdict is cached on the topic row (similarity_state/_checked_at,
    pinned to a model+thresholds fingerprint) so the hourly kickoff does not
    re-embed the same pending topics; a cache hit returns topic_vec=None (the
    board-open path re-embeds once if needed). A model switch or threshold
    change flips the fingerprint and lazily invalidates every cached verdict.
    Degradation (embed → None) writes NO cache and returns pass — behavior is
    byte-identical to the pre-gate kickoff. Never raises.
    """
    try:
        cfg = await get_similarity_config()
        fingerprint = _similarity_fingerprint(cfg)
        checked_at = top.get("similarity_checked_at")
        if (
            checked_at
            and checked_at > _iso_ago(hours=SIMILARITY_CACHE_TTL_H)
            and top.get("similarity_fingerprint") == fingerprint
        ):
            state = top.get("similarity_state")
            if state in ("skip", "augment", "pass"):
                return state, top.get("similar_board_id"), None

        vec = await vectors.embed(_topic_embed_text(top["topic"], top.get("question") or ""))
        if vec is None:
            return "pass", None, None  # vectors unavailable → every gate opens

        # Look back over BOTH windows: they are independent knobs, so an
        # inverted configuration (skip window longer than augment) must still
        # see boards that only the skip window covers (REVIEW-B4 M1).
        lookback_days = max(cfg["skip_window_days"], cfg["augment_window_days"])
        rows = await db.query(
            "SELECT b.id, b.created_at, v.embedding, v.dim "
            "FROM whiteboard_boards b JOIN whiteboard_topic_vectors v ON v.board_id = b.id "
            "WHERE v.model = ? AND b.created_at >= ? AND b.status != 'failed'",
            (vectors.model_name(), _iso_ago(days=lookback_days)),
        )
        skip_cutoff = _iso_ago(days=cfg["skip_window_days"])
        augment_cutoff = _iso_ago(days=cfg["augment_window_days"])
        # strongest verdict each prior board justifies; keep the most similar per verdict
        best: dict[str, tuple[float, str]] = {}
        for r in rows:
            other = _unpack_vec(r["embedding"], r["dim"])
            if len(other) != len(vec):
                continue  # different embedding space (defensive; model already filtered)
            sim = _cosine(vec, other)
            v = _classify_prior(
                sim, r["created_at"], cfg,
                skip_cutoff=skip_cutoff, augment_cutoff=augment_cutoff,
            )
            if v != "pass" and (v not in best or sim > best[v][0]):
                best[v] = (sim, r["id"])

        if "skip" in best:
            verdict, (sim, prior_id) = "skip", best["skip"]
        elif "augment" in best:
            verdict, (sim, prior_id) = "augment", best["augment"]
        else:
            verdict, sim, prior_id = "pass", 0.0, None

        await db.execute(
            "UPDATE topic_pool SET similarity_state=?, similarity_checked_at=?, "
            "similar_board_id=?, similarity_fingerprint=? WHERE id=?",
            (verdict, bus.now_iso(), prior_id, fingerprint, top["id"]),
        )
        if verdict != "pass":
            log.info(
                "similarity gate: %s topic %s (cosine %.3f vs board %s)",
                verdict, top["id"], sim, prior_id,
            )
        return verdict, prior_id, vec
    except Exception:  # noqa: BLE001 - the gate must never block kickoff
        log.exception("similarity gate failed for topic %s; passing", top.get("id"))
        return "pass", None, None


# ---- diversity pick + category rotation guard ------------------------------

def _topic_category(row: dict[str, Any]) -> str:
    return row.get("category") or "uncategorized"


async def _rotation_streak_category(max_streak: int) -> str | None:
    """The category that filled the last ``max_streak`` boards, else None."""
    if max_streak < 1:
        return None
    recent = await db.query(
        "SELECT category FROM whiteboard_boards ORDER BY created_at DESC LIMIT ?",
        (max_streak,),
    )
    if len(recent) < max_streak:
        return None
    cats = {r["category"] or "uncategorized" for r in recent}
    return cats.pop() if len(cats) == 1 else None


async def _pick_candidates(limit: int = KICKOFF_CANDIDATES) -> list[dict[str, Any]]:
    """Pool candidates ordered by diversity-adjusted score.

    effective = score × category_weight − diversity_penalty × recent same-category boards.
    With no categories, no weight rows and no recent boards this reduces to
    the original pure ``score DESC, created_at ASC`` order. Topics with a
    fresh 'skip' verdict are excluded in SQL so they cost nothing hourly —
    but only while the verdict's fingerprint still matches the current
    model+thresholds (a stale-fingerprint skip re-enters and is re-evaluated).
    """
    cfg = await get_similarity_config()
    rows = await db.query(
        "SELECT * FROM topic_pool WHERE status='pending' "
        "AND NOT (COALESCE(similarity_state,'') = 'skip' "
        "         AND COALESCE(similarity_checked_at,'') > ? "
        "         AND COALESCE(similarity_fingerprint,'') = ?) "
        "ORDER BY score DESC, created_at ASC LIMIT ?",
        (_iso_ago(hours=SIMILARITY_CACHE_TTL_H), _similarity_fingerprint(cfg), limit),
    )
    if not rows:
        return []
    weights = await _category_weight_map()
    recent = await db.query(
        "SELECT COALESCE(category,'uncategorized') AS cat, COUNT(*) AS n "
        "FROM whiteboard_boards WHERE created_at >= ? GROUP BY cat",
        (_iso_ago(days=cfg["diversity_window_days"]),),
    )
    counts = {r["cat"]: r["n"] for r in recent}

    def effective(row: dict[str, Any]) -> float:
        cat = _topic_category(row)
        return row["score"] * weights.get(cat, 1.0) - cfg["diversity_penalty"] * counts.get(cat, 0)

    rows.sort(key=effective, reverse=True)  # stable: ties keep score DESC, created_at ASC

    streak_cat = await _rotation_streak_category(int(cfg["rotation_max_streak"]))
    if streak_cat is not None and rows and _topic_category(rows[0]) == streak_cat:
        alt = next((i for i, r in enumerate(rows) if _topic_category(r) != streak_cat), None)
        if alt is not None:  # no other category available → let the streak continue
            rows.insert(0, rows.pop(alt))
            log.info(
                "category rotation guard: %d consecutive %r boards; promoting topic %s (%s)",
                int(cfg["rotation_max_streak"]), streak_cat, rows[0]["id"], _topic_category(rows[0]),
            )
    return rows


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


def _new_board_session(topic: str, now: str) -> dict[str, str]:
    """Allocate session metadata; its row lands atomically with the board."""
    session_id = new_id()
    workspace = get_settings().workspaces_dir / "sessions" / session_id
    return {
        "id": session_id,
        "title": f"WB {topic}",
        "workspace_dir": str(workspace),
        "created_at": now,
        "updated_at": now,
    }


async def _board_workspace(board: dict[str, Any]) -> Path:
    row = None
    if board.get("session_id"):
        row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (board["session_id"],))
    ws = Path(row["workspace_dir"]) if row and row["workspace_dir"] else (
        get_settings().workspaces_dir / "whiteboard" / board["id"]
    )
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def _open_board(
    topic: str, question: str = "", max_cards: int = DEFAULT_MAX_CARDS, *,
    category: str | None = None,
    prior_board_id: str | None = None,
    topic_vec: list[float] | None = None,
    topic_claim: tuple[int, str, str] | None = None,
) -> dict[str, Any]:
    if topic_claim is None:
        board_id = new_id()
    else:
        try:
            # The claim owner doubles as the reserved board id. This lets the
            # reaper distinguish "board committed, release crashed" from
            # "kickoff died before board" without adding a schema column.
            board_id = str(json.loads(topic_claim[2])["owner"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("invalid whiteboard topic claim token") from exc
    root = _match_root_analyst(f"{topic} {question}")
    now = bus.now_iso()
    wd = work_date()
    session = _new_board_session(topic, now)
    session_id = session["id"]
    # One short transaction: a claimed topic, session row, board and first
    # card land together. The workspace directory is created lazily on first
    # use, so a rolled-back transaction leaves no filesystem orphan either.
    # NB: transaction() holds the db write lock — use the yielded conn directly
    # (db.execute/bus.emit in here would deadlock); events after commit.
    async with db.transaction() as conn:
        if topic_claim is not None:
            topic_id, claim_key, claim_token = topic_claim
            cur = await conn.execute(
                "UPDATE topic_pool SET status='used' "
                "WHERE id=? AND status='pending' "
                "AND EXISTS (SELECT 1 FROM admin_state WHERE key=? AND value=?)",
                (topic_id, claim_key, claim_token),
            )
            claimed = cur.rowcount
            await cur.close()
            if claimed != 1:
                raise RuntimeError(f"lost topic claim for pool row {topic_id}")
        await conn.execute(
            "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) "
            "VALUES (?,?, 'whiteboard', ?,?,?)",
            (
                session_id, session["title"], session["workspace_dir"],
                session["created_at"], session["updated_at"],
            ),
        )
        await conn.execute(
            "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, session_id, work_date, category, prior_board_id, created_at, updated_at) "
            "VALUES (?,?,?,'active',?,?,?,?,?,?,?)",
            (board_id, topic, question, max_cards, session_id, wd, category, prior_board_id, now, now),
        )
        await conn.execute(
            "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, created_at) "
            "VALUES (?,?,1,?,'pending',?,?)",
            (new_id(), board_id, root, question, now),
        )
    # COMMIT done: the board exists. From here on no ordinary exception may
    # escape — callers (kickoff) treat a raise as "nothing landed" and release
    # the topic claim, which would let the same topic open a second board.
    try:
        Path(session["workspace_dir"]).mkdir(parents=True, exist_ok=True)
    except OSError:
        # _board_workspace retries lazily before the first card writes.
        log.exception("could not create workspace for board %s", board_id)
    try:
        await bus.emit("whiteboard.board_opened", "board", board_id, {"topic": topic})
    except Exception:  # noqa: BLE001
        log.exception("board_opened emit failed for board %s", board_id)
    # topic vector projection for future similarity gates (never raises;
    # kickoff passes the gate's vector through so nothing is embedded twice)
    await _store_board_vector(board_id, topic, question, topic_vec)
    board: dict[str, Any] | None = None
    try:
        board = await db.query_one("SELECT * FROM whiteboard_boards WHERE id = ?", (board_id,))
    except Exception:  # noqa: BLE001
        log.exception("post-commit read failed for board %s; returning committed fields", board_id)
    return board or {
        "id": board_id, "topic": topic, "question": question, "status": "active",
        "max_cards": max_cards, "session_id": session_id, "work_date": wd,
        "category": category, "prior_board_id": prior_board_id,
        "created_at": now, "updated_at": now,
    }


async def kickoff() -> str | None:
    """Open one board from the topic pool if capacity allows. Never raises.

    Candidates come diversity-ordered from ``_pick_candidates``; each passes
    a leased admin_state claim BEFORE the similarity gate, so an overlapping
    kickoff burns no model work on a live-claimed topic. The pool row becomes
    ``used`` only in the session+board+first-card transaction. Every normal
    exit CAS-releases its exact claim token; stale claims can be taken over.
    """
    try:
        # Existing periodic entrypoint doubles as the janitor hook; no extra
        # scheduler job is needed.
        await reap_orphans()
        row = await db.query_one("SELECT COUNT(*) AS n FROM whiteboard_boards WHERE status='active'")
        if row and row["n"] >= MAX_ACTIVE_BOARDS:
            return None
        for top in await _pick_candidates():
            claim = await _claim_topic(top["id"])
            if claim is None:
                continue
            claim_key, claim_token = claim
            try:
                # A concurrent expire may have changed the candidate after it
                # was selected. Check before any embedding/model call; the
                # transaction repeats the authoritative status+token check.
                fresh = await db.query_one("SELECT * FROM topic_pool WHERE id=?", (top["id"],))
                if fresh is None or fresh["status"] != "pending":
                    continue
                top = fresh
                verdict, prior_board_id, topic_vec = await _similarity_gate(top)
                if verdict == "skip":
                    continue  # pending + claim released in finally
                board = await _open_board(
                    top["topic"], top["question"], max_cards=DEFAULT_MAX_CARDS,
                    category=top.get("category"),
                    prior_board_id=prior_board_id if verdict == "augment" else None,
                    topic_vec=topic_vec,
                    topic_claim=(top["id"], claim_key, claim_token),
                )
            except Exception:  # noqa: BLE001 - board insert failed: release the claim so the topic isn't lost
                log.exception(
                    "board open failed for topic %s; transaction rolled back", top["id"],
                )
                return None
            finally:
                try:
                    await _release_topic_claim(claim_key, claim_token)
                except Exception:  # noqa: BLE001 - committed board remains authoritative
                    log.exception("could not release topic claim %s", claim_key)
            log.info("kicked off board %s: %s", board["id"], top["topic"])
            return board["id"]
        return None
    except Exception:  # noqa: BLE001 - scheduler-driven, must not raise
        log.exception("whiteboard kickoff failed")
        return None


async def create_board(
    topic: str, question: str = "", max_cards: int = DEFAULT_MAX_CARDS,
    category: str | None = None,
) -> dict[str, Any]:
    board = await _open_board(topic, question, max_cards=max_cards, category=category)
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

async def _build_on_prior_block(board: dict[str, Any]) -> str:
    """The BUILD-ON context block for augment boards; '' when N/A. Never raises."""
    prior_id = board.get("prior_board_id")
    if not prior_id:
        return ""
    try:
        prior = await db.query_one(
            "SELECT topic, work_date FROM whiteboard_boards WHERE id = ?", (prior_id,)
        )
        if prior is None:
            return ""
        cards = await db.query(
            "SELECT idx, analyst_id, summary FROM whiteboard_cards "
            "WHERE board_id = ? AND status='completed' AND COALESCE(summary,'') != '' "
            "ORDER BY idx DESC LIMIT 3",
            (prior_id,),
        )
        if cards:
            summaries = "\n".join(
                f"- [card {c['idx']} · {c['analyst_id']}] {(c['summary'] or '')[:400]}"
                for c in reversed(cards)
            )
        else:
            summaries = "（先前白板未留下卡片摘要）"
        return BUILD_ON_PRIOR_BLOCK.format(
            prior_topic=prior["topic"], prior_date=prior["work_date"], prior_summaries=summaries,
        )
    except Exception:  # noqa: BLE001 - context enrichment must not fail the card
        log.warning("build-on-prior block failed for board %s", board.get("id"), exc_info=True)
        return ""


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
        context_blocks = [context] if context else []
        # augment boards (similarity gate 0.65–0.85) prepend the BUILD-ON block;
        # ordinary boards get exactly the same blocks as before.
        prior_block = await _build_on_prior_block(board)
        if prior_block:
            context_blocks.insert(0, prior_block)

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
        from . import memory
        prompt = await memory.prompt_with_memory(
            analyst, task_text,
            context_blocks=context_blocks or None,
            output_file=output_file,
        )
        # opt-in weighted pick (settings.enable_hand_weights, default False = the
        # default hand is final): pool = available hands with a positive
        # 'whiteboard' weight row; an explicit analyst.hand is always respected.
        hand = get_registry().pick_weighted("whiteboard", explicit=analyst.hand) or (
            analyst.hand or settings.default_hand
        )
        task = await executor.submit(
            hand, prompt,
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
        (new_id(), board["id"], next_idx, analyst_id, question or "", bus.now_iso()),
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
