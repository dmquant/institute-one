"""Thesis registry: schema (card M1-001) + domain module and API (card M1-002).

Schema-level tests exercise raw SQL against the migration (lifecycle CHECKs,
version history, indices); the domain/API sections cover app/institute/theses.py
and app/api/theses.py. The autouse ``app_runtime`` fixture applies migrations
(db.init()) with foreign_keys=ON.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import theses


async def _mk_thesis(
    tid: str = "ai/gpu",
    *,
    parent: str | None = None,
    kind: str = "thesis",
    slug: str | None = None,
    status: str = "candidate",
    view: str = "unknown",
) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO theses (id, parent_id, kind, slug, name_zh, status, current_view, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, parent, kind, slug or tid, "国产 GPU", status, view, now, now),
    )


async def _mk_version(
    vid: str,
    thesis_id: str,
    version: int,
    *,
    supersedes: str | None = None,
    view: str = "unknown",
    summary: str = "initial view",
) -> None:
    await db.execute(
        "INSERT INTO thesis_versions (id, thesis_id, version, supersedes_id, view, summary, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (vid, thesis_id, version, supersedes, view, summary, bus.now_iso()),
    )


# ---- lifecycle status ------------------------------------------------------

async def test_lifecycle_status_check_enforced():
    # the contract lifecycle (02-thesis-stock-model.md): candidate|active|watch|dormant|retired
    for i, status in enumerate(("candidate", "active", "watch", "dormant", "retired")):
        await _mk_thesis(f"t-{i}", status=status)
    rows = await db.query("SELECT status FROM theses ORDER BY id")
    assert len(rows) == 5

    with pytest.raises(sqlite3.IntegrityError):
        await _mk_thesis("t-bad", status="validated")  # not a contract state


async def test_view_and_kind_checks():
    # imported directions are `conflicting` (all 74 bundle theses) — must be storable
    await _mk_thesis("t-conf", view="conflicting")
    for bad in ({"view": "sideways"}, {"kind": "theme"}):
        with pytest.raises(sqlite3.IntegrityError):
            await _mk_thesis("t-bad", **bad)


async def test_slug_unique():
    await _mk_thesis("ai/gpu", slug="ai-gpu")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_thesis("ai/gpu-2", slug="ai-gpu")


async def test_conditional_claim_transition():
    # house idiom: UPDATE … WHERE status=<expected> — re-entrant, restart-safe
    await _mk_thesis("t-1", status="candidate")
    now = bus.now_iso()
    claimed = await db.execute(
        "UPDATE theses SET status='active', updated_at=? WHERE id=? AND status='candidate'", (now, "t-1")
    )
    assert claimed == 1
    again = await db.execute(
        "UPDATE theses SET status='active', updated_at=? WHERE id=? AND status='candidate'", (now, "t-1")
    )
    assert again == 0
    row = await db.query_one("SELECT status FROM theses WHERE id=?", ("t-1",))
    assert row["status"] == "active"


# ---- version history -------------------------------------------------------

async def test_version_history_preserved_across_revisions():
    await _mk_thesis("t-1", status="active", view="unknown")
    await _mk_version("v-1", "t-1", 1, view="conflicting", summary="imported core view")

    # a revision appends a new row (supersedes the old one) and updates the projection;
    # the old version row must stay intact
    await _mk_version("v-2", "t-1", 2, supersedes="v-1", view="bullish", summary="revised after earnings")
    await db.execute(
        "UPDATE theses SET current_view='bullish', updated_at=? WHERE id=?", (bus.now_iso(), "t-1")
    )

    rows = await db.query(
        "SELECT id, version, supersedes_id, view, summary FROM thesis_versions "
        "WHERE thesis_id=? ORDER BY version", ("t-1",),
    )
    assert [r["id"] for r in rows] == ["v-1", "v-2"]
    assert rows[0]["summary"] == "imported core view"  # history intact
    assert rows[0]["supersedes_id"] is None
    assert rows[1]["supersedes_id"] == "v-1"

    latest = await db.query_one(
        "SELECT view FROM thesis_versions WHERE thesis_id=? ORDER BY version DESC LIMIT 1", ("t-1",)
    )
    assert latest["view"] == "bullish"


async def test_version_number_unique_per_thesis():
    await _mk_thesis("t-1")
    await _mk_thesis("t-2")
    await _mk_version("v-1", "t-1", 1)
    await _mk_version("v-2", "t-2", 1)  # same version number on another thesis is fine
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_version("v-dup", "t-1", 1)


async def test_versions_require_thesis_and_cascade_on_delete():
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_version("v-orphan", "no-such-thesis", 1)

    await _mk_thesis("t-1")
    await _mk_version("v-1", "t-1", 1)
    await db.execute("DELETE FROM theses WHERE id=?", ("t-1",))
    assert await db.query("SELECT id FROM thesis_versions WHERE thesis_id=?", ("t-1",)) == []


# ---- lane tree ---------------------------------------------------------------

async def test_lane_parent_linkage():
    # lanes are theses with kind='lane' (10-market-thesis-data-bootstrap.md)
    await _mk_thesis("ai", kind="lane", status="active")
    await _mk_thesis("thesis-05c3f6f33c", parent="ai", status="watch", view="conflicting")

    children = await db.query("SELECT id FROM theses WHERE parent_id=? ORDER BY id", ("ai",))
    assert [c["id"] for c in children] == ["thesis-05c3f6f33c"]

    with pytest.raises(sqlite3.IntegrityError):
        await _mk_thesis("t-orphan", parent="no-such-lane")


# ==== card M1-002: domain module =============================================

async def test_create_thesis_domain():
    lane = await theses.create_thesis(
        {"id": "ai", "kind": "lane", "name_zh": "人工智能", "name_en": "AI", "status": "active"}
    )
    assert lane["kind"] == "lane"
    assert lane["slug"] == "ai"        # slug defaults to id
    assert lane["status"] == "active"
    assert lane["versions"] == []      # no content -> no seeded version

    t = await theses.create_thesis({
        "id": "ai/gpu", "name_zh": "国产 GPU", "parent_id": "ai",
        "view": "conflicting", "summary": "supply constrained, demand policy-driven",
        "drivers": ["export controls"], "priority": 5,
    })
    assert t["parent_id"] == "ai"
    assert t["status"] == "candidate"  # default entry state
    assert t["current_view"] == "conflicting"
    # content at create seeds version 1 (root of the supersedes chain)
    assert [v["version"] for v in t["versions"]] == [1]
    assert t["versions"][0]["supersedes_id"] is None
    assert t["versions"][0]["drivers"] == ["export controls"]
    assert (await theses.get_thesis("ai"))["children"] == ["ai/gpu"]

    events = await bus.replay(0, types=["thesis.created"])
    assert [e.ref_id for e in events] == ["ai", "ai/gpu"]
    assert events[1].payload["version"] == 1


async def test_create_validation_errors():
    await theses.create_thesis({"id": "ai", "kind": "lane", "name_zh": "人工智能"})

    with pytest.raises(theses.ThesisError, match="duplicate slug"):
        await theses.create_thesis({"id": "ai-2", "slug": "ai", "name_zh": "重复"})
    with pytest.raises(theses.ThesisError, match="duplicate thesis id"):
        await theses.create_thesis({"id": "ai", "name_zh": "重复"})
    # a new thesis enters as candidate or active, never mid-lifecycle
    with pytest.raises(theses.ThesisError, match="candidate or active"):
        await theses.create_thesis({"id": "t-w", "name_zh": "x", "status": "watch"})
    with pytest.raises(theses.ThesisError, match="unknown status"):
        await theses.create_thesis({"id": "t-b", "name_zh": "x", "status": "validated"})
    with pytest.raises(theses.ThesisError, match="unknown kind"):
        await theses.create_thesis({"id": "t-k", "name_zh": "x", "kind": "theme"})
    with pytest.raises(theses.ThesisError, match="name_zh"):
        await theses.create_thesis({"id": "t-n"})
    with pytest.raises(theses.ThesisError, match="id or a slug"):
        await theses.create_thesis({"name_zh": "无名"})
    with pytest.raises(theses.ThesisError, match="not found"):
        await theses.create_thesis({"id": "t-p", "name_zh": "x", "parent_id": "no-such-lane"})
    with pytest.raises(theses.ThesisError, match="unknown thesis fields"):
        await theses.create_thesis({"id": "t-x", "name_zh": "x", "bogus": 1})
    # failed creates wrote nothing (transactional)
    assert len(await theses.list_theses()) == 1


async def test_update_appends_version_history():
    await theses.create_thesis({
        "id": "ai/gpu", "name_zh": "国产 GPU", "view": "conflicting",
        "summary": "imported core view", "drivers": ["export controls"],
    })

    # content revision -> version 2 supersedes version 1; untouched content carries over
    t = await theses.update_thesis("ai/gpu", {"view": "bullish", "summary": "revised after earnings"})
    assert t["current_view"] == "bullish"  # projection mirrors the head of the chain
    assert [v["version"] for v in t["versions"]] == [1, 2]
    v1, v2 = t["versions"]
    assert v1["summary"] == "imported core view"  # history intact
    assert v2["supersedes_id"] == v1["id"]
    assert v2["drivers"] == ["export controls"]   # partial revision merges from latest

    # projection-only update: no new version row
    t = await theses.update_thesis("ai/gpu", {"priority": 9, "owner_analyst": "chief-strategist"})
    assert t["priority"] == 9
    assert len(t["versions"]) == 2

    events = await bus.replay(0, types=["thesis.updated"])
    assert [e.payload["version"] for e in events] == [2, None]

    assert await theses.update_thesis("no-such", {"priority": 1}) is None
    with pytest.raises(theses.ThesisError, match="set_status"):
        await theses.update_thesis("ai/gpu", {"status": "active"})
    with pytest.raises(theses.ThesisError, match="unknown view"):
        await theses.update_thesis("ai/gpu", {"view": "sideways"})
    with pytest.raises(theses.ThesisError, match="run_id"):
        await theses.update_thesis("ai/gpu", {"run_id": "r-1"})  # annotation needs content
    with pytest.raises(theses.ThesisError, match="must be a list"):
        await theses.update_thesis("ai/gpu", {"drivers": "not-a-list"})


async def test_update_slug_and_parent_guards():
    await theses.create_thesis({"id": "ai", "kind": "lane", "name_zh": "AI"})
    await theses.create_thesis({"id": "ai/gpu", "name_zh": "GPU", "parent_id": "ai"})

    with pytest.raises(theses.ThesisError, match="duplicate slug"):
        await theses.update_thesis("ai/gpu", {"slug": "ai"})
    t = await theses.update_thesis("ai/gpu", {"slug": "ai/gpu"})  # own slug is fine
    assert t["slug"] == "ai/gpu"

    with pytest.raises(theses.ThesisError, match="own parent"):
        await theses.update_thesis("ai", {"parent_id": "ai"})
    with pytest.raises(theses.ThesisError, match="cycle"):
        await theses.update_thesis("ai", {"parent_id": "ai/gpu"})


async def test_lifecycle_transition_conditional_claim():
    await theses.create_thesis({"id": "t-1", "name_zh": "x"})

    t = await theses.set_status("t-1", "active", expected_status="candidate")
    assert t["status"] == "active"

    # stale expectation loses the claim and the row stays untouched
    with pytest.raises(theses.TransitionConflict):
        await theses.set_status("t-1", "watch", expected_status="candidate")
    assert (await theses.get_thesis("t-1"))["status"] == "active"

    with pytest.raises(theses.ThesisError, match="unknown status"):
        await theses.set_status("t-1", "archived")
    assert await theses.set_status("no-such", "active") is None

    events = await bus.replay(0, types=["thesis.status_changed"])
    assert len(events) == 1
    assert events[0].payload == {"from": "candidate", "to": "active", "reason": ""}

    # same-status move is a no-op (no event, no claim burned)
    await theses.set_status("t-1", "active")
    assert len(await bus.replay(0, types=["thesis.status_changed"])) == 1


async def test_tree_shapes_lanes_to_theses():
    await theses.create_thesis({"id": "ai", "kind": "lane", "name_zh": "AI", "status": "active"})
    await theses.create_thesis({"id": "ai/gpu", "name_zh": "GPU", "parent_id": "ai", "priority": 5})
    await theses.create_thesis({"id": "ai/gpu/domestic", "name_zh": "国产 GPU", "parent_id": "ai/gpu"})
    await theses.create_thesis({"id": "ai/hbm", "name_zh": "HBM", "parent_id": "ai", "priority": 1})
    await theses.create_thesis({"id": "stray", "name_zh": "无赛道"})  # no lane -> root

    roots = await theses.tree()
    assert [r["id"] for r in roots] == ["ai", "stray"]  # lanes sort first at the root
    ai = roots[0]
    assert [c["id"] for c in ai["children"]] == ["ai/gpu", "ai/hbm"]  # priority DESC
    assert [c["id"] for c in ai["children"][0]["children"]] == ["ai/gpu/domestic"]

    flat = await theses.list_theses()
    assert len(flat) == 5
    assert [t["id"] for t in await theses.list_theses(kind="lane")] == ["ai"]
    assert [t["id"] for t in await theses.list_theses(parent_id="ai")] == ["ai/gpu", "ai/hbm"]
    assert [t["id"] for t in await theses.list_theses(search="HBM")] == ["ai/hbm"]
    with pytest.raises(theses.ThesisError, match="unknown status"):
        await theses.list_theses(status="everything")


# ==== card M1-002: API surface ================================================

async def test_api_roundtrip():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/theses", json={"id": "ai", "kind": "lane", "name_zh": "AI", "status": "active"})
        assert r.status_code == 200
        assert r.json()["kind"] == "lane"

        r = await client.post("/api/theses", json={
            "id": "ai/gpu", "name_zh": "国产 GPU", "parent_id": "ai",
            "view": "conflicting", "summary": "seed view",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "candidate"
        assert len(r.json()["versions"]) == 1

        # duplicate slug and mid-lifecycle entry map to 400
        r = await client.post("/api/theses", json={"id": "x", "slug": "ai", "name_zh": "重复"})
        assert r.status_code == 400
        assert "duplicate slug" in r.json()["detail"]
        r = await client.post("/api/theses", json={"id": "t-w", "name_zh": "x", "status": "watch"})
        assert r.status_code == 400

        # GET tree by default, flat list on demand
        r = await client.get("/api/theses")
        assert r.status_code == 200
        tree = r.json()
        assert [n["id"] for n in tree] == ["ai"]
        assert [c["id"] for c in tree[0]["children"]] == ["ai/gpu"]
        r = await client.get("/api/theses", params={"flat": 1})
        assert {t["id"] for t in r.json()} == {"ai", "ai/gpu"}
        r = await client.get("/api/theses", params={"kind": "lane"})
        assert [t["id"] for t in r.json()] == ["ai"]

        # path-like ids ("ai/gpu") resolve through the :path param
        r = await client.get("/api/theses/ai/gpu")
        assert r.status_code == 200
        assert r.json()["id"] == "ai/gpu"
        assert (await client.get("/api/theses/nope")).status_code == 404

        # PATCH: content revision appends a version; projection-only does not
        r = await client.patch("/api/theses/ai/gpu", json={"view": "bullish", "summary": "revised"})
        assert r.status_code == 200
        assert r.json()["current_view"] == "bullish"
        assert [v["version"] for v in r.json()["versions"]] == [1, 2]
        r = await client.patch("/api/theses/ai/gpu", json={"priority": 7})
        assert r.status_code == 200
        assert r.json()["priority"] == 7
        assert len(r.json()["versions"]) == 2
        assert (await client.patch("/api/theses/nope", json={"priority": 1})).status_code == 404
        r = await client.patch("/api/theses/ai/gpu", json={"view": "sideways"})
        assert r.status_code == 400

        # lifecycle via PATCH status: conditional claim; stale expectation -> 409
        r = await client.patch("/api/theses/ai/gpu", json={"status": "active", "expected_status": "candidate"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        r = await client.patch("/api/theses/ai/gpu", json={"status": "watch", "expected_status": "candidate"})
        assert r.status_code == 409
        assert (await client.get("/api/theses/ai/gpu")).json()["status"] == "active"


async def test_import_batches_static_route_precedes_path_thesis_route():
    from app.main import create_app

    await db.execute(
        "INSERT INTO market_thesis_imports "
        "(id, schema, generated_at, mode, status, manifest_json, warnings_json, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "import-batch-1",
            "researchos.market_thesis_export.bundle.v1",
            "2026-07-01T01:45:20.376Z",
            "dry_run",
            "completed",
            '{"stats":{"thesisCount":0}}',
            '["fixture warning"]',
            "2026-07-21T12:00:00+00:00",
        ),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/theses/import-batches", params={"limit": 1})
        assert r.status_code == 200
        assert r.json() == [{
            "id": "import-batch-1",
            "schema": "researchos.market_thesis_export.bundle.v1",
            "generated_at": "2026-07-01T01:45:20.376Z",
            "source_schema": None,
            "source_generated_at": None,
            "source_first_date": None,
            "source_last_date": None,
            "thesis_count": 0,
            "lane_count": 0,
            "stock_count": 0,
            "edge_count": 0,
            "thesis_stock_edge_count": 0,
            "bundle_sha256": None,
            "idempotency_key": None,
            "mode": "dry_run",
            "status": "completed",
            "error": None,
            "imported_at": "2026-07-21T12:00:00+00:00",
            "finished_at": None,
            "manifest": {"stats": {"thesisCount": 0}},
            "warnings": ["fixture warning"],
        }]
        assert (await client.get("/api/theses/import-batches", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/theses/import-batches", params={"limit": 201})).status_code == 422


async def test_update_null_and_falsy_inputs():
    # NOT NULL columns reject explicit nulls with a readable error, never a
    # raw IntegrityError (ThesisPatch types them str|None, so nulls do arrive)
    await theses.create_thesis({"id": "ai", "kind": "lane", "name_zh": "AI"})
    await theses.create_thesis({"id": "ai/gpu", "name_zh": "GPU", "parent_id": "ai"})
    for field in ("scope", "exclusions", "source", "summary"):
        with pytest.raises(theses.ThesisError, match="cannot be null"):
            await theses.update_thesis("ai/gpu", {field: None})

    # falsy parent_id normalizes to NULL (clean un-parenting, mirrors create) —
    # not an "" write into the FK column
    t = await theses.update_thesis("ai/gpu", {"parent_id": ""})
    assert t["parent_id"] is None
    t = await theses.update_thesis("ai/gpu", {"parent_id": "ai"})
    assert t["parent_id"] == "ai"
    t = await theses.update_thesis("ai/gpu", {"parent_id": None})
    assert t["parent_id"] is None


async def test_concurrent_revisions_get_distinct_versions():
    # the head is read inside the write transaction, so parallel revisions can
    # never both claim version+1 — every writer succeeds with its own number
    await theses.create_thesis({"id": "t-1", "name_zh": "x", "summary": "seed"})
    results = await asyncio.gather(
        *(theses.update_thesis("t-1", {"summary": f"rev {i}"}) for i in range(6)),
        return_exceptions=True,
    )
    assert [r for r in results if isinstance(r, BaseException)] == []
    t = await theses.get_thesis("t-1")
    assert [v["version"] for v in t["versions"]] == list(range(1, 8))
    by_ver = {v["version"]: v for v in t["versions"]}
    for n in range(2, 8):  # supersedes chain stays linked, no forks
        assert by_ver[n]["supersedes_id"] == by_ver[n - 1]["id"]


async def test_concurrent_duplicate_creates_map_to_thesis_error():
    # racers that slip past the pre-check hit UNIQUE(theses.slug) at insert;
    # that must surface as ThesisError (API 400), never a raw IntegrityError
    results = await asyncio.gather(
        *(theses.create_thesis({"id": f"t-{i}", "slug": "same-slug", "name_zh": "x"}) for i in range(6)),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 5
    assert all(isinstance(e, theses.ThesisError) for e in losers)
    assert all("duplicate slug" in str(e) for e in losers)
    assert len(await db.query("SELECT id FROM theses WHERE slug = ?", ("same-slug",))) == 1


async def test_api_patch_null_guards_and_unparenting():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/theses", json={"id": "ai", "kind": "lane", "name_zh": "AI"})
        await client.post("/api/theses", json={"id": "ai/gpu", "name_zh": "GPU", "parent_id": "ai"})

        for field in ("scope", "exclusions", "source", "summary"):
            r = await client.patch("/api/theses/ai/gpu", json={field: None})
            assert r.status_code == 400, field  # was a NOT NULL IntegrityError 500
            assert "cannot be null" in r.json()["detail"]

        r = await client.patch("/api/theses/ai/gpu", json={"parent_id": ""})
        assert r.status_code == 200  # was a FOREIGN KEY IntegrityError 500
        assert r.json()["parent_id"] is None


async def test_api_status_changes_travel_alone():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/theses", json={"id": "t-1", "name_zh": "x", "summary": "seed", "priority": 9})
        assert r.status_code == 200

        # a transition mixed with field updates is rejected up front, so a 409
        # can never land after the fields (and a version row) already committed
        r = await client.patch("/api/theses/t-1", json={
            "priority": 42, "summary": "sneaky", "status": "watch", "expected_status": "candidate",
        })
        assert r.status_code == 400
        assert "alone" in r.json()["detail"]
        t = (await client.get("/api/theses/t-1")).json()
        assert t["priority"] == 9                                # nothing applied
        assert [v["version"] for v in t["versions"]] == [1]      # no version appended
        assert t["status"] == "candidate"

        # transition riders without a status change are meaningless -> 400
        r = await client.patch("/api/theses/t-1", json={"priority": 1, "expected_status": "candidate"})
        assert r.status_code == 400

        # status alone still transitions
        r = await client.patch("/api/theses/t-1", json={"status": "active", "expected_status": "candidate"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"


async def test_api_rejects_unknown_fields_and_folds_view_alias():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/theses", json={"id": "t-1", "name_zh": "x"})

        # extra="forbid": typos are 422s, not silently swallowed
        r = await client.post("/api/theses", json={"id": "t-2", "name_zh": "x", "priorty": 99})
        assert r.status_code == 422
        r = await client.patch("/api/theses/t-1", json={"bogus": 1})
        assert r.status_code == 422

        # current_view (the column name) is a declared alias, so it reaches the
        # domain's fold instead of being ignored
        r = await client.patch("/api/theses/t-1", json={"current_view": "bearish"})
        assert r.status_code == 200
        assert r.json()["current_view"] == "bearish"
        r = await client.post("/api/theses", json={"id": "t-3", "name_zh": "x", "current_view": "bullish"})
        assert r.status_code == 200
        assert r.json()["current_view"] == "bullish"
