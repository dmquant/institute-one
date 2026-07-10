"""Security master schema (card M2-001): canonical ids, aliases, thesis edges.

Schema-level tests only — the domain module, API, and importer are later cards.
The autouse ``app_runtime`` fixture applies migrations (db.init()) with
foreign_keys=ON, so CHECK and FK failures surface as sqlite3.IntegrityError.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import bus, db


async def _mk_security(
    sid: str,
    *,
    market: str,
    instrument_type: str = "stock",
    symbol: str | None = None,
    name_zh: str | None = None,
    name_en: str | None = "some name",
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, instrument_type, name_zh, name_en, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (sid, symbol or sid.split(".")[0], market, instrument_type, name_zh, name_en, now, now),
    )


async def _mk_alias(aid: str, security_id: str, alias: str, kind: str) -> None:
    await db.execute(
        "INSERT INTO security_aliases (id, security_id, alias, kind, created_at) VALUES (?,?,?,?,?)",
        (aid, security_id, alias, kind, bus.now_iso()),
    )


async def _mk_thesis(tid: str = "ai/gpu") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO theses (id, slug, name_zh, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (tid, tid, "国产 GPU", "active", now, now),
    )


async def _mk_edge(
    eid: str,
    thesis_id: str,
    security_id: str,
    *,
    role: str = "pure_play",
    bucket: str | None = None,
    exposure: float = 0.5,
    confidence: str = "medium",
    rationale: str = "",
    import_id: str | None = None,
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO thesis_security_edges "
        "(id, thesis_id, security_id, role, bucket, exposure, confidence, rationale, import_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (eid, thesis_id, security_id, role, bucket, exposure, confidence, rationale, import_id, now, now),
    )


# ---- canonical id scheme ----------------------------------------------------

async def test_canonical_suffixes_accepted_for_all_markets():
    # CN_A covers all three domestic suffixes (02-thesis-stock-model.md id scheme)
    await _mk_security("688256.SH", market="CN_A", name_zh="寒武纪", name_en=None)
    await _mk_security("000001.SZ", market="CN_A", name_zh="平安银行", name_en=None)
    await _mk_security("830799.BJ", market="CN_A", name_zh="艾融软件", name_en=None)
    await _mk_security("0700.HK", market="HK", name_zh="腾讯控股", name_en="Tencent")
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    # context-only markets keep native suffixes (bundle: 005930.KS, 6954.T)
    await _mk_security("005930.KS", market="GLOBAL_CONTEXT", name_en="Samsung Electronics")
    await _mk_security("6954.T", market="GLOBAL_CONTEXT", name_en="Fanuc")
    rows = await db.query("SELECT id, market FROM securities ORDER BY id")
    assert len(rows) == 7


async def test_suffix_must_agree_with_market():
    for sid, market in (
        ("NVDA.US", "CN_A"),   # wrong suffix for market
        ("688256.SH", "US"),
        ("0700.HK", "CN_A"),
        ("NVDA", "US"),        # unsuffixed — importer must append .US
        ("688256", "CN_A"),
        ("nvda.us", "US"),     # suffix is case-sensitive (GLOB, not LIKE)
        # reserved canonical suffixes may not hide under the catch-all market
        ("600519.SH", "GLOBAL_CONTEXT"),
        ("0700.HK", "GLOBAL_CONTEXT"),
        ("NVDA.US", "GLOBAL_CONTEXT"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await _mk_security(sid, market=market)


async def test_id_must_agree_with_symbol():
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_security("600519.SH", market="CN_A", symbol="FOO", name_zh="贵州茅台", name_en=None)
    await _mk_security("600519.SH", market="CN_A", symbol="600519", name_zh="贵州茅台", name_en=None)


# ---- market normalization ----------------------------------------------------

async def test_market_check_rejects_raw_bundle_values():
    # stocks.json market strings must normalize BEFORE insert
    # (10-market-thesis-data-bootstrap.md normalization table)
    for sid, market in (
        ("600000.SH", "A-share"),
        ("510300.SH", "A-share ETF"),
        ("SPY.US", "US ETF"),
        ("TSM.US", "US ADR"),
        ("005930.KS", "Korea"),
        ("6954.T", "Japan"),
        ("XXX.US", "NASDAQ"),  # arbitrary junk
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await _mk_security(sid, market=market)


async def test_instrument_type_normalization():
    # ETF/ADR land as instrument_type on the normalized market
    await _mk_security("510300.SH", market="CN_A", instrument_type="ETF", name_zh="沪深300 ETF", name_en=None)
    await _mk_security("2800.HK", market="HK", instrument_type="ETF", name_zh="盈富基金", name_en=None)
    await _mk_security("IEF.US", market="US", instrument_type="ETF", name_en="iShares 7-10 Year Treasury Bond ETF")
    await _mk_security("TSM.US", market="US", instrument_type="ADR", name_en="TSMC")
    rows = await db.query("SELECT id FROM securities WHERE instrument_type IN ('ETF','ADR')")
    assert len(rows) == 4

    with pytest.raises(sqlite3.IntegrityError):
        await _mk_security("SPY.US", market="US", instrument_type="US ETF")  # raw bundle string
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_security("QQQ.US", market="US", instrument_type="fund")


async def test_security_requires_at_least_one_name():
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_security("600000.SH", market="CN_A", name_zh=None, name_en=None)
    # empty strings do not satisfy the guard either
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_security("600000.SH", market="CN_A", name_zh="", name_en=None)
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_security("600000.SH", market="CN_A", name_zh="", name_en="")


# ---- aliases ------------------------------------------------------------------

async def test_alias_round_trip_chinese_name_and_unsuffixed_ticker():
    await _mk_security("688256.SH", market="CN_A", name_zh="寒武纪", name_en=None)
    await _mk_alias("a-1", "688256.SH", "寒武纪", "name_zh")
    await _mk_alias("a-2", "688256.SH", "688256", "ticker")

    for alias, kind in (("寒武纪", "name_zh"), ("688256", "ticker")):
        row = await db.query_one(
            "SELECT s.id, s.market FROM security_aliases a JOIN securities s ON s.id = a.security_id "
            "WHERE a.alias = ? AND a.kind = ?",
            (alias, kind),
        )
        assert row is not None
        assert row["id"] == "688256.SH"
        assert row["market"] == "CN_A"


async def test_alias_unique_within_kind_not_across_kinds():
    await _mk_security("0700.HK", market="HK", name_zh="腾讯控股", name_en=None)
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    await _mk_alias("a-1", "0700.HK", "0700", "ticker")

    # same (alias, kind) — even for a different security — is ambiguous: rejected
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_alias("a-dup", "NVDA.US", "0700", "ticker")
    # same alias text under a different kind is allowed by contract
    await _mk_alias("a-2", "0700.HK", "0700", "abbreviation")

    with pytest.raises(sqlite3.IntegrityError):
        await _mk_alias("a-bad-kind", "0700.HK", "Tencent ADR", "nickname")  # not a contract kind


async def test_alias_fk_enforced_and_cascades():
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_alias("a-orphan", "600000.SH", "浦发银行", "name_zh")

    await _mk_security("600000.SH", market="CN_A", name_zh="浦发银行", name_en=None)
    await _mk_alias("a-1", "600000.SH", "浦发银行", "name_zh")
    await db.execute("DELETE FROM securities WHERE id = ?", ("600000.SH",))
    assert await db.query_one("SELECT id FROM security_aliases WHERE id = 'a-1'") is None


# ---- thesis-security edges ------------------------------------------------------

async def test_edge_stores_role_exposure_confidence_rationale():
    await _mk_thesis("ai/gpu")
    await _mk_security("688256.SH", market="CN_A", name_zh="寒武纪", name_en=None)
    await _mk_edge(
        "e-1", "ai/gpu", "688256.SH",
        role="pure_play", bucket="core", exposure=0.9, confidence="high",
        rationale="国产 AI 训练芯片直接受益者",
    )
    # raw bundle labels (free-text Chinese roles) are storable as-is
    await _mk_edge("e-2", "ai/gpu", "688256.SH", role="中长久期代理", bucket="peer", exposure=0.3)

    row = await db.query_one("SELECT * FROM thesis_security_edges WHERE id = 'e-1'")
    assert row["role"] == "pure_play"
    assert row["bucket"] == "core"
    assert row["exposure"] == 0.9
    assert row["confidence"] == "high"
    assert row["rationale"] == "国产 AI 训练芯片直接受益者"
    assert row["status"] == "active"


async def test_edge_unique_per_thesis_security_role():
    await _mk_thesis("ai/gpu")
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    await _mk_edge("e-1", "ai/gpu", "NVDA.US", role="global_leader")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_edge("e-dup", "ai/gpu", "NVDA.US", role="global_leader")
    # same pair under a different role is a different relationship
    await _mk_edge("e-2", "ai/gpu", "NVDA.US", role="read_through")


async def test_edge_value_checks():
    await _mk_thesis("ai/gpu")
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    for kwargs in (
        {"exposure": 1.5},
        {"exposure": -0.1},
        {"confidence": "certain"},
        {"bucket": "basket"},  # bucket set = observed csv values: core|watch|peer|hedge
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await _mk_edge("e-bad", "ai/gpu", "NVDA.US", **kwargs)
    for i, bucket in enumerate(("core", "watch", "peer", "hedge")):
        await _mk_edge(f"e-{i}", "ai/gpu", "NVDA.US", role=f"role-{i}", bucket=bucket)


async def test_edge_fk_enforcement_and_provenance():
    await _mk_thesis("ai/gpu")
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")

    with pytest.raises(sqlite3.IntegrityError):
        await _mk_edge("e-bad", "no/such/thesis", "NVDA.US")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_edge("e-bad", "ai/gpu", "600000.SH")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_edge("e-bad", "ai/gpu", "NVDA.US", import_id="imp-missing")

    now = bus.now_iso()
    await db.execute(
        "INSERT INTO market_thesis_imports (id, schema, generated_at, status, imported_at) VALUES (?,?,?,?,?)",
        ("imp-1", "researchos.market_thesis_export.v1", now, "completed", now),
    )
    await _mk_edge("e-1", "ai/gpu", "NVDA.US", import_id="imp-1")
    row = await db.query_one("SELECT import_id FROM thesis_security_edges WHERE id = 'e-1'")
    assert row["import_id"] == "imp-1"

    # deleting the security removes its edges (rows are truth; edges follow)
    await db.execute("DELETE FROM securities WHERE id = ?", ("NVDA.US",))
    assert await db.query_one("SELECT id FROM thesis_security_edges WHERE id = 'e-1'") is None


async def test_edge_conditional_claim_retire():
    # house idiom: UPDATE … WHERE status=<expected> — re-entrant, restart-safe
    await _mk_thesis("ai/gpu")
    await _mk_security("NVDA.US", market="US", name_en="NVIDIA")
    await _mk_edge("e-1", "ai/gpu", "NVDA.US")
    now = bus.now_iso()
    claimed = await db.execute(
        "UPDATE thesis_security_edges SET status='retired', updated_at=? WHERE id=? AND status='active'",
        (now, "e-1"),
    )
    assert claimed == 1
    again = await db.execute(
        "UPDATE thesis_security_edges SET status='retired', updated_at=? WHERE id=? AND status='active'",
        (now, "e-1"),
    )
    assert again == 0
