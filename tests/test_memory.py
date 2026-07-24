"""Analyst memory: versioned compacts, cursor exactness, concurrency, injection, export."""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid

import pytest

from app import bus, db
from app.institute import memory
from app.institute.analysts import get_analyst
from app.institute.prompts import build_analyst_prompt, work_date
from app.router import executor
from app.vault import exporter
from app.vault.writer import REGION_BEGIN, REGION_END, get_writer

ANALYST = "macro-analyst"


async def _seed_card(analyst_id: str, summary: str) -> None:
    """A completed whiteboard card + its completion event (what collectors read).

    No timestamp games needed: material is consumed through monotonic event ids,
    so a card seeded in the same second as a fresh memory version still counts.
    """
    now = bus.now_iso()
    board_id = uuid.uuid4().hex[:12]
    card_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, session_id, work_date, created_at, updated_at) "
        "VALUES (?,?,?,'completed',5,NULL,?,?,?)",
        (board_id, f"议题-{board_id[:4]}", "测试问题", work_date(), now, now),
    )
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, summary, created_at, finished_at) "
        "VALUES (?,?,1,?,'completed','q',?,?,?)",
        (card_id, board_id, analyst_id, summary, now, now),
    )
    await bus.emit("whiteboard.card_completed", "card", card_id, {
        "board_id": board_id, "idx": 1, "analyst_id": analyst_id,
    })


async def _seed_daily(analyst_id: str, body: str) -> None:
    """A completed analyst-daily: tasks row + the completed event pointing at it."""
    task_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO tasks (id, prompt, status, source, output, workspace_dir, created_at) "
        "VALUES (?,?,'completed','analyst-daily',?,'',?)",
        (task_id, "daily prompt", body, bus.now_iso()),
    )
    await bus.emit("analyst_daily.completed", "analyst", analyst_id, {
        "date": work_date(), "task_id": task_id,
    })


async def _seed_mail_reply(analyst_id: str, body: str) -> None:
    thread_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO mailbox_threads (id, subject, analyst_id, status, created_at, updated_at) "
        "VALUES (?,?,?,'open',?,?)",
        (thread_id, "测试主题", analyst_id, now, now),
    )
    await db.insert(
        "INSERT INTO mailbox_messages (thread_id, author, kind, body, status, created_at) "
        "VALUES (?,?,'reply',?,'done',?)",
        (thread_id, analyst_id, body, now),
    )


# ---- compaction ---------------------------------------------------------------

async def test_compact_one_versions_and_skips_without_material():
    await _seed_card(ANALYST, "首个判断：利率见顶。")
    r1 = await memory.compact_one(ANALYST)
    assert r1["status"] == "completed" and r1["version"] == 1

    v1 = await memory.latest(ANALYST)
    assert v1["version"] == 1
    assert v1["supersedes"] is None
    assert v1["work_date"] == work_date()
    assert (v1["compact_md"] or "").strip()
    assert memory._parse_cursors(v1["cursors"])["card_event"] > 0  # consumption recorded

    # nothing new since v1 -> no model call, no new row
    again = await memory.compact_one(ANALYST)
    assert again.get("skipped") == "no new material"
    assert (await memory.latest(ANALYST))["version"] == 1

    # new material -> version increments and links back. Seeded within the same
    # second as v1: the id cursor must count it anyway (B3-H1 same-second case).
    await _seed_card(ANALYST, "更新判断：曲线陡峭化。")
    r2 = await memory.compact_one(ANALYST)
    assert r2["status"] == "completed" and r2["version"] == 2

    v2 = await memory.latest(ANALYST)
    assert v2["version"] == 2
    assert v2["supersedes"] == v1["id"]
    assert "曲线陡峭化" in v2["compact_md"]
    rows = await db.query("SELECT * FROM analyst_memory WHERE analyst_id = ?", (ANALYST,))
    assert len(rows) == 2

    events = [e for e in await bus.replay(0, types=["memory.compacted"]) if e.ref_id == ANALYST]
    assert [e.payload["version"] for e in events] == [1, 2]


async def test_compact_consumes_material_arriving_mid_flight(monkeypatch):
    """REVIEW-B3 B3-H1 probe: output landing while the model runs is not lost.

    The second card arrives inside executor.submit — after the material window
    was fixed, before the version row exists (same second as its created_at).
    Timestamp cursors dropped it forever; id cursors pick it up next round.
    """
    await _seed_card(ANALYST, "飞行前素材。")

    real_submit = executor.submit
    fired = False

    async def submit_with_midflight_arrival(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            await _seed_card(ANALYST, "飞行中到达的素材。")
        return await real_submit(*args, **kwargs)

    monkeypatch.setattr(executor, "submit", submit_with_midflight_arrival)

    r1 = await memory.compact_one(ANALYST)
    assert r1["status"] == "completed" and r1["version"] == 1
    v1 = await memory.latest(ANALYST)
    assert "飞行前素材" in v1["compact_md"]
    assert "飞行中到达的素材" not in v1["compact_md"]  # outside v1's fixed window …

    r2 = await memory.compact_one(ANALYST)  # … but consumed by the next round
    assert r2["status"] == "completed" and r2["version"] == 2
    assert "飞行中到达的素材" in (await memory.latest(ANALYST))["compact_md"]

    r3 = await memory.compact_one(ANALYST)
    assert r3.get("skipped") == "no new material"  # exactly once, no re-reads


async def test_limit_overflow_backfills_next_round():
    """REVIEW-B3 B3-M3 probe: rows beyond a per-source LIMIT are consumed later,
    oldest first, instead of being silently dropped."""
    total = memory.MAX_CARD_ITEMS + 2
    for i in range(1, total + 1):
        await _seed_card(ANALYST, f"素材{i:02d}号")

    assert (await memory.compact_one(ANALYST))["version"] == 1
    v1 = (await memory.latest(ANALYST))["compact_md"]
    assert "素材01号" in v1 and f"素材{memory.MAX_CARD_ITEMS:02d}号" in v1
    assert f"素材{total:02d}号" not in v1  # beyond this round's LIMIT window

    assert (await memory.compact_one(ANALYST))["version"] == 2
    v2 = (await memory.latest(ANALYST))["compact_md"]
    assert f"素材{memory.MAX_CARD_ITEMS + 1:02d}号" in v2 and f"素材{total:02d}号" in v2

    assert (await memory.compact_one(ANALYST)).get("skipped") == "no new material"


async def test_failed_model_call_does_not_consume_material(monkeypatch):
    """Cursors persist only with a successful version row: a failed compact
    leaves the material unconsumed for the retry."""
    from types import SimpleNamespace

    await _seed_card(ANALYST, "失败重试素材。")

    async def failing_submit(*args, **kwargs):
        return SimpleNamespace(id="t-fail", status="failed", output="", error="boom")

    monkeypatch.setattr(executor, "submit", failing_submit)
    r = await memory.compact_one(ANALYST)
    assert r.get("status") == "failed"
    assert await memory.latest(ANALYST) is None

    monkeypatch.undo()
    r2 = await memory.compact_one(ANALYST)
    assert r2["status"] == "completed"
    assert "失败重试素材" in (await memory.latest(ANALYST))["compact_md"]


async def test_compact_collects_all_three_sources():
    await _seed_daily(ANALYST, "今日观察：利率上行 20bp（来源：test）。")
    await _seed_card(ANALYST, "白板判断：成长股承压。")
    await _seed_mail_reply(ANALYST, "信箱回复：估值弹性约 -8%。")

    r = await memory.compact_one(ANALYST)
    assert r["status"] == "completed"
    md = (await memory.latest(ANALYST))["compact_md"]
    # echo reflects the prompt, so every source section header must be in there
    assert "### 观察日报" in md and "利率上行 20bp" in md
    assert "### 白板卡片" in md and "成长股承压" in md
    assert "### 信箱回复" in md and "估值弹性" in md


async def test_concurrent_compact_claims_single_version():
    await _seed_card(ANALYST, "并发压缩素材。")
    results = await asyncio.gather(memory.compact_one(ANALYST), memory.compact_one(ANALYST))

    rows = await db.query("SELECT * FROM analyst_memory WHERE analyst_id = ?", (ANALYST,))
    assert len(rows) == 1 and rows[0]["version"] == 1
    statuses = sorted("completed" if r.get("status") == "completed" else "skipped" for r in results)
    assert statuses == ["completed", "skipped"]  # exactly one claim wins


async def test_concurrent_compact_burns_model_once(monkeypatch):
    """M8-005 acceptance: concurrent compacts for one analyst burn the model at
    most once — the loser skips at the claim, BEFORE any model call (the old
    UNIQUE-only guard let both run and discarded one output = 2x quota)."""
    await _seed_card(ANALYST, "并发单烧素材。")

    calls = 0
    real_submit = executor.submit

    async def counting_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_submit(*args, **kwargs)

    monkeypatch.setattr(executor, "submit", counting_submit)
    results = await asyncio.gather(memory.compact_one(ANALYST), memory.compact_one(ANALYST))

    assert calls == 1  # the whole point: one model burn, not two
    rows = await db.query("SELECT * FROM analyst_memory WHERE analyst_id = ?", (ANALYST,))
    assert len(rows) == 1 and rows[0]["version"] == 1
    statuses = sorted("completed" if r.get("status") == "completed" else "skipped" for r in results)
    assert statuses == ["completed", "skipped"]
    # every exit released its claim: nothing left to wedge the next compact
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (memory._claim_key(ANALYST),)
    )
    assert row is None


async def test_compact_skips_live_claim_and_takes_over_stale(monkeypatch):
    """Cross-process single-burn: a LIVE claim held by another process makes
    compact_one skip without touching the model or the claim row; a stale
    claim (holder hard-killed, lease expired) is taken over via CAS."""
    await _seed_card(ANALYST, "跨进程认领素材。")
    key = memory._claim_key(ANALYST)

    calls = 0
    real_submit = executor.submit

    async def counting_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_submit(*args, **kwargs)

    monkeypatch.setattr(executor, "submit", counting_submit)

    # "another process" holds a live claim (fresh claimed_at)
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?)",
        (key, json.dumps({"owner": "other-proc", "claimed_at": bus.now_iso()})),
    )
    r = await memory.compact_one(ANALYST)
    assert r.get("skipped") == "compact already running"
    assert calls == 0                                # zero model burn for the loser
    assert await memory.latest(ANALYST) is None
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    assert json.loads(row["value"])["owner"] == "other-proc"  # loser never releases someone else's claim

    # the holder died: claimed_at beyond COMPACT_LEASE_S -> CAS takeover wins
    await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = ?",
        (json.dumps({"owner": "other-proc", "claimed_at": "2026-01-01T00:00:00+00:00"}), key),
    )
    r2 = await memory.compact_one(ANALYST)
    assert r2["status"] == "completed" and r2["version"] == 1
    assert calls == 1
    # finished -> released (CAS delete of our own token)
    assert await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,)) is None


async def test_corrupt_claim_is_taken_over():
    """A corrupt claim row must not wedge the analyst forever."""
    await _seed_card(ANALYST, "损坏认领素材。")
    key = memory._claim_key(ANALYST)
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?, ?)", (key, "not-json"))

    r = await memory.compact_one(ANALYST)
    assert r["status"] == "completed" and r["version"] == 1
    assert await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,)) is None


async def test_taken_over_zombie_cannot_double_write(monkeypatch):
    """Last-line guard: a compact taken over mid-model-call (lease expired,
    another process claimed and finished) loses its version INSERT to
    UNIQUE(analyst_id, version) — output discarded, no double version row."""
    await _seed_card(ANALYST, "僵尸兜底素材。")
    real_submit = executor.submit
    fired = False

    async def hijack_submit(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            # simulate the takeover while OUR model call is in flight:
            # the other process claimed the lease and completed version 1
            await db.execute(
                "DELETE FROM admin_state WHERE key = ?", (memory._claim_key(ANALYST),)
            )
            await db.execute(
                "INSERT INTO analyst_memory "
                "(id, analyst_id, version, work_date, compact_md, supersedes, cursors, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("m-takeover", ANALYST, 1, work_date(), "接管者的记忆。", None, "{}", bus.now_iso()),
            )
        return await real_submit(*args, **kwargs)

    monkeypatch.setattr(executor, "submit", hijack_submit)
    r = await memory.compact_one(ANALYST)

    assert r.get("skipped", "").startswith("version 1 already claimed")
    rows = await db.query("SELECT * FROM analyst_memory WHERE analyst_id = ?", (ANALYST,))
    assert len(rows) == 1 and rows[0]["id"] == "m-takeover"  # the winner's row survives


async def test_compact_all_skips_ops_and_survives_failures(monkeypatch):
    await _seed_card("macro-analyst", "宏观素材。")
    await _seed_card("equity-analyst", "权益素材。")

    real = memory.compact_one

    async def flaky(analyst_id: str):
        if analyst_id == "equity-analyst":
            raise RuntimeError("boom")
        return await real(analyst_id)

    monkeypatch.setattr(memory, "compact_one", flaky)
    summary = await memory.compact_all()

    by_id = {r["analyst_id"]: r for r in summary["results"]}
    assert "ops-editor" not in by_id                      # ops excluded
    assert by_id["macro-analyst"]["status"] == "completed"
    assert by_id["equity-analyst"]["status"] == "crashed"  # failure didn't break the chain
    assert summary["ran"] == len(by_id)
    # analysts after the crash in roster order still ran
    assert by_id["fixed-income-analyst"].get("skipped") == "no new material"


# ---- memory_block + prompt injection --------------------------------------------

async def test_memory_block_empty_then_formatted():
    assert await memory.memory_block(ANALYST) == ""

    await db.execute(
        "INSERT INTO analyst_memory (id, analyst_id, version, work_date, compact_md, supersedes, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m-fixed", ANALYST, 3, "2026-07-20", "【核心立场】利率见顶（2026-07-01 起）。", None, bus.now_iso()),
    )
    block = await memory.memory_block(ANALYST)
    assert block.startswith("## 常备记忆（第 3 版 · 2026-07-20）")
    assert "【核心立场】利率见顶" in block


async def test_prompt_with_memory_matches_manual_assembly(monkeypatch):
    """The unified entrypoint (M8-005) is byte-identical to the seven former
    call sites' manual ``build_analyst_prompt(memory_block=await
    memory.memory_block(id))`` assembly — with and without stored memory,
    across every kwargs shape the call sites use. ``now_sgt`` is pinned so the
    date anchor cannot flip mid-comparison."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.institute import prompts as prompts_mod

    monkeypatch.setattr(
        prompts_mod, "now_sgt",
        lambda: datetime(2026, 7, 20, 15, 0, tzinfo=ZoneInfo("Asia/Singapore")),
    )
    analyst = get_analyst(ANALYST)
    kwargs_shapes = [
        {},
        {"output_file": "note.md"},
        {"context_blocks": ["## 临时上下文\n上下文C。"]},
        {"context_blocks": None, "output_file": "note.md"},
        {"context_blocks": ["## 临时上下文\n上下文C。"], "output_file": "note.md"},
    ]

    async def assert_identical():
        for kwargs in kwargs_shapes:
            manual = build_analyst_prompt(
                analyst, "任务正文T",
                memory_block=await memory.memory_block(analyst.id), **kwargs,
            )
            assert await memory.prompt_with_memory(analyst, "任务正文T", **kwargs) == manual

    # no stored memory: empty block is a strict no-op
    await assert_identical()
    assert "常备记忆" not in await memory.prompt_with_memory(analyst, "任务正文T")

    # with stored memory: the block lands between persona and task
    await db.execute(
        "INSERT INTO analyst_memory (id, analyst_id, version, work_date, compact_md, supersedes, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m-entry", ANALYST, 2, "2026-07-19", "【核心立场】入口等价性素材。", None, bus.now_iso()),
    )
    await assert_identical()
    unified = await memory.prompt_with_memory(analyst, "任务正文T")
    assert "## 常备记忆（第 2 版 · 2026-07-19）" in unified
    assert unified.index(f"你是 {analyst.name}") < unified.index("## 常备记忆") < unified.index("## 任务")


async def test_prompt_injection_between_persona_and_task():
    analyst = get_analyst(ANALYST)
    block = "## 常备记忆（第 1 版 · 2026-07-19）\n记忆正文M。"
    prompt = build_analyst_prompt(
        analyst, "任务正文T",
        context_blocks=["## 临时上下文\n上下文C。"], memory_block=block,
    )
    i_persona = prompt.index(f"你是 {analyst.name}")
    i_memory = prompt.index("## 常备记忆")
    i_context = prompt.index("## 临时上下文")
    i_task = prompt.index("## 任务")
    assert i_persona < i_memory < i_context < i_task

    # omitted/empty memory keeps the prompt unchanged
    plain = build_analyst_prompt(analyst, "任务正文T", context_blocks=["## 临时上下文\n上下文C。"])
    assert "常备记忆" not in plain
    assert plain == build_analyst_prompt(
        analyst, "任务正文T", context_blocks=["## 临时上下文\n上下文C。"], memory_block="",
    )


# ---- vault export ----------------------------------------------------------------

@pytest.fixture
def clean_vault_dir():
    writer = get_writer()
    assert writer.enabled and writer.root is not None
    shutil.rmtree(writer.root, ignore_errors=True)
    writer.root.mkdir(parents=True, exist_ok=True)
    yield


async def _export_latest(analyst_id: str) -> None:
    row = await memory.latest(analyst_id)
    event = bus.Event(
        id=0, type="memory.compacted", ref_kind="analyst", ref_id=analyst_id,
        payload={"version": row["version"], "memory_id": row["id"]},
    )
    await exporter._on_memory(event)


async def test_memory_export_region_note_preserves_annotations(clean_vault_dir):
    writer = get_writer()
    await _seed_card(ANALYST, "导出素材一。")
    assert (await memory.compact_one(ANALYST))["status"] == "completed"
    await _export_latest(ANALYST)

    note = writer.root / "Analysts" / ANALYST / "memory.md"
    text = note.read_text(encoding="utf-8")
    assert "managed: institute" in text
    assert REGION_BEGIN in text and REGION_END in text
    assert "第 1 版" in text

    # human annotation outside the managed region
    note.write_text(text + "\n> 操作员批注：这条要长期保留。\n", encoding="utf-8")

    await _seed_card(ANALYST, "导出素材二。")
    assert (await memory.compact_one(ANALYST))["version"] == 2
    await _export_latest(ANALYST)

    text2 = note.read_text(encoding="utf-8")
    assert "操作员批注：这条要长期保留" in text2   # annotation survived
    assert "第 2 版" in text2                       # region content advanced
    # no conflict sibling: the regeneration landed in place
    assert sorted(p.name for p in note.parent.iterdir()) == ["memory.md"]

    counts = await writer.doctor()
    assert counts["drifted"] == 0 and counts["conflict"] == 0
