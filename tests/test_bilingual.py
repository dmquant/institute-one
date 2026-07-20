"""Bilingual twins (ROADMAP Phase 7, REVIEW-D5 revision).

Covers the whole opt-in chain on the echo hand: the byte-stable
TRANSLATE_PROMPT through executor.submit (asserted VERBATIM against the task
row — REVIEW-D5 L3), twin_for_workflow's source order (compiled report file,
then step summaries) and the BY-REFERENCE bilingual.twin_ready event (payload
carries task_id/summary/text_bytes, the full text lives once in tasks.output
— REVIEW-D5 M2), and the workflow.completed handler's three gates — workflow
filter, the DEFAULT-OFF 'bilingual:enabled' switch, and the FAIL-CLOSED
maintenance read (corrupt/unreadable state counts as paused and spawns
nothing — REVIEW-D5 H2). Handlers are invoked directly with synthetic events
so no registration leaks into the process-wide bus (test_forecast_extract
idiom); spawned twins are drained in-test because the _bg_tasks registry
joins the shutdown drain only via the PATCH-NOTES-D5 integration patch.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app import bus, db
from app.config import get_settings
from app.institute import bilingual, scheduler
from app.institute.prompts import work_date


@pytest.fixture(autouse=True)
async def clean_bilingual_tasks():
    """Cancel stray twin tasks before conftest closes the DB."""
    bilingual._bg_tasks.clear()
    yield
    for t in list(bilingual._bg_tasks):
        t.cancel()
    if bilingual._bg_tasks:
        await asyncio.gather(*list(bilingual._bg_tasks), return_exceptions=True)
    bilingual._bg_tasks.clear()


async def _drain() -> None:
    while bilingual._bg_tasks:
        await asyncio.gather(*list(bilingual._bg_tasks), return_exceptions=True)


async def _mk_run(
    workflow_id: str = "briefing", *,
    file_text: str | None = "# 晨会简报\n\n今日核心：测试正文。",
    summaries: list[str] | None = None,
    status: str = "completed",
) -> str:
    """A workflow_runs row shaped like the engine's, with an optional compiled
    report file in its session workspace."""
    run_id = uuid.uuid4().hex[:12]
    session_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    ws = get_settings().workspaces_dir / "twin-tests" / run_id
    ws.mkdir(parents=True, exist_ok=True)
    await db.execute(
        "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, f"run {run_id}", "workflow", str(ws), now, now),
    )
    if file_text is not None:
        fname = bilingual._SOURCE_FILES.get(workflow_id, "报告.md")
        (ws / fname).write_text(file_text, encoding="utf-8")
    results = [
        {"step_id": f"0{i+1}", "title": f"步骤{i+1}", "status": "completed", "summary": s}
        for i, s in enumerate(summaries or [])
    ]
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, session_id, status, variables, results, source, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, workflow_id, session_id, status,
         json.dumps({"WORK_DATE": work_date()}, ensure_ascii=False),
         json.dumps(results, ensure_ascii=False), "test", now, now),
    )
    return run_id


def _completed_event(run_id: str, workflow_id: str) -> bus.Event:
    """Synthetic workflow.completed exactly as workflows._finish_run shapes it
    (run_id rides ref_id; the payload carries workflow_id)."""
    return bus.Event(
        id=0, type="workflow.completed", ref_kind="workflow_run", ref_id=run_id,
        payload={"workflow_id": workflow_id, "session_id": None, "variables": {}, "results": []},
    )


async def _twin_events() -> list[dict]:
    rows = await db.query("SELECT * FROM events WHERE type = 'bilingual.twin_ready' ORDER BY id")
    for r in rows:
        r["payload"] = json.loads(r["payload"] or "{}")
    return rows


async def _bilingual_tasks() -> list[dict]:
    return await db.query("SELECT * FROM tasks WHERE source = 'bilingual'")


async def _set_maintenance_raw(value: str) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES ('maintenance', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (value,),
    )


# ---- switch ---------------------------------------------------------------------

async def test_switch_default_off_and_roundtrip():
    assert await bilingual.is_enabled() is False          # fresh institute: OFF
    await bilingual.set_enabled(True)
    assert await bilingual.is_enabled() is True
    await bilingual.set_enabled(False)
    assert await bilingual.is_enabled() is False
    # corrupt row degrades to OFF, never to burning quota
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, 'not-json') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (bilingual.ENABLED_KEY,),
    )
    assert await bilingual.is_enabled() is False


# ---- translate_note ----------------------------------------------------------------

async def test_translate_note_prompt_is_byte_stable():
    out = await bilingual.translate_note("固态电池量产提速。")
    assert out.startswith("[echo] ")
    assert "固态电池量产提速。" in out                 # the source text rode the prompt
    tasks = await _bilingual_tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "completed"
    # verbatim constant (REVIEW-D5 L3): the task prompt is EXACTLY the
    # formatted template — any punctuation/whitespace drift fails here
    assert tasks[0]["prompt"] == bilingual.TRANSLATE_PROMPT.format(text="固态电池量产提速。")


async def test_translate_note_empty_raises():
    with pytest.raises(ValueError):
        await bilingual.translate_note("   ")


# ---- twin_for_workflow ---------------------------------------------------------------

async def test_twin_from_compiled_report_file_by_reference():
    run_id = await _mk_run("briefing", file_text="# 晨会简报\n\n宏观：利率企稳。")
    payload = await bilingual.twin_for_workflow(run_id)
    assert payload["run_id"] == run_id
    assert payload["workflow_id"] == "briefing"
    assert payload["locale"] == "en"
    assert payload["work_date"] == work_date()

    # by-reference payload (REVIEW-D5 M2): no full text inline — the durable
    # home is the tasks row; summary/text_bytes describe it
    assert "text" not in payload
    task = await db.query_one("SELECT output FROM tasks WHERE id = ?", (payload["task_id"],))
    assert "宏观：利率企稳。" in task["output"]        # echo twin carries the source
    assert payload["summary"] == task["output"][:bilingual.TWIN_SUMMARY_CAP]
    assert payload["text_bytes"] == len(task["output"].encode("utf-8"))

    events = await _twin_events()
    assert len(events) == 1
    assert events[0]["ref_id"] == run_id
    assert events[0]["payload"]["task_id"] == payload["task_id"]
    assert "text" not in events[0]["payload"]


async def test_twin_falls_back_to_step_summaries():
    run_id = await _mk_run("daily", file_text=None, summaries=["A股放量。", "港股缩量。"])
    payload = await bilingual.twin_for_workflow(run_id)
    task = await db.query_one("SELECT output FROM tasks WHERE id = ?", (payload["task_id"],))
    assert "A股放量。" in task["output"] and "港股缩量。" in task["output"]


async def test_twin_nothing_to_translate_returns_none():
    run_id = await _mk_run("daily", file_text=None, summaries=[])
    assert await bilingual.twin_for_workflow(run_id) is None
    assert await _twin_events() == []
    assert await _bilingual_tasks() == []


async def test_twin_rejects_unknown_run_and_other_workflows():
    with pytest.raises(ValueError, match="unknown workflow run"):
        await bilingual.twin_for_workflow("ghost")
    run_id = await _mk_run("research")
    with pytest.raises(ValueError, match="not one of"):
        await bilingual.twin_for_workflow(run_id)


# ---- the workflow.completed handler ------------------------------------------------

async def test_handler_default_off_spends_nothing():
    run_id = await _mk_run("briefing")
    await bilingual._on_workflow_completed(_completed_event(run_id, "briefing"))
    await _drain()
    assert await _bilingual_tasks() == []          # zero model calls by default
    assert await _twin_events() == []


async def test_handler_enabled_produces_twin():
    await bilingual.set_enabled(True)
    run_id = await _mk_run("daily", file_text="# 每日日报\n\n结论：维持中性。")
    await bilingual._on_workflow_completed(_completed_event(run_id, "daily"))
    await _drain()
    events = await _twin_events()
    assert len(events) == 1 and events[0]["ref_id"] == run_id
    assert "维持中性" in events[0]["payload"]["summary"]
    assert len(await _bilingual_tasks()) == 1


async def test_handler_skips_under_maintenance():
    await bilingual.set_enabled(True)
    await scheduler.set_maintenance(True)
    try:
        run_id = await _mk_run("briefing")
        await bilingual._on_workflow_completed(_completed_event(run_id, "briefing"))
        await _drain()
        assert await _bilingual_tasks() == []      # gated: a twin is a NEW model call
        assert await _twin_events() == []
    finally:
        await scheduler.set_maintenance(False)


async def test_handler_runs_after_clean_resume():
    """{"paused": false} written by set_maintenance(False) is well-formed —
    the conservative read must not mistake a normal resume for corruption."""
    await bilingual.set_enabled(True)
    await scheduler.set_maintenance(True)
    await scheduler.set_maintenance(False)
    run_id = await _mk_run("daily", file_text="# 每日日报\n\n恢复后正文。")
    await bilingual._on_workflow_completed(_completed_event(run_id, "daily"))
    await _drain()
    assert len(await _twin_events()) == 1


@pytest.mark.parametrize("bad", ["not-json", "[]", "true", '{"paused": "yes"}', "{}"])
async def test_handler_fail_closed_on_corrupt_maintenance(bad):
    """REVIEW-D5 H2: corrupt/malformed maintenance state must be treated as
    PAUSED for the twin gate — quota-burning fail-open is the bug. (scheduler.
    get_maintenance() keeps its own fail-open posture for no-quota jobs.)"""
    await bilingual.set_enabled(True)
    await _set_maintenance_raw(bad)
    run_id = await _mk_run("briefing")
    await bilingual._on_workflow_completed(_completed_event(run_id, "briefing"))
    await _drain()
    assert await _bilingual_tasks() == []
    assert await _twin_events() == []


async def test_handler_fail_closed_when_maintenance_read_fails(monkeypatch):
    """The read itself exploding is also fail-closed: no spawn, no raise."""
    await bilingual.set_enabled(True)
    run_id = await _mk_run("briefing")
    orig = db.query_one

    async def failing(sql, params=()):
        if "key = 'maintenance'" in sql:
            raise RuntimeError("db exploded")
        return await orig(sql, params)

    monkeypatch.setattr(db, "query_one", failing)
    await bilingual._on_workflow_completed(_completed_event(run_id, "briefing"))
    await _drain()
    assert await _bilingual_tasks() == []
    assert await _twin_events() == []


async def test_maintenance_conservative_read_unit():
    assert await bilingual._maintenance_paused() is False        # no row = not paused
    await scheduler.set_maintenance(True)
    assert await bilingual._maintenance_paused() is True
    await scheduler.set_maintenance(False)
    assert await bilingual._maintenance_paused() is False
    await _set_maintenance_raw("[]")
    assert await bilingual._maintenance_paused() is True         # fail-closed


async def test_handler_ignores_other_workflows():
    await bilingual.set_enabled(True)
    run_id = await _mk_run("research")
    await bilingual._on_workflow_completed(_completed_event(run_id, "research"))
    await _drain()
    assert await _bilingual_tasks() == []


async def test_handler_never_raises():
    await bilingual.set_enabled(True)
    # payload without workflow_id -> filtered, no raise
    await bilingual._on_workflow_completed(
        bus.Event(id=0, type="workflow.completed", ref_kind="workflow_run", ref_id="x", payload={})
    )
    # unknown run: the spawn happens, _twin_safe swallows the ValueError
    await bilingual._on_workflow_completed(_completed_event("ghost-run", "briefing"))
    await _drain()
    assert await _twin_events() == []


async def test_register_subscribes_and_emit_path_stays_quiet_when_off(monkeypatch):
    """register() + a real emit round-trip: with the switch OFF the twin chain
    is inert end to end. The handler list is restored afterwards so nothing
    leaks into the process-wide bus (conftest does not reset it)."""
    monkeypatch.setattr(bus, "_handlers", list(bus._handlers))
    bilingual.register()
    run_id = await _mk_run("briefing")
    await bus.emit("workflow.completed", "workflow_run", run_id, {"workflow_id": "briefing"})
    await _drain()
    assert await _bilingual_tasks() == []
    assert await _twin_events() == []
