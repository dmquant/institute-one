"""Forecast extraction (Phase 5 card C3): regex matrix + idempotency + hooks.

Covers the ROADMAP contract: direction/conviction/horizon cue matrices, the
ticker stoplist (generic market words + YYYYMM-shaped six-digit codes), the
CJK guard (digit-boundary assertions), source-level idempotency through the
forecast_extractions claim table, and the two bus handlers
(research.completed / daily workflow.completed) — invoked directly with
synthetic events so no handler registration leaks into the process-wide bus.
Created forecasts go through the real forecasts.create_forecast (never
mocked), anchored to the structured item's thesis or the fallback singleton.
"""
from __future__ import annotations

import json

import pytest

from app import bus, db
from app.institute import forecast_extract as fx
from app.institute import forecasts


async def _mk_thesis(tid: str = "t-macro", name: str = "宏观论点") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO theses (id, kind, slug, name_zh, status, current_view, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tid, "thesis", tid, name, "active", "unknown", now, now),
    )


async def _mk_security(
    sid: str, market: str, name_zh: str | None = None, name_en: str | None = None,
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_zh, name_en, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (sid, sid.split(".")[0], market, name_zh, name_en, now, now),
    )


async def _mk_alias(alias: str, sid: str, kind: str = "abbreviation") -> None:
    await db.execute(
        "INSERT INTO security_aliases (id, security_id, alias, kind, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"al-{alias}-{kind}", sid, alias, kind, bus.now_iso()),
    )


async def _seed_universe() -> None:
    await _mk_security("600519.SH", "CN_A", name_zh="贵州茅台")
    await _mk_security("000001.SZ", "CN_A", name_zh="平安银行")
    await _mk_security("0700.HK", "HK", name_zh="腾讯控股")
    await _mk_security("NVDA.US", "US", name_zh="英伟达", name_en="NVIDIA")
    await _mk_alias("茅台", "600519.SH")
    # production shape (REVIEW-C3 H1): the importer stamps a kind='ticker'
    # alias equal to every symbol — the guards must hold against it
    for alias, sid in (("600519", "600519.SH"), ("000001", "000001.SZ"),
                       ("0700", "0700.HK"), ("NVDA", "NVDA.US")):
        await _mk_alias(alias, sid, kind="ticker")


# ==== direction matrix =========================================================

def test_direction_matrix():
    cases = [
        ("我们看多贵州茅台", "long"),
        ("维持增持评级，逢低买入", "long"),
        ("建议超配半导体", "long"),
        ("预计将跑赢基准", "long"),
        ("看空地产链", "short"),
        ("下调至减持", "short"),
        ("建议低配出口链", "short"),
        ("维持中性评级", "neutral"),
        ("短期保持观望", "neutral"),
        ("We stay bullish on the name", "long"),
        ("move to overweight", "long"),
        ("we turn bearish here", "short"),
        ("cut to underweight", "short"),
        ("we are neutral on valuation", "neutral"),
        ("公司今日发布财报", None),                      # no direction word at all
        ("我们不看空茅台", None),                        # negated hit is voided, not flipped
        ("我们并不看多该板块", None),
        ("由看多转为看空", "short"),                     # the LAST hit wins
        ("先前看空，现在转为看多", "long"),
        ("我们不看空茅台，反而看多", "long"),            # voided hit + live hit
    ]
    for sentence, expected in cases:
        assert fx.match_direction(sentence) == expected, sentence


def test_conviction_and_horizon_cues():
    assert fx.match_conviction("强烈看多") == fx.STRONG_CONVICTION
    assert fx.match_conviction("we strongly recommend") == fx.STRONG_CONVICTION
    assert fx.match_conviction("谨慎看多") == fx.CAUTIOUS_CONVICTION
    assert fx.match_conviction("初步看多，可能有波动") == fx.CAUTIOUS_CONVICTION
    assert fx.match_conviction("看多") == fx.DEFAULT_CONVICTION
    # the conservative cue wins when both appear
    assert fx.match_conviction("强烈看多但建议谨慎参与") == fx.CAUTIOUS_CONVICTION

    cases = [
        ("三个月内有望修复", 90),
        ("未来3个月看多", 90),
        ("十二个月维度", 360),
        ("一个季度内兑现", 90),
        ("本季度内落地", 90),
        ("半年内估值切换", 180),
        ("年内目标价上调", 365),
        ("两周内催化剂密集", 14),
        ("10天内验证", 10),
        ("15日内公布", 15),
        ("within 6 months", 180),
        ("over the next 2 weeks", 14),
        ("2026年公司将投产", fx.DEFAULT_HORIZON_DAYS),   # a year label, not a horizon
        ("没有任何时间线索", fx.DEFAULT_HORIZON_DAYS),
    ]
    for sentence, expected in cases:
        assert fx.match_horizon_days(sentence) == expected, sentence


# ==== security resolution: stoplist + CJK guard ================================

async def test_find_securities_stoplist_and_cjk_guard():
    await _seed_universe()
    # a YYYYMM-shaped code that really exists in the DB — the stoplist must
    # still beat the lookup on the bare-code rail, INCLUDING when the
    # importer has stamped its kind='ticker' alias (REVIEW-C3 H1)
    await _mk_security("200601.SZ", "CN_A", name_zh="测试B股")
    await _mk_alias("200601", "200601.SZ", kind="ticker")
    await _mk_alias("大盘", "600519.SH")  # hostile generic alias: must never resolve
    table = await fx._load_name_table()

    async def find(sentence: str) -> list[str]:
        return await fx._find_securities(sentence, table)

    assert await find("看多600519") == ["600519.SH"]                    # bare code, CJK boundary
    assert await find("我们看多贵州茅台") == ["600519.SH"]              # name_zh
    assert await find("茅台批价企稳") == ["600519.SH"]                  # alias
    assert await find("看多 600519.SH 的中长期逻辑") == ["600519.SH"]   # canonical id
    assert await find("超配0700.HK") == ["0700.HK"]
    assert await find("bullish on NVDA.US") == ["NVDA.US"]
    assert await find("英伟达业绩超预期") == ["NVDA.US"]

    # CJK guard: six digits inside a longer digit run never match
    assert await find("订单号1600519000已发货") == []
    # decimal tail / quantity units are numbers, not tickers
    assert await find("该指标为600519.5") == []
    assert await find("成交额600519万元") == []
    # date-like six-digit codes are refused even though 200601.SZ exists...
    assert await find("根据200601的研究我们看多") == []
    # ...but the canonical-id rail still resolves that security
    assert await find("看多200601.SZ") == ["200601.SZ"]
    # generic market words never resolve, whatever the alias table says
    assert await find("看多大盘") == []
    # unknown codes / unknown canonical ids resolve to nothing
    assert await find("看多999999") == []
    assert await find("看多 999999.SH") == []

    # REVIEW-C3 H1 regression: the guards above must hold with the
    # importer-shaped ticker aliases planted (the reviewer's three probes
    # previously slipped through the NAME rail) — digit strings may only
    # travel the bare-code rail, whose guards apply
    assert await find("根据200601的研究我们看多") == []       # YYYYMM beats ticker alias
    assert await find("成交额600519万元") == []                # unit guard beats ticker alias
    assert await find("指标600519.5") == []                    # decimal tail beats ticker alias
    assert await find("订单号1600519000已发货") == []          # digit-run boundary holds
    assert fx._name_rail_eligible("600519") is False           # digit alias: name rail refuses
    assert fx._name_rail_eligible("NVDA") is True              # lettered ticker alias stays
    assert all(not name.isdigit() for name, _ in table)        # loader already drops them


async def test_extract_candidates_matrix_dedup_and_cap():
    await _seed_universe()
    text = (
        "## 核心结论\n"
        "我们强烈看多贵州茅台（600519.SH），三个月内有望跑赢。\n"
        "平安银行转为看空，预计半年内承压；\n"
        "腾讯控股维持中性。\n"
        "情绪面看多大盘，但这不构成个股意见。\n"
        "公司于202606发布了公告。\n"
        "后文改口：看空贵州茅台。\n"      # same security again — the FIRST call wins
    )
    cands = {c["security_id"]: c for c in await fx.extract_candidates(text)}
    assert set(cands) == {"600519.SH", "000001.SZ", "0700.HK"}
    mt = cands["600519.SH"]
    assert (mt["direction"], mt["conviction"], mt["horizon_days"]) == ("long", 0.9, 90)
    pa = cands["000001.SZ"]
    assert (pa["direction"], pa["horizon_days"]) == ("short", 180)
    tx = cands["0700.HK"]
    assert (tx["direction"], tx["conviction"], tx["horizon_days"]) == (
        "neutral", fx.DEFAULT_CONVICTION, fx.DEFAULT_HORIZON_DAYS)
    assert mt["claim"].startswith("我们强烈看多贵州茅台")

    # cap: six long calls, only MAX_FORECASTS_PER_SOURCE survive
    await _mk_security("600036.SH", "CN_A", name_zh="招商银行")
    await _mk_security("000858.SZ", "CN_A", name_zh="五粮液")
    flood = "。".join(
        f"看多{name}" for name in ("贵州茅台", "平安银行", "腾讯控股", "英伟达", "招商银行", "五粮液")
    )
    assert len(await fx.extract_candidates(flood)) == fx.MAX_FORECASTS_PER_SOURCE

    assert await fx.extract_candidates("") == []
    assert await fx.extract_candidates("没有任何标的的看多观点") == []


# ==== REVIEW-C3 M4: negation + horizon counterexamples ==========================

def test_direction_negation_counterexamples():
    """The reviewer's live probes: a question answered in the negative and an
    advisory negation ahead of the cue must void the hit (when in doubt, do
    not extract) — while the previously-correct semantics stay intact."""
    cases = [
        ("看多？不", None),                    # asked and answered in the negative
        ("不建议看多", None),                  # advisory negation, not adjacent
        ("不推荐买入", None),
        ("暂不看多", None),
        ("别追高买入", None),                  # word-initial 别 voids
        ("切勿追买入", None),
        ("看多吗？未必", None),
        ("看多？确定", "long"),                # a POSITIVE answer keeps the hit
        ("级别上调，看多", "long"),            # 别 inside 级别 must not void
        ("不看空反而看多", "long"),            # voided hit + live hit (unchanged)
        ("由看多转为看空", "short"),
    ]
    for sentence, expected in cases:
        assert fx.match_direction(sentence) == expected, sentence


def test_horizon_counterexamples_shortest_wins():
    """2026年内 must not resolve through the 年内 substring, and with several
    surviving cues the SHORTEST horizon governs (conservative)."""
    cases = [
        ("2026年内", fx.DEFAULT_HORIZON_DAYS),         # substring of a rejected span
        ("2026年内，未来2周", 14),                     # vague 年内 loses to explicit 2周
        ("2026年内，未来2周兑现", 14),
        ("年内目标价上调", 365),                       # bare 年内 still works
        ("一年内兑现", 365),                           # numeric-CN rail unaffected
        ("十年内", fx.DEFAULT_HORIZON_DAYS),           # over-cap, no substring rescue
        ("年内看多，未来10天有催化", 10),              # min(365, 10)
        ("半年内估值切换", 180),
    ]
    for sentence, expected in cases:
        assert fx.match_horizon_days(sentence) == expected, sentence


async def test_extract_candidates_question_negation_across_split():
    """End-to-end M4: the splitter used to eat the '不' after the question
    mark, leaving the long half alive."""
    await _seed_universe()
    assert await fx.extract_candidates("看多贵州茅台？不") == []
    assert await fx.extract_candidates("看多贵州茅台？不，风险太大") == []
    # a question with a positive continuation still extracts
    cands = await fx.extract_candidates("看多贵州茅台？是的，维持判断")
    assert [c["security_id"] for c in cands] == ["600519.SH"]
    # an advisory negation inside a full sentence never reaches the ledger
    assert await fx.extract_candidates("我们不建议看多贵州茅台") == []


# ==== REVIEW-C3 M1: cross-listing homonym arbitration ===========================

async def test_homonym_name_refused_unless_anchored():
    """0004's documented A/H case: one Chinese name on two listings. A bare
    name mention is refused (fail closed, counted); a canonical id in the
    same sentence anchors THE listing and suppresses the sibling."""
    await _seed_universe()
    await _mk_security("688981.SH", "CN_A", name_zh="中芯国际")
    await _mk_security("0981.HK", "HK", name_zh="中芯国际")
    table = await fx._load_name_table()

    # bare homonym: refused outright — never a double open
    stats: dict = {}
    assert await fx._find_securities("看多中芯国际", table, stats) == []
    assert stats["ambiguous_names"] == ["中芯国际"]

    # canonical evidence disambiguates: the OTHER listing is suppressed
    assert await fx._find_securities("看多中芯国际（688981.SH）", table) == ["688981.SH"]
    assert await fx._find_securities("看多中芯国际（0981.HK）", table) == ["0981.HK"]
    # both listings written explicitly: both open (user said so)
    both = await fx._find_securities("对比 688981.SH 与 0981.HK，看多中芯国际", table)
    assert set(both) == {"688981.SH", "0981.HK"}

    # unique names keep resolving; ambiguous ALIASES are arbitrated the same
    # way (same alias text under different kinds — 0004 allows exactly that)
    assert await fx._find_securities("看多贵州茅台", table) == ["600519.SH"]
    await _mk_alias("双城股份", "600519.SH", kind="abbreviation")
    await _mk_alias("双城股份", "000001.SZ", kind="name_zh")
    table = await fx._load_name_table()
    assert await fx._find_securities("看多双城股份", table) == []

    # end-to-end: the refusal is recorded in the claim's detail, no forecast
    out = await fx.process_source("research:amb", "research", "强烈看多中芯国际")
    assert out["status"] == "processed" and out["created"] == []
    row = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("research:amb",))
    assert "ambiguous" in row["detail"] and "中芯国际" in row["detail"]
    assert row["status"] == "complete"


# ==== process_source: ledger writes + idempotency ==============================

async def test_process_source_creates_forecasts_idempotently():
    await _seed_universe()
    text = "我们强烈看多贵州茅台，三个月内跑赢。平安银行谨慎看空。"

    out = await fx.process_source("research:r1", "research", text)
    assert out["status"] == "processed"
    assert len(out["created"]) == 2

    rows = await forecasts.list_forecasts()
    assert {f["id"] for f in rows} == set(out["created"])
    by_sec = {f["security_id"]: f for f in rows}
    mt = by_sec["600519.SH"]
    assert (mt["direction"], mt["conviction"], mt["horizon_days"]) == ("long", 0.9, 90)
    assert mt["settlement_rule"] == {"type": "absolute_move", "threshold": 0.05}
    assert mt["status"] == "open"
    assert mt["thesis_id"] == fx.FALLBACK_THESIS_ID     # fallback singleton anchors
    pa = by_sec["000001.SZ"]
    assert (pa["direction"], pa["conviction"]) == ("short", fx.CAUTIOUS_CONVICTION)

    # the fallback thesis was created idempotently (watch = parked, not curated)
    thesis = await db.query_one("SELECT * FROM theses WHERE id = ?", (fx.FALLBACK_THESIS_ID,))
    assert thesis["status"] == "watch"

    # same source again: the claim table is the arbiter — nothing new
    again = await fx.process_source("research:r1", "research", text)
    assert again["status"] == "duplicate"
    assert len(await forecasts.list_forecasts()) == 2

    # a different source with the same text is a different claim
    other = await fx.process_source("research:r2", "research", text)
    assert other["status"] == "processed"
    assert len(await forecasts.list_forecasts()) == 4

    events = await bus.replay(0, types=["forecast.extracted"])
    assert [e.ref_id for e in events] == ["research:r1", "research:r2"]


async def test_process_source_empty_text_stays_retryable_and_thesis_anchor():
    await _seed_universe()
    # empty text does not burn the claim — the source can be retried later
    out = await fx.process_source("research:r9", "research", "   ")
    assert out["status"] == "empty"
    assert await db.query_one(
        "SELECT id FROM forecast_extractions WHERE source_ref = ?", ("research:r9",)) is None
    out = await fx.process_source("research:r9", "research", "看多贵州茅台")
    assert out["status"] == "processed" and len(out["created"]) == 1

    # an existing thesis anchors; a vanished thesis_id falls back to the singleton
    await _mk_thesis("t-real")
    out = await fx.process_source("research:r10", "research", "看多腾讯控股", thesis_id="t-real")
    fc = await forecasts.get_forecast(out["created"][0])
    assert fc["thesis_id"] == "t-real"
    out = await fx.process_source("research:r11", "research", "看多英伟达", thesis_id="t-ghost")
    fc = await forecasts.get_forecast(out["created"][0])
    assert fc["thesis_id"] == fx.FALLBACK_THESIS_ID

    # a text with zero candidates still claims (processed, nothing created)
    out = await fx.process_source("research:r12", "research", "纯事实陈述，无观点。")
    assert out["status"] == "processed" and out["created"] == []
    row = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("research:r12",))
    assert (row["n_candidates"], row["n_forecasts"]) == (0, 0)
    assert row["status"] == "complete"


# ==== REVIEW-C3 M2: crash-consistent extraction state machine ===================

async def test_crash_between_candidates_resumes_exactly_the_missing_rest(monkeypatch):
    """Kill the process at a candidate boundary (after #1 is fully recorded,
    before #2 is claimed): the claim stays 'pending', an ordinary replay
    RESUMES it — skipping the already-created forecast, creating exactly the
    missing two — instead of being bricked by 'duplicate'."""
    await _seed_universe()
    text = "看多贵州茅台。看空平安银行。看多腾讯控股。"

    real_execute = db.execute
    item_inserts = 0

    async def crashing_execute(sql, params=()):
        nonlocal item_inserts
        if "INSERT INTO forecast_extraction_items" in sql:
            item_inserts += 1
            if item_inserts == 2:
                raise RuntimeError("simulated crash at a candidate boundary")
        return await real_execute(sql, params)

    monkeypatch.setattr(fx.db, "execute", crashing_execute)
    with pytest.raises(RuntimeError):
        await fx.process_source("research:crash1", "research", text)
    monkeypatch.undo()

    row = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("research:crash1",))
    assert row["status"] == "pending"                       # unfinished, resumable
    assert len(await forecasts.list_forecasts()) == 1       # only candidate #1 landed

    out = await fx.process_source("research:crash1", "research", text)
    assert out["status"] == "processed"
    assert len(out["created"]) == 3                         # 1 resumed + 2 created
    rows = await forecasts.list_forecasts()
    assert len(rows) == 3                                   # nothing duplicated
    assert {f["security_id"] for f in rows} == {"600519.SH", "000001.SZ", "0700.HK"}

    row = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("research:crash1",))
    assert row["status"] == "complete"
    assert set(json.loads(row["forecast_ids"])) == {f["id"] for f in rows}
    assert row["n_forecasts"] == 3

    # complete claims are duplicates again — replay is inert
    again = await fx.process_source("research:crash1", "research", text)
    assert again["status"] == "duplicate"
    assert len(await forecasts.list_forecasts()) == 3

    events = await bus.replay(0, types=["forecast.extracted"])
    assert [e.ref_id for e in events] == ["research:crash1"]  # emitted exactly once


async def test_crash_inside_create_never_duplicates(monkeypatch):
    """The one-statement doubt window: create_forecast COMMITS, then the
    process dies before the item back-fill. Resume must NOT re-create that
    candidate (fails closed, reported in detail) while still completing the
    untouched rest — the old DELETE-and-re-extract path would have copied it."""
    await _seed_universe()
    text = "看多贵州茅台。看空平安银行。看多腾讯控股。"

    real_create = forecasts.create_forecast
    calls = 0

    async def create_then_die(fields):
        nonlocal calls
        calls += 1
        fc = await real_create(fields)
        if calls == 2:
            raise RuntimeError("simulated crash after commit, before back-fill")
        return fc

    monkeypatch.setattr(fx.forecasts, "create_forecast", create_then_die)
    with pytest.raises(RuntimeError):
        await fx.process_source("research:crash2", "research", text)
    monkeypatch.undo()

    assert len(await forecasts.list_forecasts()) == 2       # #1 + the orphaned #2

    out = await fx.process_source("research:crash2", "research", text)
    assert out["status"] == "processed"
    rows = await forecasts.list_forecasts()
    assert len(rows) == 3                                   # #3 created, #2 NOT copied
    assert sum(1 for f in rows if f["security_id"] == "000001.SZ") == 1
    assert len(out["created"]) == 2                         # the in-doubt one excluded
    assert any("in doubt" in p for p in out["problems"])

    row = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("research:crash2",))
    assert row["status"] == "complete"                      # decided; replay = duplicate
    assert "in doubt" in row["detail"]
    item = await db.query_one(
        "SELECT * FROM forecast_extraction_items WHERE extraction_id = ? AND security_id = ?",
        (row["id"], "000001.SZ"))
    assert item["forecast_id"] is None                      # the surgical marker

    # ForecastError candidates release their claim (refusal, not doubt)
    await db.execute("DELETE FROM securities WHERE id = ?", ("0700.HK",))
    out = await fx.process_source("research:refused", "research", "看多腾讯控股")
    assert out["status"] == "processed" and out["created"] == []
    row = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("research:refused",))
    items = await db.query(
        "SELECT * FROM forecast_extraction_items WHERE extraction_id = ?", (row["id"],))
    assert items == []                                       # claim released, no doubt row


# ==== REVIEW-C3 M5: analyst attribution on the claim row ========================

async def test_extraction_attribution_resolves_last_non_ops_step():
    """research and daily runs attribute to the workflow's last non-ops
    analyst (both compile steps are ops-editor — editors organize, they don't
    originate calls); anything missing degrades to NULL, never a guess."""
    await _seed_universe()
    from app.institute import workflows as workflows_mod
    await workflows_mod.reconcile_from_disk()
    now = bus.now_iso()

    # research: payload run_id → workflow steps → 07-followups (chief-strategist)
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, status, started_at) "
        "VALUES (?,?,?,?)", ("run-attr-r", "research", "completed", now))
    await db.execute(
        "INSERT INTO research_queue (id, topic, status, source, created_at, run_id) "
        "VALUES (?,?,?,?,?,?)", ("ritem-attr", "主题", "completed", "api", now, "run-attr-r"))
    await fx._on_research_completed(_event(
        "research.completed", "ritem-attr",
        {"summary": "看多贵州茅台", "run_id": "run-attr-r"}))
    row = await db.query_one(
        "SELECT analyst_id FROM forecast_extractions WHERE source_ref = ?",
        ("research:ritem-attr",))
    assert row["analyst_id"] == "chief-strategist"

    # daily: the ops-editor compile step is skipped → 02-outlook (chief-strategist)
    await db.execute(
        "INSERT INTO workflow_runs (id, workflow_id, status, started_at) "
        "VALUES (?,?,?,?)", ("run-attr-d", "daily", "completed", now))
    await fx._on_workflow_completed(_event(
        "workflow.completed", "run-attr-d",
        {"workflow_id": "daily", "run_id": "run-attr-d",
         "results": [{"step_id": "02", "summary": "看空平安银行"}]}))
    row = await db.query_one(
        "SELECT analyst_id FROM forecast_extractions WHERE source_ref = ?",
        ("workflow:run-attr-d",))
    assert row["analyst_id"] == "chief-strategist"

    # fails closed: unknown/missing run, unknown workflow → no attribution
    assert await fx._resolve_analyst("no-such-run") is None
    assert await fx._resolve_analyst(None) is None


# ==== bus handlers (invoked directly; registration is snapshot-restored) ========

def _event(etype: str, ref_id: str, payload: dict) -> bus.Event:
    return bus.Event(id=0, type=etype, ref_id=ref_id, payload=payload)


async def test_research_completed_handler(tmp_path):
    await _seed_universe()
    await _mk_thesis("t-research")
    now = bus.now_iso()
    ws = tmp_path / "ws-research"
    ws.mkdir()
    (ws / "06_深度报告.md").write_text("结论：强烈看多贵州茅台，三个月内兑现。", encoding="utf-8")
    await db.execute(
        "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)", ("sess-r", "研究", "workflow", str(ws), now, now),
    )
    await db.execute(
        "INSERT INTO research_queue (id, topic, status, source, created_at, thesis_id) "
        "VALUES (?,?,?,?,?,?)", ("ritem", "白酒", "completed", "api", now, "t-research"),
    )

    await fx._on_research_completed(_event(
        "research.completed", "ritem",
        {"topic": "白酒", "session_id": "sess-r", "summary": "（摘要占位）"},
    ))
    rows = await forecasts.list_forecasts()
    assert len(rows) == 1
    assert rows[0]["thesis_id"] == "t-research"        # the structured item's thesis anchors
    assert rows[0]["security_id"] == "600519.SH"

    # replayed event: idempotent, still one forecast
    await fx._on_research_completed(_event(
        "research.completed", "ritem", {"session_id": "sess-r", "summary": ""}))
    assert len(await forecasts.list_forecasts()) == 1

    # no workspace: falls back to the payload summary; unknown item: fallback thesis
    await fx._on_research_completed(_event(
        "research.completed", "ghost-item", {"summary": "看空平安银行"}))
    rows = await forecasts.list_forecasts()
    assert len(rows) == 2
    added = next(f for f in rows if f["security_id"] == "000001.SZ")
    assert (added["direction"], added["thesis_id"]) == ("short", fx.FALLBACK_THESIS_ID)

    # garbage event: handler never raises, never writes
    await fx._on_research_completed(_event("research.completed", "", {}))
    assert len(await forecasts.list_forecasts()) == 2


async def test_daily_workflow_handler_and_register(tmp_path):
    await _seed_universe()
    now = bus.now_iso()
    ws = tmp_path / "ws-daily"
    ws.mkdir()
    (ws / "每日日报.md").write_text("市场综述……我们看多腾讯控股。", encoding="utf-8")
    await db.execute(
        "INSERT INTO sessions (id, title, kind, workspace_dir, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)", ("sess-d", "日报", "workflow", str(ws), now, now),
    )

    # non-daily workflows are ignored — no claim, no forecasts
    await fx._on_workflow_completed(_event(
        "workflow.completed", "run-b",
        {"workflow_id": "briefing", "run_id": "run-b", "session_id": "sess-d"},
    ))
    assert await forecasts.list_forecasts() == []
    assert await db.query_one("SELECT id FROM forecast_extractions") is None

    await fx._on_workflow_completed(_event(
        "workflow.completed", "run-d",
        {"workflow_id": "daily", "run_id": "run-d", "session_id": "sess-d"},
    ))
    rows = await forecasts.list_forecasts()
    assert [f["security_id"] for f in rows] == ["0700.HK"]
    claim = await db.query_one(
        "SELECT * FROM forecast_extractions WHERE source_ref = ?", ("workflow:run-d",))
    assert claim["source_kind"] == "daily"

    # no workspace file: degrade to the payload's step summaries
    await fx._on_workflow_completed(_event(
        "workflow.completed", "run-d2",
        {"workflow_id": "daily", "run_id": "run-d2",
         "results": [{"step_id": "01", "summary": "看多英伟达"}]},
    ))
    assert {f["security_id"] for f in await forecasts.list_forecasts()} == {"0700.HK", "NVDA.US"}

    # register() wires exactly the two hooks; restore the process-wide bus after
    before = list(bus._handlers)
    try:
        fx.register()
        added = [(p, h) for (p, h) in bus._handlers if (p, h) not in before]
        assert {(p, h.__name__) for p, h in added} == {
            ("research.completed", "_on_research_completed"),
            ("workflow.completed", "_on_workflow_completed"),
        }
    finally:
        bus._handlers[:] = before
