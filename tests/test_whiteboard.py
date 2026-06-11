"""Whiteboard: topic dedup, kickoff, tick-driven board lifecycle on echo."""
from __future__ import annotations

import asyncio
from pathlib import Path

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
