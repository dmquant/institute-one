"""Thesis registry: tree CRUD, version history, lifecycle validation, API."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus
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

    # view + status change: version 2 records the new content
    updated = await theses.update_thesis(
        thesis["id"], {"view": "供需缺口到 2027", "status": "paused"}, author="operator"
    )
    assert [v["version"] for v in updated["versions"]] == [1, 2]
    assert updated["versions"][-1]["view"] == "供需缺口到 2027"
    assert updated["versions"][-1]["status"] == "paused"
    assert updated["versions"][-1]["author"] == "operator"
    # unchanged fields carry forward into the version row
    assert updated["versions"][-1]["title"] == "HBM 供不应求"

    with pytest.raises(theses.ThesisError, match="unknown status"):
        await theses.update_thesis(thesis["id"], {"status": "zombie"})
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

        r = await client.patch(f"/api/theses/{thesis_id}", json={"status": "active", "view": "批价企稳"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        assert [v["version"] for v in r.json()["versions"]] == [1, 2]

        r = await client.patch(f"/api/theses/{thesis_id}", json={"status": "nonsense"})
        assert r.status_code == 400
        assert (await client.get("/api/theses/missing")).status_code == 404
        assert (await client.patch("/api/theses/missing", json={"view": "x"})).status_code == 404
