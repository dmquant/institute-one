"""Whiteboard: topic dedup, kickoff, tick-driven board lifecycle on echo."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app import bus, db
from app.institute import whiteboard


async def _drain_bg(max_rounds: int = 50) -> None:
    """Wait until the whiteboard has no in-flight background card coroutines."""
    for _ in range(max_rounds):
        tasks = list(whiteboard._bg_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("whiteboard background tasks never drained")


async def test_add_topic_dedups_by_hash():
    first = await whiteboard.add_topic("AI 芯片竞争格局", "英伟达的护城河还有多宽？", source="test")
    second = await whiteboard.add_topic("AI 芯片竞争格局", "英伟达的护城河还有多宽？", source="test")
    assert first["id"] == second["id"]
    assert first["content_hash"] == second["content_hash"]

    pending = await whiteboard.list_topics("pending")
    assert len([t for t in pending if t["content_hash"] == first["content_hash"]]) == 1

    # different question -> different hash -> a second row
    third = await whiteboard.add_topic("AI 芯片竞争格局", "国产替代进展如何？", source="test")
    assert third["id"] != first["id"]


async def test_kickoff_creates_board_and_first_card():
    await whiteboard.add_topic("AI 芯片竞争格局", "英伟达的护城河还有多宽？", source="test")
    board_id = await whiteboard.kickoff()
    assert board_id is not None

    board = await whiteboard.get_board(board_id)
    assert board["status"] == "active"
    assert board["topic"] == "AI 芯片竞争格局"
    assert board["session_id"]
    assert len(board["cards"]) == 1
    assert board["cards"][0]["idx"] == 1
    assert board["cards"][0]["status"] == "pending"

    # the topic was consumed from the pool
    assert await whiteboard.list_topics("pending") == []
    events = await bus.replay(0, types=["whiteboard.board_opened"])
    assert any(e.ref_id == board_id for e in events)

    # kickoff with an empty pool is a no-op
    assert await whiteboard.kickoff() is None


async def test_tick_drives_board_to_completed_on_echo():
    await whiteboard.add_topic("宏观利率走向", "美联储下一步会怎么走？", source="test")
    board_id = await whiteboard.kickoff()
    assert board_id is not None

    for _ in range(20):
        await whiteboard.tick()
        await _drain_bg()
        board = await whiteboard.get_board(board_id)
        if board["status"] != "active":
            break
    else:
        raise AssertionError(f"board never left active: {board}")

    assert board["status"] == "completed"
    cards = board["cards"]
    assert len(cards) == board["max_cards"]
    assert all(c["status"] == "completed" for c in cards)
    assert all(c["summary"] for c in cards)
    # the handoff fell back deterministically: every card has a roster analyst
    assert all(c["analyst_id"] for c in cards)
    # rotation means consecutive cards are written by different analysts
    assert cards[0]["analyst_id"] != cards[1]["analyst_id"]

    events = await bus.replay(0, types=["whiteboard.board_completed"])
    mine = [e for e in events if e.ref_id == board_id]
    assert len(mine) == 1
    assert mine[0].payload["cards"] == board["max_cards"]

    # the digest landed in the board's session workspace
    session = await db.query_one(
        "SELECT workspace_dir FROM sessions WHERE id = ?", (board["session_id"],)
    )
    digest = Path(session["workspace_dir"]) / "_board.md"
    assert digest.is_file()
    text = digest.read_text(encoding="utf-8")
    assert "宏观利率走向" in text
    assert "## 停止条件" in text
    assert "## 需监控的新证据清单" in text


async def test_tick_marks_missing_card_output_failed(monkeypatch):
    board = await whiteboard.create_board("空输出防护", "completed 但没有文件怎么办？", max_cards=1)
    board_id = board["id"]
    seen: dict[str, object] = {}

    async def fake_submit(*args, **kwargs):
        return SimpleNamespace(id="task-empty-card", status="completed", output="", error=None, hand="agy-opus")

    class FakeRegistry:
        def mark_rate_limited(self, hand_name, info):
            seen["cooldown"] = (hand_name, info.reason, info.retry_after_s)

        def record_result(self, hand_name, *, ok, rate_limited=False):
            seen["record"] = (hand_name, ok, rate_limited)

    monkeypatch.setattr(whiteboard.executor, "submit", fake_submit)
    monkeypatch.setattr(whiteboard, "get_registry", lambda: FakeRegistry())

    await whiteboard.tick()
    await _drain_bg()

    after = await whiteboard.get_board(board_id)
    assert after["cards"][0]["status"] == "failed"
    assert seen["cooldown"] == ("agy-opus", "invalid_output", whiteboard.INVALID_OUTPUT_COOLDOWN_S)
    assert seen["record"] == ("agy-opus", False, True)

    events = await bus.replay(0, types=["whiteboard.card_invalid_output"])
    assert any(e.ref_id == after["cards"][0]["id"] for e in events)


def test_closure_block_prefers_analyst_written_sections():
    block = whiteboard.closure_block_from_texts(
        "SpaceX 估值补充",
        "太空 AI 算力是否改变估值？",
        [
            "## 核心结论\n还需要讨论。",
            (
                "## 核心结论\n已经收束。\n\n"
                "## 停止条件\n- 等待 Starship 发射成本新披露。\n\n"
                "## 需监控的新证据清单\n- DoD 或情报系统采购合同。"
            ),
        ],
        card_count=2,
    )

    assert "等待 Starship 发射成本新披露" in block
    assert "DoD 或情报系统采购合同" in block
    assert "继续让模型互相推理只会重复既有假设" not in block


def test_closure_block_falls_back_when_sections_are_missing():
    block = whiteboard.closure_block_from_texts(
        "AI 基础模型估值",
        "平台还是出版商？",
        ["## 核心结论\n只剩几个情景。"],
        card_count=1,
    )

    assert "## 停止条件" in block
    assert "## 需监控的新证据清单" in block
    assert "继续让模型互相推理只会重复既有假设" in block


async def test_finalize_refuses_pending_cards_after_handoff():
    board = await whiteboard.create_board("能源通胀联动", "还有宏观和固收分歧吗？", max_cards=3)
    board_id = board["id"]
    first = board["cards"][0]
    await db.execute(
        "UPDATE whiteboard_cards SET status='completed', summary='商品端认为 headline 先回落', "
        "output_file='card-01.md', finished_at=? WHERE id=?",
        (bus.now_iso(), first["id"]),
    )
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, created_at) "
        "VALUES (?,?,2,'macro-analyst','pending','宏观端是否确认政策转向？',?)",
        ("pendingcard01", board_id, bus.now_iso()),
    )

    fresh = await db.query_one("SELECT * FROM whiteboard_boards WHERE id=?", (board_id,))
    await whiteboard._finalize(fresh)

    after = await whiteboard.get_board(board_id)
    assert after["status"] == "active"
    assert [c["status"] for c in after["cards"]] == ["completed", "pending"]
