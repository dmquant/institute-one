"""Workflow engine: reconcile, full run on echo, cancel between steps."""
from __future__ import annotations

import asyncio
import json
import logging

from app import bus, db
from app.config import get_settings
from app.hands.base import Hand, HandResult
from app.hands.registry import get_registry
from app.institute import workflows
from app.router import executor


async def test_reconcile_from_disk_loads_repo_workflows():
    n = await workflows.reconcile_from_disk()
    # count the definitions on disk rather than pinning a literal: new
    # workflow files (e.g. committee.json) must not break this test
    expected = len(list((workflows.get_settings().workflows_dir).glob("*.json")))
    assert n == expected >= 3
    ids = {w["id"] for w in await workflows.list_workflows()}
    assert {"briefing", "daily", "research"} <= ids

    briefing = await workflows.get_workflow("briefing")
    assert briefing["name"]
    assert len(briefing["steps"]) == 3
    assert isinstance(briefing["variables"], list)

    # reconcile is an upsert: running it again neither duplicates nor fails
    assert await workflows.reconcile_from_disk() == expected
    assert len(await workflows.list_workflows()) == len(ids)


# ---- analyst key normalization ---------------------------------------------

async def test_reconcile_normalizes_analyst_key_to_analyst_id():
    await workflows.reconcile_from_disk()
    for wf in await workflows.list_workflows():
        for step in wf["steps"]:
            assert "analyst" not in step, f"{wf['id']} step {step.get('id')} kept the legacy key"

    briefing = await workflows.get_workflow("briefing")
    assert [s["analyst_id"] for s in briefing["steps"]] == [
        "macro-analyst", "chief-strategist", "ops-editor",
    ]


def test_normalize_steps_warns_on_unknown_analyst(caplog):
    steps = [
        {"id": "s1", "analyst": "ghost-analyst", "prompt": "x"},
        {"id": "s2", "prompt": "y"},  # no analyst at all: documented fallback, no warning
    ]
    with caplog.at_level(logging.WARNING, logger="institute.workflows"):
        out = workflows._normalize_steps("wf-x", steps)

    assert out[0]["analyst_id"] == "ghost-analyst"  # normalized, value preserved
    assert "analyst" not in out[0]
    assert "analyst_id" not in out[1]

    warnings = [r.getMessage() for r in caplog.records if "unknown analyst" in r.getMessage()]
    assert len(warnings) == 1
    assert "ghost-analyst" in warnings[0]
    assert "wf-x" in warnings[0]


async def test_run_with_unknown_analyst_falls_back_and_warns(caplog):
    """Unknown id at run time: loud warning, chief-strategist fallback, never a raise."""
    await db.execute(
        "INSERT INTO workflows (id, name, description, variables, steps, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "wf-ghost", "ghost wf", "", "[]",
            json.dumps([{"id": "s1", "analyst_id": "ghost-analyst", "prompt": "hello"}]),
            bus.now_iso(),
        ),
    )
    with caplog.at_level(logging.WARNING, logger="institute.workflows"):
        run = await workflows.run_workflow_and_wait("wf-ghost", source="test")

    assert run["status"] == "completed"
    assert any(
        "unknown analyst" in r.getMessage() and "ghost-analyst" in r.getMessage()
        for r in caplog.records
    )


async def test_run_briefing_on_echo_completes_with_three_steps():
    await workflows.reconcile_from_disk()
    run = await workflows.run_workflow_and_wait("briefing", source="test")

    assert run["status"] == "completed"
    assert len(run["results"]) == 3
    assert all(r["status"] == "completed" for r in run["results"])
    assert all(r["task_id"] for r in run["results"])
    assert run["current_step"] == 3

    # one executor task per step, tied to the run
    tasks = await executor.list_tasks(parent_run_id=run["id"])
    assert len(tasks) == 3
    assert all(t["status"] == "completed" for t in tasks)

    events = await bus.replay(0, types=["workflow.completed"])
    mine = [e for e in events if e.ref_id == run["id"]]
    assert len(mine) == 1
    assert mine[0].payload["workflow_id"] == "briefing"
    assert len(mine[0].payload["results"]) == 3


class GatedHand(Hand):
    """First call returns immediately; later calls block until released."""

    name = "gated"
    hand_type = "cli"

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return HandResult(output="step one done", exit_code=0)
        await self.release.wait()
        return HandResult(output="late finish", exit_code=0)


class RecordingHand(Hand):
    hand_type = "cli"

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        self.calls += 1
        return HandResult(output=f"{self.name} done", exit_code=0)


async def test_research_workflow_uses_configured_research_hands(monkeypatch):
    await workflows.reconcile_from_disk()
    settings = get_settings()
    monkeypatch.setattr(settings, "research_hands", "codex,agy")
    codex = RecordingHand("codex")
    agy = RecordingHand("agy")
    get_registry().register(codex)
    get_registry().register(agy)

    run = await workflows.run_workflow_and_wait("research", variables={"TOPIC": "AI"}, source="test")

    assert run["status"] == "completed"
    tasks = await executor.list_tasks(parent_run_id=run["id"], limit=20)
    assert len(tasks) == 7
    assert {t["requested_hand"] for t in tasks} <= {"codex", "agy"}
    assert {t["hand"] for t in tasks} <= {"codex", "agy"}
    assert codex.calls > 0
    assert agy.calls > 0


# ---- research prompt wiring: ${DATA_BUNDLE} + Step-0 digest line ------------
# (Round-5 prompt-change card: PATCH-NOTES-B5 §3 wording, CLAUDE.md rule 4)

# must match workflows/research.json byte-for-byte
_STEP0_LINE = (
    "【研究前置】开始研究前，先执行 curl -s "
    "'http://127.0.0.1:8100/api/institute/recent-reports.md?days=7' "
    "了解研究所近 7 天已完成的工作，避免重复劳动；若命令失败则忽略，直接开始。"
)
_ANALYSIS_STEPS = ("01-company", "02-industry", "03-financials", "04-drivers-risks", "05-thesis")
_COMPILE_STEPS = ("06-report", "07-followups")


async def test_research_definition_carries_step0_and_data_bundle():
    await workflows.reconcile_from_disk()
    wf = await workflows.get_workflow("research")
    steps = {s["id"]: s for s in wf["steps"]}

    # Step-0 digest line: the five analysis steps only (06/07 compile from
    # workspace files and must not curl external context)
    for sid in _ANALYSIS_STEPS:
        assert _STEP0_LINE in steps[sid]["prompt"], f"{sid} missing Step-0 line"
    for sid in _COMPILE_STEPS:
        assert "recent-reports.md" not in steps[sid]["prompt"], f"{sid} must not carry Step-0"

    # ${DATA_BUNDLE}: bare variable on BOTH steps (the bundle renders its own
    # 【行情数据注入】 header; a titled block would leave an orphan header on
    # empty data and a double header otherwise — ROUND5-AUDIT-F5 NIT-F5-1)
    assert "${DATA_BUNDLE}" in steps["01-company"]["prompt"]
    assert "${DATA_BUNDLE}" in steps["03-financials"]["prompt"]
    assert "【本地行情数据】" not in steps["01-company"]["prompt"]
    assert "【本地行情数据】" not in steps["03-financials"]["prompt"]
    for sid in ("02-industry", "04-drivers-risks", "05-thesis", *_COMPILE_STEPS):
        assert "${DATA_BUNDLE}" not in steps[sid]["prompt"], f"{sid} must not reference the bundle"

    # 03: the web-search sentence now prefers the injected local data
    assert "优先使用上方已注入的本地行情数据；数据缺失的部分再联网搜索核实。" in steps["03-financials"]["prompt"]
    assert "请使用联网搜索（如当前 CLI 支持）核实。" not in steps["03-financials"]["prompt"]
    assert wf["variables"] == ["TOPIC", "WORK_DATE", "ANALYST_CATALOG", "DATA_BUNDLE"]


async def _seed_maotai_bars(n: int = 5) -> None:
    from datetime import date, timedelta

    from app.institute import market_data

    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_zh, name_en, currency, "
        "listing_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("600519.SH", "600519", "CN_A", "贵州茅台", None, "CNY", "active", now, now),
    )
    d0 = date.fromisoformat(workflows.work_date()) - timedelta(days=n)
    for i in range(n):
        px = 100.0 + i
        await market_data.upsert_bar({
            "security_id": "600519.SH", "bar_date": (d0 + timedelta(days=i)).isoformat(),
            "open": px, "high": px + 1, "low": px - 1, "close": px + 0.5,
            "volume": 1000 + i, "source": "sina",
        })


async def test_research_run_renders_bundle_and_step0_end_to_end():
    """Echo run of the production research workflow: the computed bundle and
    the Step-0 line land in the exact prompts the hands received."""
    await _seed_maotai_bars()
    await workflows.reconcile_from_disk()

    run = await workflows.run_workflow_and_wait(
        "research",
        variables={"TOPIC": "贵州茅台", "ANALYST_CATALOG": "- equity-analyst"},
        source="test",
    )
    assert run["status"] == "completed"
    assert len(run["results"]) == 7
    # the lazily computed bundle is persisted on the run row (auditable)
    assert "600519.SH" in run["variables"]["DATA_BUNDLE"]

    # assert on the ## 任务 section (the rendered step prompt): under the echo
    # hand the previous-steps context block echoes earlier prompts back, so
    # the full prompt of ANY later step may contain earlier steps' text.
    # rsplit: the step's own 任务 marker is the LAST one (echoed earlier
    # markers all live in the context block above it)
    tasks_section = {}
    for r in run["results"]:
        row = await db.query_one("SELECT prompt FROM tasks WHERE id = ?", (r["task_id"],))
        assert "${DATA_BUNDLE}" not in row["prompt"], f"{r['step_id']} left an unsubstituted variable"
        tasks_section[r["step_id"]] = row["prompt"].rsplit("\n## 任务\n", 1)[1]

    for sid in _ANALYSIS_STEPS:
        assert _STEP0_LINE in tasks_section[sid], f"{sid} rendered prompt missing Step-0 line"
    for sid in _COMPILE_STEPS:
        assert "recent-reports.md" not in tasks_section[sid]

    # the bundle body reached exactly the two steps that reference it; the
    # header comes from the bundle itself (bare-variable form, NIT-F5-1)
    for sid in ("01-company", "03-financials"):
        assert "600519.SH" in tasks_section[sid] and "最新日线" in tasks_section[sid]
    for sid in ("02-industry", "04-drivers-risks", "05-thesis", *_COMPILE_STEPS):
        assert "最新日线" not in tasks_section[sid], f"{sid} must not receive the bundle"
    assert "【行情数据注入】" in tasks_section["03-financials"]
    assert "【本地行情数据】" not in tasks_section["03-financials"]


async def test_cancel_run_stops_between_steps(monkeypatch):
    await workflows.reconcile_from_disk()
    gated = GatedHand()
    get_registry().register(gated)
    monkeypatch.setattr(get_settings(), "default_hand", "gated")

    run_id = await workflows.run_workflow("briefing", source="test")

    # wait until step 2 is in flight (blocked inside the gated hand)
    for _ in range(200):
        if gated.calls >= 2:
            break
        await asyncio.sleep(0.02)
    assert gated.calls == 2

    assert await workflows.cancel_run(run_id) is True
    gated.release.set()  # even if released now, the run must stay cancelled

    for _ in range(200):
        run = await workflows.get_run(run_id)
        if run["status"] != "running" and not workflows._driving:
            break
        await asyncio.sleep(0.02)

    run = await workflows.get_run(run_id)
    assert run["status"] == "cancelled"
    assert len(run["results"]) == 1  # only step 1 ever recorded
    assert gated.calls == 2          # step 3 never started

    events = await bus.replay(0, types=["workflow.cancelled"])
    assert any(e.ref_id == run_id for e in events)
