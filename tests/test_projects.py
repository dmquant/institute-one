"""Research projects (ROADMAP Phase 7, migrations/0021, REVIEW-D5 revision).

Covers the domain contract — create/name-uniqueness (names collapse to one
line — REVIEW-D5 M3), the archive conditional claim AND its atomic freeze
(the active check and the INSERT are one conditional statement; barrier tests
interleave an archive right before the write — REVIEW-D5 H1), idempotent
linking (INSERT OR IGNORE + UNIQUE is the arbiter), per-kind referential
validation including research_trees with cherry-pick degradation (REVIEW-D5
M1), the two-rail research merge in get()/digest()/digest_md()/n_links (REVIEW-D5 L2),
the 8KB digest clamp and heading escape (REVIEW-D5 M3) — plus the
enqueue(project_id=) backward-compatible extension and the project API routes
(router mounted on a bare FastAPI app, test_operator idiom).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import projects, research
from app.institute.digests import DIGEST_CAP_BYTES
from app.institute.prompts import work_date


# ---- fixtures / helpers -------------------------------------------------------

async def _mk_board(topic: str = "白板主题") -> str:
    board_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, max_cards, work_date, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (board_id, topic, "", "completed", 5, work_date(), now, now),
    )
    return board_id


async def _mk_thread(subject: str = "邮件主题") -> str:
    thread_id = uuid.uuid4().hex[:12]
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO mailbox_threads (id, subject, analyst_id, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (thread_id, subject, "macro-analyst", "open", now, now),
    )
    return thread_id


async def _mk_tree(root_topic: str = "探索树主题") -> str:
    tree_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO research_trees (id, root_topic, status, created_at) VALUES (?,?,?,?)",
        (tree_id, root_topic, "pending", bus.now_iso()),
    )
    return tree_id


def _app() -> FastAPI:
    from app.api import projects as api_projects

    app = FastAPI()
    app.include_router(api_projects.router)
    return app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")


# ---- create / list -------------------------------------------------------------

async def test_create_and_list():
    p = await projects.create("固态电池产业链", "跟踪 2026 固态电池量产进度")
    assert p["status"] == "active"
    assert p["name"] == "固态电池产业链"
    assert p["description"].startswith("跟踪")
    assert p["created_at"]

    rows = await projects.list_projects()
    assert [r["id"] for r in rows] == [p["id"]]
    assert rows[0]["n_links"] == 0

    assert await projects.list_projects(status="archived") == []
    with pytest.raises(ValueError):
        await projects.list_projects(status="bogus")


async def test_create_validation():
    with pytest.raises(ValueError):
        await projects.create("   ")
    await projects.create("重名项目")
    with pytest.raises(ValueError, match="already exists"):
        await projects.create("重名项目")
    with pytest.raises(ValueError, match="exceeds"):
        await projects.create("x" * (projects.MAX_NAME_LEN + 1))
    with pytest.raises(ValueError, match="exceeds"):
        await projects.create("desc-cap", "y" * (projects.MAX_DESCRIPTION_LEN + 1))


async def test_create_collapses_name_to_one_line():
    """Names are structural metadata (headings/filenames): inner newlines and
    whitespace runs collapse to single spaces at create time (REVIEW-D5 M3)."""
    p = await projects.create("多行\n\n名字\t带  空白")
    assert p["name"] == "多行 名字 带 空白"


# ---- archive --------------------------------------------------------------------

async def test_archive_conditional_and_idempotent():
    p = await projects.create("将归档")
    archived = await projects.archive(p["id"])
    assert archived["status"] == "archived"
    # idempotent: a second archive is a no-op, not an error
    again = await projects.archive(p["id"])
    assert again["status"] == "archived"
    assert await projects.archive("nope") is None
    active = await projects.unarchive(p["id"])
    assert active["status"] == "active"
    assert (await projects.unarchive(p["id"]))["status"] == "active"
    assert await projects.unarchive("nope") is None


# ---- link / unlink ---------------------------------------------------------------

async def test_link_idempotent():
    p = await projects.create("链接幂等")
    item = await research.enqueue("锂电隔膜格局")

    first = await projects.link(p["id"], "research", item["id"])
    assert first["linked"] is True
    second = await projects.link(p["id"], "research", item["id"])
    assert second["linked"] is False          # INSERT OR IGNORE: the DB is the arbiter
    rows = await db.query("SELECT * FROM project_links WHERE project_id = ?", (p["id"],))
    assert len(rows) == 1


async def test_link_validation():
    p = await projects.create("链接校验")
    item = await research.enqueue("校验话题")

    with pytest.raises(ValueError, match="unknown link kind"):
        await projects.link(p["id"], "bogus", item["id"])
    with pytest.raises(ValueError, match="must not be empty"):
        await projects.link(p["id"], "research", "  ")
    with pytest.raises(ValueError, match="not found"):
        await projects.link("ghost-project", "research", item["id"])
    for kind in ("research", "board", "thread", "tree"):
        with pytest.raises(ValueError, match="not found"):
            await projects.link(p["id"], kind, "ghost-ref")
    # a REAL tree links fine (research_trees exists on this checkout — 0020)
    tree_id = await _mk_tree()
    assert (await projects.link(p["id"], "tree", tree_id))["linked"] is True

    await projects.archive(p["id"])
    with pytest.raises(ValueError, match="archived"):
        await projects.link(p["id"], "research", item["id"])


async def test_tree_validation_degrades_without_0020_table():
    """Standalone cherry-pick tolerance (REVIEW-D5 M1): when research_trees is
    absent, tree refs are accepted unvalidated and get() falls back to bare
    refs — the other kinds still validate against their 0001 tables."""
    p = await projects.create("无树表容错")
    await db.execute("DROP TABLE research_trees")
    assert (await projects.link(p["id"], "tree", "tree-anything"))["linked"] is True
    full = await projects.get(p["id"])
    assert full["links"]["tree"][0]["ref_id"] == "tree-anything"


# ---- H1: the archived freeze is atomic against concurrent archive ------------------

async def test_link_archive_barrier_race_is_closed(monkeypatch):
    """Interleave an archive() right before link's INSERT (the old pre-read
    window): the conditional INSERT ... SELECT ... WHERE status='active' must
    see the archived row and refuse — nothing may land (REVIEW-D5 H1)."""
    p = await projects.create("竞态链接")
    item = await research.enqueue("竞态链接话题")
    orig_execute = db.execute
    fired = False

    async def racing_execute(sql, params=()):
        nonlocal fired
        if not fired and sql.lstrip().startswith("INSERT OR IGNORE INTO project_links"):
            fired = True
            await orig_execute(
                "UPDATE projects SET status='archived' WHERE id=?", (p["id"],)
            )
        return await orig_execute(sql, params)

    monkeypatch.setattr(db, "execute", racing_execute)
    with pytest.raises(ValueError, match="archived"):
        await projects.link(p["id"], "research", item["id"])
    assert fired
    assert await db.query("SELECT * FROM project_links WHERE project_id = ?", (p["id"],)) == []


async def test_enqueue_archive_barrier_race_is_closed(monkeypatch):
    """Same barrier for research.enqueue(project_id=): archive lands between
    the active pre-read and the INSERT — the conditional INSERT must refuse
    and no tagged queue row may exist (REVIEW-D5 H1)."""
    p = await projects.create("竞态入队")
    orig_execute = db.execute
    fired = False

    async def racing_execute(sql, params=()):
        nonlocal fired
        if not fired and sql.lstrip().startswith("INSERT INTO research_queue"):
            fired = True
            await orig_execute(
                "UPDATE projects SET status='archived' WHERE id=?", (p["id"],)
            )
        return await orig_execute(sql, params)

    monkeypatch.setattr(db, "execute", racing_execute)
    with pytest.raises(ValueError, match="archived concurrently"):
        await research.enqueue("竞态入队话题", project_id=p["id"])
    assert fired
    assert await db.query("SELECT * FROM research_queue WHERE topic = '竞态入队话题'") == []


async def test_unlink():
    p = await projects.create("解除链接")
    item = await research.enqueue("解绑话题")
    await projects.link(p["id"], "research", item["id"])
    assert await projects.unlink(p["id"], "research", item["id"]) is True
    assert await projects.unlink(p["id"], "research", item["id"]) is False
    direct = await research.enqueue("解绑直挂话题", project_id=p["id"])
    await projects.link(p["id"], "research", direct["id"])
    assert await projects.unlink(p["id"], "research", direct["id"]) is True
    assert (await db.query_one(
        "SELECT project_id FROM research_queue WHERE id = ?", (direct["id"],)
    ))["project_id"] is None
    assert all(r["ref_id"] != direct["id"] for r in (await projects.get(p["id"]))["links"]["research"])
    with pytest.raises(ValueError, match="unknown link kind"):
        await projects.unlink(p["id"], "bogus", item["id"])


# ---- get (expanded) ----------------------------------------------------------------

async def test_get_expands_all_kinds_and_merges_research_rails():
    p = await projects.create("全展开", "四类关联")
    board_id = await _mk_board("光伏排产")
    thread_id = await _mk_thread("追问：硅料价格")
    tree_id = await _mk_tree("产业链探索")
    linked_item = await research.enqueue("经 link 挂上的研究")
    direct_item = await research.enqueue("经 enqueue 直挂的研究", project_id=p["id"])
    # the same item on BOTH rails must appear exactly once
    await projects.link(p["id"], "research", linked_item["id"])
    await projects.link(p["id"], "research", direct_item["id"])
    await projects.link(p["id"], "board", board_id)
    await projects.link(p["id"], "thread", thread_id)
    await projects.link(p["id"], "tree", tree_id)

    full = await projects.get(p["id"])
    assert full["name"] == "全展开"
    research_refs = [r["ref_id"] for r in full["links"]["research"]]
    assert sorted(research_refs) == sorted([linked_item["id"], direct_item["id"]])
    assert len(research_refs) == len(set(research_refs))  # deduplicated across rails
    assert full["links"]["board"][0]["topic"] == "光伏排产"
    assert full["links"]["thread"][0]["subject"] == "追问：硅料价格"
    assert full["links"]["tree"][0]["ref_id"] == tree_id
    assert full["links"]["tree"][0]["root_topic"] == "产业链探索"   # 0020 enrichment

    assert await projects.get("nope") is None


async def test_n_links_counts_both_research_rails():
    """REVIEW-D5 L2: n_links is the TOTAL attachment count — direct
    research_queue.project_id rows count too, without double-counting items
    that are also linked explicitly."""
    p = await projects.create("计数项目")
    direct = await research.enqueue("直挂计数", project_id=p["id"])
    assert (await projects.list_projects())[0]["n_links"] == 1     # direct only
    await projects.link(p["id"], "research", direct["id"])         # same item, both rails
    assert (await projects.list_projects())[0]["n_links"] == 1     # not double-counted
    await projects.link(p["id"], "board", await _mk_board("计数白板"))
    assert (await projects.list_projects())[0]["n_links"] == 2


# ---- digest ---------------------------------------------------------------------

async def test_digest_md_renders_and_clamps():
    p = await projects.create("摘要项目", "项目描述正文")
    board_id = await _mk_board("摘要白板主题")
    await projects.link(p["id"], "board", board_id)
    await research.enqueue("摘要研究主题", project_id=p["id"])

    text = await projects.digest_md(p["id"])
    assert text.startswith("# 项目：摘要项目")
    assert "项目描述正文" in text
    assert "摘要研究主题" in text and "摘要白板主题" in text
    assert "## 邮件线程（0）" in text and "_（无）_" in text  # empty sections stay stable

    assert await projects.digest_md("nope") is None

    # byte clamp: a huge description cannot blow the 8KB digest contract
    big = await projects.create("超大项目", "长" * (projects.MAX_DESCRIPTION_LEN - 1))
    clamped = await projects.digest_md(big["id"])
    assert len(clamped.encode("utf-8")) <= DIGEST_CAP_BYTES
    assert "[digest truncated at 8KB]" in clamped


async def test_digest_groups_counts_and_limits_recent_timeline():
    p = await projects.create("结构化摘要")
    item = await research.enqueue("摘要研究", project_id=p["id"])
    board_id = await _mk_board("摘要白板")
    await projects.link(p["id"], "board", board_id)

    summary = await projects.digest(p["id"], limit=1)
    assert summary["name"] == "结构化摘要"
    assert summary["counts"] == {"research": 1, "board": 1, "thread": 0, "tree": 0}
    assert len(summary["timeline"]) == 1
    assert summary["timeline"][0]["title"] in {"摘要研究", "摘要白板"}
    assert {item["id"], board_id} >= {summary["timeline"][0]["ref_id"]}
    assert await projects.digest("nope") is None


async def test_digest_escapes_markdown_in_project_name():
    """REVIEW-D5 M3: the name is structural metadata — newlines collapse at
    create time and markdown/HTML structure characters are escaped in the
    heading, so a hostile name cannot forge headings, links, images or HTML."""
    p = await projects.create(
        "正常\n\n## 注入标题 [链接](http://x) ![图](http://y) `代码` <img src=x>"
    )
    text = await projects.digest_md(p["id"])
    first = text.splitlines()[0]
    assert first.startswith("# 项目：正常 ")               # ONE heading line, name inlined
    assert "\n## 注入标题" not in text                     # no forged heading anywhere
    # structure characters arrive escaped, not interpretable
    assert r"\#\# 注入标题" in first
    assert r"\[链接\]\(http://x\)" in first
    assert r"\!\[图\]" in first
    assert r"\`代码\`" in first
    assert r"\<img src=x\>" in first
    # the digest's own section headings are intact and follow the title
    assert text.index("## 深度研究") > text.index(first)


# ---- enqueue(project_id=) compatibility ------------------------------------------

async def test_enqueue_without_project_keeps_old_behavior():
    item = await research.enqueue("无项目话题")
    row = await db.query_one("SELECT project_id FROM research_queue WHERE id = ?", (item["id"],))
    assert row["project_id"] is None


async def test_enqueue_with_project_tags_row():
    p = await projects.create("入队项目")
    item = await research.enqueue("入队话题", project_id=p["id"])
    row = await db.query_one("SELECT project_id FROM research_queue WHERE id = ?", (item["id"],))
    assert row["project_id"] == p["id"]
    # empty-string project_id normalizes to None (kwargs hygiene)
    item2 = await research.enqueue("空串项目话题", project_id="  ")
    row2 = await db.query_one("SELECT project_id FROM research_queue WHERE id = ?", (item2["id"],))
    assert row2["project_id"] is None


async def test_enqueue_project_validation():
    with pytest.raises(ValueError, match="not found"):
        await research.enqueue("未知项目话题", project_id="ghost")
    p = await projects.create("已归档入队")
    await projects.archive(p["id"])
    with pytest.raises(ValueError, match="archived"):
        await research.enqueue("归档话题", project_id=p["id"])


async def test_enqueue_dedup_hit_does_not_retag():
    p = await projects.create("去重项目")
    first = await research.enqueue("去重话题")
    hit = await research.enqueue("去重话题", project_id=p["id"])
    assert hit.get("deduped") is True and hit["id"] == first["id"]
    row = await db.query_one("SELECT project_id FROM research_queue WHERE id = ?", (first["id"],))
    assert row["project_id"] is None          # the existing row is returned untouched


# ---- API -------------------------------------------------------------------------

async def test_api_create_list_get():
    async with _client() as client:
        r = await client.post("/api/projects", json={"name": "API 项目", "description": "d"})
        assert r.status_code == 200
        pid = r.json()["id"]

        assert (await client.post("/api/projects", json={"name": "API 项目"})).status_code == 400
        # pydantic min_length guard: blank name is a 422 before the domain
        assert (await client.post("/api/projects", json={"name": ""})).status_code == 422

        r = await client.get("/api/projects")
        assert r.status_code == 200 and [p["id"] for p in r.json()] == [pid]
        r = await client.get("/api/projects", params={"status": "archived"})
        assert r.status_code == 200 and r.json() == []
        assert (await client.get("/api/projects", params={"status": "bogus"})).status_code == 400

        r = await client.get(f"/api/projects/{pid}")
        assert r.status_code == 200
        assert set(r.json()["links"]) == {"research", "board", "thread", "tree"}
        assert (await client.get("/api/projects/nope")).status_code == 404


async def test_api_links_and_digest():
    item = await research.enqueue("API 链接话题")
    async with _client() as client:
        pid = (await client.post("/api/projects", json={"name": "API 链接"})).json()["id"]

        r = await client.post(f"/api/projects/{pid}/links",
                              json={"kind": "research", "ref_id": item["id"]})
        assert r.status_code == 200 and r.json()["linked"] is True
        r = await client.post(f"/api/projects/{pid}/links",
                              json={"kind": "research", "ref_id": item["id"]})
        assert r.status_code == 200 and r.json()["linked"] is False
        assert (await client.post(f"/api/projects/{pid}/links",
                                  json={"kind": "bogus", "ref_id": "x"})).status_code == 400
        assert (await client.post("/api/projects/ghost/links",
                                  json={"kind": "research", "ref_id": item["id"]})).status_code == 400

        r = await client.get(f"/api/projects/{pid}/digest", params={"limit": 1})
        assert r.status_code == 200
        assert r.json()["counts"]["research"] == 1
        assert r.json()["timeline"][0]["title"] == "API 链接话题"
        assert (await client.get("/api/projects/nope/digest")).status_code == 404

        r = await client.get(f"/api/projects/{pid}/digest.md")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        assert "API 链接话题" in r.text
        assert (await client.get("/api/projects/nope/digest.md")).status_code == 404

        assert (await client.post(f"/api/projects/{pid}/archive")).json()["status"] == "archived"
        refused = await client.post(
            f"/api/projects/{pid}/links", json={"kind": "research", "ref_id": item["id"] + "x"}
        )
        assert refused.status_code == 400  # reference validation runs before lifecycle validation
        refused = await client.post(
            f"/api/projects/{pid}/links", json={"kind": "research", "ref_id": item["id"]}
        )
        assert refused.status_code == 200  # idempotent replay remains allowed while archived
        other = await research.enqueue("API 归档拒绝新链接")
        refused = await client.post(
            f"/api/projects/{pid}/links", json={"kind": "research", "ref_id": other["id"]}
        )
        assert refused.status_code == 409

        assert (await client.post(f"/api/projects/{pid}/unarchive")).json()["status"] == "active"
        assert (await client.post(f"/api/projects/{pid}/unarchive")).status_code == 200
        assert (await client.post("/api/projects/nope/archive")).status_code == 404
        assert (await client.post("/api/projects/nope/unarchive")).status_code == 404

        removed = await client.delete(
            f"/api/projects/{pid}/links/research/{item['id']}"
        )
        assert removed.status_code == 204 and removed.content == b""
        again = await client.delete(
            f"/api/projects/{pid}/links/research/{item['id']}"
        )
        assert again.status_code == 204
