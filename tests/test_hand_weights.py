"""Hand weights + scorecard (ROADMAP Phase 2, migrations/0009).

Covers: weighted-pick statistics (seeded RNG, proportion assertions),
all-zero/missing-row/never-loaded degradation, non-finite weight defenses,
the weights API round-trip (+ registry cache refresh), the scorecard
heuristics on constructed samples (incl. the REVIEW-B2 M1 citation
counter-examples), the previous-day settlement default (REVIEW-B2 M2),
run_once rerun-safety, and hand_stats hourly aggregation.

pick_weighted_hand is deliberately NOT wired into resolve()/resolve_chain()
(wiring call sites is a follow-up card) — resolution semantics are asserted
unchanged at the end of this file.

API tests go through create_app() like tests/test_tasks_retry.py (the hands
router is already mounted in main.py; no lifespan — conftest owns DB/registry).
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta

import httpx
import pytest

from app import bus, db
from app.hands.registry import get_registry
from app.institute import scorecard
from app.institute.prompts import work_date
from app.main import create_app
from app.router import executor


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _proportions(picks: list[str]) -> dict[str, float]:
    return {name: picks.count(name) / len(picks) for name in set(picks)}


# ==== weighted pick: statistical properties ==================================

async def test_pick_weighted_follows_weights():
    reg = get_registry()
    reg.set_weights_cache({"research": {"a": 3.0, "b": 1.0}})
    rng = random.Random(42)
    picks = [reg.pick_weighted_hand("research", ["a", "b"], rng=rng) for _ in range(20_000)]
    p = _proportions(picks)
    assert abs(p["a"] - 0.75) < 0.02
    assert abs(p["b"] - 0.25) < 0.02


async def test_pick_weighted_explicit_weights_override_cache():
    reg = get_registry()
    reg.set_weights_cache({"daily": {"a": 100.0, "b": 1.0}})
    rng = random.Random(7)
    picks = [
        reg.pick_weighted_hand("daily", ["a", "b"], weights={"a": 1.0, "b": 1.0}, rng=rng)
        for _ in range(10_000)
    ]
    p = _proportions(picks)
    assert abs(p["a"] - 0.5) < 0.03  # explicit dict wins over the cached 100:1


async def test_pick_weighted_missing_row_defaults_to_one():
    reg = get_registry()
    reg.set_weights_cache({"whiteboard": {"a": 2.0}})  # b has no row anywhere
    rng = random.Random(1234)
    picks = [reg.pick_weighted_hand("whiteboard", ["a", "b"], rng=rng) for _ in range(15_000)]
    p = _proportions(picks)
    assert abs(p["a"] - 2 / 3) < 0.02
    assert abs(p["b"] - 1 / 3) < 0.02


async def test_pick_weighted_all_zero_degrades_to_uniform():
    reg = get_registry()
    reg.set_weights_cache({"mailbox": {"a": 0.0, "b": 0.0}})
    rng = random.Random(99)
    picks = [reg.pick_weighted_hand("mailbox", ["a", "b"], rng=rng) for _ in range(10_000)]
    p = _proportions(picks)
    assert abs(p["a"] - 0.5) < 0.03
    assert abs(p["b"] - 0.5) < 0.03


async def test_pick_weighted_edge_cases():
    reg = get_registry()
    reg.set_weights_cache({})
    assert reg.pick_weighted_hand("research", []) is None
    assert reg.pick_weighted_hand("research", ["only"]) == "only"
    with pytest.raises(ValueError, match="unknown weight scope"):
        reg.pick_weighted_hand("nope", ["a"])
    # negative weights (only reachable via the explicit dict; schema forbids
    # storing them) clamp to 0 instead of crashing random.choices
    rng = random.Random(5)
    picks = [
        reg.pick_weighted_hand("research", ["a", "b"], weights={"a": -5.0, "b": 1.0}, rng=rng)
        for _ in range(200)
    ]
    assert set(picks) == {"b"}


async def test_pick_weighted_nonfinite_and_huge_weights_never_crash():
    """REVIEW-B2 #4: inf/nan clamp to 0; huge finite sums are normalized."""
    reg = get_registry()
    reg.set_weights_cache({})
    rng = random.Random(11)
    # inf/nan behave as 0 — the finite hand always wins
    picks = [
        reg.pick_weighted_hand(
            "research", ["a", "b", "c"],
            weights={"a": float("inf"), "b": float("nan"), "c": 1.0}, rng=rng,
        )
        for _ in range(200)
    ]
    assert set(picks) == {"c"}
    # all non-finite degrades to uniform instead of crashing
    picks = [
        reg.pick_weighted_hand(
            "research", ["a", "b"], weights={"a": float("inf"), "b": float("nan")}, rng=rng,
        )
        for _ in range(2_000)
    ]
    assert abs(picks.count("a") / len(picks) - 0.5) < 0.06
    # two 1e308 weights would overflow a naive sum to inf; normalization keeps
    # sampling finite and proportional (equal here)
    picks = [
        reg.pick_weighted_hand(
            "research", ["a", "b"], weights={"a": 1e308, "b": 1e308}, rng=rng,
        )
        for _ in range(2_000)
    ]
    assert abs(picks.count("a") / len(picks) - 0.5) < 0.06


async def test_never_loaded_cache_warns_once_and_runs_neutral(caplog):
    """REVIEW-B2 M3: a fresh process (no pre-warm) is visible, not silent."""
    reg = get_registry()
    assert reg.weights_loaded() is False  # conftest builds a fresh registry
    rng = random.Random(3)
    with caplog.at_level("WARNING", logger="institute.registry"):
        picks = [reg.pick_weighted_hand("research", ["a", "b"], rng=rng) for _ in range(5_000)]
    p = _proportions(picks)
    assert abs(p["a"] - 0.5) < 0.04  # neutral 1.0 behaviour
    warnings = [r for r in caplog.records if "never loaded" in r.message]
    assert len(warnings) == 1  # warned exactly once, not per pick

    # any push (even empty = "DB has no rows") marks the cache loaded
    caplog.clear()
    reg.set_weights_cache({})
    assert reg.weights_loaded() is True
    with caplog.at_level("WARNING", logger="institute.registry"):
        reg.pick_weighted_hand("research", ["a", "b"], rng=rng)
    assert [r for r in caplog.records if "never loaded" in r.message] == []


async def test_weight_for_scope_then_default_then_one():
    reg = get_registry()
    reg.set_weights_cache({"research": {"a": 5.0}, "default": {"a": 1.5, "b": 0.5}})
    assert reg.weight_for("research", "a") == 5.0   # scope row wins
    assert reg.weight_for("research", "b") == 0.5   # falls through to default
    assert reg.weight_for("daily", "a") == 1.5      # no scope rows at all -> default
    assert reg.weight_for("daily", "zzz") == 1.0    # nothing anywhere -> neutral


# ==== schema guards ===========================================================

async def test_hand_weights_schema_checks():
    now = bus.now_iso()
    with pytest.raises(sqlite3.IntegrityError):  # scope outside the enum
        await db.execute(
            "INSERT INTO hand_weights (scope, hand, weight, updated_at) VALUES (?,?,?,?)",
            ("bogus", "echo", 1.0, now),
        )
    with pytest.raises(sqlite3.IntegrityError):  # negative weight
        await db.execute(
            "INSERT INTO hand_weights (scope, hand, weight, updated_at) VALUES (?,?,?,?)",
            ("daily", "echo", -1.0, now),
        )
    with pytest.raises(sqlite3.IntegrityError):  # bad verdict enum
        await db.execute(
            "INSERT INTO hand_scorecard (hand, work_date, task_id, verdict, created_at) VALUES (?,?,?,?,?)",
            ("echo", "2026-07-20", "t1", "great", now),
        )


# ==== weights API round-trip ==================================================

async def test_weights_api_roundtrip_and_cache_refresh():
    async with _client() as client:
        r = await client.get("/api/hands/weights")
        assert r.status_code == 200 and r.json() == []

        r = await client.put("/api/hands/weights", json={"entries": [
            {"scope": "research", "hand": "codex", "weight": 2.5},
            {"scope": "default", "hand": "echo", "weight": 0.5},
        ]})
        assert r.status_code == 200
        assert r.json()["upserted"] == 2

        r = await client.get("/api/hands/weights")
        rows = r.json()
        assert [(x["scope"], x["hand"], x["weight"]) for x in rows] == [
            ("default", "echo", 0.5), ("research", "codex", 2.5),
        ]

        # single-entry upsert overwrites in place
        r = await client.put("/api/hands/weights", json={"entries": [
            {"scope": "research", "hand": "codex", "weight": 4.0},
        ]})
        assert r.status_code == 200
        r = await client.get("/api/hands/weights")
        assert [(x["scope"], x["hand"], x["weight"]) for x in r.json()] == [
            ("default", "echo", 0.5), ("research", "codex", 4.0),
        ]

    # the PUT refreshed the registry's process-local cache
    reg = get_registry()
    assert reg.weight_for("research", "codex") == 4.0
    assert reg.weight_for("daily", "echo") == 0.5  # via default scope


async def test_weights_api_replace_and_validation():
    async with _client() as client:
        await client.put("/api/hands/weights", json={"entries": [
            {"scope": "research", "hand": "codex", "weight": 2.0},
            {"scope": "daily", "hand": "claude", "weight": 3.0},
        ]})
        # replace=True swaps the whole set
        r = await client.put("/api/hands/weights", json={
            "replace": True,
            "entries": [{"scope": "mailbox", "hand": "gemini", "weight": 1.5}],
        })
        assert r.status_code == 200
        r = await client.get("/api/hands/weights")
        assert [(x["scope"], x["hand"]) for x in r.json()] == [("mailbox", "gemini")]
        assert get_registry().weights_snapshot() == {"mailbox": {"gemini": 1.5}}

        # validation: bad scope / negative weight / empty or whitespace hand /
        # typo key -> 422
        for bad in (
            {"entries": [{"scope": "nope", "hand": "x", "weight": 1.0}]},
            {"entries": [{"scope": "daily", "hand": "x", "weight": -0.1}]},
            {"entries": [{"scope": "daily", "hand": "", "weight": 1.0}]},
            {"entries": [{"scope": "daily", "hand": "a b", "weight": 1.0}]},
            {"entries": [{"scope": "daily", "hand": "x", "weight": 1.0, "wieght": 2}]},
        ):
            r = await client.put("/api/hands/weights", json=bad)
            assert r.status_code == 422, bad
    # non-finite weights (REVIEW-B2 #4) are rejected by allow_inf_nan=False
    # before the handler runs, so they can never reach the DB. Asserted on the
    # model directly: standards-compliant JSON can't even express inf/nan
    # (httpx refuses to serialize them), and FastAPI's 422 echo of a Python-
    # style Infinity literal crashes its own response render — either way the
    # request fails and nothing is stored.
    from pydantic import ValidationError

    from app.api.hands import WeightEntry

    for w in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            WeightEntry(scope="daily", hand="x", weight=w)
    rows = await db.query("SELECT hand FROM hand_weights WHERE weight IS NULL OR weight > 1e300")
    assert rows == []


async def test_weights_get_lazy_loads_cache_after_restart():
    """REVIEW-B2 M3: persisted weights + fresh process (conftest registry) —
    any GET /weights re-syncs the cache, healing a missed boot pre-warm."""
    now = bus.now_iso()
    await db.execute(  # rows persisted by a "previous process"
        "INSERT INTO hand_weights (scope, hand, weight, updated_at) VALUES (?,?,?,?)",
        ("research", "codex", 3.0, now),
    )
    reg = get_registry()
    assert reg.weights_loaded() is False
    assert reg.weight_for("research", "codex") == 1.0  # cold: neutral, not 3.0

    async with _client() as client:
        r = await client.get("/api/hands/weights")
        assert [(x["scope"], x["hand"], x["weight"]) for x in r.json()] == [("research", "codex", 3.0)]

    assert reg.weights_loaded() is True
    assert reg.weight_for("research", "codex") == 3.0  # cache healed by the read


# ==== scorecard heuristics (pure function) ====================================

LONG_OK = "## 核心结论\n" + "本季度库存周转天数由 58 天降至 41 天，主要由三个因素驱动。" * 5

def test_judge_refusal_is_false_complete():
    v, r = scorecard.judge_output("p", "作为一个AI语言模型，我无法帮助完成这项针对实盘资金的操作建议。")
    assert (v, r) == ("false_complete", "refusal")
    v, r = scorecard.judge_output("p", "As an AI language model, I cannot help with this request in any form.")
    assert (v, r) == ("false_complete", "refusal")
    v, r = scorecard.judge_output("p", "请您先提供更多信息，例如目标公司的财报文件，我才能继续。")
    assert (v, r) == ("false_complete", "needs_input")


def test_judge_citation_counterexamples_stay_ok():
    """REVIEW-B2 M1 regressions: quoting/discussing refusals is NOT refusing.

    Three counter-examples from the review: DONE+artifact with a quoted
    refusal in the body, a CJK report quoting a refusal sample, and an English
    industry analysis mentioning AI disclaimers.
    """
    # 1. THE review reproducer: a real deliverable quoting a refusal sample
    v, r = scorecard.judge_output(
        "正常任务",
        "DONE: report.md\n报告引用“作为AI，我无法访问实时数据”并完成事实核验。",
        ["report.md"],
    )
    assert (v, r) == ("ok", "done_with_artifacts")
    # 2. CJK quoted refusal inside a long normal report (no DONE line at all)
    v, r = scorecard.judge_output(
        "分析模型拒答样本",
        "本次测评收集了三类拒答样本，其中最典型的一条是「作为AI，我无法提供投资建议」。" + LONG_OK,
    )
    assert v == "ok"
    # 3. English analysis discussing AI disclaimers mid-report — identity
    #    mention without same-sentence inability is not a refusal
    v, r = scorecard.judge_output(
        "industry analysis",
        "Vendors now ship standard disclaimers such as acting as an AI assistant "
        "in customer-facing flows. " + LONG_OK,
    )
    assert v == "ok"
    # 4. a refusal buried deep in a long report is analysis, not a refusal
    #    (the probe only reads the opening REFUSAL_HEAD_CHARS)
    body = LONG_OK * 3  # comfortably past REFUSAL_HEAD_CHARS
    assert len(body) > scorecard.REFUSAL_HEAD_CHARS
    v, r = scorecard.judge_output("p", body + "\n附录：模型原话——作为AI我无法执行该请求。")
    assert v == "ok"
    # 5. genuine refusal openers still caught after the guards
    v, r = scorecard.judge_output("p", "作为AI助手，这类请求我确实无法完成，抱歉。")
    assert (v, r) == ("false_complete", "refusal")
    # 6. ...even with a DONE line but NO artifact to back it up
    v, r = scorecard.judge_output("p", "我无法帮助完成该任务。\nDONE: report.md", [])
    assert (v, r) == ("false_complete", "refusal")


def test_judge_echo_reply_is_false_complete():
    v, r = scorecard.judge_output("写一份研究报告", "[echo] 写一份研究报告")
    assert (v, r) == ("false_complete", "echo_reply")
    # a long prompt reproduced verbatim inside "output" = echo, even without the prefix
    prompt = "请分析以下三家公司的库存周转与现金流状况并给出对比结论：" * 10
    v, r = scorecard.judge_output(prompt, "收到任务：\n" + prompt + "\n我会尽快处理。")
    assert (v, r) == ("false_complete", "echo_reply")
    # short prompts legitimately reappear in output — no echo verdict
    v, r = scorecard.judge_output("英伟达", "英伟达 " + LONG_OK)
    assert v == "ok"


def test_judge_stub_short_and_placeholder():
    assert scorecard.judge_output("p", "好的，已完成。") == ("stub", "short_output")
    v, r = scorecard.judge_output("p", LONG_OK + "\n\nTODO: 补充估值部分的敏感性分析表格")
    assert (v, r) == ("stub", "todo_marker")
    v, r = scorecard.judge_output("p", LONG_OK + "\n\n[placeholder] 财务数据待接入")
    assert (v, r) == ("stub", "placeholder")
    v, r = scorecard.judge_output("p", LONG_OK + "\n\n结论：{{此处填入最终评级}}")
    assert (v, r) == ("stub", "placeholder")


def test_judge_done_line_and_ok():
    # FILE_DELIVERABLE happy path: a bare DONE line + a real artifact is ok
    assert scorecard.judge_output("p", "DONE: report.md", ["report.md"]) == ("ok", "done_with_artifacts")
    # ... but a DONE line with NO artifact produced is a stub
    assert scorecard.judge_output("p", "DONE: report.md", []) == ("stub", "short_output")
    assert scorecard.judge_output("p", LONG_OK) == ("ok", "")
    assert scorecard.judge_output("p", "") == ("false_complete", "empty_output")


# ==== run_once: verdict rows + stats aggregation ==============================

async def _mk_task(
    task_id: str, *, hand: str = "echo", status: str = "completed",
    prompt: str = "析构样本任务", output: str = "", artifacts: list[str] | None = None,
    started_at: str | None = None, finished_at: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO tasks (id, hand, requested_hand, prompt, status, source, output,"
        "                   artifacts, started_at, finished_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, hand, hand, prompt, status, "test", output,
         json.dumps(artifacts or []), started_at, finished_at, bus.now_iso()),
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


async def test_run_once_scores_and_aggregates():
    d = work_date()
    start, _end = scorecard._utc_range_for_work_date(d)
    base = datetime.fromisoformat(start) + timedelta(hours=2)

    await _mk_task("ok1", hand="alpha", output=LONG_OK,
                   started_at=_iso(base), finished_at=_iso(base + timedelta(seconds=2)))
    await _mk_task("stub1", hand="alpha", output="收到。",
                   started_at=_iso(base), finished_at=_iso(base + timedelta(seconds=4)))
    await _mk_task("fake1", hand="beta", output="作为AI我无法协助完成该任务，抱歉无法进一步展开。",
                   started_at=_iso(base + timedelta(hours=1)),
                   finished_at=_iso(base + timedelta(hours=1, seconds=6)))
    # terminal-but-not-completed rows: counted by stats, ignored by verdicts
    await _mk_task("fail1", hand="alpha", status="failed",
                   started_at=_iso(base), finished_at=_iso(base + timedelta(seconds=8)))
    await _mk_task("rl1", hand="beta", status="rate_limited", finished_at=_iso(base))
    # outside the day's window: invisible to this run
    await _mk_task("old1", hand="alpha", output=LONG_OK,
                   finished_at=_iso(datetime.fromisoformat(start) - timedelta(hours=3)))

    summary = await scorecard.run_once(d)
    assert "error" not in summary
    assert summary["scanned"] == 3
    assert summary["verdicts"] == {"ok": 1, "stub": 1, "false_complete": 1}

    rows = await db.query("SELECT task_id, hand, verdict, reason FROM hand_scorecard ORDER BY task_id")
    assert [(r["task_id"], r["verdict"]) for r in rows] == [
        ("fake1", "false_complete"), ("ok1", "ok"), ("stub1", "stub"),
    ]
    assert rows[0]["reason"] == "refusal"

    # hourly stats: alpha has 3 terminal tasks in hour(base); beta split across hours
    stats = await db.query(
        "SELECT hand, window_start, tasks_total, tasks_ok, tasks_failed, tasks_rate_limited,"
        "       avg_duration_ms FROM hand_stats ORDER BY hand, window_start"
    )
    by_key = {(s["hand"], s["window_start"]): s for s in stats}
    hour0 = _iso(base.replace(minute=0, second=0, microsecond=0))
    hour1 = _iso((base + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
    alpha = by_key[("alpha", hour0)]
    # tasks_ok counts status='completed' (stub1 included — verdicts live in
    # hand_scorecard, stats are status-level)
    assert (alpha["tasks_total"], alpha["tasks_ok"], alpha["tasks_failed"]) == (3, 2, 1)
    assert alpha["avg_duration_ms"] == pytest.approx((2000 + 4000 + 8000) / 3)
    beta0 = by_key[("beta", hour0)]
    assert (beta0["tasks_total"], beta0["tasks_rate_limited"]) == (1, 1)
    assert beta0["avg_duration_ms"] is None  # no started_at on the rate-limited row
    beta1 = by_key[("beta", hour1)]
    assert (beta1["tasks_total"], beta1["tasks_ok"]) == (1, 1)


async def test_run_once_is_idempotent_and_rejudges():
    d = work_date()
    start, _ = scorecard._utc_range_for_work_date(d)
    ts = _iso(datetime.fromisoformat(start) + timedelta(hours=3))
    await _mk_task("t-flip", hand="alpha", output="好的。", finished_at=ts)

    first = await scorecard.run_once(d)
    again = await scorecard.run_once(d)
    assert first["scanned"] == again["scanned"] == 1
    rows = await db.query("SELECT verdict FROM hand_scorecard WHERE task_id = 't-flip'")
    assert [r["verdict"] for r in rows] == ["stub"]  # one row, not two

    # output fixed later (e.g. bug in the cap) -> rerun re-judges the same row
    await db.execute("UPDATE tasks SET output = ? WHERE id = 't-flip'", (LONG_OK,))
    await scorecard.run_once(d)
    rows = await db.query("SELECT verdict FROM hand_scorecard WHERE task_id = 't-flip'")
    assert [r["verdict"] for r in rows] == ["ok"]

    stats = await db.query("SELECT tasks_total FROM hand_stats WHERE hand = 'alpha'")
    assert [s["tasks_total"] for s in stats] == [1]  # windows overwritten, not duplicated


async def test_run_once_default_settles_previous_day():
    """REVIEW-B2 M2: no date = settle YESTERDAY (whose task set is closed).

    A 00:05 SGT job scanning the previous day can never miss end-of-day
    tasks; settling "today" at 23:45 would. Today's tasks are untouched by a
    no-arg run and get settled by the next day's run.
    """
    yesterday = scorecard.previous_work_date()
    assert yesterday < work_date()
    y_start, _ = scorecard._utc_range_for_work_date(yesterday)
    # a task finishing in yesterday's LAST minute (the 23:45-gap victim)
    late = datetime.fromisoformat(y_start) + timedelta(hours=23, minutes=59)
    await _mk_task("late-yday", hand="alpha", output=LONG_OK, finished_at=_iso(late))
    # and one finishing today — outside a default settlement run
    t_start, _ = scorecard._utc_range_for_work_date(work_date())
    await _mk_task("today1", hand="alpha", output=LONG_OK,
                   finished_at=_iso(datetime.fromisoformat(t_start) + timedelta(hours=1)))

    summary = await scorecard.run_once()  # no date
    assert summary["date"] == yesterday
    assert summary["scanned"] == 1
    rows = await db.query("SELECT task_id FROM hand_scorecard")
    assert [r["task_id"] for r in rows] == ["late-yday"]  # today1 not judged yet

    today_summary = await scorecard.run_once(work_date())  # explicit date still works
    assert today_summary["scanned"] == 1


async def test_run_once_never_raises_and_catches_real_echo_run(tmp_path):
    # a bogus date reports the error instead of raising (scheduler safety)
    bad = await scorecard.run_once("not-a-date")
    assert bad["date"] == "not-a-date" and "error" in bad

    # integration: a real executor run through the echo hand lands as an
    # echo_reply false-complete — THE case the scorecard exists to catch
    # (explicit date: the task finished today; a no-arg run settles yesterday)
    task = await executor.submit("echo", "请写一份完整的行业研究报告", source="test", workspace=tmp_path)
    assert task.status == "completed"
    summary = await scorecard.run_once(work_date())
    assert summary["scanned"] >= 1
    row = await db.query_one("SELECT verdict, reason FROM hand_scorecard WHERE task_id = ?", (task.id,))
    assert (row["verdict"], row["reason"]) == ("false_complete", "echo_reply")


# ==== scorecard + stats API ====================================================

async def test_scorecard_and_stats_api():
    d = work_date()
    start, _ = scorecard._utc_range_for_work_date(d)
    base = datetime.fromisoformat(start) + timedelta(hours=1)
    # alpha, hour 0: one 2s task + one with NO started_at (duration unknowable)
    await _mk_task("api1", hand="alpha", output=LONG_OK,
                   started_at=_iso(base), finished_at=_iso(base + timedelta(seconds=2)))
    await _mk_task("api2", hand="alpha", output=LONG_OK, finished_at=_iso(base))
    # alpha, hour 1: one 8s task — cross-window average must be sample-weighted
    await _mk_task("api3", hand="alpha", output=LONG_OK,
                   started_at=_iso(base + timedelta(hours=1)),
                   finished_at=_iso(base + timedelta(hours=1, seconds=8)))
    await _mk_task("api4", hand="beta", output="嗯。", finished_at=_iso(base))
    await scorecard.run_once(d)

    async with _client() as client:
        r = await client.get("/api/hands/scorecard", params={"date": d})
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == d
        assert body["counts"] == {"ok": 3, "stub": 1, "false_complete": 0}
        assert body["by_hand"]["beta"]["stub"] == 1
        assert {e["task_id"] for e in body["entries"]} == {"api1", "api2", "api3", "api4"}

        # no date param = the previous (settled) day, matching run_once()
        assert (await client.get("/api/hands/scorecard")).json()["date"] == scorecard.previous_work_date()
        # bad shape AND fake calendar dates -> 400 (REVIEW-B2 #8)
        assert (await client.get("/api/hands/scorecard", params={"date": "07/20"})).status_code == 400
        assert (await client.get("/api/hands/scorecard", params={"date": "2026-99-99"})).status_code == 400

        r = await client.get("/api/hands/stats", params={"hours": 24})
        assert r.status_code == 200
        body = r.json()
        assert body["by_hand"]["alpha"]["tasks_total"] == 3
        # sample-weighted mean (2000*1 + 8000*1) / 2 — NOT the tasks_total
        # weighting (2000*2 + 8000*1) / 3 ≈ 4000 (REVIEW-B2 #7)
        assert body["by_hand"]["alpha"]["avg_duration_ms"] == pytest.approx(5000)
        assert body["by_hand"]["beta"]["tasks_total"] == 1
        assert body["by_hand"]["beta"]["avg_duration_ms"] is None
        assert len(body["windows"]) == 3
        by_win = {(w["hand"], w["window_start"]): w for w in body["windows"]}
        hour0 = _iso(base.replace(minute=0, second=0, microsecond=0))
        assert by_win[("alpha", hour0)]["duration_samples"] == 1  # api2 has no started_at

        assert (await client.get("/api/hands/stats", params={"hours": 0})).status_code == 422


# ==== resolve semantics untouched =============================================

async def test_resolve_semantics_unchanged_by_weights():
    """Weights are opt-in: resolve/resolve_chain stay deterministic first-available."""
    reg = get_registry()
    reg.set_weights_cache({"default": {"echo": 0.0}})  # even a zero weight...
    hand, tried = reg.resolve("echo", allow_fallback=True)
    assert hand is not None and hand.name == "echo"    # ...does not affect resolve
    hand, tried = reg.resolve_chain(["nope", "echo"])
    assert hand.name == "echo" and tried == ["nope", "echo"]
