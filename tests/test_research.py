"""Deep research queue: dedup, full tick on echo, daily cap.

Card M3-001 sections below cover the structured rail (thesis/security/question
columns from migrations/0012_research_thesis.sql): backward-compatible
enqueue, dual-rail dedup + cooldown, NULL-column legacy rows, actionCode
seeding, and the research API additions (bare-app mount, same pattern as
tests/test_market_data.py).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.config import get_settings
from app.institute import research, workflows
from app.institute.prompts import work_date


async def test_enqueue_dedups_same_topic_pending():
    first = await research.enqueue("NVDA", source="test")
    assert first["status"] == "pending"
    assert "deduped" not in first

    second = await research.enqueue("NVDA", source="test")
    assert second.get("deduped") is True
    assert second["id"] == first["id"]

    rows = await research.list_queue(status="pending")
    assert len([r for r in rows if r["topic"] == "NVDA"]) == 1


async def test_tick_runs_research_workflow_to_completed():
    await workflows.reconcile_from_disk()
    item = await research.enqueue("AAPL", source="test")

    item_id = await research.tick()
    assert item_id == item["id"]

    done = await research.get_item(item_id)
    assert done["status"] == "completed"
    assert done["run_id"]
    assert done["run"]["status"] == "completed"
    assert len(done["run"]["results"]) == 7  # 6 research steps + follow-ups
    assert all(r["status"] == "completed" for r in done["run"]["results"])

    log_rows = await db.query("SELECT * FROM research_log WHERE topic = ?", ("AAPL",))
    assert len(log_rows) == 1
    assert log_rows[0]["run_id"] == done["run_id"]
    assert log_rows[0]["work_date"] == work_date()  # SGT work day written at insert time

    events = await bus.replay(0, types=["research.completed"])
    mine = [e for e in events if e.ref_id == item_id]
    assert len(mine) == 1
    assert mine[0].payload["topic"] == "AAPL"

    # nothing left to do: another tick is a no-op
    assert await research.tick() is None


async def test_recover_orphans_requeues_running_and_unblocks_tick():
    await workflows.reconcile_from_disk()
    item = await research.enqueue("MSFT", source="test")

    # simulate a crash mid-run: the row was claimed 'running' by a dead process
    await db.execute(
        "UPDATE research_queue SET status='running', started_at=? WHERE id=?",
        (bus.now_iso(), item["id"]),
    )
    # deadlock: _claim_next refuses to claim while a running row exists
    assert await research.tick() is None

    assert await research.recover_orphans() == 1
    row = await db.query_one("SELECT status, started_at FROM research_queue WHERE id = ?", (item["id"],))
    assert row["status"] == "pending"
    assert row["started_at"] is None

    # the pipeline moves again: tick claims and completes the requeued item
    assert await research.tick() == item["id"]
    done = await research.get_item(item["id"])
    assert done["status"] == "completed"

    # idempotent: nothing left to recover
    assert await research.recover_orphans() == 0


async def test_daily_cap_respected(monkeypatch):
    await workflows.reconcile_from_disk()
    monkeypatch.setattr(get_settings(), "research_daily_cap", 1)

    # one research already completed today (work_date is the SGT calendar date)
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at, work_date) "
        "VALUES (?,?,?,?,?)",
        ("ALREADY-DONE", "run0", "done earlier today", f"{work_date()}T00:00:00+00:00", work_date()),
    )

    item = await research.enqueue("TSLA", source="test")
    assert item["status"] == "pending"

    assert await research.tick() is None  # cap reached: nothing claimed
    after = await research.get_item(item["id"])
    assert after["status"] == "pending"


async def test_daily_cap_ignores_legacy_null_work_date_rows(monkeypatch):
    """Migration 0005 semantics: pre-migration rows keep work_date NULL and
    never count toward any day's cap — even if completed_at is today."""
    await workflows.reconcile_from_disk()
    monkeypatch.setattr(get_settings(), "research_daily_cap", 1)

    # a legacy row exactly as it would exist after the (backfill-free) 0005
    # migration: completed today by timestamp, but work_date IS NULL
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at) VALUES (?,?,?,?)",
        ("LEGACY", "run-legacy", "pre-0005 row", bus.now_iso()),
    )

    item = await research.enqueue("INTC", source="test")
    assert item["status"] == "pending"

    # the NULL row is invisible to the cap: tick still claims and completes
    assert await research.tick() == item["id"]
    done = await research.get_item(item["id"])
    assert done["status"] == "completed"


async def test_daily_cap_counts_sgt_work_date_not_utc_timestamp(monkeypatch):
    await workflows.reconcile_from_disk()
    monkeypatch.setattr(get_settings(), "research_daily_cap", 1)

    # completed late "yesterday" in UTC but already TODAY in SGT: the old
    # substr(completed_at) comparison would miss it; work_date must count it
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at, work_date) "
        "VALUES (?,?,?,?,?)",
        ("UTC-EDGE", "run1", "late utc, sgt today", "2020-01-01T17:30:00+00:00", work_date()),
    )

    item = await research.enqueue("AMD", source="test")
    assert item["status"] == "pending"
    assert await research.tick() is None  # cap reached via work_date
    after = await research.get_item(item["id"])
    assert after["status"] == "pending"


# ==== card M3-001: thesis-aware structured rail ==============================

async def _mk_thesis(
    tid: str,
    *,
    name: str = "国产 GPU",
    action_code: str | None = None,
    status: str = "active",
) -> None:
    now = bus.now_iso()
    metadata = json.dumps({"practical": {"actionCode": action_code, "riskBudget": "M"}},
                          ensure_ascii=False) if action_code else "{}"
    await db.execute(
        "INSERT INTO theses (id, kind, slug, name_zh, status, current_view, metadata_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, "thesis", tid, name, status, "conflicting", metadata, now, now),
    )


async def _mk_security(sid: str = "688256.SH", name: str = "寒武纪") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_zh, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, sid.split(".")[0], "CN_A", name, now, now),
    )


async def test_topic_only_enqueue_unchanged_null_structured_columns():
    """Old rail acceptance: topic-only rows carry NULL in every 0012 column."""
    item = await research.enqueue("NVDA", source="test")
    row = await db.query_one("SELECT * FROM research_queue WHERE id = ?", (item["id"],))
    for col in ("thesis_id", "security_id", "question", "output_type", "priority_reason", "dedup_key"):
        assert row[col] is None, col


async def test_structured_enqueue_stores_context_and_dedups_on_normalized_triple():
    await _mk_thesis("thesis-hbm", name="HBM 供需")
    await _mk_security()

    first = await research.enqueue(
        "HBM 供需", source="test",
        thesis_id="thesis-hbm", security_id="688256.SH",
        question="为什么　ＨＢＭ  缺货？", output_type="deep_report",
        priority_reason="operator pick",
    )
    assert first["status"] == "pending"
    assert first["thesis_id"] == "thesis-hbm"
    assert first["security_id"] == "688256.SH"
    assert first["output_type"] == "deep_report"
    assert first["priority_reason"] == "operator pick"
    assert first["dedup_key"] == research.structured_dedup_key(
        "thesis-hbm", "688256.SH", "为什么　ＨＢＭ  缺货？"
    )

    # same triple after NFKC + casefold + whitespace collapse -> dedup
    dup = await research.enqueue(
        "别的标题也一样去重", source="test",
        thesis_id="thesis-hbm", security_id="688256.SH", question="为什么 hbm 缺货?",
    )
    assert dup.get("deduped") is True
    assert dup["id"] == first["id"]

    # a genuinely different question is a different task
    other = await research.enqueue(
        "HBM 供需", source="test",
        thesis_id="thesis-hbm", security_id="688256.SH", question="产能什么时候释放？",
    )
    assert "deduped" not in other
    assert other["id"] != first["id"]

    # so is the same question anchored on another security
    await _mk_security("000001.SZ", name="平安银行")
    third = await research.enqueue(
        "HBM 供需", source="test",
        thesis_id="thesis-hbm", security_id="000001.SZ", question="为什么 hbm 缺货?",
    )
    assert "deduped" not in third


async def test_dedup_rails_are_independent():
    """A pending structured item never swallows a broader topic-only request
    for the same topic string, and vice versa."""
    await _mk_thesis("thesis-npu")
    structured = await research.enqueue("昇腾产业链", source="test", thesis_id="thesis-npu")
    topic_only = await research.enqueue("昇腾产业链", source="test")
    assert "deduped" not in topic_only
    assert topic_only["id"] != structured["id"]

    # each rail still dedups against itself
    assert (await research.enqueue("昇腾产业链", source="test"))["id"] == topic_only["id"]
    again = await research.enqueue("昇腾产业链", source="test", thesis_id="thesis-npu")
    assert again["id"] == structured["id"]


async def test_structured_enqueue_validation():
    with pytest.raises(ValueError, match="thesis_id"):
        await research.enqueue("x", source="test", question="没有论点锚")
    with pytest.raises(ValueError, match="thesis_id"):
        await research.enqueue("x", source="test", security_id="688256.SH")
    with pytest.raises(ValueError, match="not found"):
        await research.enqueue("x", source="test", thesis_id="no-such-thesis")
    await _mk_thesis("thesis-ok")
    with pytest.raises(ValueError, match="not found"):
        await research.enqueue("x", source="test", thesis_id="thesis-ok", security_id="404.SH")


async def test_concurrent_structured_enqueue_single_active_row():
    """REVIEW-B6: check-then-insert is racy — the partial unique index makes
    the INSERT the arbiter, losers re-read the winner as a dedup."""
    await _mk_thesis("thesis-race")
    results = await asyncio.gather(*(
        research.enqueue("竞态主题", source="test", thesis_id="thesis-race", question="同一问题")
        for _ in range(5)
    ))
    assert len({r["id"] for r in results}) == 1        # everyone sees the same item
    assert len([r for r in results if not r.get("deduped")]) == 1
    key = research.structured_dedup_key("thesis-race", None, "同一问题")
    rows = await db.query(
        "SELECT id FROM research_queue WHERE dedup_key = ? AND status IN ('pending','running')",
        (key,),
    )
    assert len(rows) == 1

    # a completed row leaves the partial index: the triple can be re-queued later
    await db.execute(
        "UPDATE research_queue SET status='completed', finished_at=? WHERE id=?",
        (bus.now_iso(), rows[0]["id"]),
    )
    again = await research.enqueue(
        "竞态主题", priority=1, source="test", thesis_id="thesis-race", question="同一问题",
    )
    assert again["status"] == "pending"
    assert "deduped" not in again


async def test_concurrent_seed_yields_one_row_per_thesis():
    for tid in ("seed-r1", "seed-r2", "seed-r3"):
        await _mk_thesis(tid, name=f"论点{tid}", action_code="deep_research_candidate")

    out1, out2 = await asyncio.gather(
        research.seed_from_theses(cap=10), research.seed_from_theses(cap=10),
    )
    fresh = [e["thesis_id"] for e in out1["enqueued"]] + [e["thesis_id"] for e in out2["enqueued"]]
    assert sorted(fresh) == ["seed-r1", "seed-r2", "seed-r3"]  # each thesis lands exactly once
    for tid in ("seed-r1", "seed-r2", "seed-r3"):
        rows = await db.query(
            "SELECT id FROM research_queue WHERE thesis_id = ? AND status IN ('pending','running')",
            (tid,),
        )
        assert len(rows) == 1, tid


async def test_structured_cooldown_keys_on_triple_not_topic():
    await _mk_thesis("thesis-cool")
    key = research.structured_dedup_key("thesis-cool", None, "库存周期到哪了？")
    # a structured completion inside the cooldown window (dedup_key set)
    await db.execute(
        "INSERT INTO research_log (topic, run_id, summary, completed_at, work_date, dedup_key) "
        "VALUES (?,?,?,?,?,?)",
        ("库存专题", "run-s", "done", bus.now_iso(), work_date(), key),
    )

    refused = await research.enqueue(
        "库存专题", source="test", thesis_id="thesis-cool", question="库存周期到哪了？"
    )
    assert refused.get("refused") == "cooldown"

    # a different question on the same thesis is NOT cooled down
    ok = await research.enqueue(
        "库存专题", source="test", thesis_id="thesis-cool", question="出口链影响多大？"
    )
    assert ok["status"] == "pending"
    # the topic rail is untouched by the structured completion (rail independence)
    topic_ok = await research.enqueue("库存专题", source="test")
    assert topic_ok["status"] == "pending"
    # priority > 0 bypasses the structured cooldown, mirroring the topic rail
    bumped = await research.enqueue(
        "库存专题", priority=1, source="test", thesis_id="thesis-cool", question="库存周期到哪了？"
    )
    assert bumped["status"] == "pending"


async def test_tick_injects_thesis_context_into_topic_variable():
    await workflows.reconcile_from_disk()
    await _mk_thesis("thesis-gpu", name="国产 GPU 追赶")
    await _mk_security()

    item = await research.enqueue(
        "国产 GPU", source="test",
        thesis_id="thesis-gpu", security_id="688256.SH", question="良率拐点何时出现？",
    )
    assert await research.tick() == item["id"]

    done = await research.get_item(item["id"])
    assert done["status"] == "completed"
    topic_var = done["run"]["variables"]["TOPIC"]
    # context is injected into the ${TOPIC} value only — prompts stay verbatim
    assert topic_var.startswith("国产 GPU【论点上下文】")
    assert "thesis-gpu" in topic_var and "国产 GPU 追赶" in topic_var
    assert "688256.SH" in topic_var and "寒武纪" in topic_var
    assert "良率拐点何时出现？" in topic_var

    # the completion lands on the structured cooldown rail
    log_row = await db.query_one("SELECT dedup_key FROM research_log WHERE run_id = ?", (done["run_id"],))
    assert log_row["dedup_key"] == item["dedup_key"]
    # and research_log keeps the plain topic string (vault export/log surfaces)
    log_topic = await db.query_one("SELECT topic FROM research_log WHERE run_id = ?", (done["run_id"],))
    assert log_topic["topic"] == "国产 GPU"


async def test_tick_handles_pre_0012_rows_with_null_columns():
    """A row exactly as it existed before migration 0012 (all new columns
    NULL) must tick to completion with unchanged topic semantics."""
    await workflows.reconcile_from_disk()
    await db.execute(
        "INSERT INTO research_queue (id, topic, priority, status, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("legacy-row-01", "LEGACY-TOPIC", 0, "pending", "test", bus.now_iso()),
    )
    assert await research.tick() == "legacy-row-01"
    done = await research.get_item("legacy-row-01")
    assert done["status"] == "completed"
    assert done["run"]["variables"]["TOPIC"] == "LEGACY-TOPIC"  # no context suffix
    log_row = await db.query_one(
        "SELECT dedup_key FROM research_log WHERE topic = ?", ("LEGACY-TOPIC",)
    )
    assert log_row["dedup_key"] is None


async def test_seed_from_theses_is_idempotent_and_capped():
    await _mk_thesis("seed-a", name="论点A", action_code="deep_research_candidate")
    await _mk_thesis("seed-b", name="论点B", action_code="deep_research_candidate")
    await _mk_thesis("seed-c", name="论点C", action_code="deep_research_candidate")
    await _mk_thesis("seed-x", name="论点X", action_code="watch_only")        # code mismatch
    await _mk_thesis("seed-d", name="论点D", action_code="deep_research_candidate",
                     status="dormant")                                        # shelved lifecycle

    out = await research.seed_from_theses(cap=2)
    assert out["matched"] == 3
    assert [e["thesis_id"] for e in out["enqueued"]] == ["seed-a", "seed-b"]
    assert out["deduped"] == 0

    rows = await research.list_queue(status="pending")
    seeded = [r for r in rows if r["thesis_id"]]
    assert {r["thesis_id"] for r in seeded} == {"seed-a", "seed-b"}
    assert all(r["priority_reason"] == "practical.actionCode=deep_research_candidate" for r in seeded)
    assert all(r["source"] == "thesis_seed" for r in seeded)

    # idempotent re-run: existing triples dedup, only the capped-out one lands
    out2 = await research.seed_from_theses(cap=10)
    assert [e["thesis_id"] for e in out2["enqueued"]] == ["seed-c"]
    assert out2["deduped"] == 2
    out3 = await research.seed_from_theses(cap=10)
    assert out3["enqueued"] == []
    assert out3["deduped"] == 3

    with pytest.raises(ValueError, match="action code"):
        await research.seed_from_theses(action_codes=[])


async def test_seed_cap_zero_is_dry_sweep_and_bounds_enforced():
    """REVIEW-B6: cap=0 means 'enqueue nothing', never silently becomes 1;
    negative and oversized caps are refused."""
    await _mk_thesis("seed-dry", name="干跑论点", action_code="deep_research_candidate")

    out = await research.seed_from_theses(cap=0)
    assert out["matched"] == 1
    assert out["enqueued"] == [] and out["deduped"] == 0 and out["refused_cooldown"] == 0
    assert await research.list_queue(status="pending") == []   # truly nothing enqueued

    with pytest.raises(ValueError, match=">= 0"):
        await research.seed_from_theses(cap=-1)
    with pytest.raises(ValueError, match="MAX_SEED_CAP"):
        await research.seed_from_theses(cap=research.MAX_SEED_CAP + 1)


async def test_topic_context_is_bounded_and_field_caps_explicit():
    """REVIEW-B6: the ${TOPIC} context suffix recurs at every substitution
    site of the 7-step workflow, so it hard-caps at _CTX_SUFFIX_CAP; field
    limits are explicit errors, not silent truncation (truncation would
    change the dedup triple behind the caller's back)."""
    await _mk_thesis("thesis-long", name="超长论点名" * 60)          # 300 chars
    await _mk_security("600000.SH", name="超长证券名" * 40)          # 200 chars
    item = await research.enqueue(
        "主题", source="test", thesis_id="thesis-long", security_id="600000.SH",
        question="问" * research.MAX_QUESTION_LEN,
    )
    topic_var = await research._topic_with_context(item)
    assert topic_var.startswith("主题【论点上下文】")
    assert len(topic_var) <= len("主题") + len("【论点上下文】") + research._CTX_SUFFIX_CAP
    assert "thesis-long" in topic_var                              # id survives the name slice

    with pytest.raises(ValueError, match="question exceeds"):
        await research.enqueue("x", source="test", thesis_id="thesis-long",
                               question="问" * (research.MAX_QUESTION_LEN + 1))
    with pytest.raises(ValueError, match="output_type exceeds"):
        await research.enqueue("x", source="test", thesis_id="thesis-long",
                               output_type="t" * (research.MAX_ANNOTATION_LEN + 1))
    with pytest.raises(ValueError, match="priority_reason exceeds"):
        await research.enqueue("x", source="test", thesis_id="thesis-long",
                               priority_reason="r" * (research.MAX_ANNOTATION_LEN + 1))


async def test_api_structured_enqueue_and_seed():
    from app.api import research as api_research

    app = FastAPI()
    app.include_router(api_research.router)
    await _mk_thesis("thesis-api", name="API 论点", action_code="deep_research_candidate")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/research/queue", json={
            "topic": "API 论点", "thesis_id": "thesis-api", "question": "值得深挖吗？",
        })
        assert r.status_code == 200
        assert r.json()["thesis_id"] == "thesis-api"
        assert r.json()["dedup_key"]

        r = await client.post("/api/research/queue", json={"topic": "x", "thesis_id": "nope"})
        assert r.status_code == 400
        assert "not found" in r.json()["detail"]

        # seeding dedups against the manually enqueued triple? different
        # question -> different triple, so the seed lands its own item
        r = await client.post("/api/research/seed-from-theses", json={})
        assert r.status_code == 200
        assert r.json()["matched"] == 1
        assert [e["thesis_id"] for e in r.json()["enqueued"]] == ["thesis-api"]

        r = await client.post("/api/research/seed-from-theses", json={"action_codes": []})
        assert r.status_code == 400

        # cap=0 dry sweep passes through untouched; bad caps and typos are 422s
        r = await client.post("/api/research/seed-from-theses", json={"cap": 0})
        assert r.status_code == 200
        assert r.json()["enqueued"] == []
        assert (await client.post("/api/research/seed-from-theses", json={"cap": -1})).status_code == 422
        assert (await client.post(
            "/api/research/seed-from-theses", json={"cap": research.MAX_SEED_CAP + 1}
        )).status_code == 422
        assert (await client.post(
            "/api/research/seed-from-theses", json={"bogus": 1}
        )).status_code == 422
        # question over the domain cap is rejected at the API boundary too
        r = await client.post("/api/research/queue", json={
            "topic": "长问题", "thesis_id": "thesis-api",
            "question": "问" * (research.MAX_QUESTION_LEN + 1),
        })
        assert r.status_code == 422
