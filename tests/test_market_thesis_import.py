"""market_thesis_import: provenance schema (card M1-001) + the importer (M1-003).

The first block is schema-level (mirrors the documented bundle contract without
reading market-thesis-data/). The importer block DOES read the real bundle —
commercial data kept OUTSIDE the repo and located via the
INSTITUTE_THESIS_BUNDLE env var (in-repo market-thesis-data/ as legacy
fallback) — so those two integration tests skip when the bundle is absent.
The apply tests run on a SELF-CONTAINED subset fixture mirroring documented
bundle entries and need no real bundle (S4-P0-02: the old fixture derived the
subset FROM the real bundle, which over-skipped six bundle-independent tests
under one marker).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app import bus, db
from app.institute.market_thesis_import import MarketThesisImportError, import_bundle

MANIFEST = {
    "schema": "researchos.market_thesis_export.manifest.v1",
    "generated_at": "2026-07-01T01:45:20.376Z",
    "source_schema": "vibe.ai_institute.public_research_network.v1",
    "source_generated_at": "2026-06-30T13:20:12.832Z",
    "source_first_date": "2026-04-23",
    "source_last_date": "2026-06-30",
    "thesis_count": 74,
    "lane_count": 55,
    "stock_count": 236,
    "edge_count": 1888,
    "thesis_stock_edge_count": 1020,
}


async def _mk_import(
    iid: str,
    *,
    mode: str = "apply",
    status: str = "running",
    key: str | None = None,
    sha256: str = "deadbeef" * 8,
) -> None:
    await db.execute(
        "INSERT INTO market_thesis_imports (id, schema, generated_at, source_schema, source_generated_at, "
        "source_first_date, source_last_date, thesis_count, lane_count, stock_count, edge_count, "
        "thesis_stock_edge_count, bundle_sha256, idempotency_key, mode, status, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            iid, MANIFEST["schema"], MANIFEST["generated_at"], MANIFEST["source_schema"],
            MANIFEST["source_generated_at"], MANIFEST["source_first_date"], MANIFEST["source_last_date"],
            MANIFEST["thesis_count"], MANIFEST["lane_count"], MANIFEST["stock_count"],
            MANIFEST["edge_count"], MANIFEST["thesis_stock_edge_count"],
            sha256, key, mode, status, bus.now_iso(),
        ),
    )


async def _mk_item(
    item_id: str,
    import_id: str,
    item_type: str,
    external_id: str,
    *,
    local_id: str | None = None,
    status: str = "inserted",
    message: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO market_thesis_import_items (id, import_id, item_type, external_id, local_id, status, "
        "message, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (item_id, import_id, item_type, external_id, local_id, status, message, bus.now_iso()),
    )


# ---- batches -----------------------------------------------------------------

async def test_batch_row_holds_manifest_counts():
    await _mk_import("imp-1", key="v1:2026-07-01:deadbeef")
    row = await db.query_one("SELECT * FROM market_thesis_imports WHERE id=?", ("imp-1",))
    assert row["schema"] == MANIFEST["schema"]
    assert (row["thesis_count"], row["lane_count"], row["stock_count"]) == (74, 55, 236)
    assert (row["edge_count"], row["thesis_stock_edge_count"]) == (1888, 1020)
    assert row["source_first_date"] == "2026-04-23"
    assert row["finished_at"] is None


async def test_idempotency_key_unique_for_completed_and_null_repeats():
    await _mk_import("imp-1", key="v1:2026-07-01:deadbeef", status="completed")
    with pytest.raises(sqlite3.IntegrityError):
        # same bundle re-applied after a COMPLETED run is blocked
        await _mk_import("imp-2", key="v1:2026-07-01:deadbeef", status="completed")

    # dry-runs leave the key NULL and may repeat freely
    await _mk_import("dry-1", mode="dry_run", key=None)
    await _mk_import("dry-2", mode="dry_run", key=None)
    rows = await db.query("SELECT id FROM market_thesis_imports WHERE mode='dry_run'")
    assert len(rows) == 2


async def test_failed_apply_does_not_block_retry():
    """Idempotency is a partial unique index over status='completed' only — a
    failed apply must never brick a re-run of the same bundle."""
    await _mk_import("imp-1", key="v1:2026-07-01:deadbeef", status="failed")

    # retry with the SAME key: insert succeeds, and the completion claim lands
    await _mk_import("imp-2", key="v1:2026-07-01:deadbeef", status="running")
    claimed = await db.execute(
        "UPDATE market_thesis_imports SET status='completed', finished_at=? WHERE id=? AND status='running'",
        (bus.now_iso(), "imp-2"),
    )
    assert claimed == 1

    # once the retry completes, the key IS occupied: a further apply conflicts
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_import("imp-3", key="v1:2026-07-01:deadbeef", status="completed")


async def test_mode_and_status_checks_enforced():
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_import("imp-bad", mode="preview")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_import("imp-bad", status="pending")


async def test_completion_is_a_conditional_claim():
    await _mk_import("imp-1", status="running")
    now = bus.now_iso()
    claimed = await db.execute(
        "UPDATE market_thesis_imports SET status='completed', finished_at=? WHERE id=? AND status='running'",
        (now, "imp-1"),
    )
    assert claimed == 1
    again = await db.execute(
        "UPDATE market_thesis_imports SET status='completed', finished_at=? WHERE id=? AND status='running'",
        (now, "imp-1"),
    )
    assert again == 0  # a second claimer (restart recovery) must see 0 rows


# ---- items -------------------------------------------------------------------

async def test_item_rows_unique_per_batch_and_counted():
    await _mk_import("imp-1")
    await _mk_item("it-1", "imp-1", "lane", "ai", local_id="ai")
    await _mk_item("it-2", "imp-1", "thesis", "thesis-05c3f6f33c", local_id="thesis-05c3f6f33c")
    await _mk_item("it-3", "imp-1", "stock", "NVDA", local_id="NVDA.US", status="updated")
    await _mk_item("it-4", "imp-1", "edge", "edge-19a5fd38e40f", status="failed",
                   message="edge references unknown ticker")  # failed items carry no local_id

    # replaying the same bundle record into the same batch is a conflict
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-5", "imp-1", "lane", "ai", local_id="ai")

    # the same external id in a NEW batch is fine (re-import creates a new batch)
    await _mk_import("imp-2")
    await _mk_item("it-6", "imp-2", "lane", "ai", local_id="ai", status="skipped")

    counts = {
        r["status"]: r["n"]
        for r in await db.query(
            "SELECT status, COUNT(*) AS n FROM market_thesis_import_items WHERE import_id=? GROUP BY status",
            ("imp-1",),
        )
    }
    assert counts == {"inserted": 2, "updated": 1, "failed": 1}


async def test_item_type_and_status_checks_and_fk():
    await _mk_import("imp-1")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-bad", "imp-1", "security", "NVDA")  # not lane|thesis|stock|edge
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-bad", "imp-1", "stock", "NVDA", status="imported")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-orphan", "no-such-import", "stock", "NVDA")


async def test_items_cascade_with_batch():
    await _mk_import("imp-1")
    await _mk_item("it-1", "imp-1", "thesis", "thesis-029ce03da1", local_id="thesis-029ce03da1")
    await db.execute("DELETE FROM market_thesis_imports WHERE id=?", ("imp-1",))
    assert await db.query("SELECT id FROM market_thesis_import_items WHERE import_id=?", ("imp-1",)) == []


# ---- the importer (card M1-003) ------------------------------------------------
# The real dataset contains commercial data and never enters the repo (card
# M1-003 contract): it is imported from an EXTERNAL path via the
# INSTITUTE_THESIS_BUNDLE env var, with the legacy in-repo location
# market-thesis-data/bundle.json as fallback. ONLY the two integration tests
# that read the real bundle skip when neither is present; the apply tests use
# the self-contained subset fixture below (S4-P0-02).

_BUNDLE_ENV = os.environ.get("INSTITUTE_THESIS_BUNDLE", "").strip()
REAL_BUNDLE = (Path(_BUNDLE_ENV).expanduser() if _BUNDLE_ENV
               else Path(__file__).resolve().parent.parent / "market-thesis-data" / "bundle.json")
requires_bundle = pytest.mark.skipif(
    not REAL_BUNDLE.exists(),
    reason="thesis bundle not present (set INSTITUTE_THESIS_BUNDLE or provide market-thesis-data/bundle.json)")

DOMAIN_TABLES = ("theses", "thesis_versions", "securities", "security_aliases",
                 "thesis_security_edges", "market_thesis_import_items")
SUBSET_LANES = {"ai", "index-rebalance-and-passive-flow-events"}
SUBSET_THESES = {"thesis-029ce03da1", "thesis-05c3f6f33c"}


async def _count(table: str, where: str = "1=1", params: tuple = ()) -> int:
    row = await db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
    return row["n"]


def _subset_bundle() -> dict:
    """SELF-CONTAINED apply fixture mirroring documented bundle entry shapes
    (roadmap/07-market-thesis-data-kickoff.md; no real bundle required —
    S4-P0-02): 2 lanes, their 2 theses, the securities those theses track,
    the documented cross-listed zh-name collisions (0004 IMPORTER WARNING:
    中芯国际/中远海控) plus the context-only Korea/Japan names so both warn
    paths fire, and every edge kind confined to them."""
    lanes = [
        {"id": "ai", "lane": "AI 与算力", "laneEn": "AI & Compute",
         "href": "https://example.invalid/lane/ai",
         "firstSeen": "2026-04-23", "lastSeen": "2026-06-30",
         "avgConviction": 55.5, "topTerms": ["AI", "算力", "推理"],
         "actionDistribution": {"deep_research_candidate": 1, "watch": 1},
         "stockTickers": ["NVDA", "0700.HK"]},
        {"id": "index-rebalance-and-passive-flow-events",
         "lane": "指数调仓与被动资金流事件",
         "laneEn": "Index rebalance & passive flow events",
         "href": "https://example.invalid/lane/index-rebalance",
         "firstSeen": "2026-04-23", "lastSeen": "2026-06-30",
         "avgConviction": 62.0, "topTerms": ["调仓", "被动流"],
         "actionDistribution": {"deep_research_candidate": 1},
         "stockTickers": ["MCHI", "510300.SH"]},
    ]
    theses = [
        {"id": "thesis-029ce03da1", "title": "MSCI 中国调仓的被动资金流错价",
         "titleEn": "Passive-flow mispricing around MSCI China rebalances",
         "laneId": "index-rebalance-and-passive-flow-events",
         "coreView": "指数调仓日的被动资金流在离岸中国资产上造成可预测的短时错价窗口。",
         "direction": "conflicting", "directionLabel": "分歧", "conviction": 90,
         "firstSeen": "2026-04-23", "lastSeen": "2026-06-30",
         "href": "https://example.invalid/thesis/029ce03da1",
         "networkHref": "https://example.invalid/network/029ce03da1",
         "practical": {"score": 72, "actionCode": "deep_research_candidate",
                       "riskBudget": "small",
                       "signals": ["调仓日成交放大", "跟踪误差收敛"]},
         "stockUniverse": ["MCHI", "510300.SH"],
         "investableFocus": "离岸中国 ETF 与沪深300 ETF 的调仓窗口"},
        {"id": "thesis-05c3f6f33c", "title": "AI 推理算力供需缺口",
         "titleEn": "AI inference compute supply gap",
         "laneId": "ai",
         "coreView": "推理侧算力需求增速仍高于供给端交付节奏，头部芯片与云应用受益。",
         "direction": "bullish", "directionLabel": "看多", "conviction": 60,
         "firstSeen": "2026-05-02", "lastSeen": "2026-06-30",
         "href": "https://example.invalid/thesis/05c3f6f33c",
         "networkHref": "https://example.invalid/network/05c3f6f33c",
         "practical": {"score": 55, "actionCode": "watch", "riskBudget": "small",
                       "signals": ["交付周期", "云资本开支"]},
         "stockUniverse": ["NVDA", "0700.HK"],
         "investableFocus": "AI 算力供应链龙头"},
    ]
    stocks = [
        {"ticker": "NVDA", "name": "英伟达", "market": "US",
         "href": "https://example.invalid/stock/NVDA", "thesisCount": 1},
        {"ticker": "0700.HK", "name": "腾讯控股", "market": "HK",
         "href": "https://example.invalid/stock/0700.HK", "thesisCount": 1},
        {"ticker": "MCHI", "name": "MSCI China ETF", "market": "US ETF",
         "href": "https://example.invalid/stock/MCHI", "thesisCount": 2},
        {"ticker": "510300.SH", "name": "沪深300ETF", "market": "A-share ETF",
         "href": "https://example.invalid/stock/510300.SH", "thesisCount": 1},
        # the documented cross-listed zh-name collision pairs
        {"ticker": "688981.SH", "name": "中芯国际", "market": "A-share", "thesisCount": 0},
        {"ticker": "0981.HK", "name": "中芯国际", "market": "HK", "thesisCount": 0},
        {"ticker": "601919.SH", "name": "中远海控", "market": "A-share", "thesisCount": 0},
        {"ticker": "1919.HK", "name": "中远海控", "market": "HK", "thesisCount": 0},
        # context-only markets keep their vendor suffix -> GLOBAL_CONTEXT + warning
        {"ticker": "005930.KS", "name": "三星电子", "market": "Korea", "thesisCount": 0},
        {"ticker": "6954.T", "name": "发那科", "market": "Japan", "thesisCount": 0},
    ]
    edges = [
        {"id": "edge-t1-mchi", "type": "tracks_stock",
         "source": "thesis-029ce03da1", "target": "MCHI", "role": "离岸中国风险",
         "bucket": "core", "weight": 3,
         "laneId": "index-rebalance-and-passive-flow-events"},
        {"id": "edge-t1-510300", "type": "tracks_stock",
         "source": "thesis-029ce03da1", "target": "510300.SH", "role": "沪深300被动流",
         "bucket": "core", "weight": 2,
         "laneId": "index-rebalance-and-passive-flow-events"},
        {"id": "edge-t2-nvda", "type": "tracks_stock",
         "source": "thesis-05c3f6f33c", "target": "NVDA", "role": "AI 算力龙头",
         "bucket": "core", "weight": 3, "laneId": "ai"},
        {"id": "edge-t2-0700", "type": "tracks_stock",
         "source": "thesis-05c3f6f33c", "target": "0700.HK", "role": "中国 AI 应用",
         "bucket": "watch", "weight": 1, "laneId": "ai"},
        {"id": "edge-bl-1", "type": "belongs_to_lane",
         "source": "thesis-029ce03da1", "target": "index-rebalance-and-passive-flow-events"},
        {"id": "edge-bl-2", "type": "belongs_to_lane",
         "source": "thesis-05c3f6f33c", "target": "ai"},
        {"id": "edge-lc-1", "type": "lane_contains_stock", "source": "ai", "target": "NVDA"},
        {"id": "edge-lc-2", "type": "lane_contains_stock", "source": "ai", "target": "0700.HK"},
        {"id": "edge-lc-3", "type": "lane_contains_stock",
         "source": "index-rebalance-and-passive-flow-events", "target": "MCHI"},
        {"id": "edge-lc-4", "type": "lane_contains_stock",
         "source": "index-rebalance-and-passive-flow-events", "target": "510300.SH"},
    ]
    tracks = [e for e in edges if e["type"] == "tracks_stock"]
    return {
        "schema": "researchos.market_thesis_export.bundle.v1",
        "generatedAt": "2026-07-01T01:45:20.376Z",
        "sourceSchema": "vibe.ai_institute.public_research_network.v1",
        "sourceGeneratedAt": "2026-06-30T13:20:12.832Z",
        "lanes": lanes, "theses": theses, "stocks": stocks, "edges": edges,
        "stats": {
            "laneCount": len(lanes), "thesisCount": len(theses),
            "stockCount": len(stocks), "edgeCount": len(edges),
            "thesisStockEdgeCount": len(tracks),
            "sourceDateRange": {"first": "2026-04-23", "last": "2026-06-30"},
        },
    }


@pytest.fixture
def subset(tmp_path):
    bundle = _subset_bundle()
    path = tmp_path / "subset-bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path, bundle


@requires_bundle
async def test_dry_run_real_bundle_reports_counts_and_writes_nothing():
    report = await import_bundle(REAL_BUNDLE, mode="dry_run")
    assert (report["mode"], report["status"]) == ("dry_run", "completed")
    # the manifest counts (roadmap/07-market-thesis-data-kickoff.md)
    assert report["counts"] == {"lanes": 55, "theses": 74, "stocks": 236,
                                "edges": 1888, "thesis_stock_edges": 1020}
    # what apply WOULD do
    assert report["actions"]["lanes"]["inserted"] == 55
    assert report["actions"]["theses"]["inserted"] == 74
    assert report["actions"]["stocks"]["inserted"] == 236
    assert report["actions"]["edges"] == {"inserted": 1020, "skipped": 74 + 794, "failed": 0}
    assert report["actions"]["aliases"]["skipped"] == 2  # 中芯国际 + 中远海控
    assert report["edge_kinds"]["tracks_stock"]["handling"] == "thesis_security_edges"
    assert "no schema home" in report["edge_kinds"]["lane_contains_stock"]["handling"]
    # warnings: alias collisions, context-only markets, all-conflicting directions
    assert any("中芯国际" in w for w in report["warnings"])
    assert any("Korea" in w for w in report["warnings"])
    assert any("conflicting" in w for w in report["warnings"])
    # no domain rows, no item rows — only the provenance batch row
    for table in DOMAIN_TABLES:
        assert await _count(table) == 0, table
    row = await db.query_one("SELECT * FROM market_thesis_imports WHERE id=?", (report["import_id"],))
    assert (row["mode"], row["status"], row["idempotency_key"]) == ("dry_run", "completed", None)
    assert (row["thesis_count"], row["lane_count"], row["stock_count"]) == (74, 55, 236)
    assert (row["edge_count"], row["thesis_stock_edge_count"]) == (1888, 1020)
    assert json.loads(row["manifest_json"])["stats"]["thesisCount"] == 74
    # dry-runs repeat freely (idempotency_key stays NULL)
    again = await import_bundle(REAL_BUNDLE, mode="dry_run")
    assert again["status"] == "completed"


async def test_apply_subset_inserts_every_entity_kind(subset):
    path, bundle = subset
    report = await import_bundle(path, mode="apply")
    assert (report["mode"], report["status"]) == ("apply", "completed")
    tracks = [e for e in bundle["edges"] if e["type"] == "tracks_stock"]
    skipped_kinds = len(bundle["edges"]) - len(tracks)

    # lanes are theses rows with kind='lane'
    lanes = await db.query("SELECT * FROM theses WHERE kind='lane' ORDER BY id")
    assert {l["id"] for l in lanes} == SUBSET_LANES
    assert all(l["status"] == "active" and l["source"] == "market_thesis_import" for l in lanes)
    assert lanes[0]["scope"] == "Imported lane from market-thesis-data"

    # theses hang under their lane, seeded with version 1 = coreView
    src = {t["id"]: t for t in bundle["theses"]}
    thesis = await db.query_one("SELECT * FROM theses WHERE id=?", ("thesis-029ce03da1",))
    assert thesis["parent_id"] == "index-rebalance-and-passive-flow-events"
    assert thesis["current_view"] == "conflicting"
    assert thesis["conviction_score"] == 90
    assert thesis["alpha_prior_score"] == src["thesis-029ce03da1"]["practical"]["score"]
    ver = await db.query_one("SELECT * FROM thesis_versions WHERE thesis_id=?", ("thesis-029ce03da1",))
    assert ver["version"] == 1 and ver["summary"] == src["thesis-029ce03da1"]["coreView"]
    assert json.loads(ver["stock_map_json"]) == src["thesis-029ce03da1"]["stockUniverse"]
    assert await _count("theses", "kind='thesis'") == len(bundle["theses"])

    # securities: canonical-id normalization per market
    tencent = await db.query_one("SELECT * FROM securities WHERE id=?", ("0700.HK",))
    assert (tencent["symbol"], tencent["market"], tencent["name_zh"]) == ("0700", "HK", "腾讯控股")
    assert (tencent["exchange"], tencent["currency"]) == ("HKEX", "HKD")
    mchi = await db.query_one("SELECT * FROM securities WHERE id=?", ("MCHI.US",))
    assert (mchi["symbol"], mchi["market"], mchi["instrument_type"]) == ("MCHI", "US", "ETF")
    csi300 = await db.query_one("SELECT * FROM securities WHERE id=?", ("510300.SH",))
    assert (csi300["market"], csi300["instrument_type"], csi300["exchange"]) == ("CN_A", "ETF", "SSE")
    samsung = await db.query_one("SELECT * FROM securities WHERE id=?", ("005930.KS",))
    assert samsung["market"] == "GLOBAL_CONTEXT"
    assert any("Korea" in w for w in report["warnings"])
    assert await _count("securities") == len(bundle["stocks"])

    # aliases: unsuffixed ticker + script-detected name
    a = await db.query_one("SELECT security_id FROM security_aliases WHERE alias=? AND kind='ticker'", ("NVDA",))
    assert a["security_id"] == "NVDA.US"
    a = await db.query_one("SELECT security_id FROM security_aliases WHERE alias=? AND kind='name_zh'", ("腾讯控股",))
    assert a["security_id"] == "0700.HK"
    a = await db.query_one("SELECT security_id FROM security_aliases WHERE alias=? AND kind='ticker'", ("0700",))
    assert a["security_id"] == "0700.HK"

    # tracks_stock edges land in thesis_security_edges with role/bucket/weight/exposure
    assert await _count("thesis_security_edges") == len(tracks)
    edge = await db.query_one(
        "SELECT * FROM thesis_security_edges WHERE thesis_id=? AND security_id=?",
        ("thesis-029ce03da1", "MCHI.US"),
    )
    assert (edge["role"], edge["bucket"], edge["weight"], edge["exposure"]) == ("离岸中国风险", "core", 3.0, 1.0)
    assert edge["import_id"] == report["import_id"]

    # the other edge kinds are counted + warned, never dropped silently
    assert report["edge_kinds"]["belongs_to_lane"]["skipped"] == 2
    assert "parent_id" in report["edge_kinds"]["belongs_to_lane"]["handling"]
    assert report["edge_kinds"]["lane_contains_stock"]["skipped"] == \
        report["edge_kinds"]["lane_contains_stock"]["count"] > 0
    assert any("lane_contains_stock" in w for w in report["warnings"])

    # per-item provenance covers every bundle record
    items = {(r["item_type"], r["status"]): r["n"] for r in await db.query(
        "SELECT item_type, status, COUNT(*) AS n FROM market_thesis_import_items "
        "WHERE import_id=? GROUP BY item_type, status", (report["import_id"],))}
    assert items[("lane", "inserted")] == len(bundle["lanes"])
    assert items[("thesis", "inserted")] == len(bundle["theses"])
    assert items[("stock", "inserted")] == len(bundle["stocks"])
    assert items[("edge", "inserted")] == len(tracks)
    assert items[("edge", "skipped")] == skipped_kinds

    # batch row completed with an idempotency key; completion event emitted
    row = await db.query_one("SELECT * FROM market_thesis_imports WHERE id=?", (report["import_id"],))
    assert (row["status"], row["mode"]) == ("completed", "apply")
    assert row["idempotency_key"] == report["idempotency_key"] is not None
    event = await db.query_one("SELECT * FROM events WHERE type='market_thesis_import.completed'")
    assert json.loads(event["payload"])["counts"]["thesis_stock_edges"] == len(tracks)


async def test_apply_preserves_practical_metadata(subset):
    path, bundle = subset
    await import_bundle(path, mode="apply")
    src = {t["id"]: t for t in bundle["theses"]}["thesis-029ce03da1"]
    thesis = await db.query_one("SELECT metadata_json FROM theses WHERE id=?", ("thesis-029ce03da1",))
    meta = json.loads(thesis["metadata_json"])
    assert meta["practical"] == src["practical"]  # actionCode, riskBudget, signals, ... verbatim
    assert meta["directionLabel"] == "分歧"
    assert meta["stockUniverse"] == src["stockUniverse"]
    assert meta["investableFocus"] == src["investableFocus"]
    lane = await db.query_one("SELECT metadata_json FROM theses WHERE id=?", ("ai",))
    lane_meta = json.loads(lane["metadata_json"])
    assert lane_meta["avgConviction"] == 55.5
    assert lane_meta["topTerms"] and lane_meta["actionDistribution"]
    sec = await db.query_one("SELECT metadata_json FROM securities WHERE id=?", ("MCHI.US",))
    sec_meta = json.loads(sec["metadata_json"])
    assert sec_meta["market"] == "US ETF"  # raw market string survives in metadata
    assert isinstance(sec_meta["thesisCount"], int)
    edge = await db.query_one(
        "SELECT metadata_json FROM thesis_security_edges WHERE thesis_id=? AND security_id=?",
        ("thesis-029ce03da1", "MCHI.US"))
    edge_meta = json.loads(edge["metadata_json"])
    assert (edge_meta["type"], edge_meta["laneId"]) == ("tracks_stock", "index-rebalance-and-passive-flow-events")


async def test_alias_collision_warns_and_skips(subset):
    path, _ = subset
    report = await import_bundle(path, mode="apply")
    # both cross-listings exist; the duplicate zh-name alias resolves to exactly one
    for sid in ("688981.SH", "0981.HK", "601919.SH", "1919.HK"):
        assert await db.query_one("SELECT id FROM securities WHERE id=?", (sid,)) is not None
    rows = await db.query("SELECT security_id FROM security_aliases WHERE alias=? AND kind='name_zh'", ("中芯国际",))
    assert len(rows) == 1
    assert report["actions"]["aliases"]["skipped"] == 2
    assert any("中芯国际" in w and "collides" in w for w in report["warnings"])
    # company_key groups the cross-listed pair
    keys = {r["company_key"] for r in await db.query(
        "SELECT company_key FROM securities WHERE id IN ('688981.SH','0981.HK')")}
    assert keys == {"中芯国际"}


async def test_apply_is_idempotent(subset):
    path, bundle = subset
    first = await import_bundle(path, mode="apply")
    n_theses = await _count("theses")
    with pytest.raises(MarketThesisImportError, match="refusing to re-apply"):
        await import_bundle(path, mode="apply")
    assert await _count("theses") == n_theses  # nothing changed
    batches = await db.query("SELECT id, status FROM market_thesis_imports WHERE mode='apply'")
    assert [(b["id"], b["status"]) for b in batches] == [(first["import_id"], "completed")]


async def test_failed_apply_rolls_back_and_retry_works(subset):
    path, bundle = subset
    # occupy the PRIMARY KEY of a bundle edge so the apply fails mid-transaction
    now = bus.now_iso()
    edge_id = next(e["id"] for e in bundle["edges"] if e["type"] == "tracks_stock")
    await db.execute(
        "INSERT INTO theses (id, kind, slug, name_zh, status, created_at, updated_at) "
        "VALUES ('t-x','thesis','t-x','占位','active',?,?)", (now, now))
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_en, created_at, updated_at) "
        "VALUES ('ZZZZ.US','ZZZZ','US','Placeholder',?,?)", (now, now))
    await db.execute(
        "INSERT INTO thesis_security_edges (id, thesis_id, security_id, role, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)", (edge_id, "t-x", "ZZZZ.US", "x", now, now))

    with pytest.raises(MarketThesisImportError, match="rolled back"):
        await import_bundle(path, mode="apply")
    failed = await db.query_one("SELECT * FROM market_thesis_imports WHERE mode='apply'")
    assert failed["status"] == "failed" and failed["error"]
    # zero domain writes: only the pre-existing placeholders remain
    assert await _count("theses") == 1
    assert await _count("securities") == 1
    assert await _count("thesis_security_edges") == 1
    assert await _count("market_thesis_import_items") == 0

    # a failed apply never blocks the retry (completed-only idempotency index)
    await db.execute("DELETE FROM thesis_security_edges WHERE id=?", (edge_id,))
    report = await import_bundle(path, mode="apply")
    assert report["status"] == "completed"
    assert await _count("theses", "kind='lane'") == len(bundle["lanes"])
    assert await _count("thesis_security_edges") == \
        sum(1 for e in bundle["edges"] if e["type"] == "tracks_stock")


async def test_conflicting_existing_title_fails_closed(subset):
    path, _ = subset
    now = bus.now_iso()
    await db.execute(  # a manual thesis already holds the 'ai' lane id with another name
        "INSERT INTO theses (id, kind, slug, name_zh, status, created_at, updated_at) "
        "VALUES ('ai','thesis','ai','Not the AI lane','active',?,?)", (now, now))
    with pytest.raises(MarketThesisImportError, match="conflicts with existing"):
        await import_bundle(path, mode="apply")
    row = await db.query_one("SELECT status, error FROM market_thesis_imports WHERE mode='apply'")
    assert row["status"] == "failed" and "conflicts" in row["error"]
    assert await _count("securities") == 0  # nothing was written


@requires_bundle
async def test_apply_real_bundle_full_counts():
    """Integration: the full 2.4MB bundle lands with the manifest counts."""
    report = await import_bundle(REAL_BUNDLE, mode="apply")
    assert report["status"] == "completed"
    assert await _count("theses", "kind='lane'") == 55
    assert await _count("theses", "kind='thesis'") == 74
    assert await _count("thesis_versions") == 74
    assert await _count("securities") == 236
    assert await _count("thesis_security_edges") == 1020
    assert await _count("market_thesis_import_items") == 55 + 74 + 236 + 1888
    assert await _count("securities", "market='CN_A'") == 75 + 5
    assert await _count("securities", "market='GLOBAL_CONTEXT'") == 3
    assert report["actions"]["aliases"]["skipped"] == 2
    # every thesis got a parent lane in the full bundle
    assert await _count("theses", "kind='thesis' AND parent_id IS NOT NULL") == 74
