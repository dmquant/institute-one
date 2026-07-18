"""market-thesis-data bundle import: dry-run, apply, idempotency, provenance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.institute import market_thesis_import as mti
from app.institute import securities, theses

MANIFEST = {
    "schema": "researchos.market_thesis_export.manifest.v1",
    "generatedAt": "2026-07-01T01:45:20.376Z",
    "counts": {"lanes": 2, "theses": 2, "stocks": 3, "thesisStockEdges": 3},
}

BUNDLE = {
    "lanes": [
        {"id": "lane-ai", "name": "AI 算力", "description": "训练与推理的算力供给"},
        {"id": "lane-cn", "name": "内需复苏"},
    ],
    "theses": [
        {
            "id": "th-hbm", "laneId": "lane-ai", "title": "HBM 供不应求",
            "view": "HBM 产能扩张跟不上需求", "direction": "conflicting",
            "practical": {"actionCode": "watch", "note": "关注海力士扩产"},
        },
        {
            "id": "th-baijiu", "laneId": "lane-cn", "title": "白酒去库存见底",
            "direction": "conflicting",
        },
    ],
    "stocks": [
        {"id": "st-600519", "ticker": "600519", "market": "A-share", "name": "贵州茅台"},
        {"id": "st-nvda", "ticker": "NVDA", "market": "US", "nameEn": "NVIDIA"},
        {"id": "st-513050", "ticker": "513050", "market": "A-share ETF", "name": "中概互联ETF"},
    ],
    "thesisStockEdges": [
        {"thesisId": "th-hbm", "stockId": "st-nvda", "role": "core", "confidence": 0.8, "rationale": "HBM 大买家"},
        {"thesisId": "th-baijiu", "stockId": "st-600519", "role": "core", "confidence": 0.7},
        {"thesisId": "th-hbm", "stockId": "st-missing"},  # unknown ref -> warn + skip
    ],
}


@pytest.fixture()
def bundle_dir(tmp_path: Path) -> Path:
    src = tmp_path / "market-thesis-data"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (src / "bundle.json").write_text(json.dumps(BUNDLE, ensure_ascii=False), encoding="utf-8")
    return src


# ---- dry-run -------------------------------------------------------------------

async def test_dry_run_reports_counts_and_writes_nothing(bundle_dir: Path):
    res = await mti.import_bundle(bundle_dir)
    assert res["mode"] == "dry_run"
    assert res["counts"]["lane"] == {"created": 2, "updated": 0, "unchanged": 0, "skipped": 0}
    assert res["counts"]["thesis"]["created"] == 2
    assert res["counts"]["stock"]["created"] == 3
    assert res["counts"]["edge"] == {"created": 2, "updated": 0, "unchanged": 0, "skipped": 1}
    assert any("st-missing" in w for w in res["warnings"])

    # nothing landed in domain tables
    assert await theses.list_theses() == []
    assert await securities.list_securities() == []
    # …but provenance did
    batches = await mti.list_batches()
    assert len(batches) == 1
    assert batches[0]["mode"] == "dry_run"
    assert batches[0]["counts"]["lane"]["created"] == 2


async def test_count_mismatch_and_missing_manifest_warn(bundle_dir: Path, tmp_path: Path):
    manifest = dict(MANIFEST, counts={"lanes": 55, "theses": 74, "stocks": 236, "thesisStockEdges": 1020})
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    res = await mti.import_bundle(bundle_dir)
    assert any("declares 55 lanes" in w for w in res["warnings"])

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "bundle.json").write_text(json.dumps(BUNDLE, ensure_ascii=False), encoding="utf-8")
    res = await mti.import_bundle(bare)
    assert any("manifest.json missing" in w for w in res["warnings"])


async def test_structural_validation_raises(bundle_dir: Path, tmp_path: Path):
    with pytest.raises(mti.BundleError, match="bundle file not found"):
        await mti.import_bundle(tmp_path / "nowhere")

    (bundle_dir / "manifest.json").write_text(json.dumps({"schema": "something.else.v9"}), encoding="utf-8")
    with pytest.raises(mti.BundleError, match="schema"):
        await mti.import_bundle(bundle_dir)

    (bundle_dir / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (bundle_dir / "bundle.json").write_text(json.dumps({"lanes": []}), encoding="utf-8")
    with pytest.raises(mti.BundleError, match="missing list sections"):
        await mti.import_bundle(bundle_dir)


# ---- apply + idempotency ---------------------------------------------------------

async def test_apply_imports_and_reapply_is_idempotent(bundle_dir: Path):
    res = await mti.import_bundle(bundle_dir, apply=True)
    assert res["mode"] == "apply"
    assert res["counts"]["lane"]["created"] == 2
    assert res["counts"]["thesis"]["created"] == 2
    assert res["counts"]["stock"]["created"] == 3
    assert res["counts"]["edge"]["created"] == 2

    # lanes + theses form the tree; practical metadata is preserved
    tree = await theses.tree()
    assert {n["title"] for n in tree} == {"AI 算力", "内需复苏"}
    ai_lane = next(n for n in tree if n["title"] == "AI 算力")
    assert [c["title"] for c in ai_lane["children"]] == ["HBM 供不应求"]
    hbm = await theses.get_thesis(ai_lane["children"][0]["id"])
    assert hbm["status"] == "candidate"           # hypotheses, not conclusions
    assert hbm["direction"] == "conflicting"
    assert hbm["meta"]["practical"]["actionCode"] == "watch"
    assert hbm["meta"]["bundle_id"] == "th-hbm"
    assert hbm["source"] == "import"
    assert [v["version"] for v in hbm["versions"]] == [1]

    # securities normalized + aliased
    moutai = await securities.find_security("贵州茅台")
    assert moutai["id"] == "600519.CN_A"
    etf = await securities.find_security("513050.CN_A")
    assert etf["instrument_type"] == "etf"
    assert (await securities.find_security("st-nvda"))["id"] == "NVDA.US"  # bundle id alias

    # edges carry role/confidence/rationale
    assert len(hbm["securities"]) == 1
    edge = hbm["securities"][0]
    assert (edge["role"], edge["confidence"], edge["rationale"]) == ("core", 0.8, "HBM 大买家")

    # provenance items recorded per applied row
    items = await db.query("SELECT item_type, action, COUNT(*) AS n FROM market_thesis_import_items "
                           "GROUP BY item_type, action ORDER BY item_type")
    assert {(r["item_type"], r["action"]): r["n"] for r in items} == {
        ("edge", "created"): 2, ("lane", "created"): 2,
        ("stock", "created"): 3, ("thesis", "created"): 2,
    }

    # re-apply: everything unchanged, no duplicates, no new versions
    res2 = await mti.import_bundle(bundle_dir, apply=True)
    for kind in ("lane", "thesis", "stock", "edge"):
        assert res2["counts"][kind]["created"] == 0
        assert res2["counts"][kind]["updated"] == 0
    assert len(await theses.list_theses()) == 4
    assert len(await securities.list_securities()) == 3
    hbm2 = await theses.get_thesis(hbm["id"])
    assert [v["version"] for v in hbm2["versions"]] == [1]

    # a changed view lands as an update + a new version, and local status survives
    await theses.set_status(hbm["id"], "active")   # -> version 2 (status change)
    data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    data["theses"][0]["view"] = "2027 年前 HBM 供需缺口都在"
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res3 = await mti.import_bundle(bundle_dir, apply=True)
    assert res3["counts"]["thesis"]["updated"] == 1
    hbm3 = await theses.get_thesis(hbm["id"])
    assert hbm3["status"] == "active"             # import never rewrites local lifecycle
    assert [v["version"] for v in hbm3["versions"]] == [1, 2, 3]
    assert hbm3["versions"][-1]["view"] == "2027 年前 HBM 供需缺口都在"
    assert hbm3["versions"][-1]["status"] == "active"  # version row carries the LOCAL status


async def test_dry_run_after_apply_reports_unchanged(bundle_dir: Path):
    await mti.import_bundle(bundle_dir, apply=True)
    res = await mti.import_bundle(bundle_dir)  # dry-run against populated DB
    assert res["counts"]["thesis"] == {"created": 0, "updated": 0, "unchanged": 2, "skipped": 0}
    assert res["counts"]["edge"]["unchanged"] == 2


async def test_corrected_ticker_recanonicalizes_and_edges_follow(bundle_dir: Path):
    """A later bundle fixing a stock's ticker/market must not leave a stale duplicate."""
    await mti.import_bundle(bundle_dir, apply=True)
    assert (await securities.find_security("st-nvda"))["id"] == "NVDA.US"

    # upstream corrects the NVDA row to an HK listing (hypothetical, but the
    # shape of the failure is real: canonical id changes for a known source id)
    data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    nvda = next(s for s in data["stocks"] if s["id"] == "st-nvda")
    nvda["ticker"] = "NVDH"
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res = await mti.import_bundle(bundle_dir, apply=True)
    assert res["counts"]["stock"]["updated"] == 1
    assert any("canonical id NVDA.US -> NVDH.US" in w for w in res["warnings"])

    # exactly one row for the source id; the old canonical row is gone
    assert (await securities.find_security("st-nvda"))["id"] == "NVDH.US"
    assert await securities.find_security("NVDA.US") is None
    assert len(await securities.list_securities(market="US")) == 1

    # the thesis edge followed the migration
    hbm_row = next(
        t for t in await theses.list_theses(kind="thesis") if t["meta"].get("bundle_id") == "th-hbm"
    )
    hbm = await theses.get_thesis(hbm_row["id"])
    assert [e["security_id"] for e in hbm["securities"]] == ["NVDH.US"]


async def test_recanonicalization_merges_onto_existing_security(bundle_dir: Path):
    """Correction collapses two source stocks onto ONE canonical id: the batch
    must merge (not die on the unique edge index) and keep a single edge."""
    data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    data["stocks"].append({"id": "st-nvda2", "ticker": "NVDA2", "market": "US", "nameEn": "NVIDIA dup"})
    data["thesisStockEdges"].append(
        {"thesisId": "th-hbm", "stockId": "st-nvda2", "role": "core", "confidence": 0.5}
    )
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await mti.import_bundle(bundle_dir, apply=True)
    assert (await securities.find_security("st-nvda2"))["id"] == "NVDA2.US"

    # upstream fixes the duplicate: st-nvda2 is actually NVDA too
    dup = next(s for s in data["stocks"] if s["id"] == "st-nvda2")
    dup["ticker"] = "NVDA"
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = await mti.import_bundle(bundle_dir, apply=True)  # must not roll back
    assert any("NVDA2.US -> NVDA.US" in w for w in res["warnings"])

    # one surviving security; both bundle ids resolve to it
    assert await securities.find_security("NVDA2.US") is None
    assert (await securities.find_security("st-nvda"))["id"] == "NVDA.US"
    assert (await securities.find_security("st-nvda2"))["id"] == "NVDA.US"

    # exactly one (thesis, NVDA.US, core) edge remains; the first import owner
    # keeps its payload, the duplicate source is skipped with a warning
    hbm_row = next(
        t for t in await theses.list_theses(kind="thesis") if t["meta"].get("bundle_id") == "th-hbm"
    )
    edges = (await theses.get_thesis(hbm_row["id"]))["securities"]
    assert [e["security_id"] for e in edges] == ["NVDA.US"]
    assert edges[0]["confidence"] == 0.8
    assert any("duplicate of import edge" in w for w in res["warnings"])

    # re-apply is TRULY idempotent after the merge (no flip-flop updates)
    res2 = await mti.import_bundle(bundle_dir, apply=True)
    assert res2["counts"]["stock"]["created"] == 0
    assert res2["counts"]["edge"]["created"] == 0
    assert res2["counts"]["edge"]["updated"] == 0
    assert (await theses.get_thesis(hbm_row["id"]))["securities"][0]["confidence"] == 0.8


async def test_recanonicalization_aborts_on_two_manual_edge_owners(bundle_dir: Path):
    """A canonical merge must never choose between two operator-owned rows."""
    data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    data["stocks"].append({"id": "st-nvda2", "ticker": "NVDA2", "market": "US"})
    data["thesisStockEdges"].append(
        {"thesisId": "th-hbm", "stockId": "st-nvda2", "role": "core", "rationale": "second"}
    )
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await mti.import_bundle(bundle_dir, apply=True)

    hbm = next(t for t in await theses.list_theses(kind="thesis") if t["meta"].get("bundle_id") == "th-hbm")
    await securities.upsert_edge(hbm["id"], "NVDA.US", role="core", rationale="manual-primary")
    await securities.upsert_edge(hbm["id"], "NVDA2.US", role="core", rationale="manual-secondary")

    # The upstream correction collapses NVDA2.US onto NVDA.US, where another
    # manual edge already owns the same (thesis, security, role) key.
    next(s for s in data["stocks"] if s["id"] == "st-nvda2")["ticker"] = "NVDA"
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(mti.BundleError, match="manual edge collision"):
        await mti.import_bundle(bundle_dir, apply=True)

    # The apply transaction rolled back: both operator rows and securities survive.
    assert await securities.find_security("NVDA.US") is not None
    assert await securities.find_security("NVDA2.US") is not None
    edges = (await theses.get_thesis(hbm["id"]))["securities"]
    assert {(e["security_id"], e["rationale"], e["source"]) for e in edges} == {
        ("NVDA.US", "manual-primary", "manual"),
        ("NVDA2.US", "manual-secondary", "manual"),
    }
    failed = (await mti.list_batches())[0]
    assert failed["status"] == "failed"
    assert any("manual edge collision" in warning for warning in failed["warnings"])


async def test_manual_edge_is_never_overwritten_by_bundle(bundle_dir: Path):
    await mti.import_bundle(bundle_dir, apply=True)
    hbm_row = next(
        t for t in await theses.list_theses(kind="thesis") if t["meta"].get("bundle_id") == "th-hbm"
    )
    manual = await securities.upsert_edge(
        hbm_row["id"], "NVDA.US", role="hedge", rationale="操作员手工判断", confidence=0.3
    )
    assert manual["source"] == "manual"

    # a later bundle ships an edge with the SAME natural key
    data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    data["thesisStockEdges"].append(
        {"thesisId": "th-hbm", "stockId": "st-nvda", "role": "hedge", "confidence": 0.9, "rationale": "bundle 觉得"}
    )
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = await mti.import_bundle(bundle_dir, apply=True)
    assert res["counts"]["edge"]["skipped"] == 2  # baseline st-missing ref + manual-owned key
    assert any("manual edge" in w for w in res["warnings"])

    # the operator's row is untouched
    edges = (await theses.get_thesis(hbm_row["id"]))["securities"]
    hedge = next(e for e in edges if e["role"] == "hedge")
    assert (hedge["confidence"], hedge["rationale"], hedge["source"]) == (0.3, "操作员手工判断", "manual")


async def test_operator_edit_of_import_edge_survives_reimport(bundle_dir: Path):
    """Editing an import-created edge flips ownership to manual, so a later
    re-import must not silently restore the bundle values."""
    await mti.import_bundle(bundle_dir, apply=True)
    hbm_row = next(
        t for t in await theses.list_theses(kind="thesis") if t["meta"].get("bundle_id") == "th-hbm"
    )
    edited = await securities.upsert_edge(
        hbm_row["id"], "NVDA.US", role="core", exposure="间接", confidence=0.4, rationale="操作员改判"
    )
    assert edited["source"] == "manual"  # ownership follows the edit

    res = await mti.import_bundle(bundle_dir, apply=True)
    assert any("edited by the operator" in w for w in res["warnings"])
    edges = (await theses.get_thesis(hbm_row["id"]))["securities"]
    core = next(e for e in edges if e["role"] == "core")
    assert (core["confidence"], core["rationale"]) == (0.4, "操作员改判")


async def test_duplicate_bundle_rows_do_not_flip_flop(bundle_dir: Path):
    """Same source ids repeated inside ONE bundle with different payloads:
    the first entry wins and re-imports stay idempotent."""
    data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    data["thesisStockEdges"].append(  # exact same (thesis, stock, role), other payload
        {"thesisId": "th-hbm", "stockId": "st-nvda", "role": "core", "confidence": 0.1}
    )
    data["stocks"].append({"id": "st-600519", "ticker": "600519", "market": "A-share", "name": "假重复行"})
    (bundle_dir / "bundle.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res = await mti.import_bundle(bundle_dir, apply=True)
    assert res["counts"]["edge"]["skipped"] == 2   # st-missing + in-bundle dupe
    assert res["counts"]["stock"]["skipped"] == 1  # in-bundle dupe
    assert any("duplicate of st-600519" in w for w in res["warnings"])

    res2 = await mti.import_bundle(bundle_dir, apply=True)
    for kind in ("lane", "thesis", "stock", "edge"):
        assert res2["counts"][kind]["created"] == 0, kind
        assert res2["counts"][kind]["updated"] == 0, kind

    # first entry's payload holds; the winner's name was not clobbered
    hbm_row = next(
        t for t in await theses.list_theses(kind="thesis") if t["meta"].get("bundle_id") == "th-hbm"
    )
    core = (await theses.get_thesis(hbm_row["id"]))["securities"][0]
    assert core["confidence"] == 0.8
    assert (await securities.find_security("st-600519"))["name"] == "贵州茅台"


async def test_canonical_swap_does_not_delete_reassigned_row(tmp_path: Path):
    """A -> Y, B -> X, C -> Z, then a new bundle maps B -> Y and C -> X: B's stale
    migration must NOT delete X, which now belongs to C (a planned winner)."""
    src = tmp_path / "mtd"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"schema": MANIFEST["schema"]}), encoding="utf-8")

    def _write(stocks: list[dict], edges: list[dict]) -> None:
        (src / "bundle.json").write_text(json.dumps({
            "lanes": [{"id": "lane-1", "name": "泳道"}],
            "theses": [{"id": "th-1", "laneId": "lane-1", "title": "论点一"}],
            "stocks": stocks, "thesisStockEdges": edges,
        }, ensure_ascii=False), encoding="utf-8")

    _write(
        [
            {"id": "A", "ticker": "YYY", "market": "US"},
            {"id": "B", "ticker": "XXX", "market": "US"},
            {"id": "C", "ticker": "ZZZ", "market": "US"},
        ],
        [{"thesisId": "th-1", "stockId": "C", "role": "core", "confidence": 0.6}],
    )
    await mti.import_bundle(src, apply=True)
    assert {s["id"] for s in await securities.list_securities()} == {"YYY.US", "XXX.US", "ZZZ.US"}

    # correction: B is actually YYY too; C moves onto XXX
    _write(
        [
            {"id": "A", "ticker": "YYY", "market": "US"},
            {"id": "B", "ticker": "YYY", "market": "US"},
            {"id": "C", "ticker": "XXX", "market": "US"},
        ],
        [{"thesisId": "th-1", "stockId": "C", "role": "core", "confidence": 0.6}],
    )
    res = await mti.import_bundle(src, apply=True)  # must not roll back or delete XXX.US

    assert {s["id"] for s in await securities.list_securities()} == {"YYY.US", "XXX.US"}
    assert (await securities.find_security("A"))["id"] == "YYY.US"
    assert (await securities.find_security("B"))["id"] == "YYY.US"
    assert (await securities.find_security("C"))["id"] == "XXX.US"

    # C's edge followed C onto XXX.US
    thesis = next(t for t in await theses.list_theses(kind="thesis"))
    edges = (await theses.get_thesis(thesis["id"]))["securities"]
    assert [e["security_id"] for e in edges] == ["XXX.US"]

    # re-apply is idempotent
    res2 = await mti.import_bundle(src, apply=True)
    for kind in ("stock", "edge"):
        assert res2["counts"][kind]["created"] == 0
        assert res2["counts"][kind]["updated"] == 0


async def test_edge_pair_survives_canonical_swap(tmp_path: Path):
    """A -> X and B -> Y each carry a same-role edge; a bundle swap (A -> Y,
    B -> X) must keep BOTH edges in one apply — no transient-loss, no
    second-pass re-create."""
    src = tmp_path / "mtd"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"schema": MANIFEST["schema"]}), encoding="utf-8")

    def _write(a_ticker: str, b_ticker: str) -> None:
        (src / "bundle.json").write_text(json.dumps({
            "lanes": [{"id": "lane-1", "name": "泳道"}],
            "theses": [{"id": "th-1", "laneId": "lane-1", "title": "论点一"}],
            "stocks": [
                {"id": "A", "ticker": a_ticker, "market": "US"},
                {"id": "B", "ticker": b_ticker, "market": "US"},
            ],
            "thesisStockEdges": [
                {"thesisId": "th-1", "stockId": "A", "role": "core", "confidence": 0.9, "rationale": "A边"},
                {"thesisId": "th-1", "stockId": "B", "role": "core", "confidence": 0.2, "rationale": "B边"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    _write("XXX", "YYY")
    await mti.import_bundle(src, apply=True)

    _write("YYY", "XXX")  # the swap
    res = await mti.import_bundle(src, apply=True)
    assert res["counts"]["edge"]["created"] == 0  # both rows survived in place

    thesis = next(t for t in await theses.list_theses(kind="thesis"))
    edges = (await theses.get_thesis(thesis["id"]))["securities"]
    by_sec = {e["security_id"]: e for e in edges}
    assert set(by_sec) == {"XXX.US", "YYY.US"}
    assert by_sec["YYY.US"]["rationale"] == "A边"   # A's payload followed A onto YYY
    assert by_sec["XXX.US"]["rationale"] == "B边"
    assert all("\x1f" not in e["role"] and e["role"] == "core" for e in edges)

    # single apply reached the fixed point: the next one is a no-op
    res2 = await mti.import_bundle(src, apply=True)
    assert res2["counts"]["edge"] == {"created": 0, "updated": 0, "unchanged": 2, "skipped": 0}
    assert res2["counts"]["stock"] == {"created": 0, "updated": 0, "unchanged": 2, "skipped": 0}

    # operator claims A's edge, then the bundle swaps back: the manual row
    # migrates structurally WITH its source but keeps the operator payload,
    # and B's edge must survive the swap too
    thesis = next(t for t in await theses.list_theses(kind="thesis"))
    a_edge = next(
        e for e in (await theses.get_thesis(thesis["id"]))["securities"] if e["rationale"] == "A边"
    )
    assert a_edge["security_id"] == "YYY.US"  # A sits on YYY after the first swap
    await securities.upsert_edge(
        thesis["id"], a_edge["security_id"], role="core", confidence=0.4, rationale="A-operator-edit"
    )
    _write("XXX", "YYY")  # swap back: A -> XXX, B -> YYY
    res3 = await mti.import_bundle(src, apply=True)
    assert any("operator payload kept" in w for w in res3["warnings"])
    edges = (await theses.get_thesis(thesis["id"]))["securities"]
    by_sec = {e["security_id"]: e for e in edges}
    assert set(by_sec) == {"XXX.US", "YYY.US"}  # BOTH edges alive
    manual = by_sec["XXX.US"]  # followed source A onto XXX
    assert manual["source"] == "manual"
    assert (manual["confidence"], manual["rationale"]) == (0.4, "A-operator-edit")
    assert by_sec["YYY.US"]["rationale"] == "B边"
    assert all("\x1f" not in e["role"] for e in edges)

    # payload-only bundle changes never touch the manual row on re-apply
    res4 = await mti.import_bundle(src, apply=True)
    assert any("bundle values skipped" in w for w in res4["warnings"])
    assert res4["counts"]["edge"]["created"] == 0

    # the reserved detach mark is rejected at the domain boundary, pre-strip
    for bad in ("custom\x1fmoving", "\x1fmoving", "core\x1f"):
        with pytest.raises(securities.SecurityError, match="reserved"):
            await securities.upsert_edge(thesis["id"], "XXX.US", role=bad)


async def test_blocked_manual_move_never_leaks_detach_mark(tmp_path: Path):
    """A manual edge planned to move onto a key that is permanently owned must
    hold its current key — and no committed row may carry the \\x1f mark."""
    src = tmp_path / "mtd"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"schema": MANIFEST["schema"]}), encoding="utf-8")

    def _write(a_ticker: str, edges: list[dict]) -> None:
        (src / "bundle.json").write_text(json.dumps({
            "lanes": [{"id": "lane-1", "name": "泳道"}],
            "theses": [{"id": "th-1", "laneId": "lane-1", "title": "论点一"}],
            "stocks": [
                {"id": "A", "ticker": a_ticker, "market": "US"},
                {"id": "B", "ticker": "XXX", "market": "US"},
            ],
            "thesisStockEdges": edges,
        }, ensure_ascii=False), encoding="utf-8")

    base_edges = [
        {"thesisId": "th-1", "stockId": "A", "role": "core", "rationale": "A边"},
        {"thesisId": "th-1", "stockId": "B", "role": "core", "rationale": "B边"},
    ]
    _write("YYY", base_edges)
    await mti.import_bundle(src, apply=True)

    thesis = next(t for t in await theses.list_theses(kind="thesis"))
    # operator claims A's edge (on YYY) AND hand-creates a hedge edge that will
    # permanently own the key A's edge would need if roles collided
    a_edge = next(
        e for e in (await theses.get_thesis(thesis["id"]))["securities"] if e["rationale"] == "A边"
    )
    await securities.upsert_edge(
        thesis["id"], a_edge["security_id"], role="core", rationale="A-manual", confidence=0.4
    )

    # correction: A moves onto XXX — where B's manual-free import edge owns
    # (th, XXX, core)… and B has no plan to move away this time
    _write("XXX", base_edges)
    res = await mti.import_bundle(src, apply=True)

    rows = await db.query("SELECT role, source FROM thesis_security_edges")
    assert all("\x1f" not in r["role"] for r in rows)  # no mark ever committed
    edges = (await theses.get_thesis(thesis["id"]))["securities"]
    manual = next(e for e in edges if e["source"] == "manual")
    assert manual["rationale"] == "A-manual"  # operator payload intact
    assert any("bundle values skipped" in w or "stays put" in w for w in res["warnings"])

    # repeated applies stay stable and never leak the mark either
    res2 = await mti.import_bundle(src, apply=True)
    rows = await db.query("SELECT role FROM thesis_security_edges")
    assert all("\x1f" not in r["role"] for r in rows)


# ---- API ---------------------------------------------------------------------------

async def test_api_import_roundtrip(bundle_dir: Path):
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/theses/import-market-data", json={"path": "/etc"})
        assert r.status_code == 400  # outside the repo

        # tests run with a tmp bundle outside the repo, so exercise via domain call,
        # then verify the API surfaces batches
        await mti.import_bundle(bundle_dir, apply=True)
        r = await client.get("/api/theses/import-batches")
        assert r.status_code == 200
        assert r.json()[0]["mode"] == "apply"

        r = await client.get("/api/theses")
        assert r.status_code == 200
        assert len(r.json()) == 2  # two lanes
