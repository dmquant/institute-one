"""Vault-exporter handlers driven by synthetic bus events (ROADMAP Phase 8).

Each handler is invoked directly with a hand-built ``bus.Event`` whose payload
copies the REAL emit shape of its domain (grepped from ``bus.emit`` call
sites), against seeded rows/workspaces — then the note on disk, its
``managed: institute`` frontmatter, and the vault_index ledger row are
asserted. Handlers are never registered on the live bus here (that would leak
into every later test); ``register()``'s wiring is asserted separately with a
save/restore of the handler list.

The degrade face matters as much as the happy path: every handler must
swallow a missing/empty payload (bus handlers never raise) and write nothing.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app import bus, db
from app.config import get_settings
from app.institute import sessions
from app.institute.prompts import work_date
from app.vault import exporter
from app.vault.writer import get_writer

ANALYST = "chief-strategist"  # first roster entry; stable in catalog/analysts.json


@pytest.fixture(autouse=True)
def clean_vault(app_runtime):
    """Vault files persist across tests (one tmp tree per pytest run) while the
    ledger db is wiped per test — start each test with an empty vault so
    region-mode writes cannot see a ledger-less leftover file (conflict path)."""
    vault = get_settings().vault_dir
    assert vault is not None
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir(parents=True, exist_ok=True)
    yield


def _event(type_: str, ref_id: str, payload: dict) -> bus.Event:
    return bus.Event(id=0, type=type_, ref_kind="test", ref_id=ref_id,
                     payload=payload, created_at=bus.now_iso())


def _vault() -> Path:
    root = get_writer().root
    assert root is not None
    return root


def _read_note(rel: str) -> str:
    path = _vault() / rel
    assert path.is_file(), f"expected vault note {rel} (have: {[str(p.relative_to(_vault())) for p in _vault().rglob('*.md')]})"
    return path.read_text(encoding="utf-8")


async def _ledger(rel: str) -> dict:
    row = await db.query_one("SELECT * FROM vault_index WHERE path = ?", (rel,))
    assert row is not None, f"no vault_index row for {rel}"
    return row


# ---- research.completed ------------------------------------------------------

async def test_research_completed_exports_report_note():
    session = await sessions.create_session("研究会话", kind="research")
    ws = Path(session["workspace_dir"])
    (ws / "06_深度报告.md").write_text("# 光模块\n\n景气度维持高位。", encoding="utf-8")
    (ws / "07_后续跟进.md").write_text("- 跟踪 1.6T 出货", encoding="utf-8")

    # real emit shape: app/institute/research.py research.completed
    await exporter._on_research(_event("research.completed", "rq-001", {
        "topic": "光模块", "run_id": "run-001", "session_id": session["id"],
        "summary": "需求可持续",
    }))

    rel = f"Research/光模块/{work_date()} 深度报告.md"
    text = _read_note(rel)
    assert "managed: institute" in text
    assert "## 核心结论" in text and "需求可持续" in text
    assert "景气度维持高位" in text
    assert "## 后续跟进" in text
    row = await _ledger(rel)
    assert row["state"] == "clean"
    assert row["artifact_kind"] == "research"


# ---- workflow.completed (briefing / daily) --------------------------------------

async def test_workflow_completed_exports_compiled_briefing():
    run_id = "wfrun-001"
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, status, variables, results, source, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, "briefing", "completed", "{}",
         '[{"step_id": "s1", "title": "宏观扫描", "summary": "A股高开。"}]',
         "test", bus.now_iso()),
    )
    # real emit shape: app/institute/workflows.py _finish_run -> workflow.completed
    await exporter._on_workflow(_event("workflow.completed", run_id, {
        "workflow_id": "briefing", "session_id": None, "variables": {},
        "results": [{"step_id": "s1", "title": "宏观扫描", "summary": "A股高开。"}],
    }))

    rel = f"Briefing/{work_date()} 晨会简报.md"
    text = _read_note(rel)
    assert "managed: institute" in text
    assert "宏观扫描" in text and "A股高开" in text
    assert (await _ledger(rel))["artifact_kind"] == "briefing"

    # a non-compiled workflow (e.g. research) is not exported by this handler
    await exporter._on_workflow(_event("workflow.completed", "wfrun-002", {
        "workflow_id": "research", "session_id": None, "variables": {}, "results": [],
    }))
    assert not (_vault() / f"Research/{work_date()} 晨会简报.md").exists()


# ---- whiteboard.board_completed ---------------------------------------------------

async def test_board_completed_exports_cards_note():
    board_id = "board-001"
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, work_date, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (board_id, "机器人产业链", "国产化率还能提多少", "completed", 2, work_date(), now, now),
    )
    for idx, summary in ((1, "上游减速器盈利改善"), (2, "下游集成商价格战")):
        await db.execute(
            "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, summary, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"card-{idx}", board_id, idx, ANALYST, "completed", summary, now),
        )
    # real emit shape: app/institute/whiteboard.py whiteboard.board_completed
    await exporter._on_board(_event("whiteboard.board_completed", board_id, {
        "topic": "机器人产业链", "session_id": None, "cards": 2,
    }))

    rel = f"Whiteboard/{work_date()} 机器人产业链.md"
    text = _read_note(rel)
    assert "managed: institute" in text
    assert "> 国产化率还能提多少" in text
    assert "card-01" in text and "card-02" in text
    assert "上游减速器盈利改善" in text and "下游集成商价格战" in text
    assert (await _ledger(rel))["artifact_id"] == board_id


# ---- analyst_daily.completed -------------------------------------------------------

async def test_analyst_daily_completed_exports_from_task_output():
    task_id = "task00000001"
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, output, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, "echo", "写日报", "completed", "daily", "今日观察：出口链回暖。", bus.now_iso()),
    )
    # real emit shape: app/institute/analyst_daily.py analyst_daily.completed
    await exporter._on_analyst_daily(_event("analyst_daily.completed", ANALYST, {
        "date": work_date(), "session_id": None, "task_id": task_id,
        "file": f"{ANALYST}.md", "whiteboard_topics": 0, "mailbox_threads": 0,
    }))

    rel = f"Analysts/{ANALYST}/{work_date()} 日报.md"
    text = _read_note(rel)
    assert "managed: institute" in text
    assert "今日观察：出口链回暖。" in text
    assert (await _ledger(rel))["artifact_id"] == f"{ANALYST}:{work_date()}"


# ---- memory.compacted (managed-region note) -------------------------------------------

async def test_memory_compacted_exports_region_note():
    await db.execute(
        "INSERT INTO analyst_memory (id, analyst_id, version, work_date, compact_md, cursors, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("mem-001", ANALYST, 3, work_date(), "- 看多出口链\n- 撤回：地产反转判断", "{}", bus.now_iso()),
    )
    # real emit shape: app/institute/memory.py memory.compacted
    await exporter._on_memory(_event("memory.compacted", ANALYST, {
        "version": 3, "work_date": work_date(), "memory_id": "mem-001", "task_id": "t-1",
    }))

    rel = f"Analysts/{ANALYST}/memory.md"
    text = _read_note(rel)
    assert "managed: institute" in text
    assert "%% institute:begin %%" in text and "%% institute:end %%" in text
    assert "第 3 版" in text and "看多出口链" in text
    row = await _ledger(rel)
    assert row["mode"] == "region"
    assert row["state"] == "clean"


# ---- factcheck.disputed ------------------------------------------------------------

async def test_factcheck_disputed_regenerates_disputes_digest():
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO fact_cards (id, source_kind, source_ref, analyst_id, claim, category, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("fc-001", "whiteboard_card", "card-9", ANALYST,
         "某公司 2025 年出货量翻三倍", "numerical", "disputed", now),
    )
    await db.execute(
        "INSERT INTO verified_facts (id, fact_card_id, verdict, evidence, source_urls, work_date, verified_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("vf-001", "fc-001", "DISPUTED", "官方口径为同比 +40%",
         '["https://example.com/ir"]', work_date(), now, now),
    )
    # real emit shape: app/institute/factcheck.py factcheck.disputed
    await exporter._on_factcheck_disputed(_event("factcheck.disputed", "fc-001", {
        "kind": "disputed", "claim": "某公司 2025 年出货量翻三倍", "category": "numerical",
        "analyst_id": ANALYST, "source_kind": "whiteboard_card", "source_ref": "card-9",
    }))

    text = _read_note("Inbox/Disputed Claims.md")
    assert "managed: institute" in text
    assert "某公司 2025 年出货量翻三倍" in text
    assert "已驳斥（DISPUTED）" in text
    assert "官方口径为同比 +40%" in text
    assert "https://example.com/ir" in text


# ---- workflow.* (committee) — M8-012 ---------------------------------------------------

_COMMITTEE_RESULTS = [
    {"step_id": "01-agenda", "title": "辩题遴选", "task_id": "t-1",
     "status": "completed", "summary": "命题：是否见顶"},
    {"step_id": "05-verdict", "title": "委员会裁决汇编", "task_id": "t-5",
     "status": "completed", "summary": "多数看多，一人保留"},
]


async def _mk_committee_run(run_id: str, *, status: str = "completed",
                            session_id: str | None = None, error: str | None = None,
                            variables: dict | None = None) -> dict:
    variables = variables if variables is not None else {
        "WORK_DATE": "2026-07-17",
        "WEEK_DISPUTES": "- 「A股流动性拐点」（2026-07-15 研讨）：外资回流节奏分歧",
    }
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, session_id, status, variables, results, error, source, started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "committee", session_id, status,
         json.dumps(variables, ensure_ascii=False),
         json.dumps(_COMMITTEE_RESULTS, ensure_ascii=False),
         error, "test", bus.now_iso()),
    )
    return variables


async def test_committee_completed_exports_note_with_input_snapshot():
    from app.institute import workflows

    await workflows.reconcile_from_disk()      # the committee group derives from the definition
    session = await sessions.create_session("委员会会话", kind="workflow")
    ws = Path(session["workspace_dir"])
    (ws / "委员会裁决.md").write_text("## 裁决\n\n多数意见：看多，保留意见完整记录。", encoding="utf-8")
    run_id = "wfrun-cmt-01"
    variables = await _mk_committee_run(run_id, session_id=session["id"])

    # real emit shape: app/institute/workflows.py _finish_run -> workflow.completed
    await exporter._on_committee(_event("workflow.completed", run_id, {
        "workflow_id": "committee", "session_id": session["id"],
        "variables": variables, "results": _COMMITTEE_RESULTS,
    }))

    rel = "Committee/2026-07-17 委员会裁决.md"   # the run's frozen WORK_DATE
    text = _read_note(rel)
    assert "managed: institute" in text
    assert "多数意见：看多" in text                          # verdict text from the workspace
    assert "## 输入快照（当周白板研讨摘要）" in text
    assert "A股流动性拐点" in text                           # the frozen ${WEEK_DISPUTES}
    row = await _ledger(rel)
    assert row["artifact_kind"] == "committee" and row["artifact_id"] == run_id
    # rows are truth: the durable committee run record settled too
    rec = await db.query_one(
        "SELECT * FROM multi_agent_runs WHERE workflow_run_id = ?", (run_id,)
    )
    assert rec is not None and rec["status"] == "completed"
    assert "A股流动性拐点" in rec["prompt"]                  # input snapshot on the record


async def test_committee_completed_without_workspace_falls_back_to_steps():
    from app.institute import workflows

    await workflows.reconcile_from_disk()
    run_id = "wfrun-cmt-02"
    variables = await _mk_committee_run(run_id, variables={"WORK_DATE": "2026-07-18",
                                                           "WEEK_DISPUTES": ""})
    await exporter._on_committee(_event("workflow.completed", run_id, {
        "workflow_id": "committee", "session_id": None,
        "variables": variables, "results": _COMMITTEE_RESULTS,
    }))

    text = _read_note("Committee/2026-07-18 委员会裁决.md")
    assert "委员会裁决汇编" in text and "多数看多" in text    # step-summary fallback
    assert "（本周无已完结的白板研讨记录）" in text           # empty snapshot placeholder


async def test_committee_started_opens_record_without_note():
    from app.institute import workflows

    await workflows.reconcile_from_disk()
    run_id = "wfrun-cmt-03"
    await _mk_committee_run(run_id, status="running")

    # real emit shape: app/institute/workflows.py _create_run -> workflow.started
    await exporter._on_committee(_event("workflow.started", run_id, {
        "workflow_id": "committee", "session_id": None, "variables": {"WORK_DATE": "2026-07-18"},
    }))

    rec = await db.query_one(
        "SELECT * FROM multi_agent_runs WHERE workflow_run_id = ?", (run_id,)
    )
    assert rec is not None and rec["status"] == "running"
    group = await db.query_one("SELECT * FROM multi_agent_groups WHERE id = 'committee'")
    assert group is not None                                 # panel upserted alongside
    assert list(_vault().rglob("*.md")) == []                # started never writes a note


async def test_committee_failed_settles_record_without_note():
    from app.institute import workflows

    await workflows.reconcile_from_disk()
    run_id = "wfrun-cmt-04"
    await _mk_committee_run(run_id, status="failed", error="step 02-round1 failed: boom")

    await exporter._on_committee(_event("workflow.failed", run_id, {
        "workflow_id": "committee", "session_id": None, "variables": {}, "results": [],
    }))

    rec = await db.query_one(
        "SELECT * FROM multi_agent_runs WHERE workflow_run_id = ?", (run_id,)
    )
    assert rec is not None and rec["status"] == "failed"
    assert rec["error"] == "step 02-round1 failed: boom"
    assert list(_vault().rglob("*.md")) == []                # failed runs export nothing


async def test_committee_handler_ignores_other_workflows():
    await exporter._on_committee(_event("workflow.completed", "wfrun-brief", {
        "workflow_id": "briefing", "session_id": None, "variables": {}, "results": [],
    }))
    assert list(_vault().rglob("*.md")) == []
    assert await db.query("SELECT * FROM multi_agent_runs") == []


# ---- paper_book.marked ----------------------------------------------------------------

async def test_paper_book_marked_exports_journal():
    wd = work_date()
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO nav_history (work_date, nav, gross_exposure, n_open, n_unpriced, realized_pnl_cum, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (wd, 1.042, 2.0, 2, 0, 0.042, now, now),
    )
    # real emit shape: app/institute/paper_book.py paper_book.marked
    await exporter._on_paper_book(_event("paper_book.marked", wd, {
        "work_date": wd, "nav": 1.042, "n_open": 2, "closed": [],
    }))

    rel = f"Book/journal/{wd}.md"
    text = _read_note(rel)
    assert "managed: institute" in text
    assert f"纸面交易日志 · {wd}" in text
    assert "1.042" in text
    assert (await _ledger(rel))["artifact_id"] == wd


# ---- ## Entities footer coverage across every artifact kind --------------------
#
# ROADMAP Phase 4 "Vault projection": every exporter note whose body can
# mention a known chain entity carries the `## Entities` wikilink footer.
# The research/briefing/daily/whiteboard/analyst-daily/memory/committee
# handlers gained it earlier; these tests pin the remaining kinds —
# factcheck digest, paper-book journal, research tree, bilingual twin —
# and prove the footer never stacks on re-export (handlers rebuild the
# body from rows/workspaces, so the writer's skip-if-unchanged holds).

async def _seed_entity(name: str, aliases: list[str] | None = None) -> None:
    from app.institute import chain

    await chain.create_node(name, "company", aliases=aliases or [])


async def test_factcheck_digest_gets_entity_footer_without_bloat():
    await _seed_entity("宁德时代", aliases=["CATL"])
    await db.execute(
        "INSERT INTO fact_cards (id, source_kind, source_ref, analyst_id, claim, category, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("fc-ef1", "research_report", "rq-gone", ANALYST,
         "宁德时代 2026 年出货量翻三倍", "numerical", "disputed", bus.now_iso()),
    )
    event = _event("factcheck.disputed", "fc-ef1", {
        "kind": "disputed", "claim": "宁德时代 2026 年出货量翻三倍", "category": "numerical",
        "analyst_id": ANALYST, "source_kind": "research_report", "source_ref": "rq-gone",
    })
    await exporter._on_factcheck_disputed(event)

    text = _read_note("Inbox/Disputed Claims.md")
    assert "## Entities" in text and "[[宁德时代]]" in text

    await exporter._on_factcheck_disputed(event)                 # idempotent re-export
    assert _read_note("Inbox/Disputed Claims.md").count("## Entities") == 1


async def test_paper_book_journal_gets_entity_footer_without_bloat():
    # the journal mentions entities via position tickers -> alias-match the node
    await _seed_entity("宁德时代", aliases=["300750.SZ"])
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO theses (id, kind, slug, name_zh, status, current_view, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("t-ef", "thesis", "t-ef", "论点", "active", "unknown", now, now),
    )
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_zh, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("300750.SZ", "300750", "CN_A", "宁德时代", now, now),
    )
    await db.execute(
        "INSERT INTO forecasts (id, thesis_id, security_id, claim, direction, horizon_days, "
        "settlement_rule, made_at, expires_at, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("fcast-ef", "t-ef", "300750.SZ", "看多", "long", 30, "{}", now, now, "open", now, now),
    )
    await db.execute(
        "INSERT INTO paper_positions (id, forecast_id, security_id, direction, entry_date, "
        "entry_price, size, stop_pct, target_pct, status, opened_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pos-ef", "fcast-ef", "300750.SZ", "long", work_date(), 100.0, 1.0, 0.1, 0.2,
         "open", now, now),                                      # opened_at inside today's SGT window
    )
    wd = work_date()
    event = _event("paper_book.marked", wd, {"work_date": wd, "nav": None, "n_open": 1, "closed": []})
    await exporter._on_paper_book(event)

    rel = f"Book/journal/{wd}.md"
    text = _read_note(rel)
    assert "300750.SZ" in text                                   # ticker in the journal body
    assert "## Entities" in text and "[[宁德时代]]" in text

    await exporter._on_paper_book(event)                         # idempotent re-export
    assert _read_note(rel).count("## Entities") == 1


async def test_research_tree_note_gets_entity_footer_without_bloat():
    await _seed_entity("台积电")
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO research_trees (id, root_topic, status, max_depth, max_nodes, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("tree-ef1", "先进封装", "completed", 2, 12, now, now),
    )
    await db.execute(
        "INSERT INTO research_tree_nodes (id, tree_id, parent_id, depth, topic, question, status, summary, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("tnode-ef1", "tree-ef1", None, 0, "先进封装", "", "completed",
         "台积电 CoWoS 扩产是主线。", now),
    )
    event = _event("tree.completed", "tree-ef1", {"status": "completed", "nodes": {"completed": 1}})
    await exporter._on_research_tree_completed(event)

    text = _read_note("Research/先进封装/tree.md")
    assert "## Entities" in text and "[[台积电]]" in text

    await exporter._on_research_tree_completed(event)            # idempotent re-export
    assert _read_note("Research/先进封装/tree.md").count("## Entities") == 1


async def test_bilingual_twin_gets_entity_footer_without_bloat():
    # aliases match inside English twin text (CATL), the link targets the node
    await _seed_entity("宁德时代", aliases=["CATL"])
    task_id = "task-twin-ef1"
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, output, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, "echo", "translate", "completed", "bilingual",
         "# Morning Briefing\n\nCATL keeps raising its shipment guidance.", bus.now_iso()),
    )
    wd = work_date()
    # real emit shape: app/institute/bilingual.py bilingual.twin_ready (by reference)
    event = _event("bilingual.twin_ready", "run-tw1", {
        "run_id": "run-tw1", "workflow_id": "briefing", "locale": "en",
        "work_date": wd, "task_id": task_id, "summary": "CATL guidance", "text_bytes": 60,
    })
    await exporter._on_twin_ready(event)

    rel = f"Briefing/{wd} 晨会简报_en.md"
    text = _read_note(rel)
    assert "## Entities" in text and "[[宁德时代]]" in text

    await exporter._on_twin_ready(event)                         # idempotent re-export
    assert _read_note(rel).count("## Entities") == 1


# ---- the degrade face: no handler ever raises, nothing is written -------------------

async def test_every_handler_swallows_empty_and_dangling_payloads():
    handlers = (
        (exporter._on_research, "research.completed"),
        (exporter._on_workflow, "workflow.completed"),
        (exporter._on_board, "whiteboard.board_completed"),
        (exporter._on_analyst_daily, "analyst_daily.completed"),
        (exporter._on_memory, "memory.compacted"),
        (exporter._on_factcheck_disputed, "factcheck.disputed"),
        (exporter._on_paper_book, "paper_book.marked"),
        (exporter._on_research_tree_completed, "tree.completed"),
        (exporter._on_twin_ready, "bilingual.twin_ready"),
        (exporter._on_committee, "workflow.completed"),
    )
    for handler, type_ in handlers:
        await handler(_event(type_, "", {}))                       # empty payload
        await handler(_event(type_, "zz-missing", {"topic": ""}))  # dangling refs
    assert list(_vault().rglob("*.md")) == []
    assert await db.query("SELECT * FROM vault_index") == []
    assert await db.query("SELECT * FROM multi_agent_runs") == []  # no record invented


async def test_handlers_noop_when_vault_disabled(monkeypatch):
    """`get_writer().enabled is False` (no vault_dir) short-circuits every handler."""
    from app.vault import writer as writer_mod

    class _Disabled:
        enabled = False
        root = None

    monkeypatch.setattr(writer_mod, "_writer", _Disabled())
    await exporter._on_research(_event("research.completed", "rq-1", {
        "topic": "任意", "run_id": None, "session_id": None, "summary": "x",
    }))
    assert await db.query("SELECT * FROM vault_index") == []


# ---- register() wiring ------------------------------------------------------------------

def test_register_wires_exactly_the_ten_event_prefixes():
    saved = list(bus._handlers)
    try:
        exporter.register()
        new = [(prefix, fn.__name__) for prefix, fn in bus._handlers[len(saved):]]
        assert new == [
            ("research.completed", "_on_research"),
            ("workflow.completed", "_on_workflow"),
            ("whiteboard.board_completed", "_on_board"),
            ("analyst_daily.completed", "_on_analyst_daily"),
            ("memory.compacted", "_on_memory"),
            ("factcheck.disputed", "_on_factcheck_disputed"),
            ("paper_book.marked", "_on_paper_book"),
            ("tree.completed", "_on_research_tree_completed"),
            ("bilingual.twin_ready", "_on_twin_ready"),
            ("workflow.", "_on_committee"),
        ]
    finally:
        bus._handlers[:] = saved  # never leak registrations into later tests
