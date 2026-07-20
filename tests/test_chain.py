"""Chain graph (ROADMAP Phase 4, partition C2): INSTR backstop idempotency,
candidate promotion (manual + auto threshold), entity extraction via the echo
hand, ``## Entities`` footers, graph API depth truncation, vault projection.

The chain bus handlers are exercised by direct calls with constructed
``bus.Event`` objects (the register() wiring itself is asserted and rolled
back) — registering into the process-global bus would leak handlers into
other tests. API tests build a bare FastAPI app around the router, matching
test_forecasts.py: the router mount in app/main.py is outside this partition
(PATCH-NOTES-C2.md).
"""
from __future__ import annotations

import shutil

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import chain
from app.vault.writer import REGION_BEGIN, REGION_END, get_writer


@pytest.fixture(autouse=True)
def clean_vault_dir():
    """The vault tmp dir outlives the per-test DB wipe; keep disk and ledger in sync."""
    writer = get_writer()
    assert writer.enabled and writer.root is not None
    shutil.rmtree(writer.root, ignore_errors=True)
    writer.root.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
async def client():
    app = FastAPI()
    from app.api import chain as api_chain

    app.include_router(api_chain.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _mk_security(sid: str = "CATL.US", name: str = "CATL Inc") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_en, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, sid.split(".")[0], "US", name, now, now),
    )


async def _candidate_id(name: str) -> str:
    row = await db.query_one("SELECT id FROM chain_candidates WHERE name = ?", (name,))
    assert row is not None, f"candidate {name} not recorded"
    return row["id"]


# ==== INSTR backstop ==========================================================

async def test_backstop_hits_chinese_name_and_alias_idempotently():
    ning = await chain.create_node("宁德时代", "company", aliases=["CATL"])
    byd = await chain.create_node("比亚迪", "company")
    solid = await chain.create_node("固态电池", "technology")

    text = "本季 CATL 产能利用率回升，比亚迪跟进降价；固态电池量产仍需两年。"
    new = await chain.backstop_tag("research", "item-1", text)
    assert set(new) == {ning["id"], byd["id"], solid["id"]}

    # idempotent: same artifact re-tagged (live handler + catch-up tick) adds nothing
    again = await chain.backstop_tag("research", "item-1", text)
    assert again == []
    rows = await db.query("SELECT * FROM chain_mentions WHERE artifact_kind='research' AND artifact_ref='item-1'")
    assert len(rows) == 3

    # the alias hit resolved to the node and captured a snippet around the term
    m = await db.query_one("SELECT * FROM chain_mentions WHERE node_id = ?", (ning["id"],))
    assert "CATL" in m["snippet"]

    # a different artifact records fresh mentions
    new2 = await chain.backstop_tag("whiteboard", "board-9", "宁德时代的议价权")
    assert new2 == [ning["id"]]


async def test_backstop_case_sensitive_and_empty_inputs():
    await chain.create_node("宁德时代", "company", aliases=["CATL"])
    assert await chain.backstop_tag("research", "r1", "catl 小写不命中") == []
    assert await chain.backstop_tag("research", "r2", "") == []
    assert await chain.backstop_tag("", "r3", "宁德时代") == []


async def test_backstop_via_research_event_handler():
    node = await chain.create_node("英伟达", "company")
    event = bus.Event(
        id=1, type="research.completed", ref_kind="research", ref_id="q-77",
        payload={"topic": "算力供需", "summary": "英伟达 B 系列供给仍紧。", "session_id": None},
    )
    await chain._on_artifact_event(event)
    row = await db.query_one("SELECT * FROM chain_mentions WHERE node_id = ?", (node["id"],))
    assert row is not None and row["artifact_kind"] == "research" and row["artifact_ref"] == "q-77"


async def test_backstop_via_board_event_reads_cards():
    node = await chain.create_node("中芯国际", "company")
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO whiteboard_boards (id, topic, question, status, work_date, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("b-1", "成熟制程", "产能过剩吗？", "completed", "2026-07-20", now, now),
    )
    await db.execute(
        "INSERT INTO whiteboard_cards (id, board_id, idx, analyst_id, status, summary, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("c-1", "b-1", 1, "chief-strategist", "completed", "中芯国际扩产节奏放缓。", now),
    )
    event = bus.Event(id=2, type="whiteboard.board_completed", ref_kind="board", ref_id="b-1",
                      payload={"topic": "成熟制程", "session_id": None, "cards": 1})
    await chain._on_artifact_event(event)
    row = await db.query_one("SELECT * FROM chain_mentions WHERE node_id = ?", (node["id"],))
    assert row is not None and row["artifact_ref"] == "b-1"


async def test_artifact_handler_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(chain, "backstop_tag", boom)
    event = bus.Event(id=3, type="research.completed", ref_kind="research", ref_id="q-1",
                      payload={"topic": "x", "summary": "y"})
    await chain._on_artifact_event(event)  # must swallow


async def test_register_wires_expected_prefixes_and_rolls_back():
    before = list(bus._handlers)
    try:
        chain.register()
        added = {prefix for prefix, _ in bus._handlers[len(before):]}
        assert added == set(chain.ARTIFACT_EVENT_TYPES) | {"chain.node_updated"}
    finally:
        bus._handlers[:] = before


# ==== aliases / nodes ==========================================================

async def test_merge_aliases_idempotent_and_ambiguity_rejected():
    ning = await chain.create_node("宁德时代", "company")
    await chain.create_node("比亚迪", "company")

    node = await chain.merge_aliases(ning["id"], "CATL")
    assert node["aliases"] == ["CATL"]
    node = await chain.merge_aliases(ning["id"], "CATL")  # idempotent
    assert node["aliases"] == ["CATL"]
    node = await chain.merge_aliases(ning["id"], "宁德时代")  # name itself: no-op
    assert node["aliases"] == ["CATL"]

    with pytest.raises(chain.ChainError, match="at least"):
        await chain.merge_aliases(ning["id"], "锂")
    with pytest.raises(chain.ChainError, match="another node"):
        await chain.merge_aliases(ning["id"], "比亚迪")
    with pytest.raises(LookupError):
        await chain.merge_aliases("nope", "任意别名")


async def test_create_node_validation():
    with pytest.raises(chain.ChainError, match="kind"):
        await chain.create_node("宁德时代", "empire")
    with pytest.raises(chain.ChainError, match="at least"):
        await chain.create_node("锂", "commodity")
    with pytest.raises(chain.ChainError, match="not found"):
        await chain.create_node("宁德时代", "company", security_id="NOPE.US")


# ==== candidates: extraction, promotion, auto threshold ========================

FIXTURE_EXTRACTION = """\
分析正文开头。
ENTITY: 宁德时代 | company
ENTITY: 固态电池 | technology
ENTITY: 固态电池 | company
ENTITY: 锂 | commodity
ENTITY: 特斯拉 | weirdkind
ENTITY 缺冒号的行不算
其他内容。
"""


def test_parse_extraction_fixture():
    parsed = chain.parse_extraction(FIXTURE_EXTRACTION)
    assert parsed == [
        {"name": "宁德时代", "kind": "company"},
        {"name": "固态电池", "kind": "technology"},   # duplicate: first kind wins
        {"name": "特斯拉", "kind": "other"},          # unknown kind degrades
    ]
    assert chain.parse_extraction("NONE") == []
    assert chain.parse_extraction("") == []


async def test_extract_entities_echo_roundtrip():
    """The echo hand echoes the prompt back, so ENTITY: lines inside the
    artifact text come home and parse — the whole executor path runs."""
    cands = await chain.extract_entities(FIXTURE_EXTRACTION)
    assert {c["name"] for c in cands} == {"宁德时代", "固态电池", "特斯拉"}
    task = await db.query_one("SELECT * FROM tasks WHERE source = 'chain'")
    assert task is not None and task["status"] == "completed"


async def test_extract_entities_prompt_carries_no_bare_entity_line():
    """No template line may START with ENTITY: — the echo hand would turn it
    into a phantom candidate on every extraction."""
    rendered = chain.ENTITY_EXTRACT_PROMPT.format(text="占位文本")
    assert not [
        line for line in rendered.splitlines() if line.strip().startswith("ENTITY:")
    ]
    assert chain.parse_extraction(f"[echo] {rendered}") == []


async def test_record_candidates_skips_known_and_bumps_counts():
    await chain.create_node("宁德时代", "company", aliases=["CATL"])
    cands = [
        {"name": "宁德时代", "kind": "company"},   # known by name
        {"name": "CATL", "kind": "company"},       # known by alias
        {"name": "清陶能源", "kind": "company"},
    ]
    counts = await chain.record_candidates(cands, "research:q-1")
    assert counts == {"new": 1, "bumped": 0, "known": 2}
    counts = await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "research:q-2")
    assert counts == {"new": 0, "bumped": 1, "known": 0}
    row = await db.query_one("SELECT * FROM chain_candidates WHERE name = '清陶能源'")
    assert row["mention_count"] == 2
    assert row["first_seen_ref"] == "research:q-1"  # first sighting preserved


async def test_promote_candidate_manual_with_security_and_conflict():
    await _mk_security("CATL.US")
    await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "research:q-1")
    cid = await _candidate_id("清陶能源")

    res = await chain.promote_candidate(cid, "company", "CATL.US")
    assert res["merged"] is False
    assert res["node"]["name"] == "清陶能源"
    assert res["node"]["security_id"] == "CATL.US"
    cand = await db.query_one("SELECT status FROM chain_candidates WHERE id = ?", (cid,))
    assert cand["status"] == "promoted"

    with pytest.raises(chain.PromoteConflict):        # conditional claim: only once
        await chain.promote_candidate(cid, "company")
    with pytest.raises(LookupError):
        await chain.promote_candidate("missing", "company")

    await chain.record_candidates([{"name": "卫蓝新能源", "kind": "company"}], "research:q-2")
    cid2 = await _candidate_id("卫蓝新能源")
    with pytest.raises(chain.ChainError, match="kind"):
        await chain.promote_candidate(cid2, "empire")
    with pytest.raises(chain.ChainError, match="not found"):
        await chain.promote_candidate(cid2, "company", "NOPE.US")


async def test_promote_merges_into_existing_node():
    node = await chain.create_node("比亚迪", "company")
    await chain.record_candidates([{"name": "比亚迪", "kind": "company"}], "r:1")
    # record_candidates skips known names — inject the stale candidate directly
    # (it predates the node in the real race this guards against)
    if await db.query_one("SELECT id FROM chain_candidates WHERE name='比亚迪'") is None:
        await db.execute(
            "INSERT INTO chain_candidates (id, name, kind_guess, first_seen_ref, mention_count, status, created_at) "
            "VALUES ('cand-byd', '比亚迪', 'company', 'r:1', 1, 'pending', ?)",
            (bus.now_iso(),),
        )
    cid = await _candidate_id("比亚迪")
    res = await chain.promote_candidate(cid, "company")
    assert res["merged"] is True and res["node"]["id"] == node["id"]
    assert len(await db.query("SELECT * FROM chain_nodes WHERE name='比亚迪'")) == 1


async def test_auto_promotion_threshold_default_and_admin_override():
    assert await chain.get_promote_threshold() == 3
    for ref in ("r:1", "r:2"):
        await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], ref)
    assert (await chain.tick())["auto_promoted"] == 0          # 2 < 3: stays pending
    assert await db.query_one("SELECT id FROM chain_nodes WHERE name='清陶能源'") is None

    await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "r:3")
    assert (await chain.tick())["auto_promoted"] == 1          # 3 >= 3: promoted
    node = await db.query_one("SELECT * FROM chain_nodes WHERE name='清陶能源'")
    assert node is not None and node["kind"] == "company"
    cand = await db.query_one("SELECT status FROM chain_candidates WHERE name='清陶能源'")
    assert cand["status"] == "promoted"

    # admin_state override drops the bar to 2
    await chain.set_promote_threshold(2)
    assert await chain.get_promote_threshold() == 2
    for ref in ("r:4", "r:5"):
        await chain.record_candidates([{"name": "卫蓝新能源", "kind": "weird"}], ref)
    assert (await chain.tick())["auto_promoted"] == 1
    node = await db.query_one("SELECT * FROM chain_nodes WHERE name='卫蓝新能源'")
    assert node["kind"] == "other"                             # bad kind_guess degrades
    with pytest.raises(chain.ChainError):
        await chain.set_promote_threshold(0)


async def test_tick_consumes_artifact_events_once():
    """The events.id cursor: one extraction task per artifact, replays are free."""
    await bus.emit("research.completed", "research", "q-1", {
        "topic": "固态电池", "summary": "ENTITY: 清陶能源 | company", "session_id": None,
    })
    first = await chain.tick()
    assert first["events"] == 1
    assert await db.query_one("SELECT id FROM chain_candidates WHERE name='清陶能源'") is not None
    tasks = await db.query("SELECT id FROM tasks WHERE source = 'chain'")
    assert len(tasks) == 1

    second = await chain.tick()                     # cursor advanced: nothing to do
    assert second["events"] == 0
    tasks = await db.query("SELECT id FROM tasks WHERE source = 'chain'")
    assert len(tasks) == 1


# ==== entity footer ============================================================

async def test_entity_footer_orders_by_first_appearance():
    await chain.create_node("宁德时代", "company", aliases=["CATL"])
    await chain.create_node("比亚迪", "company")
    footer = await chain.entity_footer("比亚迪降价之后，CATL 的应对策略。")
    assert footer == "## Entities\n[[比亚迪]] [[宁德时代]]"   # alias hit links the node name
    assert await chain.entity_footer("没有任何已知实体。") == ""
    assert await chain.entity_footer("") == ""


async def test_entity_footer_slugs_hostile_names_with_display_alias():
    await chain.create_node("AMD/Xilinx", "company")
    footer = await chain.entity_footer("AMD/Xilinx 合并后的 FPGA 路线图。")
    assert footer == "## Entities\n[[AMD-Xilinx|AMD/Xilinx]]"


# ==== graph API ================================================================

async def _linear_chain() -> list[dict]:
    a = await chain.create_node("矿商甲", "company")
    b = await chain.create_node("材料乙", "company")
    c = await chain.create_node("电池丙", "company")
    d = await chain.create_node("整车丁", "company")
    await chain.add_edge(a["id"], b["id"], "supplier_of")
    await chain.add_edge(b["id"], c["id"], "supplier_of")
    await chain.add_edge(c["id"], d["id"], "supplier_of")
    return [a, b, c, d]


async def test_graph_depth_truncation(client):
    a, b, c, d = await _linear_chain()

    r = await client.get("/api/chain/graph", params={"center": a["id"], "depth": 1})
    assert r.status_code == 200
    g = r.json()
    assert {n["name"] for n in g["nodes"]} == {"矿商甲", "材料乙"}
    assert len(g["edges"]) == 1

    r = await client.get("/api/chain/graph", params={"center": a["id"], "depth": 2})
    g = r.json()
    assert {n["name"] for n in g["nodes"]} == {"矿商甲", "材料乙", "电池丙"}
    assert {n["name"]: n["distance"] for n in g["nodes"]}["电池丙"] == 2
    assert len(g["edges"]) == 2

    r = await client.get("/api/chain/graph", params={"center": a["id"], "depth": 99})
    g = r.json()
    assert g["depth"] == chain.MAX_GRAPH_DEPTH == 3            # clamped
    assert {n["name"] for n in g["nodes"]} == {"矿商甲", "材料乙", "电池丙", "整车丁"}

    r = await client.get("/api/chain/graph", params={"center": "材料乙", "depth": 1})
    assert r.status_code == 200                                 # center by exact name
    assert {n["name"] for n in r.json()["nodes"]} == {"矿商甲", "材料乙", "电池丙"}

    r = await client.get("/api/chain/graph", params={"center": "不存在"})
    assert r.status_code == 404


async def test_edges_idempotent_and_validated():
    a = await chain.create_node("矿商甲", "company")
    b = await chain.create_node("材料乙", "company")
    e1 = await chain.add_edge(a["id"], b["id"], "supplier_of", confidence=0.8, evidence_ref="research:q1")
    assert e1["created"] is True
    e2 = await chain.add_edge(a["id"], b["id"], "supplier_of")
    assert e2["created"] is False and e2["id"] == e1["id"]      # UNIQUE(src,dst,relation)
    with pytest.raises(chain.ChainError, match="self-loop"):
        await chain.add_edge(a["id"], a["id"], "supplier_of")
    with pytest.raises(chain.ChainError, match="confidence"):
        await chain.add_edge(a["id"], b["id"], "customer_of", confidence=1.5)
    with pytest.raises(LookupError):
        await chain.add_edge(a["id"], "missing", "supplier_of")


# ==== REST surface =============================================================

async def test_nodes_api_search_pagination_and_detail(client):
    ning = await chain.create_node("宁德时代", "company", aliases=["CATL"])
    byd = await chain.create_node("比亚迪", "company")
    await chain.add_edge(byd["id"], ning["id"], "competitor_of")
    await chain.backstop_tag("research", "q-1", "宁德时代产能数据。")

    r = await client.get("/api/chain/nodes")
    assert r.status_code == 200 and len(r.json()) == 2

    r = await client.get("/api/chain/nodes", params={"q": "CATL"})
    assert [n["name"] for n in r.json()] == ["宁德时代"]        # alias substring hits

    r = await client.get("/api/chain/nodes", params={"limit": 1, "offset": 1})
    assert len(r.json()) == 1

    r = await client.get("/api/chain/nodes", params={"kind": "empire"})
    assert r.status_code == 400

    r = await client.get(f"/api/chain/nodes/{ning['id']}")
    detail = r.json()
    assert detail["edges_in"][0]["src_name"] == "比亚迪"
    assert detail["mentions"][0]["artifact_ref"] == "q-1"

    r = await client.get("/api/chain/nodes/missing")
    assert r.status_code == 404


async def test_candidates_promote_and_alias_endpoints(client):
    await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "research:q-1")
    r = await client.get("/api/chain/candidates")
    cands = r.json()
    assert len(cands) == 1 and cands[0]["status"] == "pending"

    cid = cands[0]["id"]
    r = await client.post(f"/api/chain/candidates/{cid}/promote", json={"kind": "company"})
    assert r.status_code == 200 and r.json()["node"]["name"] == "清陶能源"
    r = await client.post(f"/api/chain/candidates/{cid}/promote", json={"kind": "company"})
    assert r.status_code == 409                                 # lost the claim
    r = await client.post("/api/chain/candidates/missing/promote", json={"kind": "company"})
    assert r.status_code == 404

    node_id = (await db.query_one("SELECT id FROM chain_nodes WHERE name='清陶能源'"))["id"]
    r = await client.post(f"/api/chain/nodes/{node_id}/aliases", json={"alias": "QingTao"})
    assert r.status_code == 200 and r.json()["aliases"] == ["QingTao"]
    r = await client.post(f"/api/chain/nodes/{node_id}/aliases", json={"alias": "锂"})
    assert r.status_code == 400

    r = await client.post("/api/chain/edges", json={
        "src_id": node_id, "dst_id": node_id, "relation": "supplier_of",
    })
    assert r.status_code == 400                                 # self-loop


# ==== vault projection =========================================================

async def test_entity_note_region_projection_and_human_notes_survive():
    writer = get_writer()
    ning = await chain.create_node("宁德时代", "company", aliases=["CATL"])
    tsmc = await chain.create_node("台积电", "company")
    await chain.add_edge(ning["id"], tsmc["id"], "customer_of", evidence_ref="research:q1")
    await chain.backstop_tag("research", "q-1", "宁德时代四季度排产上调。")

    rel = await chain.export_entity_note(ning["id"])
    assert rel == "Chain/宁德时代.md"
    text = (writer.root / rel).read_text(encoding="utf-8")
    assert "managed: institute" in text and REGION_BEGIN in text and REGION_END in text
    assert "kind:: company" in text
    assert "customer_of:: [[台积电]]" in text                   # Dataview inline relation
    assert "CATL" in text                                       # aliases in region body
    assert "research:q-1" in text                               # recent mention listed
    assert "aliases: [CATL]" in text                            # Obsidian alias frontmatter

    # human notes outside the region survive a regeneration (writer rule 4)
    human = text + "\n人工批注：盯 Q3 排产兑现。\n"
    (writer.root / rel).write_text(human, encoding="utf-8")
    await chain.merge_aliases(ning["id"], "寧德時代")           # changes the region content
    rel2 = await chain.export_entity_note(ning["id"])
    assert rel2 == rel                                          # in-place, no sibling
    text2 = (writer.root / rel).read_text(encoding="utf-8")
    assert "人工批注：盯 Q3 排产兑现。" in text2
    assert "寧德時代" in text2


async def test_node_updated_handler_writes_note_and_dashboards():
    writer = get_writer()
    node = await chain.create_node("台积电", "company")
    event = bus.Event(id=9, type="chain.node_updated", ref_kind="chain_node",
                      ref_id=node["id"], payload={"reason": "created"})
    await chain._on_node_updated(event)
    assert (writer.root / "Chain/台积电.md").is_file()
    dashboards = (writer.root / "_meta/Dashboards.md").read_text(encoding="utf-8")
    assert "dataview" in dashboards and "FROM \"Chain\"" in dashboards

    # incoming edge renders on the destination note too
    src = await chain.create_node("中芯国际", "company")
    await chain.add_edge(src["id"], node["id"], "competitor_of")
    await chain.export_entity_note(node["id"])
    text = (writer.root / "Chain/台积电.md").read_text(encoding="utf-8")
    assert "[[中芯国际]] —competitor_of→ 本实体" in text


# ==== REVIEW-C2 must-fix regressions ===========================================

# ---- C2-M1: periodic auto-cluster / alias merge -------------------------------

async def test_auto_cluster_normalized_equal_merges_candidate_as_alias():
    """A pending candidate that is a case/whitespace variant of a known node's
    alias folds into that node: status=merged, merged_into set, surface form
    joins the aliases, sightings become real mentions."""
    node = await chain.create_node("宁德时代", "company", aliases=["CATL"])
    await chain.record_candidates(
        [{"name": "catl", "kind": "company"}], "research:q-9", text="catl 出货量创新高。",
    )

    result = await chain.tick()
    assert result["clustered"] == 1 and result["auto_promoted"] == 0

    cand = await db.query_one("SELECT * FROM chain_candidates WHERE name = 'catl'")
    assert cand["status"] == "merged" and cand["merged_into"] == node["id"]
    assert "catl" in (await chain.get_node(node["id"]))["aliases"]
    mention = await db.query_one(
        "SELECT * FROM chain_mentions WHERE node_id = ? AND artifact_kind='research' AND artifact_ref='q-9'",
        (node["id"],),
    )
    assert mention is not None and "catl" in mention["snippet"]

    # merged candidates never re-cluster or promote
    assert (await chain.tick())["clustered"] == 0


async def test_auto_cluster_containment_folds_long_form_name():
    node = await chain.create_node("宁德时代", "company")
    await chain.record_candidates(
        [{"name": "宁德时代股份有限公司", "kind": "company"}], "research:q-1",
    )
    assert (await chain.tick())["clustered"] == 1
    cand = await db.query_one(
        "SELECT * FROM chain_candidates WHERE name = '宁德时代股份有限公司'"
    )
    assert cand["status"] == "merged" and cand["merged_into"] == node["id"]
    # the long form now backstop-resolves to the node as an alias
    assert await chain.backstop_tag("research", "q-2", "宁德时代股份有限公司公告扩产") == [node["id"]]


async def test_auto_cluster_skips_short_fragments_and_ambiguous_matches():
    await chain.create_node("宁德时代", "company")
    await chain.create_node("时代新材", "company")
    await chain.create_node("固态电池", "technology")
    await chain.record_candidates([
        {"name": "电池", "kind": "other"},            # 2 chars: too short to contain-match
        {"name": "宁德时代新材", "kind": "company"},  # contains BOTH 宁德时代 and 时代新材: ambiguous
        {"name": "全固态电池技术", "kind": "technology"},  # contains exactly one node
    ], "research:q-1")

    assert (await chain.tick())["clustered"] == 1  # only the unambiguous one
    rows = {r["name"]: r["status"] for r in await db.query("SELECT name, status FROM chain_candidates")}
    assert rows["电池"] == "pending"
    assert rows["宁德时代新材"] == "pending"
    assert rows["全固态电池技术"] == "merged"


# ---- C2-M2: promotion backfills candidate sightings into chain_mentions -------

async def test_promotion_backfills_all_sightings_as_mentions():
    """REVIEW-C2 M2 reproduction: a candidate sighted in 3 artifacts must own 3
    chain_mentions rows after auto-promotion (was 0)."""
    refs = ("research:q-1", "research:q-2", "whiteboard:b-1")
    for ref in refs:
        await chain.record_candidates(
            [{"name": "清陶能源", "kind": "company"}], ref, text="清陶能源半固态出货。",
        )
    assert (await chain.tick())["auto_promoted"] == 1

    node = await db.query_one("SELECT * FROM chain_nodes WHERE name = '清陶能源'")
    mentions = await db.query("SELECT * FROM chain_mentions WHERE node_id = ?", (node["id"],))
    assert {(m["artifact_kind"], m["artifact_ref"]) for m in mentions} == {
        ("research", "q-1"), ("research", "q-2"), ("whiteboard", "b-1"),
    }
    assert all("清陶能源" in (m["snippet"] or "") for m in mentions)

    # backfill and backstop share the UNIQUE(node, kind, ref) contract: re-tagging
    # an already-backfilled artifact adds nothing
    assert await chain.backstop_tag("research", "q-1", "清陶能源新产线投产。") == []
    mentions = await db.query("SELECT * FROM chain_mentions WHERE node_id = ?", (node["id"],))
    assert len(mentions) == 3


async def test_manual_promotion_also_backfills_mentions():
    await chain.record_candidates(
        [{"name": "卫蓝新能源", "kind": "company"}], "research:q-7", text="卫蓝新能源中标。",
    )
    cid = await _candidate_id("卫蓝新能源")
    res = await chain.promote_candidate(cid, "company")
    assert res["merged"] is False
    rows = await db.query("SELECT * FROM chain_mentions WHERE node_id = ?", (res["node"]["id"],))
    assert [(r["artifact_kind"], r["artifact_ref"]) for r in rows] == [("research", "q-7")]
    cand = await db.query_one("SELECT merged_into FROM chain_candidates WHERE id = ?", (cid,))
    assert cand["merged_into"] == res["node"]["id"]


async def test_promote_failure_rolls_back_claim_and_node(monkeypatch):
    """REVIEW-C2 S1: claim + node insert + backfill are ONE transaction — a
    crash mid-promotion leaves the candidate pending and retryable, never a
    stranded 'promoted' row without a node."""
    await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "research:q-1")
    cid = await _candidate_id("清陶能源")

    async def boom(*a, **k):
        raise RuntimeError("boom mid-promotion")

    monkeypatch.setattr(chain, "_assign_slug", boom)
    with pytest.raises(RuntimeError, match="mid-promotion"):
        await chain.promote_candidate(cid, "company")
    cand = await db.query_one("SELECT status FROM chain_candidates WHERE id = ?", (cid,))
    assert cand["status"] == "pending"                          # claim rolled back
    assert await db.query_one("SELECT id FROM chain_nodes WHERE name = '清陶能源'") is None

    monkeypatch.undo()
    res = await chain.promote_candidate(cid, "company")         # retry succeeds
    assert res["merged"] is False and res["node"]["name"] == "清陶能源"


# ---- C2-M3: persisted unique slug — colliding names never share a note --------

async def test_slug_collision_keeps_one_note_per_node():
    """REVIEW-C2 M3 reproduction: "A/B" and "A:B" both _slug() to "A-B" — they
    must project to two different notes that survive re-export."""
    writer = get_writer()
    a = await chain.create_node("A/B", "company")
    b = await chain.create_node("A:B", "company")
    assert a["slug"] == "A-B"
    assert b["slug"] != a["slug"] and b["slug"].startswith("A-B-")

    rel_a = await chain.export_entity_note(a["id"])
    rel_b = await chain.export_entity_note(b["id"])
    assert rel_a == "Chain/A-B.md" and rel_a != rel_b
    assert "# A/B" in (writer.root / rel_a).read_text(encoding="utf-8")
    assert "# A:B" in (writer.root / rel_b).read_text(encoding="utf-8")

    # re-export stays in place and does not cross-clobber
    assert await chain.export_entity_note(a["id"]) == rel_a
    assert "# A/B" in (writer.root / rel_a).read_text(encoding="utf-8")

    # footer links target the persisted slugs, one per node
    footer = await chain.entity_footer("对比 A/B 与 A:B 的定价。")
    assert f"[[{a['slug']}|A/B]]" in footer and f"[[{b['slug']}|A:B]]" in footer


async def test_slug_truncation_collision_gets_unique_suffix():
    long_a = "很" * 79 + "甲"
    long_b = "很" * 79 + "乙"                                   # same first 80 chars? no — 80th differs
    long_c = "很" * 80 + "丙"                                   # truncates to 很*80 …
    long_d = "很" * 80 + "丁"                                   # … and so does this one
    a = await chain.create_node(long_a, "company")
    b = await chain.create_node(long_b, "company")
    c = await chain.create_node(long_c, "company")
    d = await chain.create_node(long_d, "company")
    slugs = {n["slug"] for n in (a, b, c, d)}
    assert len(slugs) == 4                                      # all distinct, DB UNIQUE holds
    assert c["slug"] == "很" * 80                               # first claimant keeps the plain slug
    assert d["slug"].endswith(d["id"])                          # collider carries its node id


# ---- C2-M4: one term resolves to exactly one node -----------------------------

async def test_create_node_rejects_name_that_is_anothers_alias():
    """REVIEW-C2 M4 reproduction: with 宁德时代(alias CATL) present, creating a
    node named CATL must fail — and the backstop must keep resolving CATL to
    exactly one node."""
    ning = await chain.create_node("宁德时代", "company", aliases=["CATL"])
    with pytest.raises(chain.ChainError, match="resolves to another node"):
        await chain.create_node("CATL", "company")
    # same-name duplicate is also a clean ChainError now (checked in-transaction)
    with pytest.raises(chain.ChainError, match="resolves to another node"):
        await chain.create_node("宁德时代", "company")

    new = await chain.backstop_tag("research", "r-1", "CATL 的产能布局。")
    assert new == [ning["id"]]
    rows = await db.query(
        "SELECT * FROM chain_mentions WHERE artifact_kind='research' AND artifact_ref='r-1'"
    )
    assert len(rows) == 1                                       # one term, one node, one mention


async def test_promote_candidate_matching_alias_merges_into_owner():
    """A stale pending candidate whose name became another node's alias merges
    into that node instead of creating a conflicting one."""
    ning = await chain.create_node("宁德时代", "company")
    await db.execute(
        "INSERT INTO chain_candidates (id, name, kind_guess, first_seen_ref, mention_count, status, created_at) "
        "VALUES ('cand-catl', 'CATL', 'company', 'research:q-1', 1, 'pending', ?)",
        (bus.now_iso(),),
    )
    await chain.merge_aliases(ning["id"], "CATL")               # alias lands AFTER the candidate
    res = await chain.promote_candidate("cand-catl", "company")
    assert res["merged"] is True and res["node"]["id"] == ning["id"]
    assert await db.query_one("SELECT id FROM chain_nodes WHERE name = 'CATL'") is None


# ---- C2-M5: cursor crash-replay must not double-count candidates --------------

async def test_cursor_crash_replay_does_not_double_count(monkeypatch):
    """REVIEW-C2 M5 reproduction: candidate work committed but _set_cursor()
    never ran (crash window). The replay re-processes the same event; the
    sighting UNIQUE key keeps mention_count at 1 — no phantom promotion."""
    await bus.emit("research.completed", "research", "q-1", {
        "topic": "固态电池", "summary": "ENTITY: 清陶能源 | company", "session_id": None,
    })

    async def crashed_set_cursor(event_id: int) -> None:
        return None                                             # cursor write lost

    monkeypatch.setattr(chain, "_set_cursor", crashed_set_cursor)
    first = await chain.tick()
    assert first["events"] == 1
    row = await db.query_one("SELECT mention_count FROM chain_candidates WHERE name='清陶能源'")
    assert row["mention_count"] == 1

    monkeypatch.undo()                                          # process restarts
    replay = await chain.tick()                                 # same event replays fully
    assert replay["events"] == 1
    row = await db.query_one("SELECT * FROM chain_candidates WHERE name='清陶能源'")
    assert row["mention_count"] == 1                            # NOT 2 (was the bug)
    assert row["status"] == "pending"                           # threshold 3 untouched
    sightings = await db.query("SELECT * FROM chain_candidate_sightings")
    assert len(sightings) == 1

    third = await chain.tick()                                  # cursor advanced now
    assert third["events"] == 0


async def test_record_candidates_same_artifact_is_idempotent():
    """Direct unit check of the sighting key: one artifact can never add more
    than one count, distinct artifacts each add exactly one."""
    for _ in range(3):
        await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "research:q-1")
    row = await db.query_one("SELECT mention_count FROM chain_candidates WHERE name='清陶能源'")
    assert row["mention_count"] == 1
    await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "research:q-2")
    row = await db.query_one("SELECT mention_count FROM chain_candidates WHERE name='清陶能源'")
    assert row["mention_count"] == 2


# ---- C2-M2 backfill channel: POST /api/chain/reproject -------------------------
# Historical source notes exported BEFORE a node existed never got its footer;
# reproject_footers() re-reads them from disk, recomputes ## Entities against
# the current node set and rewrites through the writer.

async def _mk_old_note(rel: str, body: str, *, kind: str = "research",
                       artifact_id: str = "r-1") -> str:
    """A ledgered file-mode note the way the exporter would have written it —
    dated frontmatter marks it as pre-existing history."""
    written = await get_writer().write_note(
        rel, {"type": kind, "topic": "旧文", "created": "2026-07-01"}, body,
        artifact_kind=kind, artifact_id=artifact_id,
    )
    assert written == rel
    return rel


async def test_reproject_backfills_footer_on_old_file_note():
    writer = get_writer()
    rel = await _mk_old_note("Research/旧题/2026-07-01 深度报告.md",
                             "## 核心结论\n\n宁德时代四季度排产上调。")
    assert "## Entities" not in (writer.root / rel).read_text(encoding="utf-8")

    await chain.create_node("宁德时代", "company")   # the node arrives AFTER the export
    res = await chain.reproject_footers()
    assert res["reprojected"] == 1 and res["conflicts"] == 0

    text = (writer.root / rel).read_text(encoding="utf-8")
    assert "## Entities\n[[宁德时代]]" in text
    assert text.count("## Entities") == 1
    assert "## 核心结论\n\n宁德时代四季度排产上调。" in text   # body untouched
    assert "created: 2026-07-01" in text and "topic: 旧文" in text  # metadata survives
    row = await db.query_one("SELECT state, mode FROM vault_index WHERE path = ?", (rel,))
    assert row["state"] == "clean" and row["mode"] == "file"

    # idempotent: the footer is current now — nothing rewrites, nothing moves
    before = (writer.root / rel).read_text(encoding="utf-8")
    res2 = await chain.reproject_footers()
    assert res2["reprojected"] == 0 and res2["conflicts"] == 0 and res2["skipped"] >= 1
    assert (writer.root / rel).read_text(encoding="utf-8") == before


async def test_reproject_region_note_updates_footer_and_keeps_annotations():
    """Region-mode notes (memory): the recomputed footer lands INSIDE the
    managed region and human annotations outside the markers survive."""
    writer = get_writer()
    rel = "Analysts/macro-analyst/memory.md"
    await writer.write_note(
        rel, {"type": "memory", "analyst": "macro-analyst"},
        "# 常备记忆\n\n持续跟踪比亚迪出货节奏。",
        artifact_kind="memory", artifact_id="macro-analyst", region=True,
    )
    path = writer.root / rel
    path.write_text(path.read_text(encoding="utf-8") + "\n人工批注：保留我。\n", encoding="utf-8")

    await chain.create_node("比亚迪", "company")
    res = await chain.reproject_footers(kind="memory")
    assert res["reprojected"] == 1 and res["conflicts"] == 0

    text = path.read_text(encoding="utf-8")
    assert "人工批注：保留我。" in text                        # outside-region content survives
    assert "## Entities\n[[比亚迪]]" in text
    begin, end = text.index(REGION_BEGIN), text.index(REGION_END)
    assert begin < text.index("## Entities") < end             # footer INSIDE the region
    row = await db.query_one("SELECT state FROM vault_index WHERE path = ?", (rel,))
    assert row["state"] == "clean"                             # in-place, no sibling

    assert (await chain.reproject_footers(kind="memory"))["reprojected"] == 0


async def test_reproject_reports_human_edited_note_and_leaves_it_alone():
    writer = get_writer()
    rel = await _mk_old_note("Research/编辑过/2026-07-01 深度报告.md",
                             "宁德时代产能布局。", artifact_id="r-2")
    path = writer.root / rel
    edited = path.read_text(encoding="utf-8") + "\n人工改动。\n"
    path.write_text(edited, encoding="utf-8")

    await chain.create_node("宁德时代", "company")
    res = await chain.reproject_footers()
    assert res["conflicts"] == 1 and res["reprojected"] == 0
    assert path.read_text(encoding="utf-8") == edited          # untouched
    # no conflict sibling was manufactured by the sweep
    assert [p.name for p in (writer.root / "Research/编辑过").glob("*.md")] == [path.name]
    row = await db.query_one("SELECT state FROM vault_index WHERE path = ?", (rel,))
    assert row["state"] == "clean"                             # flagging stays with real writes


async def test_reproject_kind_filter_cap_and_footer_refresh():
    writer = get_writer()
    await chain.create_node("宁德时代", "company")
    for i in range(3):
        await _mk_old_note(f"Research/题{i}/report.md", "宁德时代与比亚迪的对比。",
                           artifact_id=f"r-{i}")
    await _mk_old_note("Briefing/2026-07-01 晨会简报.md", "比亚迪销量走强。",
                       kind="briefing", artifact_id="brf-1")

    # kind filter: only the briefing moves (its footer is empty — 比亚迪 has no node yet…
    # 宁德时代 does not appear in it either, so nothing to write)
    res = await chain.reproject_footers(kind="briefing")
    assert res == {"reprojected": 0, "skipped": 1, "conflicts": 0}

    # research notes mention 宁德时代 → three rewrites pending; cap=2 stops early
    res = await chain.reproject_footers(kind="research", cap=2)
    assert res["reprojected"] == 2
    res = await chain.reproject_footers(kind="research", cap=2)
    assert res["reprojected"] == 1                             # the remainder lands
    assert (await chain.reproject_footers(kind="research"))["reprojected"] == 0

    # a NEW node refreshes existing footers in place (old footer replaced, not stacked)
    await chain.create_node("比亚迪", "company")
    res = await chain.reproject_footers()
    assert res["reprojected"] == 4                             # 3 research + 1 briefing
    text = (writer.root / "Research/题0/report.md").read_text(encoding="utf-8")
    assert "## Entities\n[[宁德时代]] [[比亚迪]]" in text       # first-appearance order
    assert text.count("## Entities") == 1
    briefing = (writer.root / "Briefing/2026-07-01 晨会简报.md").read_text(encoding="utf-8")
    assert "## Entities\n[[比亚迪]]" in briefing


async def test_reproject_api_and_validation(client):
    with pytest.raises(chain.ChainError, match="unknown reproject kind"):
        await chain.reproject_footers(kind="chain-node")       # entity notes are not sources
    r = await client.post("/api/chain/reproject", json={"kind": "nope"})
    assert r.status_code == 400

    await _mk_old_note("Research/api/report.md", "宁德时代新产线。", artifact_id="r-api")
    await chain.create_node("宁德时代", "company")
    r = await client.post("/api/chain/reproject", json={"kind": "research"})
    assert r.status_code == 200
    body = r.json()
    assert body["reprojected"] == 1 and body["conflicts"] == 0
    assert "## Entities\n[[宁德时代]]" in (
        get_writer().root / "Research/api/report.md").read_text(encoding="utf-8")

    r = await client.post("/api/chain/reproject", json={})     # all defaults
    assert r.status_code == 200 and r.json()["reprojected"] == 0
