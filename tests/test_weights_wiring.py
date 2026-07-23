"""Hand-weights wiring at the four scope call sites (ROADMAP Phase 2 follow-up).

``settings.enable_hand_weights`` is opt-in and defaults to False. The OFF
section pins the pre-weights behaviour byte-for-byte: even with weight rows
present, every call site makes exactly the choice it made before the wiring.
The ON sections use registered fake hands plus extreme weights (0 vs 1) so the
assertions are deterministic, and one seeded-RNG test pins the distribution
shift. Explicit hands (analyst.hand / workflow step hand) always win.

Call sites under test (all four funnel through registry.pick_weighted, which
centralizes the feature switch, explicit-hand precedence, pool building and
the availability filter; pick_weighted_hand samples — resolve untouched):
- analyst_daily._pick_hand        scope 'daily'      pool = ROTATION_HANDS ∩ available
- whiteboard._run_card            scope 'whiteboard' pool = positive weight rows ∩ available
- mailbox._run_dispatch           scope 'mailbox'    pool = positive weight rows ∩ available
- workflows._workflow_hand_policy scope 'research'   pool = research_hand_names ∩ available
                                                     (hard rule 10: never outside the chain)
"""
from __future__ import annotations

import asyncio
import dataclasses
import random

from app import db
from app.config import get_settings
from app.hands.base import Hand, HandResult
from app.hands.registry import get_registry
from app.institute import analyst_daily, mailbox, whiteboard, workflows
from app.institute.analysts import get_analyst
from app.institute.analyst_daily import ROTATION_HANDS


class FakeHand(Hand):
    """Always-available stub so pools are non-empty without real CLIs."""

    hand_type = "cli"

    def __init__(self, name: str):
        self.name = name

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None) -> HandResult:
        return HandResult(output=f"[{self.name}] 已完成：这是一段足够长的正文回复。", exit_code=0)


def _register_fakes(*names: str) -> None:
    reg = get_registry()
    for n in names:
        reg.register(FakeHand(n))


def _analyst(hand: str | None = None):
    a = get_analyst("macro-analyst")
    assert a is not None
    return dataclasses.replace(a, hand=hand) if hand is not None else a


async def _drain(mod) -> None:
    for _ in range(50):
        tasks = list(mod._bg_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("background tasks never drained")


async def _run_one_card() -> None:
    """add_topic → kickoff → tick until the first card ran."""
    await whiteboard.add_topic("测试议题", "接线验证问题？", source="test")
    board_id = await whiteboard.kickoff()
    assert board_id is not None
    await whiteboard.tick()
    await _drain(whiteboard)


async def _card_task_hands() -> list[str]:
    rows = await db.query(
        "SELECT hand FROM tasks WHERE source='whiteboard' AND prompt LIKE '%白板协作任务%' ORDER BY created_at"
    )
    return [r["hand"] for r in rows]


# ==== switch OFF (the default): behaviour identical, weights ignored =========

async def test_off_is_the_default():
    assert get_settings().enable_hand_weights is False


async def test_off_daily_rotation_unchanged():
    _register_fakes(*ROTATION_HANDS)
    # weight rows present and loaded — must change NOTHING while off
    get_registry().set_weights_cache({"daily": {"gemini": 100.0, "claude": 0.0}})
    picks = [analyst_daily._pick_hand(_analyst(), i) for i in range(6)]
    assert picks == [ROTATION_HANDS[i % len(ROTATION_HANDS)] for i in range(6)]


async def test_off_whiteboard_uses_default_hand():
    _register_fakes("fake2")
    get_registry().set_weights_cache({"whiteboard": {"fake2": 100.0}})
    await _run_one_card()
    assert await _card_task_hands() == ["echo"]  # settings.default_hand, weights ignored


async def test_off_mailbox_uses_default_hand():
    _register_fakes("fake2")
    get_registry().set_weights_cache({"mailbox": {"fake2": 100.0}})
    await mailbox.create_thread("接线验证", "macro-analyst", "开关关闭时应走默认手。")
    await _drain(mailbox)
    rows = await db.query("SELECT hand FROM tasks WHERE source='mailbox'")
    assert [r["hand"] for r in rows] == ["echo"]


async def test_off_research_round_robin_unchanged(monkeypatch):
    _register_fakes("fake2")
    monkeypatch.setattr(get_settings(), "research_hands", "echo,fake2")
    get_registry().set_weights_cache({"research": {"fake2": 100.0, "echo": 0.0}})
    picks = [workflows._workflow_hand_policy("research", {}, None, i) for i in range(4)]
    assert picks == [
        ("echo", ("echo", "fake2")), ("fake2", ("echo", "fake2")),
        ("echo", ("echo", "fake2")), ("fake2", ("echo", "fake2")),
    ]


# ==== switch ON: weights shift the pick =======================================

async def test_on_daily_extreme_weights_are_deterministic(monkeypatch):
    _register_fakes(*ROTATION_HANDS)
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({"daily": {"claude": 0.0, "codex": 1.0, "gemini": 0.0}})
    picks = {analyst_daily._pick_hand(_analyst(), i) for i in range(12)}
    assert picks == {"codex"}  # rotation would have produced all three


async def test_on_daily_distribution_follows_weights(monkeypatch):
    _register_fakes(*ROTATION_HANDS)
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    # deterministic RNG for the module-level `random` the call sites rely on
    monkeypatch.setattr("app.hands.registry.random", random.Random(42))
    get_registry().set_weights_cache({"daily": {"claude": 3.0, "codex": 1.0, "gemini": 0.0}})
    picks = [analyst_daily._pick_hand(_analyst(), i) for i in range(4_000)]
    assert picks.count("gemini") == 0
    assert abs(picks.count("claude") / len(picks) - 0.75) < 0.03


async def test_on_daily_empty_pool_falls_back_to_default(monkeypatch):
    # no fake hands: ROTATION_HANDS are all unavailable under conftest
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({"daily": {"claude": 5.0}})
    assert analyst_daily._pick_hand(_analyst(), 0) == "echo"


async def test_on_whiteboard_picks_weighted_hand(monkeypatch):
    _register_fakes("fake2")
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({"whiteboard": {"fake2": 1.0, "unavailable-hand": 9.0}})
    await _run_one_card()
    assert await _card_task_hands() == ["fake2"]  # only registered+positive rows enter the pool
    # the handoff moderator task is NOT a scope call site — stays on default_hand
    rows = await db.query(
        "SELECT hand FROM tasks WHERE source='whiteboard' AND prompt NOT LIKE '%白板协作任务%'"
    )
    assert all(r["hand"] == "echo" for r in rows)


async def test_on_whiteboard_zero_weight_row_stays_out_of_pool(monkeypatch):
    _register_fakes("fake2", "fake3")
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    # fake3 explicitly zeroed: it must never be picked even though the row exists
    get_registry().set_weights_cache({"whiteboard": {"fake2": 1.0, "fake3": 0.0}})
    await _run_one_card()
    assert await _card_task_hands() == ["fake2"]


async def test_on_whiteboard_no_rows_keeps_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({})  # switch on, nothing configured
    await _run_one_card()
    assert await _card_task_hands() == ["echo"]


async def test_on_mailbox_picks_weighted_hand(monkeypatch):
    _register_fakes("fake2")
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({"mailbox": {"fake2": 1.0}})
    await mailbox.create_thread("接线验证", "macro-analyst", "开关开启时应走加权手。")
    await _drain(mailbox)
    rows = await db.query("SELECT hand FROM tasks WHERE source='mailbox'")
    assert [r["hand"] for r in rows] == ["fake2"]


async def test_on_research_stays_inside_chain(monkeypatch):
    """Hard rule 10: weights reorder INSIDE research_hand_names, never beyond."""
    _register_fakes("claude")  # available AND heavily weighted — but outside the chain
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({"research": {"claude": 100.0, "echo": 1.0}})
    for i in range(8):
        hand, chain = workflows._workflow_hand_policy("research", {}, None, i)
        assert hand == "echo"            # chain is ("echo",) under conftest
        assert chain == ("echo",)        # fallback chain unchanged


async def test_on_research_weights_reorder_within_chain(monkeypatch):
    _register_fakes("fake2")
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    monkeypatch.setattr(get_settings(), "research_hands", "echo,fake2")
    get_registry().set_weights_cache({"research": {"echo": 0.0, "fake2": 1.0}})
    picks = [workflows._workflow_hand_policy("research", {}, None, i) for i in range(8)]
    # round-robin would alternate; weighted pick sticks to fake2, chain intact
    assert picks == [("fake2", ("echo", "fake2"))] * 8


# ==== explicit hands always win over weights ==================================

async def test_on_explicit_hands_respected(monkeypatch):
    _register_fakes(*ROTATION_HANDS, "fake2")
    monkeypatch.setattr(get_settings(), "enable_hand_weights", True)
    get_registry().set_weights_cache({
        "daily": {"codex": 100.0}, "research": {"fake2": 100.0},
        "whiteboard": {"fake2": 100.0}, "mailbox": {"fake2": 100.0},
    })
    # daily: analyst.hand pins the hand regardless of weights
    assert analyst_daily._pick_hand(_analyst(hand="echo"), 0) == "echo"
    # research: an explicit step hand pins it (chain still returned)
    monkeypatch.setattr(get_settings(), "research_hands", "echo,fake2")
    assert workflows._workflow_hand_policy("research", {"hand": "echo"}, None, 3) == (
        "echo", ("echo", "fake2"),
    )
    # whiteboard + mailbox: a roster analyst with an explicit hand keeps it
    pinned = _analyst(hand="echo")
    monkeypatch.setattr(whiteboard, "get_analyst", lambda _id: pinned)
    await _run_one_card()
    assert await _card_task_hands() == ["echo"]
    monkeypatch.setattr(mailbox, "get_analyst", lambda _id: pinned)
    await mailbox.create_thread("显式手验证", "macro-analyst", "显式指定必须被尊重。")
    await _drain(mailbox)
    rows = await db.query("SELECT hand FROM tasks WHERE source='mailbox'")
    assert [r["hand"] for r in rows] == ["echo"]
