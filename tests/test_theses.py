"""Thesis registry: tree CRUD, version history, lifecycle validation, API."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import theses


async def _lane_and_thesis() -> tuple[dict, dict]:
    lane = await theses.create_thesis({"title": "AI 算力", "kind": "lane", "slug": "ai-compute"})
    thesis = await theses.create_thesis({
        "title": "HBM 供不应求", "slug": "hbm-shortage", "parent_id": lane["id"],
        "view": "HBM 产能扩张跟不上训练需求", "direction": "long", "status": "active",
        "tags": ["semis"], "meta": {"practical": {"actionCode": "watch"}},
    })
    return lane, thesis


# ---- create ------------------------------------------------------------------

async def test_create_writes_version_1_and_tree():
    lane, thesis = await _lane_and_thesis()
    assert lane["kind"] == "lane" and lane["parent_id"] is None
    assert thesis["parent_id"] == lane["id"]
    assert thesis["status"] == "active"
    assert thesis["meta"] == {"practical": {"actionCode": "watch"}}
    assert [v["version"] for v in thesis["versions"]] == [1]
    assert thesis["versions"][0]["direction"] == "long"

    tree = await theses.tree()
    assert [n["slug"] for n in tree] == ["ai-compute"]
    assert [c["slug"] for c in tree[0]["children"]] == ["hbm-shortage"]

    # slug or id both resolve
    assert (await theses.get_thesis("hbm-shortage"))["id"] == thesis["id"]

    events = await bus.replay(0, types=["thesis.created"])
    assert len(events) == 2


async def test_create_validation():
    await theses.create_thesis({"title": "占位", "slug": "taken"})
    with pytest.raises(theses.ThesisError, match="already exists"):
        await theses.create_thesis({"title": "重复", "slug": "taken"})
    with pytest.raises(theses.ThesisError, match="title"):
        await theses.create_thesis({"title": "  "})
    with pytest.raises(theses.ThesisError, match="creation status"):
        await theses.create_thesis({"title": "x", "status": "retired"})
    with pytest.raises(theses.ThesisError, match="unknown direction"):
        await theses.create_thesis({"title": "x", "direction": "sideways"})
    with pytest.raises(theses.ThesisError, match="unknown parent"):
        await theses.create_thesis({"title": "x", "parent_id": "nope"})
    with pytest.raises(theses.ThesisError, match="slug"):
        await theses.create_thesis({"title": "x", "slug": "Bad Slug!"})
    lane = await theses.create_thesis({"title": "lane", "kind": "lane"})
    with pytest.raises(theses.ThesisError, match="lane cannot have a parent"):
        await theses.create_thesis({"title": "x", "kind": "lane", "parent_id": lane["id"]})


# ---- update + versions ---------------------------------------------------------

async def test_update_appends_versions_only_on_content_change():
    _, thesis = await _lane_and_thesis()

    # tag-only change: no new version
    updated = await theses.update_thesis(thesis["id"], {"tags": ["semis", "memory"]})
    assert [v["version"] for v in updated["versions"]] == [1]

    # view change: version 2 records the new content
    updated = await theses.update_thesis(
        thesis["id"], {"view": "供需缺口到 2027"}, author="operator"
    )
    assert [v["version"] for v in updated["versions"]] == [1, 2]
    assert updated["versions"][-1]["view"] == "供需缺口到 2027"
    assert updated["versions"][-1]["author"] == "operator"
    # unchanged fields carry forward into the version row
    assert updated["versions"][-1]["title"] == "HBM 供不应求"
    assert updated["versions"][-1]["status"] == "active"

    # status is not patchable: the lifecycle channel is set_status()
    with pytest.raises(theses.ThesisError, match="set_status"):
        await theses.update_thesis(thesis["id"], {"status": "paused"})
    with pytest.raises(theses.ThesisError, match="unknown thesis fields"):
        await theses.update_thesis(thesis["id"], {"slug": "new-slug"})
    with pytest.raises(theses.ThesisError, match="own parent"):
        await theses.update_thesis(thesis["id"], {"parent_id": thesis["id"]})
    with pytest.raises(theses.ThesisError, match="must be a string"):
        await theses.update_thesis(thesis["id"], {"view": None})  # e.g. PATCH {"view": null}
    with pytest.raises(theses.ThesisError, match="non-empty string"):
        await theses.update_thesis(thesis["id"], {"title": None})
    assert await theses.update_thesis("missing", {"view": "x"}) is None


async def test_reparent_cannot_create_cycle():
    a = await theses.create_thesis({"title": "A", "slug": "node-a"})
    b = await theses.create_thesis({"title": "B", "slug": "node-b", "parent_id": a["id"]})
    c = await theses.create_thesis({"title": "C", "slug": "node-c", "parent_id": b["id"]})
    with pytest.raises(theses.ThesisError, match="cycle"):
        await theses.update_thesis(a["id"], {"parent_id": c["id"]})  # A under its own subtree
    # legal reparent still works
    moved = await theses.update_thesis(c["id"], {"parent_id": a["id"]})
    assert moved["parent_id"] == a["id"]


async def test_list_filters():
    lane, thesis = await _lane_and_thesis()
    assert [t["id"] for t in await theses.list_theses(kind="lane")] == [lane["id"]]
    assert [t["id"] for t in await theses.list_theses(status="active")] == [thesis["id"]]
    assert [t["id"] for t in await theses.list_theses(parent_id=lane["id"])] == [thesis["id"]]


async def test_set_status_is_a_conditional_claim():
    _, thesis = await _lane_and_thesis()  # active

    moved = await theses.set_status(thesis["id"], "paused", reason="wait for earnings")
    assert moved["status"] == "paused"
    # the transition is versioned like any other content change
    assert [v["version"] for v in moved["versions"]] == [1, 2]
    assert moved["versions"][-1]["status"] == "paused"

    # no-op transition returns the row untouched
    same = await theses.set_status(thesis["id"], "paused")
    assert [v["version"] for v in same["versions"]] == [1, 2]

    # stale expected_status loses (409 semantics)
    with pytest.raises(theses.ThesisConflict, match="expected"):
        await theses.set_status(thesis["id"], "active", expected_status="candidate")
    assert (await theses.get_thesis(thesis["id"]))["status"] == "paused"

    with pytest.raises(theses.ThesisError, match="unknown status"):
        await theses.set_status(thesis["id"], "zombie")
    assert await theses.set_status("missing", "active") is None

    events = await bus.replay(0, types=["thesis.status_changed"])
    assert len(events) == 1
    # version stamps the event: emits happen post-commit and can arrive out of
    # commit order, so consumers order/dedupe by version, not arrival
    assert events[0].payload == {
        "from": "active", "to": "paused", "reason": "wait for earnings", "version": 2,
    }


async def test_set_status_noop_still_claims_expected_state(monkeypatch):
    _, thesis = await _lane_and_thesis()  # active
    original_query_one = db.query_one
    raced = False

    async def query_one_with_race(sql, params=()):
        nonlocal raced
        row = await original_query_one(sql, params)
        if not raced and sql.startswith("SELECT * FROM theses WHERE id = ? OR slug = ?"):
            raced = True
            await db.execute("UPDATE theses SET status = 'paused' WHERE id = ?", (thesis["id"],))
        return row

    monkeypatch.setattr(db, "query_one", query_one_with_race)
    with pytest.raises(theses.ThesisConflict, match="changed concurrently"):
        await theses.set_status(thesis["id"], "active", expected_status="active")
    assert (await theses.get_thesis(thesis["id"]))["status"] == "paused"


# ---- API ------------------------------------------------------------------------

async def test_api_roundtrip():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/theses", json={"title": "内需复苏", "kind": "lane", "slug": "cn-demand"})
        assert r.status_code == 200
        lane_id = r.json()["id"]

        r = await client.post("/api/theses", json={
            "title": "白酒去库存见底", "slug": "baijiu-destock",
            "parent_id": lane_id, "direction": "long", "status": "candidate",
        })
        assert r.status_code == 200
        thesis_id = r.json()["id"]

        # duplicate slug -> 400
        r = await client.post("/api/theses", json={"title": "x", "slug": "baijiu-destock"})
        assert r.status_code == 400

        # tree is the default projection
        r = await client.get("/api/theses")
        assert r.status_code == 200
        tree = r.json()
        assert tree[0]["slug"] == "cn-demand"
        assert tree[0]["children"][0]["slug"] == "baijiu-destock"

        r = await client.get("/api/theses", params={"flat": "true"})
        assert {t["slug"] for t in r.json()} == {"cn-demand", "baijiu-destock"}

        r = await client.patch(f"/api/theses/{thesis_id}", json={"view": "批价企稳"})
        assert r.status_code == 200
        assert [v["version"] for v in r.json()["versions"]] == [1, 2]

        # status is not a PATCH field: fail loudly so old clients cannot think
        # a lifecycle change succeeded when it was ignored
        r = await client.patch(f"/api/theses/{thesis_id}", json={"status": "paused", "view": "又变了"})
        assert r.status_code == 422
        current = (await client.get(f"/api/theses/{thesis_id}")).json()
        assert current["status"] == "candidate"
        assert current["view"] == "批价企稳"
        assert (await client.get("/api/theses/missing")).status_code == 404
        assert (await client.patch("/api/theses/missing", json={"view": "x"})).status_code == 404

        # lifecycle route: conditional transition, stale expectation -> 409
        r = await client.post(f"/api/theses/{thesis_id}/status", json={"status": "active"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        r = await client.post(f"/api/theses/{thesis_id}/status", json={"status": "nonsense"})
        assert r.status_code == 400
        r = await client.post(
            f"/api/theses/{thesis_id}/status",
            json={"status": "paused", "expected_status": "candidate"},
        )
        assert r.status_code == 409
        assert (await client.post("/api/theses/missing/status", json={"status": "active"})).status_code == 404
