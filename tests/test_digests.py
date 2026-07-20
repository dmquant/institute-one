"""Curl-back digests + streaming ask (ROADMAP Phase 2, agent B8's partition).

Digest endpoints must return plain markdown (never JSON, never 5xx), clamp at
8KB with an explicit marker, and degrade to stable placeholders when a
later-phase table (analyst_memory / fact_cards / operator_actions) is absent.
The stream endpoint must yield chunk frames then exactly one ``done`` frame,
mirror /api/ask's analyst_id preprocessing (REVIEW-B8 MF-2), buffer through a
bounded drop-oldest queue (MF-1), and a client disconnect must NOT cancel the
underlying executor task while flipping the bridge to closed.

The routers are not yet mounted in app/main.py (mounting belongs to the main
agent — see PATCH-NOTES-B8.md), so tests build a bare FastAPI app around them,
same as tests/test_market_data.py. DB + echo hand come from the autouse
``app_runtime`` fixture.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.hands.base import Hand, HandResult
from app.hands.registry import get_registry
from app.institute.digests import DIGEST_CAP_BYTES, TRUNCATION_MARK
from app.router import executor
from app.vault.writer import get_writer


def _make_app() -> FastAPI:
    from app.api import ask_stream as api_ask_stream
    from app.api import digests as api_digests

    app = FastAPI()
    app.include_router(api_digests.router)
    app.include_router(api_ask_stream.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_markdown(resp) -> str:
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    assert not body.lstrip().startswith(("{", "[")), "digest must not be JSON-wrapped"
    return body


# ---- seed helpers ------------------------------------------------------------

async def _mk_completed_run(
    run_id: str, *, workflow_id: str = "briefing", title: str, started_at: str,
    final_summary: str, work_date_var: str = "2026-07-18", status: str = "completed",
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"sess-{run_id}", title, "workflow", f"/tmp/{run_id}", now, now),
    )
    results = [
        {"step_id": "01", "title": "step one", "task_id": "t1", "status": "completed",
         "summary": "stale first-step summary", "output_file": "01.md"},
        {"step_id": "03", "title": "compile", "task_id": "t3", "status": "completed",
         "summary": final_summary, "output_file": "final.md"},
    ]
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, session_id, status, variables, results, source, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, workflow_id, f"sess-{run_id}", status,
         json.dumps({"WORK_DATE": work_date_var}, ensure_ascii=False),
         json.dumps(results, ensure_ascii=False), "api", started_at, now),
    )


async def _mk_research(topic: str, summary: str, *, completed_at: str, wd: str | None = "2026-07-18") -> None:
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at, work_date) VALUES (?,?,?,?,?)",
        (topic, None, summary, completed_at, wd),
    )


# mirrors migrations/0010_analyst_memory.sql (the analyst-memory card's real
# schema) so these tests exercise the exact column shapes B8 will read in prod
_ANALYST_MEMORY_DDL = (
    "CREATE TABLE IF NOT EXISTS analyst_memory ("
    " id TEXT PRIMARY KEY, analyst_id TEXT NOT NULL, version INTEGER NOT NULL,"
    " work_date TEXT NOT NULL, compact_md TEXT NOT NULL, supersedes TEXT,"
    " created_at TEXT NOT NULL, UNIQUE (analyst_id, version))"
)


async def _reset_analyst_memory_table(create: bool) -> None:
    """Pin the table state explicitly: the migration may or may not have landed
    on this checkout, and the missing-table path must stay covered either way."""
    await db.execute("DROP TABLE IF EXISTS analyst_memory")
    if create:
        await db.execute(_ANALYST_MEMORY_DDL)


async def _mk_memory(analyst_id: str, version: int, compact_md: str, *, supersedes: str | None = None) -> None:
    await db.execute(
        "INSERT INTO analyst_memory (id, analyst_id, version, work_date, compact_md, supersedes, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"am-{analyst_id}-{version}", analyst_id, version, "2026-07-18", compact_md, supersedes, bus.now_iso()),
    )


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ---- recent-reports.md --------------------------------------------------------

async def test_recent_reports_shape_and_days_filter():
    await db.execute(
        "INSERT INTO workflows (id, name, description, variables, steps, updated_at) VALUES (?,?,?,?,?,?)",
        ("briefing", "晨会简报", "", "[]", "[]", bus.now_iso()),
    )
    await _mk_completed_run(
        "run-new", title="晨会简报 2026-07-18", started_at=bus.now_iso(),
        final_summary="## 核心结论\n今日主线是 AI 算力",
    )
    await _mk_completed_run(
        "run-old", title="OLD-RUN-MARKER", started_at=_days_ago(30),
        final_summary="old summary", work_date_var="2026-06-20",
    )
    await _mk_completed_run(
        "run-running", title="RUNNING-MARKER", started_at=bus.now_iso(),
        final_summary="not done yet", status="running",
    )
    await _mk_research("AI 服务器供应链", "## 核心结论\n供需仍然偏紧", completed_at=bus.now_iso())
    await _mk_research("OLD-RESEARCH-MARKER", "gone", completed_at=_days_ago(30), wd="2026-06-20")

    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/recent-reports.md?days=7"))
        assert body.startswith("# Recent reports (last 7 days)")
        # workflow line: WORK_DATE variable + session title + LAST step's summary, one line
        assert "- 2026-07-18 · 晨会简报 2026-07-18 — 核心结论 今日主线是 AI 算力" in body
        assert "stale first-step summary" not in body
        # research line from research_log
        assert "- 2026-07-18 · AI 服务器供应链 — 核心结论 供需仍然偏紧" in body
        # days window + status filter
        assert "OLD-RUN-MARKER" not in body
        assert "OLD-RESEARCH-MARKER" not in body
        assert "RUNNING-MARKER" not in body

        # widening the window brings the old rows back
        body90 = _assert_markdown(await client.get("/api/institute/recent-reports.md?days=90"))
        assert "OLD-RUN-MARKER" in body90 and "OLD-RESEARCH-MARKER" in body90

        # out-of-range days clamp instead of erroring (curl-robustness)
        assert "(last 90 days)" in _assert_markdown(
            await client.get("/api/institute/recent-reports.md?days=40000")
        )
        assert "(last 1 days)" in _assert_markdown(
            await client.get("/api/institute/recent-reports.md?days=-3")
        )


async def test_recent_reports_empty_still_markdown():
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/recent-reports.md"))
        assert "_no completed workflow runs in the last 7 days_" in body
        assert "_no research completed in the last 7 days_" in body


# ---- analyst-memory/{id}.md ---------------------------------------------------

async def test_analyst_memory_missing_table_degrades_to_placeholder():
    await _reset_analyst_memory_table(create=False)
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/analyst-memory/macro-analyst.md"))
        assert body.startswith("# no memory yet")


async def test_analyst_memory_empty_table_and_blank_compact():
    await _reset_analyst_memory_table(create=True)
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/analyst-memory/macro-analyst.md"))
        assert body.startswith("# no memory yet")

        await _mk_memory("macro-analyst", 1, "   \n  ")
        body = _assert_markdown(await client.get("/api/institute/analyst-memory/macro-analyst.md"))
        assert body.startswith("# no memory yet")


async def test_analyst_memory_latest_version_verbatim_and_8kb_cap():
    await _reset_analyst_memory_table(create=True)
    big = "## 持仓观点\n" + ("多头逻辑仍在，密切跟踪出货量数据。" * 600)  # ≫ 8KB
    await _mk_memory("macro-analyst", 1, "v1 旧记忆，应被 v2 覆盖")
    await _mk_memory("macro-analyst", 2, big, supersedes="am-macro-analyst-1")
    async with _client(_make_app()) as client:
        resp = await client.get("/api/institute/analyst-memory/macro-analyst.md")
        body = _assert_markdown(resp)
        assert body.startswith("# Analyst memory — macro-analyst (v2, 2026-07-18)")
        assert "## 持仓观点" in body  # compact quoted verbatim, not paraphrased
        assert "v1 旧记忆" not in body
        assert len(body.encode("utf-8")) <= DIGEST_CAP_BYTES
        assert body.endswith(TRUNCATION_MARK)

        # other analysts don't see this memory
        other = _assert_markdown(await client.get("/api/institute/analyst-memory/equity-analyst.md"))
        assert other.startswith("# no memory yet")


# ---- analyst-disputes/{id}.md (fact-check v2 body, C1-P1-3) -------------------

async def _mk_dispute_card(
    card_id: str, analyst_id: str, claim: str, *, status: str = "disputed",
    source_kind: str = "whiteboard_card", source_ref: str = "card-1",
    category: str = "numerical", created_at: str | None = None,
    evidence: str | None = None, source_urls: list[str] | None = None,
) -> None:
    await db.execute(
        "INSERT INTO fact_cards (id, source_kind, source_ref, analyst_id, claim, "
        "category, status, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (card_id, source_kind, source_ref, analyst_id, claim, category, status,
         f"hash-{card_id}", created_at or bus.now_iso()),
    )
    if evidence is not None or source_urls is not None:
        await db.execute(
            "INSERT INTO verified_facts (id, fact_card_id, verdict, evidence, "
            "source_urls, work_date, verified_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"vf-{card_id}", card_id, "DISPUTED", evidence,
             json.dumps(source_urls or [], ensure_ascii=False),
             "2026-07-18", bus.now_iso(), bus.now_iso()),
        )


async def test_analyst_disputes_empty_still_placeholder():
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/analyst-disputes/macro-analyst.md"))
        assert body.startswith("# no disputes recorded")
        assert "macro-analyst" in body


async def test_analyst_disputes_missing_tables_degrade_to_placeholder():
    await db.execute("DROP TABLE IF EXISTS verified_facts")  # child first (FK)
    await db.execute("DROP TABLE IF EXISTS fact_cards")
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/analyst-disputes/macro-analyst.md"))
        assert body.startswith("# no disputes recorded")


async def test_analyst_disputes_lists_own_disputed_claims_newest_first():
    await _mk_dispute_card(
        "fc-old", "macro-analyst", "2025 年全球光伏新增装机为 600GW",
        created_at="2026-07-10T01:00:00+00:00",
        evidence="IEA 口径为 452GW", source_urls=["https://example.org/iea"],
    )
    await _mk_dispute_card(
        "fc-new", "macro-analyst", "美联储 6 月加息 50bp",
        status="self_contradicted", source_kind="research_report", source_ref="rq-9",
        category="event", created_at="2026-07-18T01:00:00+00:00",
    )
    # noise that must NOT surface: other analyst / non-disputed statuses
    await _mk_dispute_card("fc-other", "equity-analyst", "OTHER-ANALYST-MARKER",
                           created_at="2026-07-19T01:00:00+00:00")
    await _mk_dispute_card("fc-ok", "macro-analyst", "VERIFIED-MARKER", status="verified")
    await _mk_dispute_card("fc-pending", "macro-analyst", "PENDING-MARKER", status="pending")

    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/analyst-disputes/macro-analyst.md"))
    assert body.startswith("# Disputed claims — macro-analyst")
    # newest first: the self_contradicted July-18 card renders before July-10
    assert body.index("美联储 6 月加息 50bp") < body.index("2025 年全球光伏新增装机为 600GW")
    assert "已驳斥（DISPUTED）" in body
    assert "重复已驳斥论断（self_contradicted）" in body
    assert "- 证据：IEA 口径为 452GW" in body
    assert "https://example.org/iea" in body
    assert "whiteboard_card `card-1`（2026-07-10）" in body
    assert "research_report `rq-9`（2026-07-18）" in body
    assert "OTHER-ANALYST-MARKER" not in body
    assert "VERIFIED-MARKER" not in body
    assert "PENDING-MARKER" not in body

    # other analysts see only their own record
    async with _client(_make_app()) as client:
        other = _assert_markdown(await client.get("/api/institute/analyst-disputes/equity-analyst.md"))
    assert "OTHER-ANALYST-MARKER" in other
    assert "美联储 6 月加息 50bp" not in other


async def test_analyst_disputes_8kb_cap():
    for i in range(20):
        await _mk_dispute_card(
            f"fc-{i:02d}", "macro-analyst", f"论断{i:02d}：" + "很长的存疑内容。" * 60,
            created_at=f"2026-07-{i + 1:02d}T01:00:00+00:00",
            evidence="证据材料。" * 80,
        )
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/analyst-disputes/macro-analyst.md"))
    assert len(body.encode("utf-8")) <= DIGEST_CAP_BYTES
    assert body.endswith(TRUNCATION_MARK)


# ---- placeholder endpoints ----------------------------------------------------

async def test_operator_actions_placeholder():
    async with _client(_make_app()) as client:
        body = _assert_markdown(await client.get("/api/institute/operator-actions-digest.md"))
        assert body.startswith("# no operator actions recorded")


# ---- whiteboard source-dossier disputed callout (C1-P1-3) ----------------------
# The exporter tests live here with the rest of the fact-check delivery chain:
# tests/test_vault.py belongs to another partition.

@pytest.fixture
def clean_vault():
    """Vault tmp dir outlives the per-test DB wipe; start with disk == ledger."""
    writer = get_writer()
    assert writer.enabled and writer.root is not None
    shutil.rmtree(writer.root, ignore_errors=True)
    writer.root.mkdir(parents=True, exist_ok=True)
    return writer


async def _mk_board_with_cards() -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, work_date, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("b-1", "成熟制程", "产能过剩吗？", "completed", "2026-07-18", now, now),
    )
    for cid, idx, summary in (("c-1", 1, "中芯国际扩产节奏放缓。"), ("c-2", 2, "价格战趋缓。")):
        await db.execute(
            "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, summary, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, "b-1", idx, "chief-strategist", "completed", summary, now),
        )


async def test_export_board_injects_disputed_callout(clean_vault):
    from app.vault import exporter

    await _mk_board_with_cards()
    await _mk_dispute_card(
        "fc-wb", "chief-strategist", "成熟制程产能利用率已回到 95%",
        source_kind="whiteboard_card", source_ref="c-1", evidence="行业口径为 78%",
    )

    rel = await exporter.export_board("b-1")
    assert rel == "Whiteboard/2026-07-18 成熟制程.md"
    text = (clean_vault.root / rel).read_text(encoding="utf-8")
    assert "> [!warning] 事实核查：本白板讨论中有论断存疑" in text
    assert "> - 成熟制程产能利用率已回到 95%（行业口径为 78%）" in text
    # callout sits right under the title, before the question and the cards
    assert text.index("# 成熟制程") < text.index("> [!warning]") < text.index("> 产能过剩吗？")
    assert text.index("> [!warning]") < text.index("## card-01")


async def test_export_board_without_disputes_has_no_callout(clean_vault):
    from app.vault import exporter

    await _mk_board_with_cards()
    rel = await exporter.export_board("b-1")
    text = (clean_vault.root / rel).read_text(encoding="utf-8")
    assert "[!warning]" not in text


async def test_factcheck_disputed_handler_reprojects_board_dossier(clean_vault):
    from app.vault import exporter

    await _mk_board_with_cards()
    # board exported BEFORE the dispute existed — no callout yet
    rel = await exporter.export_board("b-1")
    assert "[!warning]" not in (clean_vault.root / rel).read_text(encoding="utf-8")

    await _mk_dispute_card(
        "fc-wb", "chief-strategist", "成熟制程产能利用率已回到 95%",
        source_kind="whiteboard_card", source_ref="c-1", evidence="行业口径为 78%",
    )
    event = bus.Event(
        id=11, type="factcheck.disputed", ref_kind="fact_card", ref_id="fc-wb",
        payload={"kind": "disputed", "source_kind": "whiteboard_card", "source_ref": "c-1"},
    )
    await exporter._on_factcheck_disputed(event)

    # the rolling digest landed AND the board dossier got its warning callout
    inbox = (clean_vault.root / "Inbox/Disputed Claims.md").read_text(encoding="utf-8")
    assert "成熟制程产能利用率已回到 95%" in inbox
    text = (clean_vault.root / rel).read_text(encoding="utf-8")
    assert "> [!warning] 事实核查：本白板讨论中有论断存疑" in text

    # unknown card id degrades to a warning, never raises (bus-handler contract)
    stray = bus.Event(
        id=12, type="factcheck.disputed", ref_kind="fact_card", ref_id="fc-x",
        payload={"kind": "disputed", "source_kind": "whiteboard_card", "source_ref": "no-such-card"},
    )
    await exporter._on_factcheck_disputed(stray)


# ---- POST /api/ask/stream -----------------------------------------------------

def _make_ask_app() -> FastAPI:
    """Bare app with BOTH ask endpoints, for stream-vs-sync parity tests."""
    from app.api import ask_stream as api_ask_stream
    from app.api import tasks as api_tasks

    app = FastAPI()
    app.include_router(api_tasks.router)
    app.include_router(api_ask_stream.router)
    return app


def _frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def test_ask_stream_echo_chunks_then_done():
    async with _client(_make_app()) as client:
        resp = await client.post("/api/ask/stream", json={"prompt": "hello stream", "hand": "echo"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        frames = _frames(resp.text)
        assert len(frames) >= 2
        for f in frames[:-1]:
            assert f["type"] in ("stdout", "stderr", "status")
        assert any(f["type"] == "stdout" and "[echo] hello stream" in f["text"] for f in frames[:-1])

        done = frames[-1]
        assert done["type"] == "done"
        task = done["task"]
        assert task["status"] == "completed" and task["exit_code"] == 0
        assert task["hand"] == "echo"
        assert "[echo] hello stream" in task["output"]
        # the run is a normal tasks row (audit spine)
        row = await executor.get_task(task["id"])
        assert row is not None and row.status == "completed"


async def test_ask_stream_defaults_to_default_hand():
    async with _client(_make_app()) as client:
        resp = await client.post("/api/ask/stream", json={"prompt": "no hand given"})
        done = _frames(resp.text)[-1]
        assert done["type"] == "done"
        assert done["task"]["hand"] == "echo"  # tests pin default hand to echo
        assert done["task"]["status"] == "completed"


async def test_ask_stream_unknown_hand_reports_done_frame_not_500():
    async with _client(_make_app()) as client:
        resp = await client.post("/api/ask/stream", json={"prompt": "x", "hand": "no-such-hand"})
        assert resp.status_code == 200
        frames = _frames(resp.text)
        done = frames[-1]
        assert done["type"] == "done"
        assert done["task"]["status"] == "rate_limited"  # executor: no hand available
        assert "no hand available" in done["task"]["error"]


def _strip_date_anchor(text: str) -> str:
    """The persona sandwich opens with the minute-resolution date anchor —
    drop that first block so parity checks can't flake across a minute roll."""
    return text.split("\n\n", 1)[1]


async def test_ask_stream_analyst_id_parity_with_sync_ask():
    """MF-2: same body contract AND same preprocessing as POST /api/ask —
    analyst_id must produce the identical persona-wrapped prompt and task shape."""
    payload = {"prompt": "评估今日流动性", "analyst_id": "macro-analyst"}
    async with _client(_make_ask_app()) as client:
        sync_task = (await client.post("/api/ask", json=payload)).json()
        frames = _frames((await client.post("/api/ask/stream", json=payload)).text)
        stream_task = frames[-1]["task"]

    assert sync_task["status"] == stream_task["status"] == "completed"
    assert sync_task["hand"] == stream_task["hand"] == "echo"
    assert sync_task["exit_code"] == stream_task["exit_code"] == 0
    assert sync_task["error"] == stream_task["error"] is None
    # both went through build_analyst_prompt: identical prompts modulo the
    # minute-resolution date anchor, persona + task sections included
    sync_row = await executor.get_task(sync_task["id"])
    stream_row = await executor.get_task(stream_task["id"])
    assert _strip_date_anchor(stream_row.prompt) == _strip_date_anchor(sync_row.prompt)
    assert "宏观分析师" in stream_row.prompt  # persona block
    assert "## 任务\n评估今日流动性" in stream_row.prompt
    assert stream_row.prompt != "评估今日流动性"  # NOT the bare prompt
    # echoed output matches too (echo prints the prompt back)
    assert _strip_date_anchor(stream_task["output"]) == _strip_date_anchor(sync_task["output"])
    # the streamed chunks carry the persona output as well
    assert any(f["type"] == "stdout" and "宏观分析师" in f["text"] for f in frames[:-1])


async def test_ask_stream_unknown_analyst_404_before_stream():
    """MF-2: unknown analyst is a request error (mirrors /api/ask), not a stream frame."""
    payload = {"prompt": "x", "analyst_id": "no-such-analyst"}
    async with _client(_make_ask_app()) as client:
        sync_resp = await client.post("/api/ask", json=payload)
        stream_resp = await client.post("/api/ask/stream", json=payload)
    assert sync_resp.status_code == stream_resp.status_code == 404
    assert "no-such-analyst" in stream_resp.json()["detail"]
    # nothing was submitted to the executor
    assert await db.query("SELECT id FROM tasks") == []


# ---- M1: bounded queue --------------------------------------------------------

def test_chunk_bridge_drops_oldest_and_counts():
    from app.api.ask_stream import _ChunkBridge

    bridge = _ChunkBridge(maxsize=3)
    for i in range(8):
        bridge.offer(i)
    assert bridge.dropped == 5
    kept = [bridge.queue.get_nowait() for _ in range(bridge.queue.qsize())]
    assert kept == [5, 6, 7]  # newest survive, FIFO order preserved

    bridge.offer(99)
    bridge.close()
    assert bridge.closed and bridge.queue.empty()  # close drains the buffer
    bridge.offer(100)  # short-circuits after close
    assert bridge.queue.empty() and bridge.dropped == 5


class BurstHand(Hand):
    """Emits 10 chunks in one synchronous burst — with a tiny queue bound the
    oldest must be dropped while the newest and the done signal survive."""

    name = "burst"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None) -> HandResult:
        for i in range(10):
            if on_chunk:
                on_chunk({"type": "stdout", "text": f"chunk-{i}"})
        return HandResult(output="burst done", exit_code=0)


async def test_ask_stream_slow_consumer_drops_oldest_reports_status(monkeypatch):
    from app.api import ask_stream as ask_stream_mod

    get_registry().register(BurstHand())
    monkeypatch.setattr(ask_stream_mod, "QUEUE_MAX_CHUNKS", 4)

    async with _client(_make_app()) as client:
        resp = await client.post("/api/ask/stream", json={"prompt": "flood", "hand": "burst"})
        frames = _frames(resp.text)

    chunk_frames, status_frame, done = frames[:-2], frames[-2], frames[-1]
    assert [f["text"] for f in chunk_frames] == ["chunk-6", "chunk-7", "chunk-8", "chunk-9"]
    assert status_frame["type"] == "status"
    assert "6 chunks dropped" in status_frame["text"]
    assert done["type"] == "done" and done["task"]["status"] == "completed"


class SlowChunkHand(Hand):
    """Emits one chunk, then finishes after a delay — lets the disconnect test
    close the stream while the task is still running."""

    name = "slowchunk"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None) -> HandResult:
        if on_chunk:
            on_chunk({"type": "stdout", "text": "first\n"})
        await asyncio.sleep(0.3)
        if on_chunk:
            on_chunk({"type": "stdout", "text": "second\n"})
        return HandResult(output="first\nsecond\n", exit_code=0)


async def test_ask_stream_client_disconnect_does_not_cancel_task(monkeypatch):
    """Fire-and-forget semantics: closing the response mid-stream must leave the
    executor task running to completion (unlike the synchronous /api/ask) —
    and (M1) the bridge must stop buffering the moment the consumer is gone."""
    get_registry().register(SlowChunkHand())
    from app.api import ask_stream as ask_stream_mod

    bridges: list = []

    class SpyBridge(ask_stream_mod._ChunkBridge):
        def __init__(self, maxsize: int) -> None:
            super().__init__(maxsize)
            bridges.append(self)

    monkeypatch.setattr(ask_stream_mod, "_ChunkBridge", SpyBridge)

    resp = await ask_stream_mod.ask_stream(
        ask_stream_mod.AskBody(prompt="slow please", hand="slowchunk")
    )
    agen = resp.body_iterator
    first = json.loads(await anext(agen))
    assert first == {"type": "stdout", "text": "first\n"}
    await agen.aclose()  # simulate the client hanging up after the first frame

    (bridge,) = bridges
    assert bridge.closed  # generator exit flipped the bridge off

    row = await db.query_one("SELECT id FROM tasks WHERE prompt = 'slow please'")
    assert row is not None
    task = None
    for _ in range(100):  # the task must finish on its own despite the disconnect
        task = await executor.get_task(row["id"])
        if task.status in executor.TERMINAL:
            break
        await asyncio.sleep(0.05)
    assert task is not None and task.status == "completed"
    assert task.output == "first\nsecond\n"
    # the post-disconnect chunk and the done sentinel were short-circuited,
    # not buffered: no unbounded accumulation after the client is gone
    assert bridge.queue.empty()
