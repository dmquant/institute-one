"""Whiteboard similarity gate + diversity pick + category weights (Phase 1a).

The gate matrix under a fake embedder (no Ollama involved):
- cosine ≥ skip_threshold within skip_window_days   → topic skipped, stays pending
- ≥ augment_threshold within augment_window_days    → board opens with BUILD-ON block
- below augment_threshold                           → plain board
- vectors unavailable (the default reality)         → gate opens, prompt byte-identical
Plus: verdict caching (no hourly re-embed), diversity penalty re-ordering,
category rotation guard, and the config/weights API round-trip.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import vectors, whiteboard
from app.institute.analysts import get_analyst
from app.institute.prompts import (
    CITATION_MANDATE,
    FILE_DELIVERABLE,
    persona_block,
    work_date,
)

KEYWORD_DIMS = {"gpu": 0, "cpu": 1, "zebra": 2}
FAKE_DIM = 16


def fake_vec(text: str) -> list[float]:
    """Deterministic bag-of-keywords embedding (cosine-friendly)."""
    vec = [0.0] * FAKE_DIM
    for token in text.lower().split():
        vec[KEYWORD_DIMS.get(token, 3)] += 1.0
    if not any(vec):
        vec[3] = 1.0
    return vec


@pytest.fixture
def fake_embedder(monkeypatch):
    """Replace vectors.embed with a deterministic local embedder; returns the
    call log so tests can assert on (the absence of) re-embedding."""
    calls: list[str] = []

    async def _fake_embed(text: str) -> list[float] | None:
        calls.append(text)
        return fake_vec(text)

    monkeypatch.setattr(vectors, "embed", _fake_embed)
    return calls


async def _drain_bg(max_rounds: int = 50) -> None:
    for _ in range(max_rounds):
        tasks = list(whiteboard._bg_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("whiteboard background tasks never drained")


async def _seed_board(
    topic: str, question: str = "", *,
    days_ago: float = 0, created_at: str | None = None,
    category: str | None = None, status: str = "completed",
    with_vector: bool = True, vec: list[float] | None = None,
) -> str:
    """Insert a historical board row (+ optional topic vector) directly."""
    board_id = uuid.uuid4().hex[:12]
    created = created_at or whiteboard._iso_ago(days=days_ago)
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, work_date, category, created_at, updated_at) "
        "VALUES (?,?,?,?,5,?,?,?,?)",
        (board_id, topic, question, status, work_date(), category, created, created),
    )
    if with_vector:
        if vec is None:
            vec = fake_vec(whiteboard._topic_embed_text(topic, question))
        await db.execute(
            "INSERT INTO whiteboard_topic_vectors (board_id, model, dim, embedding, created_at) "
            "VALUES (?,?,?,?,?)",
            (board_id, vectors._model(), len(vec), whiteboard._pack_vec(vec), created),
        )
    return board_id


async def _first_card_prompt(board_id: str) -> str:
    """Drive the first card to completion on echo and return its task prompt."""
    for _ in range(10):
        await whiteboard.tick()
        await _drain_bg()
        board = await whiteboard.get_board(board_id)
        card = board["cards"][0]
        if card["status"] in ("completed", "failed") and card["task_id"]:
            row = await db.query_one("SELECT prompt FROM tasks WHERE id = ?", (card["task_id"],))
            assert row, "card task row missing"
            return row["prompt"]
    raise AssertionError("first card never finished")


# ---- the gate: skip / augment / pass ---------------------------------------

async def test_high_similarity_within_window_skips_and_caches(fake_embedder):
    prior = await _seed_board("gpu gpu gpu gpu", days_ago=3)
    added = await whiteboard.add_topic("gpu gpu gpu gpu", "还有什么新变化？", source="test")

    assert await whiteboard.kickoff() is None  # the only candidate was skipped

    row = await db.query_one("SELECT * FROM topic_pool WHERE id = ?", (added["id"],))
    assert row["status"] == "pending"           # never claimed — skip ≠ consume
    assert row["similarity_state"] == "skip"
    assert row["similar_board_id"] == prior
    assert row["similarity_checked_at"]
    boards = await db.query("SELECT id FROM whiteboard_boards")
    assert [b["id"] for b in boards] == [prior]  # no new board

    # verdict cache: the next hourly kickoff must NOT re-embed the topic —
    # the SQL candidate filter excludes fresh skips entirely
    n_embeds = len(fake_embedder)
    assert n_embeds >= 1
    assert await whiteboard.kickoff() is None
    assert len(fake_embedder) == n_embeds


async def test_high_similarity_outside_skip_window_augments(fake_embedder):
    # identical topic, but the board is 20 days old: outside 14d skip window,
    # inside 30d augment window → open, but BUILD ON prior work
    prior = await _seed_board("gpu gpu gpu gpu", days_ago=20)
    await whiteboard.add_topic("gpu gpu gpu gpu", "复盘一下？", source="test")

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["prior_board_id"] == prior


async def test_mid_similarity_opens_with_build_on_block(fake_embedder):
    # cosine((4,0),(3,2)) = 12/(4·√13) ≈ 0.832 → inside [0.65, 0.85)
    prior = await _seed_board("gpu gpu gpu gpu", days_ago=5)
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, summary, created_at) "
        "VALUES (?,?,1,'tech-analyst','completed','','先前结论：算力紧缺仍在,重点看HBM供给',?)",
        (uuid.uuid4().hex[:12], prior, bus.now_iso()),
    )
    await whiteboard.add_topic("gpu gpu gpu cpu cpu", source="test")

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["prior_board_id"] == prior
    topic_row = await db.query_one("SELECT * FROM topic_pool WHERE status='used'")
    assert topic_row["similarity_state"] == "augment"

    prompt = await _first_card_prompt(board_id)
    assert "延续先前白板（BUILD ON prior work）" in prompt
    assert "gpu gpu gpu gpu" in prompt                      # prior topic named
    assert "先前结论：算力紧缺仍在" in prompt                 # prior summary carried in


async def test_low_similarity_passes_clean(fake_embedder):
    await _seed_board("gpu gpu gpu gpu", days_ago=2)
    await whiteboard.add_topic("zebra zebra zebra", source="test")  # orthogonal

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["prior_board_id"] is None

    prompt = await _first_card_prompt(board_id)
    assert "延续先前白板" not in prompt
    # the passing topic's vector was stored for FUTURE gates (via the gate's
    # own embedding — no second embed needed)
    vec_row = await db.query_one(
        "SELECT * FROM whiteboard_topic_vectors WHERE board_id = ?", (board_id,)
    )
    assert vec_row is not None
    assert vec_row["dim"] == FAKE_DIM


async def test_skipped_topic_reevaluated_after_cache_expiry(fake_embedder):
    """A stale skip verdict (checked_at older than the TTL) is re-evaluated."""
    await _seed_board("gpu gpu gpu gpu", days_ago=3)
    added = await whiteboard.add_topic("gpu gpu gpu gpu", "q", source="test")
    assert await whiteboard.kickoff() is None
    # age the verdict beyond the 24h TTL
    await db.execute(
        "UPDATE topic_pool SET similarity_checked_at = ? WHERE id = ?",
        (whiteboard._iso_ago(hours=whiteboard.SIMILARITY_CACHE_TTL_H + 1), added["id"]),
    )
    n = len(fake_embedder)
    assert await whiteboard.kickoff() is None   # still similar → skipped again
    assert len(fake_embedder) == n + 1          # but it WAS re-embedded once


# ---- the window/threshold matrix (REVIEW-B4 M1) -----------------------------

DEFAULT_CFG = dict(whiteboard.SIMILARITY_DEFAULTS)          # skip 0.85/14d, augment 0.65/30d
INVERTED_CFG = {**DEFAULT_CFG, "skip_window_days": 30, "augment_window_days": 14}


def _classify(cfg: dict, sim: float, created_at: str) -> str:
    return whiteboard._classify_prior(
        sim, created_at, cfg,
        skip_cutoff=whiteboard._iso_ago(days=cfg["skip_window_days"]),
        augment_cutoff=whiteboard._iso_ago(days=cfg["augment_window_days"]),
    )


@pytest.mark.parametrize(
    "sim,age_days,expected",
    [
        (0.86, 13, "skip"),        # high similarity inside the skip window
        (0.85, 13, "skip"),        # exactly at the skip threshold (inclusive)
        (0.849, 13, "augment"),    # just below skip threshold → augment tier
        (0.90, 20, "augment"),     # high similarity but outside 14d → augment
        (0.65, 29, "augment"),     # exactly at the augment threshold (inclusive)
        (0.649, 13, "pass"),       # below the augment threshold everywhere
        (0.90, 31, "pass"),        # outside both windows
        (0.65, 31, "pass"),
    ],
)
def test_classify_prior_default_windows(sim, age_days, expected):
    assert _classify(DEFAULT_CFG, sim, whiteboard._iso_ago(days=age_days)) == expected


def test_classify_prior_window_edges_inclusive():
    """A board exactly AT a cutoff instant is inside that window."""
    skip_cutoff = whiteboard._iso_ago(days=DEFAULT_CFG["skip_window_days"])
    augment_cutoff = whiteboard._iso_ago(days=DEFAULT_CFG["augment_window_days"])
    assert whiteboard._classify_prior(
        0.86, skip_cutoff, DEFAULT_CFG,
        skip_cutoff=skip_cutoff, augment_cutoff=augment_cutoff,
    ) == "skip"
    assert whiteboard._classify_prior(
        0.65, augment_cutoff, DEFAULT_CFG,
        skip_cutoff=skip_cutoff, augment_cutoff=augment_cutoff,
    ) == "augment"


@pytest.mark.parametrize(
    "sim,age_days,expected",
    [
        (1.00, 20, "skip"),        # REVIEW-B4 M1 repro: skip window covers it
        (0.86, 29, "skip"),        # deep in the (long) skip window
        (0.70, 20, "pass"),        # augment tier checks ITS OWN 14d cutoff
        (0.70, 13, "augment"),     # inside the (short) augment window
        (0.86, 31, "pass"),        # outside both windows
    ],
)
def test_classify_prior_inverted_windows(sim, age_days, expected):
    """skip_window_days > augment_window_days is a legal configuration:
    each verdict uses only its own window."""
    assert _classify(INVERTED_CFG, sim, whiteboard._iso_ago(days=age_days)) == expected


async def test_inverted_windows_still_skip_end_to_end(fake_embedder):
    """REVIEW-B4 M1 integration repro: a 20-day-old identical board with
    skip=30d/augment=14d must SKIP (it used to pass because the query only
    looked back over the augment window)."""
    await whiteboard.set_similarity_config({"skip_window_days": 30, "augment_window_days": 14})
    prior = await _seed_board("gpu gpu gpu gpu", days_ago=20)
    added = await whiteboard.add_topic("gpu gpu gpu gpu", "q", source="test")

    assert await whiteboard.kickoff() is None
    row = await db.query_one("SELECT * FROM topic_pool WHERE id = ?", (added["id"],))
    assert row["status"] == "pending"
    assert row["similarity_state"] == "skip"
    assert row["similar_board_id"] == prior
    assert await db.query_one(
        "SELECT id FROM whiteboard_boards WHERE id != ?", (prior,)
    ) is None


# ---- verdict cache invalidation (REVIEW-B4 M2) ------------------------------

async def test_threshold_change_invalidates_fresh_skip(fake_embedder):
    """A fresh 'skip' verdict must NOT survive a threshold change: the
    fingerprint flips, the topic re-enters candidates and is re-evaluated."""
    prior = await _seed_board("gpu gpu gpu gpu", days_ago=3)
    added = await whiteboard.add_topic("gpu gpu gpu gpu", "q", source="test")

    assert await whiteboard.kickoff() is None      # evaluated → fresh skip
    n = len(fake_embedder)
    assert await whiteboard.kickoff() is None      # cache holds within the TTL
    assert len(fake_embedder) == n

    await whiteboard.set_similarity_config({"skip_threshold": 1.01})  # nothing can skip now
    board_id = await whiteboard.kickoff()
    assert board_id is not None                    # re-evaluated → augment → board opens
    assert len(fake_embedder) == n + 1             # exactly one re-embed
    board = await whiteboard.get_board(board_id)
    assert board["prior_board_id"] == prior
    row = await db.query_one("SELECT * FROM topic_pool WHERE id = ?", (added["id"],))
    assert row["similarity_state"] == "augment"


async def test_model_switch_invalidates_fresh_skip(fake_embedder, monkeypatch):
    """Switching the embedding model flips the fingerprint AND hides old
    boards' vectors (model-filtered join, A8 semantics) → immediate re-eval,
    gate passes, board opens."""
    await _seed_board("gpu gpu gpu gpu", days_ago=3)   # vector stored under bge-m3
    added = await whiteboard.add_topic("gpu gpu gpu gpu", "q", source="test")

    assert await whiteboard.kickoff() is None      # fresh skip under the old model
    n = len(fake_embedder)

    monkeypatch.setattr(vectors, "_model", lambda: "bge-m4-next")
    board_id = await whiteboard.kickoff()
    assert board_id is not None                    # old vectors invisible → pass
    assert len(fake_embedder) == n + 1             # re-evaluated despite the fresh TTL
    row = await db.query_one("SELECT * FROM topic_pool WHERE id = ?", (added["id"],))
    assert row["status"] == "used"
    assert row["similarity_state"] == "pass"
    # the new board's vector is stored under the NEW model tag
    vec_row = await db.query_one(
        "SELECT model FROM whiteboard_topic_vectors WHERE board_id = ?", (board_id,)
    )
    assert vec_row["model"] == "bge-m4-next"


# ---- degradation: no vectors == the pre-gate behavior, byte for byte -------

async def test_vectors_unavailable_gate_opens_and_prompt_is_byte_identical():
    # a highly similar prior board WITH a stored vector exists, but embed()
    # degrades (enable_vectors=False is the test default) → the gate must
    # not skip, not augment, not cache, and the card prompt must be exactly
    # the pre-Phase-1a assembly.
    await _seed_board("gpu topic alpha", "question one", days_ago=2)
    added = await whiteboard.add_topic("gpu topic alpha", "question one bis", source="test")

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["prior_board_id"] is None

    row = await db.query_one("SELECT * FROM topic_pool WHERE id = ?", (added["id"],))
    assert row["status"] == "used"
    assert row["similarity_state"] is None      # degraded gate writes no verdict
    # no vector stored for the new board either (embed returned None)
    assert await db.query_one(
        "SELECT board_id FROM whiteboard_topic_vectors WHERE board_id = ?", (board_id,)
    ) is None

    prompt = await _first_card_prompt(board_id)
    card = (await whiteboard.get_board(board_id))["cards"][0]
    analyst = get_analyst(card["analyst_id"])
    output_file = f"card-01-{analyst.id}.md"
    question = card["question"] or board["question"] or board["topic"]
    task_text = (
        "白板协作任务（多位分析师接力研讨）。\n"
        f"主题：{board['topic']}\n"
        f"总问题：{board['question'] or '（无，围绕主题展开）'}\n"
        f"本卡片要回答的问题：{question}\n"
        "协作要求：先明确表态你同意或反驳前面哪位同事的哪一个观点（你是第一张卡片则直接给出开局判断），"
        "再展开你自己的分析，最后以「## 核心结论」收尾。"
    )
    # byte-identical below the (time-varying) date anchor: cut the first
    # block and compare the ENTIRE remainder — nothing may be inserted
    # anywhere, not just at the edges
    anchor, sep, rest = prompt.partition("\n\n")
    assert sep == "\n\n"
    assert anchor.startswith("【时间锚点】") and "\n" not in anchor
    expected_rest = (
        persona_block(analyst)
        + "\n\n" + f"## 任务\n{task_text}"
        + "\n\n" + CITATION_MANDATE
        + "\n\n" + FILE_DELIVERABLE.format(filename=output_file)
    )
    assert rest == expected_rest


async def test_default_pick_is_pure_score_order():
    """No categories, no weights, no recent boards → original pick order."""
    await whiteboard.add_topic("zebra low", source="test", score=1.0)
    await whiteboard.add_topic("zebra high", source="test", score=2.0)
    board_id = await whiteboard.kickoff()
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "zebra high"


# ---- diversity penalty + category rotation guard ---------------------------

async def test_diversity_penalty_reorders_pick():
    await _seed_board("旧板1", days_ago=1, category="tech", with_vector=False)
    await _seed_board("旧板2", days_ago=2, category="tech", with_vector=False)
    await whiteboard.add_topic("tech topic", source="test", score=1.0, category="tech")
    await whiteboard.add_topic("macro topic", source="test", score=0.9, category="macro")

    # tech: 1.0 − 0.15×2 = 0.7 < macro: 0.9 − 0 → macro wins despite lower raw score
    board_id = await whiteboard.kickoff()
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "macro topic"
    assert board["category"] == "macro"


async def test_category_weight_scales_score():
    await whiteboard.set_category_weight("tech", 0.4)
    await whiteboard.add_topic("tech topic", source="test", score=1.0, category="tech")
    await whiteboard.add_topic("macro topic", source="test", score=0.5, category="macro")

    # tech: 1.0×0.4 = 0.4 < macro: 0.5×1.0 → the weight alone flips the pick
    board_id = await whiteboard.kickoff()
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "macro topic"


async def test_rotation_guard_forces_category_switch():
    for i in range(3):
        await _seed_board(f"tech板{i}", days_ago=0.1 * (i + 1), category="tech", with_vector=False)
    # tech survives the penalty (5.0 − 0.45 = 4.55 ≫ 0.5) — only the guard flips it
    await whiteboard.add_topic("tech topic strong", source="test", score=5.0, category="tech")
    await whiteboard.add_topic("macro topic weak", source="test", score=0.5, category="macro")

    board_id = await whiteboard.kickoff()
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "macro topic weak"
    assert board["category"] == "macro"


async def test_rotation_guard_lets_streak_continue_without_alternative():
    for i in range(3):
        await _seed_board(f"tech板{i}", days_ago=0.1 * (i + 1), category="tech", with_vector=False)
    await whiteboard.add_topic("tech only topic", source="test", score=1.0, category="tech")

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "tech only topic"


async def test_rotation_guard_inactive_below_streak():
    """Two same-category boards with max_streak=3 must not trigger the guard."""
    await _seed_board("tech板0", days_ago=0.1, category="tech", with_vector=False)
    await _seed_board("tech板1", days_ago=0.2, category="tech", with_vector=False)
    await whiteboard.add_topic("tech topic strong", source="test", score=5.0, category="tech")
    await whiteboard.add_topic("macro topic weak", source="test", score=0.5, category="macro")

    board_id = await whiteboard.kickoff()
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "tech topic strong"


# ---- config + weights API ---------------------------------------------------

async def test_similarity_config_and_weights_api_roundtrip():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/whiteboard/similarity-config")
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["skip_threshold"] == pytest.approx(0.85)
        assert cfg["augment_threshold"] == pytest.approx(0.65)
        assert cfg["skip_window_days"] == 14
        assert cfg["augment_window_days"] == 30

        r = await client.put(
            "/api/whiteboard/similarity-config",
            json={"skip_threshold": 0.9, "rotation_max_streak": 2},
        )
        assert r.status_code == 200
        r = await client.get("/api/whiteboard/similarity-config")
        cfg = r.json()
        assert cfg["skip_threshold"] == pytest.approx(0.9)     # updated
        assert cfg["rotation_max_streak"] == 2                 # updated
        assert cfg["augment_threshold"] == pytest.approx(0.65)  # untouched

        r = await client.put(
            "/api/whiteboard/similarity-config", json={"skip_threshold": 1.5}
        )
        assert r.status_code == 422  # out of range

        r = await client.put(
            "/api/whiteboard/category-weights", json={"category": "tech", "weight": 1.5}
        )
        assert r.status_code == 200
        r = await client.put(
            "/api/whiteboard/category-weights", json={"category": "tech", "weight": 0.5}
        )
        assert r.status_code == 200  # upsert overwrites
        r = await client.get("/api/whiteboard/category-weights")
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["category"] == "tech"
        assert rows[0]["weight"] == pytest.approx(0.5)

        r = await client.put(
            "/api/whiteboard/category-weights", json={"category": "tech", "weight": -1}
        )
        assert r.status_code == 422


async def test_config_change_takes_effect_in_gate(fake_embedder):
    """Raising skip_threshold via the config row turns a skip into an augment."""
    await _seed_board("gpu gpu gpu gpu", days_ago=3)
    added = await whiteboard.add_topic("gpu gpu gpu gpu", "q", source="test")

    await whiteboard.set_similarity_config({"skip_threshold": 1.01})  # nothing can skip
    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["prior_board_id"] is not None  # cosine 1.0 ≥ 0.65 → augment
    row = await db.query_one("SELECT * FROM topic_pool WHERE id = ?", (added["id"],))
    assert row["similarity_state"] == "augment"
