"""Committee workflow (Phase 7): definition integrity + ${WEEK_DISPUTES}.

Covers: workflows/committee.json reconciles from disk with every step's
analyst_id known to the catalog roster; the ${WEEK_DISPUTES} lazy variable
(last-7-days completed whiteboard boards → topic + closing summary, ≤3KB)
including ordering, the 50-board cap, error/empty degradation and the
explicit-value override; the once-per-ISO-week idempotent kickoff
(run_committee_once, REVIEW-C5 M2); json_set variable writes preserving
concurrently-landed keys (REVIEW-C5 P2).

M8-012 additions: the durable committee bridge into the multi-agent
group/run tables — the system-maintained 'committee' group, the
open-at-kickoff record, and finalize (input snapshot = the frozen
${WEEK_DISPUTES} digest, step task ids, structured step-map verdict,
terminal status mapping, manual-run upsert).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import bus, db
from app.institute import multi_agent, sessions, workflows
from app.institute.analysts import get_analyst

REPO = Path(__file__).resolve().parent.parent
COMMITTEE_JSON = REPO / "workflows" / "committee.json"


# ---- definition integrity ----------------------------------------------------

def test_committee_json_analysts_all_known():
    """Every step's analyst_id must exist in catalog/analysts.json (a typo
    would silently fall back to chief-strategist at run time)."""
    data = json.loads(COMMITTEE_JSON.read_text(encoding="utf-8"))
    assert data["id"] == "committee"
    assert len(data["steps"]) == 5
    for step in data["steps"]:
        aid = step["analyst_id"]
        assert get_analyst(aid) is not None, f"step {step['id']}: unknown analyst {aid!r}"


def test_committee_json_declares_week_disputes():
    data = json.loads(COMMITTEE_JSON.read_text(encoding="utf-8"))
    assert set(data["variables"]) == {"WORK_DATE", "WEEK_DISPUTES"}
    # the variable is actually referenced by the agenda step
    assert any("${WEEK_DISPUTES}" in s["prompt"] for s in data["steps"])
    # every step ships an output file and a timeout (engine contract)
    for step in data["steps"]:
        assert step["output_file"] and step["timeout_s"]


def test_committee_verdict_prompt_requires_stable_structured_verdict():
    data = json.loads(COMMITTEE_JSON.read_text(encoding="utf-8"))
    debate_steps = [
        step for step in data["steps"] if step["id"] in {"02-round1", "03-round2", "04-round3"}
    ]
    assert len(debate_steps) == 3
    for step in debate_steps:
        assert multi_agent.MAJORITY_BALLOT_PROTOCOL_MARKER in step["prompt"]
        assert "VERDICT: <正方|反方>" in step["prompt"]
        assert "最后一行必须且只能" in step["prompt"]

    verdict = next(step for step in data["steps"] if step["id"] == "05-verdict")
    prompt = verdict["prompt"]
    assert multi_agent.MAJORITY_BALLOT_PROTOCOL_MARKER in prompt
    assert "VERDICT: <正方|反方|未达成裁决>" in prompt
    assert "最后一行必须且只能" in prompt
    assert "不得从自由文本推断或代为补票" in prompt
    assert all(label in prompt for label in ("正方", "反方", "未达成裁决"))


async def test_committee_reconciles_from_disk():
    n = await workflows.reconcile_from_disk()
    assert n >= 1
    wf = await workflows.get_workflow("committee")
    assert wf is not None
    assert [s["id"] for s in wf["steps"]] == [
        "01-agenda", "02-round1", "03-round2", "04-round3", "05-verdict",
    ]
    # prompts must survive reconcile byte-for-byte (prompts are the product)
    disk = json.loads(COMMITTEE_JSON.read_text(encoding="utf-8"))
    assert [s["prompt"] for s in wf["steps"]] == [s["prompt"] for s in disk["steps"]]


# ---- ${WEEK_DISPUTES} rendering ----------------------------------------------

async def _mk_board(
    topic: str,
    *,
    status: str = "completed",
    updated_at: str | None = None,
    summaries: list[tuple[int, str, str]] | None = None,  # (idx, status, summary)
) -> str:
    board_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, session_id, "
        "work_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (board_id, topic, "", status, 5, None, "2026-07-15", now, updated_at or now),
    )
    for idx, card_status, summary in summaries or []:
        await db.execute(
            "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, question, "
            "summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], board_id, idx, "macro-analyst", card_status, "", summary, now),
        )
    return board_id


async def test_week_disputes_renders_recent_completed_boards():
    await _mk_board("A股流动性拐点", summaries=[
        (1, "completed", "第一张卡"), (3, "completed", "收尾：分歧在于外资回流节奏"),
    ])
    await _mk_board("美债利率上限", summaries=[(1, "completed", "多空各执一词")])
    out = await workflows.week_disputes_variable()
    assert "A股流动性拐点" in out and "收尾：分歧在于外资回流节奏" in out
    assert "第一张卡" not in out                     # only the highest-idx completed card
    assert "美债利率上限" in out and "多空各执一词" in out


async def test_week_disputes_filters_status_and_window():
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    await _mk_board("八天前的旧板", updated_at=old, summaries=[(1, "completed", "过期")])
    await _mk_board("还在进行的板", status="active", summaries=[(1, "completed", "未完结")])
    await _mk_board("被叫停的板", status="stopped", summaries=[(1, "completed", "停了")])
    await _mk_board("失败卡的板", summaries=[(1, "failed", "失败卡摘要")])
    out = await workflows.week_disputes_variable()
    assert "八天前的旧板" not in out and "还在进行的板" not in out and "被叫停的板" not in out
    # the board itself is completed and inside the window, but its only card
    # failed -> listed with the explicit no-summary marker
    assert "失败卡的板" in out and "（无收尾摘要）" in out and "失败卡摘要" not in out


async def test_week_disputes_empty_renders_empty_string():
    assert await workflows.week_disputes_variable() == ""


async def test_week_disputes_newest_first():
    older = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    await _mk_board("旧一点的板", updated_at=older, summaries=[(1, "completed", "旧")])
    await _mk_board("最新的板", summaries=[(1, "completed", "新")])
    out = await workflows.week_disputes_variable()
    assert out.index("最新的板") < out.index("旧一点的板")


async def test_week_disputes_board_cap():
    for i in range(workflows.WEEK_DISPUTES_MAX_BOARDS + 5):
        await _mk_board(f"t{i}")                       # short topics: byte cap won't bite
    out = await workflows.week_disputes_variable()
    assert len(out.split("\n")) == workflows.WEEK_DISPUTES_MAX_BOARDS


async def test_week_disputes_caps_at_3kb():
    for i in range(30):
        await _mk_board(f"话题{i}", summaries=[(1, "completed", "长摘要" * 200)])
    out = await workflows.week_disputes_variable()
    assert 0 < len(out.encode("utf-8")) <= workflows.WEEK_DISPUTES_MAX_BYTES


async def test_week_disputes_query_error_degrades_to_empty(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(workflows.db, "query", boom)
    assert await workflows.week_disputes_variable() == ""


# ---- injection through the engine (echo hand) ----------------------------------

async def _mk_disputes_workflow(wf_id: str = "wf-disputes") -> None:
    steps = [{
        "id": "s1", "title": "辩题遴选", "prompt":
            "近一周分歧：\n${WEEK_DISPUTES}\n\nWRITE_FILE: out.md",
        "output_file": "out.md",
    }]
    await db.execute(
        "INSERT INTO workflows (id, name, description, variables, steps, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (wf_id, "disputes wf", "", json.dumps(["WEEK_DISPUTES"]),
         json.dumps(steps, ensure_ascii=False), bus.now_iso()),
    )


async def _run_and_read(wf_id: str, variables: dict | None = None) -> tuple[dict, str]:
    run = await workflows.run_workflow_and_wait(wf_id, variables=variables, source="test")
    assert run["status"] == "completed"
    session = await sessions.get_session(run["session_id"])
    out = (sessions.workspace_path(session) / "out.md").read_text(encoding="utf-8")
    return run, out


async def test_week_disputes_injection_end_to_end():
    await _mk_board("茅台提价是否可持续", summaries=[(1, "completed", "正反方 2:1")])
    await _mk_disputes_workflow()
    run, out = await _run_and_read("wf-disputes")
    assert "${WEEK_DISPUTES}" not in out            # substituted, no residue
    assert "茅台提价是否可持续" in out and "正反方 2:1" in out
    # the computed value is persisted on the run row (audit: what prompts saw)
    assert "茅台提价是否可持续" in run["variables"]["WEEK_DISPUTES"]


async def test_week_disputes_empty_data_degrades_silently():
    await _mk_disputes_workflow("wf-disputes-empty")
    run, out = await _run_and_read("wf-disputes-empty")
    assert "${WEEK_DISPUTES}" not in out
    assert "近一周分歧：\n\n" in out                 # empty string, prompt otherwise intact
    assert run["variables"]["WEEK_DISPUTES"] == ""


async def test_week_disputes_explicit_value_wins():
    await _mk_board("不该出现的板", summaries=[(1, "completed", "不该注入")])
    await _mk_disputes_workflow("wf-disputes-explicit")
    _, out = await _run_and_read(
        "wf-disputes-explicit", {"WEEK_DISPUTES": "OPERATOR-SUPPLIED"}
    )
    assert "OPERATOR-SUPPLIED" in out and "不该出现的板" not in out


async def test_workflows_without_the_variable_never_compute_it():
    """briefing has no ${WEEK_DISPUTES}: the variable never appears on the run."""
    await _mk_board("无关的板", summaries=[(1, "completed", "无关")])
    await workflows.reconcile_from_disk()
    run = await workflows.run_workflow_and_wait("briefing", source="test")
    assert run["status"] == "completed"
    assert "WEEK_DISPUTES" not in run["variables"]


async def test_variable_write_preserves_concurrently_landed_keys(monkeypatch):
    """REVIEW-C5 P2: the lazy-variable persist is json_set on ONE key — a key
    landed by a concurrent writer during the compute must survive it."""
    await _mk_disputes_workflow("wf-race")

    async def racy_disputes():
        row = await db.query_one(
            "SELECT id FROM workflow_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        )
        await db.execute(   # a concurrent writer lands a key mid-compute
            "UPDATE workflow_runs SET variables = json_set(variables, '$.RACER', ?) WHERE id = ?",
            ("survives", row["id"]),
        )
        return "本周分歧清单"

    monkeypatch.setattr(workflows, "week_disputes_variable", racy_disputes)
    run, out = await _run_and_read("wf-race")
    assert "本周分歧清单" in out
    assert run["variables"]["WEEK_DISPUTES"] == "本周分歧清单"
    assert run["variables"]["RACER"] == "survives"      # not clobbered by the persist


# ---- once-per-week idempotent kickoff (REVIEW-C5 M2) ---------------------------

async def _drain_drivers() -> None:
    while workflows._driving:
        await asyncio.gather(*list(workflows._driving), return_exceptions=True)


async def test_run_committee_once_is_idempotent_per_week():
    await workflows.reconcile_from_disk()
    run_id = await workflows.run_committee_once()
    assert run_id is not None
    # replays (misfire coalesce, restart, manual run-now) collapse into the claim
    assert await workflows.run_committee_once() is None
    rows = await db.query("SELECT id FROM workflow_runs WHERE workflow_id = 'committee'")
    assert [r["id"] for r in rows] == [run_id]          # exactly one run this week
    claim = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?",
        (f"committee:{workflows.committee_week()}",),
    )
    assert json.loads(claim["value"])["run_id"] == run_id
    await _drain_drivers()


async def test_run_committee_once_concurrent_triggers_single_winner():
    await workflows.reconcile_from_disk()
    a, b = await asyncio.gather(workflows.run_committee_once(), workflows.run_committee_once())
    assert (a is None) != (b is None)                   # exactly one winner
    rows = await db.query("SELECT id FROM workflow_runs WHERE workflow_id = 'committee'")
    assert len(rows) == 1
    await _drain_drivers()


async def test_run_committee_once_reopens_after_failed_run():
    await workflows.reconcile_from_disk()
    first = await workflows.run_committee_once()
    await _drain_drivers()
    # the week's run failed -> the claim is stale, the week reopens once
    await db.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (first,))
    second = await workflows.run_committee_once()
    assert second is not None and second != first
    await _drain_drivers()
    # the retry completed -> the week is closed again
    assert await workflows.run_committee_once() is None


# ---- durable committee group + run records (M8-012) ----------------------------


async def test_ensure_committee_group_reconciles_from_workflow():
    # no reconciled workflow yet -> no group, no crash
    assert await multi_agent.ensure_committee_group() is None
    await workflows.reconcile_from_disk()
    group = await multi_agent.ensure_committee_group()
    assert group is not None and group["id"] == "committee"
    # members = the step analysts, first-appearance order
    assert group["agents"] == [
        "chief-strategist", "macro-analyst", "equity-analyst", "policy-analyst", "ops-editor",
    ]
    assert group["mode"] == "all"
    # re-ensure is an upsert, not a duplicate
    again = await multi_agent.ensure_committee_group()
    assert again["id"] == group["id"] and again["created_at"] == group["created_at"]
    assert len(await multi_agent.list_groups()) == 1


async def test_committee_run_lands_durable_record_with_input_snapshot():
    await _mk_board("人形机器人量产分歧", summaries=[(1, "completed", "正反 2:1")])
    await workflows.reconcile_from_disk()
    run_id = await workflows.run_committee_once()
    assert run_id is not None

    opened = await multi_agent.open_committee_run(run_id)
    assert opened is not None
    assert opened["workflow_run_id"] == run_id and opened["group_id"] == "committee"
    # replaying the open is idempotent (uq_multi_agent_runs_workflow arbiter)
    again = await multi_agent.open_committee_run(run_id)
    assert again["id"] == opened["id"]

    await _drain_drivers()
    settled = await multi_agent.finalize_committee_run(run_id)
    assert settled["status"] == "completed" and settled["finished_at"]
    # the input snapshot is the frozen ${WEEK_DISPUTES} whiteboard digest
    assert "人形机器人量产分歧" in settled["prompt"]
    assert len(settled["task_ids"]) == 5                    # one per debate step
    v = settled["verdict"]
    assert v["kind"] == "committee" and v["workflow_status"] == "completed"
    assert [s["step_id"] for s in v["steps"]] == [
        "01-agenda", "02-round1", "03-round2", "04-round3", "05-verdict",
    ]
    assert all(s["status"] == "completed" and s["task_id"] for s in v["steps"])
    assert v["summary"] == v["steps"][-1]["summary"]        # the 裁决 step's summary
    # finalize replays idempotently (the claim already landed)
    assert (await multi_agent.finalize_committee_run(run_id))["finished_at"] == settled["finished_at"]
    # reachable through the generic run-record read (reconnect surface)
    rec = await multi_agent.get_run_record(opened["id"])
    assert rec["status"] == "completed" and rec["workflow_run_id"] == run_id


async def test_finalize_committee_run_upserts_for_manual_runs():
    """The manual escape hatch (POST /api/workflows/committee/run) bypasses
    the kickoff hook — finalize must land the record on its own."""
    await workflows.reconcile_from_disk()
    run = await workflows.run_workflow_and_wait("committee", source="test")
    assert run["status"] == "completed"
    settled = await multi_agent.finalize_committee_run(run["id"])
    assert settled is not None and settled["status"] == "completed"
    assert settled["workflow_run_id"] == run["id"]
    assert len(settled["task_ids"]) == 5


async def test_finalize_committee_run_maps_terminal_states():
    await workflows.reconcile_from_disk()
    # unknown workflow run -> None, no record invented
    assert await multi_agent.finalize_committee_run("zz-missing") is None

    run_id = await workflows.run_committee_once()
    await _drain_drivers()
    await db.execute(
        "UPDATE workflow_runs SET status = 'failed', error = 'boom step 02' WHERE id = ?",
        (run_id,),
    )
    settled = await multi_agent.finalize_committee_run(run_id)
    assert settled["status"] == "failed" and settled["error"] == "boom step 02"
    assert settled["verdict"]["workflow_status"] == "failed"


async def test_finalize_committee_run_still_running_keeps_record_open():
    await workflows.reconcile_from_disk()
    run_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, status, variables, source, started_at) "
        "VALUES (?, 'committee', 'running', '{}', 'test', ?)",
        (run_id, bus.now_iso()),
    )
    rec = await multi_agent.finalize_committee_run(run_id)
    assert rec is not None and rec["status"] == "running"   # opened, not settled
    assert rec["verdict"] is None
    # settle-on-read delegates committee rows to finalize: still open, no wedge
    assert (await multi_agent.get_run_record(rec["id"]))["status"] == "running"
