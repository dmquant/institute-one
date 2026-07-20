"""Whiteboard: topic dedup, kickoff, tick-driven board lifecycle on echo."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app import bus, db
from app.config import get_settings
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
    assert await db.query_one(
        "SELECT key FROM admin_state WHERE key = ?",
        (whiteboard._topic_claim_key((await whiteboard.list_topics("used"))[0]["id"]),),
    ) is None
    events = await bus.replay(0, types=["whiteboard.board_opened"])
    assert any(e.ref_id == board_id for e in events)

    # kickoff with an empty pool is a no-op
    assert await whiteboard.kickoff() is None


async def test_kickoff_releases_topic_when_board_open_fails(monkeypatch):
    await whiteboard.add_topic("新能源产业链", "锂价见底了吗？", source="test")
    real_open_board = whiteboard._open_board

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated board insert failure")

    monkeypatch.setattr(whiteboard, "_open_board", boom)
    assert await whiteboard.kickoff() is None  # never raises

    # the topic survived the failure and is claimable again
    pending = await whiteboard.list_topics("pending")
    assert len(pending) == 1
    assert pending[0]["topic"] == "新能源产业链"
    assert await db.query_one(
        "SELECT key FROM admin_state WHERE key = ?",
        (whiteboard._topic_claim_key(pending[0]["id"]),),
    ) is None

    monkeypatch.setattr(whiteboard, "_open_board", real_open_board)
    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "新能源产业链"
    assert await whiteboard.list_topics("pending") == []


@pytest.mark.parametrize("needle", ["INSERT INTO whiteboard_boards", "INSERT INTO whiteboard_cards"])
async def test_kickoff_rolls_back_board_transaction_on_insert_failure(monkeypatch, needle):
    """Real _open_board, failure injected at one INSERT: no residue, topic released."""
    await whiteboard.add_topic("机器人产业", "人形机器人量产元年了吗？", source="test")
    sessions_root = get_settings().workspaces_dir / "sessions"
    dirs_before = set(sessions_root.iterdir()) if sessions_root.is_dir() else set()

    real_transaction = db.transaction
    inject = {"on": True}

    class FailingConn:
        def __init__(self, real):
            self._real = real

        async def execute(self, sql, *args, **kwargs):
            if inject["on"] and needle in sql:
                raise RuntimeError(f"injected failure at {needle}")
            return await self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    @asynccontextmanager
    async def failing_transaction():
        async with real_transaction() as conn:
            yield FailingConn(conn)

    monkeypatch.setattr(db, "transaction", failing_transaction)
    assert await whiteboard.kickoff() is None  # never raises

    # the transaction rolled back atomically: no board, no orphan card
    assert await db.query("SELECT id FROM whiteboard_boards") == []
    assert await db.query("SELECT id FROM whiteboard_cards") == []
    assert await db.query("SELECT id FROM sessions WHERE kind='whiteboard'") == []
    dirs_after = set(sessions_root.iterdir()) if sessions_root.is_dir() else set()
    assert dirs_after == dirs_before
    # and the topic went back to the pool
    pending = await whiteboard.list_topics("pending")
    assert len(pending) == 1
    assert pending[0]["topic"] == "机器人产业"

    inject["on"] = False  # same wiring, injection off -> the retry succeeds
    board_id = await whiteboard.kickoff()
    assert board_id is not None
    board = await whiteboard.get_board(board_id)
    assert board["topic"] == "机器人产业"
    assert len(board["cards"]) == 1
    assert await whiteboard.list_topics("pending") == []


async def test_post_commit_read_failure_keeps_topic_used(monkeypatch):
    """Once the board transaction committed, an ancillary read failure must NOT
    release the topic claim (that would open a second board for the same topic)."""
    await whiteboard.add_topic("半导体设备国产化", "刻蚀机的差距还有多大？", source="test")

    real_query_one = db.query_one
    inject = {"on": True}

    async def flaky_query_one(sql, params=()):
        # the final post-commit read in _open_board selects the board by id
        if inject["on"] and "FROM whiteboard_boards WHERE id" in sql:
            raise RuntimeError("simulated post-commit read failure")
        return await real_query_one(sql, params)

    monkeypatch.setattr(db, "query_one", flaky_query_one)
    board_id = await whiteboard.kickoff()
    inject["on"] = False

    # the board landed and kickoff did NOT misread the failure as "nothing landed"
    assert board_id is not None
    boards = await db.query("SELECT * FROM whiteboard_boards")
    assert len(boards) == 1
    assert boards[0]["id"] == board_id
    assert boards[0]["status"] == "active"

    # the topic stays consumed
    assert await whiteboard.list_topics("pending") == []
    topic = (await whiteboard.list_topics("used"))[0]
    assert topic["topic"] == "半导体设备国产化"

    # a second kickoff cannot open a duplicate board for the same topic
    assert await whiteboard.kickoff() is None
    assert len(await db.query("SELECT id FROM whiteboard_boards")) == 1


async def test_live_topic_claim_blocks_kickoff_before_model_call(monkeypatch):
    added = await whiteboard.add_topic("活跃租约主题", "第二个 kickoff 不应烧模型", source="test")
    key = whiteboard._topic_claim_key(added["id"])
    token = json.dumps({"owner": "other-kickoff", "claimed_at": bus.now_iso()})
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?,?)", (key, token))

    async def must_not_embed(_text):
        raise AssertionError("live topic claim must block before embedding")

    monkeypatch.setattr(whiteboard.vectors, "embed", must_not_embed)
    assert await whiteboard.kickoff() is None
    assert await db.query("SELECT id FROM whiteboard_boards") == []
    row = await db.query_one("SELECT status FROM topic_pool WHERE id=?", (added["id"],))
    assert row["status"] == "pending"
    claim = await db.query_one("SELECT value FROM admin_state WHERE key=?", (key,))
    assert claim["value"] == token


async def test_stale_topic_claim_is_taken_over_and_released():
    added = await whiteboard.add_topic("过期租约主题", "下一次 kickoff 应接管", source="test")
    key = whiteboard._topic_claim_key(added["id"])
    stale = json.dumps({
        "owner": "dead-kickoff",
        "claimed_at": whiteboard._iso_ago(
            hours=whiteboard.TOPIC_CLAIM_LEASE_S / 3600 + 1
        ),
    })
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?,?)", (key, stale))

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    assert (await whiteboard.get_board(board_id))["topic"] == "过期租约主题"
    assert await db.query_one("SELECT key FROM admin_state WHERE key=?", (key,)) is None
    assert (await db.query_one(
        "SELECT status FROM topic_pool WHERE id=?", (added["id"],)
    ))["status"] == "used"


async def test_taken_over_zombie_cannot_commit_a_second_board():
    added = await whiteboard.add_topic("僵尸认领主题", "旧 owner 必须写不进去", source="test")
    key = whiteboard._topic_claim_key(added["id"])
    stale_token = json.dumps({
        "owner": "stale-board",
        "claimed_at": whiteboard._iso_ago(
            hours=whiteboard.TOPIC_CLAIM_LEASE_S / 3600 + 1
        ),
    })
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?,?)", (key, stale_token))

    takeover = await whiteboard._claim_topic(added["id"])
    assert takeover is not None
    _, fresh_token = takeover
    with pytest.raises(RuntimeError, match="lost topic claim"):
        await whiteboard._open_board(
            added["topic"], added["question"],
            topic_claim=(added["id"], key, stale_token),
        )
    assert await db.query("SELECT id FROM sessions WHERE kind='whiteboard'") == []
    assert await db.query("SELECT id FROM whiteboard_boards") == []

    board = await whiteboard._open_board(
        added["topic"], added["question"],
        topic_claim=(added["id"], key, fresh_token),
    )
    assert board["id"] == json.loads(fresh_token)["owner"]
    assert (await db.query_one(
        "SELECT status FROM topic_pool WHERE id=?", (added["id"],)
    ))["status"] == "used"
    await whiteboard._release_topic_claim(key, fresh_token)


async def test_reaper_recovers_legacy_crash_before_board():
    """A hard-killed old kickoff left used topic + session, but no board."""
    added = await whiteboard.add_topic("崩溃恢复主题", "reaper 后可重试", source="test")
    await db.execute("UPDATE topic_pool SET status='used' WHERE id=?", (added["id"],))
    key = whiteboard._topic_claim_key(added["id"])
    stale_at = whiteboard._iso_ago(hours=whiteboard.TOPIC_CLAIM_LEASE_S / 3600 + 1)
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?,?)",
        (key, json.dumps({"owner": "dead-kickoff", "claimed_at": stale_at})),
    )

    session_id = "orphan-wb"
    workspace = get_settings().workspaces_dir / "sessions" / session_id
    workspace.mkdir(parents=True)
    (workspace / "partial.md").write_text("partial", encoding="utf-8")
    old = whiteboard._iso_ago(hours=whiteboard.ORPHAN_SESSION_GRACE_S / 3600 + 1)
    await db.execute(
        "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) "
        "VALUES (?,?, 'whiteboard', ?,?,?)",
        (session_id, "WB 崩溃恢复主题", str(workspace), old, old),
    )

    stats = await whiteboard.reap_orphans()
    assert stats == {
        "claims_reaped": 1,
        "topics_requeued": 1,
        "sessions_reaped": 1,
        "workspaces_reaped": 1,
    }
    assert await db.query_one("SELECT id FROM sessions WHERE id=?", (session_id,)) is None
    assert not workspace.exists()
    assert (await db.query_one(
        "SELECT status FROM topic_pool WHERE id=?", (added["id"],)
    ))["status"] == "pending"

    board_id = await whiteboard.kickoff()
    assert board_id is not None
    assert (await whiteboard.get_board(board_id))["topic"] == "崩溃恢复主题"


async def test_reaper_does_not_delete_old_session_with_active_board():
    await whiteboard.add_topic("活跃白板", "即使时间旧也不能误杀", source="test")
    board_id = await whiteboard.kickoff()
    board = await whiteboard.get_board(board_id)
    session = await db.query_one("SELECT * FROM sessions WHERE id=?", (board["session_id"],))
    workspace = Path(session["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "live.md").write_text("live", encoding="utf-8")
    old = whiteboard._iso_ago(hours=whiteboard.ORPHAN_SESSION_GRACE_S / 3600 + 1)
    await db.execute(
        "UPDATE sessions SET created_at=?, updated_at=? WHERE id=?",
        (old, old, session["id"]),
    )

    stats = await whiteboard.reap_orphans()
    assert stats["sessions_reaped"] == 0
    assert await db.query_one("SELECT id FROM sessions WHERE id=?", (session["id"],))
    assert workspace.is_dir()


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
