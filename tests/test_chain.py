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

import asyncio
import json
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


async def test_match_hits_scans_only_the_capped_prefix():
    """MATCH_TEXT_CAP: mention detection binds a bounded PREFIX of the
    artifact into the instr() scan (the full text reaches the 200KB output
    cap and would hog the single shared connection twice per artifact).
    Mentions inside the cap link; a mention living ONLY beyond it does not —
    the accepted audit trade-off."""
    node = await chain.create_node("宁德时代", "company")

    head = "填充" * (chain.MATCH_TEXT_CAP // 2 - 10) + "宁德时代"
    assert await chain.backstop_tag("research", "cap-head", head) == [node["id"]]

    tail = "填" * (chain.MATCH_TEXT_CAP + 100) + "宁德时代"
    assert await chain.backstop_tag("research", "cap-tail", tail) == []
    assert await chain.entity_footer(tail) == ""


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


def test_parse_properties_fixture():
    parsed = chain.parse_properties("""\
PROPERTY: 宁德时代 | Annual Capacity | 100 GWh | 2026-Q2
PROPERTY: 宁德时代 | Annual Capacity | 100 GWh | 2026-Q2
PROPERTY: 锂 | spot_price | 85,000 元/吨 | 2026-07-20
PROPERTY: 宁德时代 | missing_period | value |
""")
    assert parsed == [
        {
            "entity": "宁德时代", "key": "annual_capacity",
            "value": "100 GWh", "as_of": "2026-Q02",   # canonical zero-padded period
        },
    ]
    assert chain.parse_properties("NONE") == []


async def test_extract_entities_echo_roundtrip():
    """The echo hand echoes the prompt back, so ENTITY: lines inside the
    artifact text come home and parse — the whole executor path runs."""
    cands = await chain.extract_entities(FIXTURE_EXTRACTION)
    assert {c["name"] for c in cands} == {"宁德时代", "固态电池", "特斯拉"}
    task = await db.query_one("SELECT * FROM tasks WHERE source = 'chain'")
    assert task is not None and task["status"] == "completed"


async def test_extract_entities_prompt_carries_no_bare_entity_line():
    """No template line may start with a parser prefix: echo would make a
    phantom entity/property on every extraction."""
    rendered = chain.ENTITY_EXTRACT_PROMPT.format(text="占位文本")
    assert not [
        line for line in rendered.splitlines()
        if line.strip().startswith(("ENTITY:", "PROPERTY:"))
    ]
    assert chain.parse_extraction(f"[echo] {rendered}") == []
    assert chain.parse_properties(f"[echo] {rendered}") == []


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


# ==== entity properties: supersede / conflict / resolution ====================

async def test_property_supersede_keeps_current_and_history():
    node = await chain.create_node("宁德时代", "company")
    first = await chain.upsert_property(
        node["id"], "annual capacity", "100 GWh", "2026-Q1", "research:r-1",
    )
    assert first["created"] is True and first["status"] == "active"

    replay = await chain.upsert_property(
        node["id"], "annual capacity", "100 GWh", "2026-Q1", "research:r-1",
    )
    assert replay["created"] is False and replay["id"] == first["id"]

    second = await chain.upsert_property(
        node["id"], "annual capacity", "120 GWh", "2026-Q2", "research:r-2",
    )
    assert second["status"] == "active" and second["supersedes_id"] == first["id"]

    props = await chain.get_properties(node["id"])
    assert [(p["value"], p["status"]) for p in props["current"]] == [("120 GWh", "active")]
    assert [(p["value"], p["status"]) for p in props["history"]] == [
        ("100 GWh", "superseded"),
    ]
    assert await chain.list_conflicts() == []


async def test_property_conflict_surfaces_operator_action_and_event():
    node = await chain.create_node("宁德时代", "company")
    first = await chain.upsert_property(
        node["id"], "annual_capacity", "100 GWh", "2026-Q2", "research:r-1",
    )
    second = await chain.upsert_property(
        node["id"], "annual_capacity", "120 GWh", "2026-Q2", "whiteboard:b-1",
    )
    assert second["conflict"] is True
    conflict_group = second["conflict_group"]

    rows = await db.query(
        "SELECT * FROM chain_properties WHERE conflict_group=? ORDER BY created_at, id",
        (conflict_group,),
    )
    assert {r["id"] for r in rows} == {first["id"], second["id"]}
    assert {r["status"] for r in rows} == {"conflicted"}

    conflicts = await chain.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_group"] == conflict_group
    assert {v["value"] for v in conflicts[0]["values"]} == {"100 GWh", "120 GWh"}

    actions = await db.query(
        "SELECT * FROM operator_actions WHERE ref=?",
        (f"chain-property:{conflict_group}",),
    )
    assert len(actions) == 1
    assert actions[0]["kind"] == "other" and actions[0]["status"] == "open"
    events = await db.query(
        "SELECT * FROM events WHERE type='chain.property_conflict' AND ref_id=?",
        (conflict_group,),
    )
    assert len(events) == 1

    replay = await chain.upsert_property(
        node["id"], "annual_capacity", "120 GWh", "2026-Q2", "whiteboard:b-1",
    )
    assert replay["created"] is False and replay["id"] == second["id"]
    assert len(await db.query(
        "SELECT id FROM operator_actions WHERE ref=?",
        (f"chain-property:{conflict_group}",),
    )) == 1
    assert len(await db.query(
        "SELECT id FROM events WHERE type='chain.property_conflict' AND ref_id=?",
        (conflict_group,),
    )) == 1


async def test_property_conflict_resolution_api_roundtrip(client):
    node = await chain.create_node("台积电", "company")
    first = await chain.upsert_property(
        node["id"], "monthly_revenue", "2000 亿", "2026-06", "research:r-1",
    )
    second = await chain.upsert_property(
        node["id"], "monthly_revenue", "2074 亿", "2026-06", "daily:2026-07-10",
    )
    conflict_group = second["conflict_group"]

    r = await client.get(f"/api/chain/nodes/{node['id']}/properties")
    assert r.status_code == 200
    assert {p["status"] for p in r.json()["current"]} == {"conflicted"}

    r = await client.get("/api/chain/properties/conflicts")
    assert r.status_code == 200
    assert r.json()[0]["conflict_group"] == conflict_group

    r = await client.post(
        f"/api/chain/properties/conflicts/{conflict_group}/resolve",
        json={"winner_id": second["id"]},
    )
    assert r.status_code == 200
    assert r.json()["winner"]["id"] == second["id"]
    assert r.json()["retired_ids"] == [first["id"]]

    props = (await client.get(f"/api/chain/nodes/{node['id']}/properties")).json()
    assert [(p["id"], p["status"]) for p in props["current"]] == [
        (second["id"], "active"),
    ]
    assert [(p["id"], p["status"]) for p in props["history"]] == [
        (first["id"], "retired"),
    ]
    assert (await client.get("/api/chain/properties/conflicts")).json() == []
    action = await db.query_one(
        "SELECT status, resolution FROM operator_actions WHERE ref=?",
        (f"chain-property:{conflict_group}",),
    )
    assert action["status"] == "done" and second["id"] in action["resolution"]

    r = await client.post(
        f"/api/chain/properties/conflicts/{conflict_group}/resolve",
        json={"winner_id": first["id"]},
    )
    assert r.status_code == 409
    r = await client.post(
        "/api/chain/properties/conflicts/missing/resolve",
        json={"winner_id": first["id"]},
    )
    assert r.status_code == 404


async def test_property_resolution_concurrent_claim_has_one_winner():
    node = await chain.create_node("比亚迪", "company")
    first = await chain.upsert_property(
        node["id"], "market_share", "18%", "2026-Q2", "research:r-1",
    )
    second = await chain.upsert_property(
        node["id"], "market_share", "20%", "2026-Q2", "whiteboard:b-1",
    )
    group = second["conflict_group"]

    outcomes = await asyncio.gather(
        chain.resolve_property_conflict(group, first["id"]),
        chain.resolve_property_conflict(group, second["id"]),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, chain.PropertyConflict) for outcome in outcomes) == 1
    rows = await db.query(
        "SELECT status FROM chain_properties WHERE conflict_group=?",
        (group,),
    )
    assert {r["status"] for r in rows} == {"active", "retired"}


async def test_tick_extracts_property_after_same_sweep_promotion():
    await chain.set_promote_threshold(1)
    await bus.emit("research.completed", "research", "q-property", {
        "topic": "电池扩产",
        "summary": (
            "ENTITY: 清陶能源 | company\n"
            "PROPERTY: 清陶能源 | annual_capacity | 12 GWh | 2026-Q2"
        ),
        "session_id": None,
    })
    result = await chain.tick()
    assert result["auto_promoted"] == 1 and result["properties"] == 1
    node = await db.query_one("SELECT * FROM chain_nodes WHERE name='清陶能源'")
    prop = await db.query_one("SELECT * FROM chain_properties WHERE entity_id=?", (node["id"],))
    assert prop["prop_key"] == "annual_capacity"
    assert prop["value"] == "12 GWh"
    assert prop["as_of"] == "2026-Q02"                 # canonical zero-padded period
    assert prop["source_ref"] == "research:q-property"
    assert prop["status"] == "active"


# ==== review fixes: transactional cards / late assertions / staging cursor =====

# ---- finding 1: the operator card commits WITH the property rows ---------------

async def test_property_conflict_action_failure_rolls_back_assertion(monkeypatch):
    """A card-creation failure must roll the whole conflict transition back:
    no committed conflicted rows without their action, and the retry lands
    everything (rows + card + event) cleanly."""
    node = await chain.create_node("宁德时代", "company")
    await chain.upsert_property(
        node["id"], "annual_capacity", "100 GWh", "2026-Q2", "research:r-1",
    )

    async def boom(*a, **k):
        raise RuntimeError("action boom")

    monkeypatch.setattr(chain, "_ensure_conflict_action", boom)
    with pytest.raises(RuntimeError, match="action boom"):
        await chain.upsert_property(
            node["id"], "annual_capacity", "120 GWh", "2026-Q2", "whiteboard:b-1",
        )
    rows = await db.query(
        "SELECT value, status FROM chain_properties WHERE entity_id=?", (node["id"],),
    )
    assert [(r["value"], r["status"]) for r in rows] == [("100 GWh", "active")]
    assert await db.query("SELECT id FROM operator_actions") == []
    assert await db.query(
        "SELECT id FROM events WHERE type='chain.property_conflict'"
    ) == []

    monkeypatch.undo()
    retry = await chain.upsert_property(
        node["id"], "annual_capacity", "120 GWh", "2026-Q2", "whiteboard:b-1",
    )
    assert retry["conflict"] is True and retry["operator_action_id"] is not None
    actions = await db.query(
        "SELECT * FROM operator_actions WHERE ref=?",
        (f"chain-property:{retry['conflict_group']}",),
    )
    assert len(actions) == 1 and actions[0]["status"] == "open"
    assert len(await db.query(
        "SELECT id FROM events WHERE type='chain.property_conflict'"
    )) == 1


async def test_property_bus_mirror_failure_never_unwinds_commit(monkeypatch):
    """The post-commit bus mirror is best-effort on BOTH property paths: an
    emit failure is logged, never raised — rows and card stay committed."""
    node = await chain.create_node("台积电", "company")
    await chain.upsert_property(
        node["id"], "monthly_revenue", "2000 亿", "2026-06", "research:r-1",
    )

    async def boom(*a, **k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(bus, "emit", boom)
    second = await chain.upsert_property(
        node["id"], "monthly_revenue", "2074 亿", "2026-06", "daily:2026-07-10",
    )
    assert second["conflict"] is True
    group = second["conflict_group"]
    rows = await db.query(
        "SELECT status FROM chain_properties WHERE conflict_group=?", (group,),
    )
    assert {r["status"] for r in rows} == {"conflicted"}
    action = await db.query_one(
        "SELECT * FROM operator_actions WHERE ref=?", (f"chain-property:{group}",),
    )
    assert action is not None and action["status"] == "open"

    resolved = await chain.resolve_property_conflict(group, second["id"])
    assert resolved["winner"]["id"] == second["id"]
    action = await db.query_one(
        "SELECT status FROM operator_actions WHERE id=?", (action["id"],),
    )
    assert action["status"] == "done"
    monkeypatch.undo()
    assert await db.query(
        "SELECT id FROM events WHERE type IN "
        "('chain.property_conflict','chain.property_resolved')"
    ) == []


# ---- finding 2: late assertions vs the superseded same-period history ----------

async def test_late_assertion_conflicts_with_superseded_same_period():
    """Review reproduction: Q1/A → Q2/A → Q1/B (different value). The third
    write must dispute Q1 against the SUPERSEDED Q1 row instead of silently
    superseding the newer Q2 current."""
    node = await chain.create_node("宁德时代", "company")
    q1_a = await chain.upsert_property(
        node["id"], "annual_capacity", "100 GWh", "2026-Q1", "research:r-1",
    )
    q2_a = await chain.upsert_property(
        node["id"], "annual_capacity", "120 GWh", "2026-Q2", "research:r-2",
    )
    q1_b = await chain.upsert_property(
        node["id"], "annual_capacity", "90 GWh", "2026-Q1", "whiteboard:b-1",
    )
    assert q1_b["conflict"] is True
    group = q1_b["conflict_group"]
    rows = await db.query(
        "SELECT id, status FROM chain_properties WHERE conflict_group=?", (group,),
    )
    assert {r["id"] for r in rows} == {q1_a["id"], q1_b["id"]}
    assert {r["status"] for r in rows} == {"conflicted"}
    current = await db.query_one(
        "SELECT id FROM chain_properties WHERE entity_id=? AND status='active'",
        (node["id"],),
    )
    assert current["id"] == q2_a["id"]                  # current never went back in time

    resolved = await chain.resolve_property_conflict(group, q1_b["id"])
    assert resolved["winner"]["status"] == "superseded"  # historical winner stays history
    assert resolved["retired_ids"] == [q1_a["id"]]
    current = await db.query_one(
        "SELECT id FROM chain_properties WHERE entity_id=? AND status='active'",
        (node["id"],),
    )
    assert current["id"] == q2_a["id"]


async def test_late_assertion_records_history_without_displacing_active():
    node = await chain.create_node("比亚迪", "company")
    q2 = await chain.upsert_property(
        node["id"], "market_share", "20%", "2026-Q2", "research:r-1",
    )

    # late same-source assertion: plain history, the newer current stands
    late_same = await chain.upsert_property(
        node["id"], "market_share", "18%", "2026-Q1", "research:r-1",
    )
    assert late_same["created"] is True and late_same["status"] == "superseded"
    assert late_same["conflict"] is False and late_same["supersedes_id"] is None

    # late different-source assertion AGREEING with the period's history: no conflict
    late_agree = await chain.upsert_property(
        node["id"], "market_share", "18%", "2026-Q1", "whiteboard:b-1",
    )
    assert late_agree["status"] == "superseded" and late_agree["conflict"] is False

    props = await chain.get_properties(node["id"])
    assert [(p["id"], p["status"]) for p in props["current"]] == [(q2["id"], "active")]
    assert {p["id"] for p in props["history"]} == {late_same["id"], late_agree["id"]}
    assert await chain.list_conflicts() == []


# ---- finding 3: staged extraction vs the tick cursor ---------------------------

async def test_tick_apply_failure_keeps_cursor_and_never_replays_model(monkeypatch):
    """A property-application failure must not lose the assertion NOR replay
    the batch: the cursor advances past the staged event, the staging row
    stays pending, and the retry applies it without a new model call."""
    await chain.create_node("宁德时代", "company")
    await bus.emit("research.completed", "research", "q-apply-fail", {
        "topic": "产能",
        "summary": "PROPERTY: 宁德时代 | annual_capacity | 100 GWh | 2026-Q2",
        "session_id": None,
    })

    async def boom(*a, **k):
        raise RuntimeError("apply boom")

    monkeypatch.setattr(chain, "upsert_property", boom)
    first = await chain.tick()
    assert first["events"] == 1 and first["properties"] == 0
    staged = await db.query("SELECT status FROM chain_property_staging")
    assert [r["status"] for r in staged] == ["pending"]
    assert len(await db.query("SELECT id FROM tasks WHERE source='chain'")) == 1

    second = await chain.tick()                     # still broken: no batch replay
    assert second["events"] == 0 and second["properties"] == 0
    assert len(await db.query("SELECT id FROM tasks WHERE source='chain'")) == 1
    staged = await db.query("SELECT status FROM chain_property_staging")
    assert [r["status"] for r in staged] == ["pending"]

    monkeypatch.undo()
    third = await chain.tick()                      # retried from staging, no model call
    assert third["events"] == 0 and third["properties"] == 1
    assert len(await db.query("SELECT id FROM tasks WHERE source='chain'")) == 1
    staged = await db.query("SELECT status FROM chain_property_staging")
    assert [r["status"] for r in staged] == ["applied"]
    node = await db.query_one("SELECT id FROM chain_nodes WHERE name='宁德时代'")
    prop = await db.query_one(
        "SELECT * FROM chain_properties WHERE entity_id=?", (node["id"],),
    )
    assert prop["value"] == "100 GWh" and prop["status"] == "active"


async def test_tick_staging_persistence_failure_halts_batch_before_cursor(monkeypatch):
    """If the durable staging write itself fails, the batch halts BEFORE that
    event's cursor: the event replays next tick (the one case that re-runs
    the model call) instead of silently dropping the paid-for assertions."""
    await chain.create_node("宁德时代", "company")
    await bus.emit("research.completed", "research", "q-stage-fail", {
        "topic": "产能",
        "summary": "PROPERTY: 宁德时代 | annual_capacity | 100 GWh | 2026-Q2",
        "session_id": None,
    })

    async def boom(*a, **k):
        raise RuntimeError("staging boom")

    monkeypatch.setattr(chain, "_stage_properties", boom)
    first = await chain.tick()
    assert first["events"] == 0 and first["properties"] == 0   # cursor held back
    assert len(await db.query("SELECT id FROM tasks WHERE source='chain'")) == 1
    assert await db.query("SELECT id FROM chain_property_staging") == []

    monkeypatch.undo()
    second = await chain.tick()                     # replays ONLY the unpersisted event
    assert second["events"] == 1 and second["properties"] == 1
    assert len(await db.query("SELECT id FROM tasks WHERE source='chain'")) == 2
    node = await db.query_one("SELECT id FROM chain_nodes WHERE name='宁德时代'")
    assert await db.query_one(
        "SELECT id FROM chain_properties WHERE entity_id=?", (node["id"],),
    ) is not None


async def test_staged_property_unknown_entity_skipped_after_bounded_attempts(monkeypatch):
    """An entity that never promotes costs the row one attempt per sweep and
    is only terminally 'skipped' at the STAGING_UNKNOWN_ATTEMPTS bound — not
    on the first miss (REVIEW-R2 P1-3)."""
    monkeypatch.setattr(chain, "STAGING_UNKNOWN_ATTEMPTS", 2)
    await bus.emit("research.completed", "research", "q-unknown", {
        "topic": "x",
        "summary": "PROPERTY: 神秘公司 | annual_capacity | 1 GWh | 2026-Q2",
        "session_id": None,
    })
    result = await chain.tick()
    assert result["events"] == 1 and result["properties"] == 0
    staged = await db.query("SELECT status, attempts FROM chain_property_staging")
    assert [(r["status"], r["attempts"]) for r in staged] == [("pending", 1)]

    assert (await chain.tick())["properties"] == 0             # second miss: the bound
    staged = await db.query("SELECT status, attempts FROM chain_property_staging")
    assert [(r["status"], r["attempts"]) for r in staged] == [("skipped", 2)]
    assert (await chain.tick())["properties"] == 0             # terminal: no more retries
    staged = await db.query("SELECT status, attempts FROM chain_property_staging")
    assert [(r["status"], r["attempts"]) for r in staged] == [("skipped", 2)]
    assert await db.query("SELECT id FROM chain_properties") == []


# ==== R2 adversarial review regressions ========================================

# ---- R2 P1-1: period strings are stored zero-padded ----------------------------

async def test_period_normalization_blocks_short_month_displacing_current():
    """R2 reproduction: raw string comparison ranked '2026-2' ABOVE '2026-10',
    so a February assertion displaced the October current. Normalized periods
    ('2026-02') compare chronologically and land as late history; spelling
    variants of one period share one identity."""
    node = await chain.create_node("宁德时代", "company")
    oct_row = await chain.upsert_property(
        node["id"], "monthly_revenue", "100 亿", "2026-10", "research:r-1",
    )
    feb_row = await chain.upsert_property(
        node["id"], "monthly_revenue", "90 亿", "2026-2", "whiteboard:b-1",
    )
    assert feb_row["as_of"] == "2026-02"                # canonical zero-padded form
    assert feb_row["status"] == "superseded"            # late history, was: active
    current = await db.query_one(
        "SELECT id FROM chain_properties WHERE entity_id=? AND status='active'",
        (node["id"],),
    )
    assert current["id"] == oct_row["id"]               # October stayed current

    # '2026-02' and '2026-2' are ONE period: the padded spelling is an exact replay
    replay = await chain.upsert_property(
        node["id"], "monthly_revenue", "90 亿", "2026-02", "whiteboard:b-1",
    )
    assert replay["created"] is False and replay["id"] == feb_row["id"]


async def test_mixed_quarter_and_month_periods_use_chronological_horizon():
    """Quarter/month ordering must use time, not the stored strings' alphabet.

    ``2026-Q02`` sorts after ``2026-07`` lexically because of ``Q``.  July is
    nevertheless after Q2, both when it arrives second and when the older
    quarter arrives after July.
    """
    forward = await chain.create_node("顺序公司", "company")
    q2 = await chain.upsert_property(
        forward["id"], "revenue", "Q2 value", "2026-Q2", "research:q2",
    )
    july = await chain.upsert_property(
        forward["id"], "revenue", "July value", "2026-07", "research:july",
    )
    assert q2["as_of"] == "2026-Q02"
    assert july["status"] == "active"
    current = await db.query_one(
        "SELECT id FROM chain_properties WHERE entity_id=? AND status='active'",
        (forward["id"],),
    )
    assert current["id"] == july["id"]

    reverse = await chain.create_node("逆序公司", "company")
    july_first = await chain.upsert_property(
        reverse["id"], "revenue", "July value", "2026-07", "research:july",
    )
    q2_late = await chain.upsert_property(
        reverse["id"], "revenue", "Q2 value", "2026-Q2", "research:q2",
    )
    assert q2_late["status"] == "superseded"
    current = await db.query_one(
        "SELECT id FROM chain_properties WHERE entity_id=? AND status='active'",
        (reverse["id"],),
    )
    assert current["id"] == july_first["id"]


def test_period_parser_supports_declared_formats_and_rejects_unknowns():
    assert chain._normalize_period("2026") == "2026"
    assert chain._normalize_period("2026-q2") == "2026-Q02"
    assert chain._normalize_period("2026-7") == "2026-07"
    assert chain._normalize_period("2026-7-2") == "2026-07-02"
    assert chain._period_sort_key("2026-Q2") < chain._period_sort_key("2026-07")
    assert chain._period_sort_key("2026-07") < chain._period_sort_key("2026-08-01")

    for invalid in ("2026-Q5", "2026-13", "2026-02-30", "FY2026", "2026-W7"):
        with pytest.raises(chain.ChainError, match="as_of|unsupported"):
            chain._normalize_period(invalid)


# ---- R2 P1-2: same-second corrections resolve by rowid, not uuid ---------------

async def test_same_second_correction_beats_uuid_order(monkeypatch):
    """R2 reproduction: two same-source assertions in one second tie on
    created_at; the uuid tiebreak could pick the RETRACTED value as the
    source's current word and manufacture a conflict against an agreeing
    source. The monotonic rowid tiebreak always picks the correction."""
    node = await chain.create_node("台积电", "company")
    monkeypatch.setattr(bus, "now_iso", lambda: "2026-07-20T00:00:00+00:00")
    # descending ids: uuid-DESC ordering would rank the FIRST (retracted) row
    # above its correction
    ids = iter(["ffffffffffff", "000000000000", "aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    monkeypatch.setattr(chain, "_new_id", lambda: next(ids))

    await chain.upsert_property(
        node["id"], "monthly_revenue", "2000 亿", "2026-06", "research:r-1",
    )
    corrected = await chain.upsert_property(              # same source, same second
        node["id"], "monthly_revenue", "2074 亿", "2026-06", "research:r-1",
    )
    assert corrected["status"] == "active"
    agreeing = await chain.upsert_property(               # agrees with the correction
        node["id"], "monthly_revenue", "2074 亿", "2026-06", "daily:2026-07-10",
    )
    assert agreeing["conflict"] is False                  # was: false conflict vs 2000 亿
    assert agreeing["status"] == "active"
    assert await chain.list_conflicts() == []
    assert await db.query("SELECT id FROM operator_actions") == []


# ---- R2 P1-3: promotion racing the skip check costs one attempt, not the row ---

async def test_staged_property_survives_promotion_racing_the_skip_check(monkeypatch):
    """R2 reproduction: the entity resolves as unknown, a promotion commits
    right after that stale read — the old one-shot skip swallowed the
    assertion forever. Now the miss only spends one attempt and the next
    sweep applies the row."""
    await chain._stage_properties(1, "research:q-race", [{
        "entity": "清陶能源", "key": "annual_capacity", "value": "12 GWh", "as_of": "2026-Q2",
    }])
    real_resolve = chain._resolve_node_term
    calls = {"n": 0}

    async def stale_read_then_promotion(name: str):
        calls["n"] += 1
        if calls["n"] == 1:
            await chain.create_node("清陶能源", "company")   # promotion wins the race
            return None                                       # ...after our read went stale
        return await real_resolve(name)

    monkeypatch.setattr(chain, "_resolve_node_term", stale_read_then_promotion)
    assert await chain._apply_staged_properties() == 0
    staged = await db.query("SELECT status, attempts FROM chain_property_staging")
    assert [(r["status"], r["attempts"]) for r in staged] == [("pending", 1)]  # was: skipped

    assert await chain._apply_staged_properties() == 1        # next sweep rescues it
    staged = await db.query("SELECT status FROM chain_property_staging")
    assert [r["status"] for r in staged] == ["applied"]
    node = await db.query_one("SELECT id FROM chain_nodes WHERE name='清陶能源'")
    prop = await db.query_one(
        "SELECT * FROM chain_properties WHERE entity_id=?", (node["id"],),
    )
    assert prop["value"] == "12 GWh" and prop["status"] == "active"


# ---- R2 P2-1: the cursor is a conditional claim across processes ---------------

async def test_tick_cursor_conditional_claim_never_regresses(monkeypatch):
    """R2 reproduction: a stale tick owner could overwrite (regress) the
    cursor and replay the winner's model calls. The CAS advance refuses the
    stale swap and the loser abandons its batch."""
    # unit: a stale owner (prev=0 after the cursor moved to 7) loses the swap
    assert await chain._advance_cursor(0, 7) is True
    assert await chain._advance_cursor(0, 3) is False
    assert await chain._get_cursor() == 7
    assert await chain._advance_cursor(7, 9) is True
    assert await chain._get_cursor() == 9

    # integration: a rival process claims the first event mid-batch — this
    # tick must abandon the batch without touching event 2 or the cursor
    e1 = await bus.emit("research.completed", "research", "q-cas-1", {
        "topic": "x", "summary": "第一篇正文。", "session_id": None,
    })
    e2 = await bus.emit("research.completed", "research", "q-cas-2", {
        "topic": "y", "summary": "第二篇正文。", "session_id": None,
    })
    await db.execute(                                          # reset for a clean race
        "UPDATE admin_state SET value = '0' WHERE key = ?", (chain.CURSOR_KEY,),
    )
    real_extract = chain.extract_graph_facts
    calls = {"n": 0}

    async def extract_with_rival(text: str):
        calls["n"] += 1
        if calls["n"] == 1:                                    # rival consumes event 1 first
            await db.execute(
                "UPDATE admin_state SET value = ? WHERE key = ?",
                (str(e1.id), chain.CURSOR_KEY),
            )
        return [], []

    monkeypatch.setattr(chain, "extract_graph_facts", extract_with_rival)
    result = await chain.tick()
    assert result["events"] == 0                               # claim lost: batch abandoned
    assert calls["n"] == 1                                     # event 2 was never extracted
    assert await chain._get_cursor() == e1.id                  # the rival's cursor stands

    monkeypatch.setattr(chain, "extract_graph_facts", real_extract)
    result = await chain.tick()                                # fresh claim from e1 onward
    assert result["events"] == 1
    assert await chain._get_cursor() == e2.id


# ==== loop-fix P4 / P8b / P11bc regressions =====================================

# ---- P4: poison persistence failure is bounded, then skipped with a card -------

async def test_tick_poison_persistence_skips_event_after_bounded_failures(monkeypatch):
    """A DETERMINISTIC persistence failure must not wedge the cursor forever
    (one extraction model call per hour, unbounded): after
    TICK_PERSIST_FAILURE_LIMIT attempts the event is skipped — cursor
    advances past it — and an operator card records the drop."""
    e = await bus.emit("research.completed", "research", "q-poison", {
        "topic": "x",
        "summary": "PROPERTY: 宁德时代 | annual_capacity | 100 GWh | 2026-Q2",
        "session_id": None,
    })

    async def boom(*a, **k):
        raise RuntimeError("poison staging")

    monkeypatch.setattr(chain, "_stage_properties", boom)
    for _ in range(chain.TICK_PERSIST_FAILURE_LIMIT - 1):
        r = await chain.tick()
        assert r["events"] == 0                        # halted, cursor held back
        assert await chain._get_cursor() == 0
    assert await db.query("SELECT id FROM operator_actions") == []   # no card yet

    final = await chain.tick()                         # Nth failure: drop + card
    assert final["events"] == 1                        # the skip consumed the event
    assert await chain._get_cursor() == e.id
    actions = await db.query(
        "SELECT * FROM operator_actions WHERE ref=?", (f"chain-extract:{e.id}",),
    )
    assert len(actions) == 1
    assert actions[0]["kind"] == "failed_run" and actions[0]["status"] == "open"
    assert str(e.id) in actions[0]["title"]

    monkeypatch.undo()
    again = await chain.tick()                         # nothing replays after the skip
    assert again["events"] == 0
    tasks = await db.query("SELECT id FROM tasks WHERE source='chain'")
    assert len(tasks) == chain.TICK_PERSIST_FAILURE_LIMIT   # bounded model spend


async def test_tick_drop_crash_before_cursor_never_reextracts(monkeypatch):
    """R3 P1 reproduction: the Nth failure writes the counter and opens the
    card, then the process dies BEFORE the cursor advance. Recovery must see
    the exhausted counter and drop the event WITHOUT another extraction —
    total model calls stay exactly TICK_PERSIST_FAILURE_LIMIT."""
    e = await bus.emit("research.completed", "research", "q-crash-drop", {
        "topic": "x",
        "summary": "PROPERTY: 宁德时代 | annual_capacity | 100 GWh | 2026-Q2",
        "session_id": None,
    })
    calls = {"n": 0}
    real_extract = chain.extract_graph_facts

    async def counting_extract(text: str):
        calls["n"] += 1
        return await real_extract(text)

    async def boom_stage(*a, **k):
        raise RuntimeError("poison staging")

    monkeypatch.setattr(chain, "extract_graph_facts", counting_extract)
    monkeypatch.setattr(chain, "_stage_properties", boom_stage)
    for _ in range(chain.TICK_PERSIST_FAILURE_LIMIT - 1):
        await chain.tick()                              # bounded halts
    assert calls["n"] == chain.TICK_PERSIST_FAILURE_LIMIT - 1

    real_advance = chain._advance_cursor

    async def crash_advance(prev: int, event_id: int) -> bool:
        raise RuntimeError("crash before cursor")

    monkeypatch.setattr(chain, "_advance_cursor", crash_advance)
    with pytest.raises(RuntimeError, match="crash before cursor"):
        await chain.tick()                              # Nth failure: card, then crash
    assert calls["n"] == chain.TICK_PERSIST_FAILURE_LIMIT
    assert await chain._get_cursor() == 0               # advance never landed
    assert len(await db.query(
        "SELECT id FROM operator_actions WHERE ref=?", (f"chain-extract:{e.id}",),
    )) == 1

    monkeypatch.setattr(chain, "_advance_cursor", real_advance)
    recovered = await chain.tick()                      # exhausted: drop WITHOUT extracting
    assert calls["n"] == chain.TICK_PERSIST_FAILURE_LIMIT   # was: limit+1 (the bug)
    assert recovered["events"] == 1
    assert await chain._get_cursor() == e.id
    assert len(await db.query(
        "SELECT id FROM operator_actions WHERE ref=?", (f"chain-extract:{e.id}",),
    )) == 1                                             # card stayed idempotent


async def test_tick_drop_crash_after_count_before_card_never_reextracts(monkeypatch):
    """R3 P1 second window: the Nth failure lands the counter but dies before
    the card opens. Recovery must open the card and skip the event from the
    durable counter alone — still no extra extraction."""
    e = await bus.emit("research.completed", "research", "q-crash-count", {
        "topic": "x",
        "summary": "PROPERTY: 宁德时代 | annual_capacity | 100 GWh | 2026-Q2",
        "session_id": None,
    })
    calls = {"n": 0}
    real_extract = chain.extract_graph_facts

    async def counting_extract(text: str):
        calls["n"] += 1
        return await real_extract(text)

    async def boom_stage(*a, **k):
        raise RuntimeError("poison staging")

    async def crash_card(*a, **k):
        raise RuntimeError("crash before card")

    monkeypatch.setattr(chain, "extract_graph_facts", counting_extract)
    monkeypatch.setattr(chain, "_stage_properties", boom_stage)
    for _ in range(chain.TICK_PERSIST_FAILURE_LIMIT - 1):
        await chain.tick()

    real_card = chain._open_extract_drop_action
    monkeypatch.setattr(chain, "_open_extract_drop_action", crash_card)
    crashed = await chain.tick()                        # counter hits N, card "crashes"
    assert crashed["events"] == 0                       # cursor held, no silent drop
    assert calls["n"] == chain.TICK_PERSIST_FAILURE_LIMIT
    assert await db.query("SELECT id FROM operator_actions") == []

    monkeypatch.setattr(chain, "_open_extract_drop_action", real_card)
    recovered = await chain.tick()
    assert calls["n"] == chain.TICK_PERSIST_FAILURE_LIMIT   # no extraction on recovery
    assert recovered["events"] == 1
    assert await chain._get_cursor() == e.id
    assert len(await db.query(
        "SELECT id FROM operator_actions WHERE ref=?", (f"chain-extract:{e.id}",),
    )) == 1


# ---- P8b: the auto-cluster pending scan is bounded and ages out ----------------

async def test_auto_cluster_scan_is_bounded_per_tick(monkeypatch):
    """The matching work is bounded by a total comparison budget per sweep;
    the remainder resumes on the next sweep instead of blocking the event
    loop on one unbounded pass."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 6)
    await chain.create_node("宁德时代", "company")
    now = bus.now_iso()
    for i, name in enumerate(["宁德时代一部", "宁德时代二部", "宁德时代三部"]):
        await db.execute(
            "INSERT INTO chain_candidates (id, name, kind_guess, first_seen_ref, "
            "mention_count, status, created_at) VALUES (?,?,?,?,?, 'pending', ?)",
            (f"cand-{i}", name, "company", "research:q-1", 3 - i, now),
        )
    assert (await chain.tick())["clustered"] == 2       # budget drained mid-rotation
    rows = {r["name"]: r["status"] for r in await db.query(
        "SELECT name, status FROM chain_candidates")}
    assert sorted(rows.values()) == ["merged", "merged", "pending"]
    assert (await chain.tick())["clustered"] == 1       # remainder lands next sweep


async def test_auto_cluster_per_tick_comparisons_bounded_as_nodes_grow(monkeypatch):
    """R3 P2 regression: the node side of the match is budgeted too. With 30
    nodes and a budget that covers ~11 term comparisons per sweep, one
    candidate's decision spreads across three ticks (persistent rotation
    state) instead of scanning the whole graph in one tick — and the merge
    still lands once the full rotation completes."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 12)
    await chain.create_node("宁德时代", "company")            # the sole real match
    for i in range(29):
        await chain.create_node(f"无关公司甲{i}", "company")   # growth: 30 nodes total
    await chain.record_candidates(
        [{"name": "宁德时代股份有限公司", "kind": "company"}], "research:q-grow",
    )
    assert (await chain.tick())["clustered"] == 0       # budget cap: rotation parked
    assert (await chain.tick())["clustered"] == 0       # still rotating
    assert (await chain.tick())["clustered"] == 1       # full rotation done: merge
    cand = await db.query_one(
        "SELECT status, merged_into FROM chain_candidates WHERE name='宁德时代股份有限公司'",
    )
    node = await db.query_one("SELECT id FROM chain_nodes WHERE name='宁德时代'")
    assert cand["status"] == "merged" and cand["merged_into"] == node["id"]


async def test_auto_cluster_ambiguity_across_windows_stays_pending(monkeypatch):
    """R3 P2 guard: two matching nodes that live in DIFFERENT budget windows
    must still be seen as ambiguous — a stateless node LIMIT would merge into
    the early match and never meet the late one."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 12)
    await chain.create_node("宁德时代", "company")            # early match
    for i in range(23):
        await chain.create_node(f"无关公司乙{i}", "company")
    await chain.create_node("宁德时代股份", "company")        # late match, other window
    await chain.record_candidates(
        [{"name": "宁德时代股份有限公司", "kind": "company"}], "research:q-ambig",
    )
    total = 0
    for _ in range(5):                                  # several full sweeps
        total += (await chain.tick())["clustered"]
    assert total == 0                                   # never merged into either
    cand = await db.query_one(
        "SELECT status FROM chain_candidates WHERE name='宁德时代股份有限公司'",
    )
    assert cand["status"] == "pending"


async def test_auto_cluster_rowid_reuse_invalidates_parked_matches(monkeypatch):
    """R4 P1: a parked match cannot survive a graph mutation. SQLite may
    reuse the deleted maximum rowid, so resuming only at ``rowid > cursor``
    would miss a newly promoted exact-match node and incorrectly merge into
    the old containment match."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 3)
    early = await chain.create_node("宁德时代", "company")
    deleted = await chain.create_node("无关节点甲", "company")
    await chain.record_candidates(
        [{"name": "宁德时代股份有限公司", "kind": "company"}], "research:q-rowid",
    )

    assert await chain._auto_cluster() == 0
    state = json.loads((await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (chain.CLUSTER_ROTATION_KEY,),
    ))["value"])
    assert state["node_cursor"] == 2 and state["matches"] == [early["id"]]

    await db.execute("DELETE FROM chain_nodes WHERE id = ?", (deleted["id"],))
    exact = await chain.create_node("宁德时代股份有限公司", "company")
    exact_row = await db.query_one(
        "SELECT rowid AS rid FROM chain_nodes WHERE id = ?", (exact["id"],),
    )
    assert exact_row["rid"] == 2                         # deleted max rowid was reused

    assert await chain._auto_cluster() == 0
    cand = await db.query_one(
        "SELECT status, merged_into FROM chain_candidates "
        "WHERE name='宁德时代股份有限公司'",
    )
    assert cand["status"] == "pending" and cand["merged_into"] is None


async def test_auto_cluster_alias_change_invalidates_parked_matches(monkeypatch):
    """R4 P1: alias/name surface mutations bump the graph generation too.
    A late matching alias behind a parked node cursor must force a full
    rescan, otherwise the old sole match becomes a false merge."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 3)
    early = await chain.create_node("宁德时代", "company")
    late = await chain.create_node("无关节点乙", "company")
    await chain.record_candidates(
        [{"name": "宁德时代股份有限公司", "kind": "company"}], "research:q-alias-generation",
    )

    assert await chain._auto_cluster() == 0
    state = json.loads((await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (chain.CLUSTER_ROTATION_KEY,),
    ))["value"])
    assert state["node_cursor"] == 2 and state["matches"] == [early["id"]]

    await chain.merge_aliases(late["id"], "宁德时代股份有限公司")
    assert await chain._auto_cluster() == 0
    cand = await db.query_one(
        "SELECT status, merged_into FROM chain_candidates "
        "WHERE name='宁德时代股份有限公司'",
    )
    assert cand["status"] == "pending" and cand["merged_into"] is None


async def test_auto_cluster_resumes_inside_long_alias_list(monkeypatch):
    """R4 P2: exhausting the budget inside one node's aliases persists a
    stable node id + term offset. Four sweeps must make forward progress,
    not restart from alias zero and livelock forever."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 4)
    aliases = [f"无关别名{i:02d}" for i in range(30)]
    await chain.create_node("无关主节点", "company", aliases=aliases)
    await chain.record_candidates(
        [{"name": "完全不同候选", "kind": "company"}], "research:q-alias-progress",
    )

    offsets = []
    node_ids = []
    for _ in range(4):
        assert await chain._auto_cluster() == 0
        state = json.loads((await db.query_one(
            "SELECT value FROM admin_state WHERE key = ?", (chain.CLUSTER_ROTATION_KEY,),
        ))["value"])
        offsets.append(state["term_offset"])
        node_ids.append(state["node_id"])
    assert offsets == [3, 7, 11, 15]
    assert len(set(node_ids)) == 1 and node_ids[0]


async def test_auto_cluster_513th_alias_preserves_ambiguity():
    """R5 P1 reproduction using production writes only. Node A's 513th
    alias is an exact match while node B's name is a containment match; the
    candidate must remain ambiguous/pending, never be merged into B."""
    target = "目标公司股份有限公司"
    aliases = [f"无关别名{i:03d}" for i in range(512)]
    hidden_exact = await chain.create_node("别名很多的节点甲", "company", aliases=aliases)
    await chain.record_candidates(
        [{"name": target, "kind": "company"}], "research:q-alias-513",
    )
    await chain.merge_aliases(hidden_exact["id"], target)      # legal alias number 513
    visible_containment = await chain.create_node("目标公司股份", "company")

    stored = await chain.get_node(hidden_exact["id"])
    assert len(stored["aliases"]) == 513
    assert await chain._auto_cluster() == 0
    cand = await db.query_one(
        "SELECT status, merged_into FROM chain_candidates WHERE name = ?", (target,),
    )
    assert cand["status"] == "pending" and cand["merged_into"] is None
    assert cand["merged_into"] != visible_containment["id"]


async def test_auto_cluster_scans_all_aliases_across_ticks_within_budget(monkeypatch):
    """All legal aliases are eventually scanned via term_offset while every
    tick stays within CLUSTER_COMPARE_BUDGET comparisons."""
    budget = 64
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", budget)
    target = "zzzz-target-company"
    aliases = [f"a-alias-{i:03d}" for i in range(520)]
    node = await chain.create_node("a-root-node", "company", aliases=aliases)
    await chain.record_candidates(
        [{"name": target, "kind": "company"}], "research:q-all-aliases",
    )
    await chain.merge_aliases(node["id"], target)               # alias number 521

    real_match = chain._cluster_term_matches
    calls = {"n": 0}

    def counted_match(candidate_norm: str, term: str) -> bool:
        calls["n"] += 1
        return real_match(candidate_norm, term)

    monkeypatch.setattr(chain, "_cluster_term_matches", counted_match)
    per_tick = []
    clustered = 0
    for _ in range(12):
        calls["n"] = 0
        clustered += await chain._auto_cluster()
        per_tick.append(calls["n"])
        assert calls["n"] <= budget
        if clustered:
            break

    assert clustered == 1 and len(per_tick) > 1
    cand = await db.query_one(
        "SELECT status, merged_into FROM chain_candidates WHERE name = ?", (target,),
    )
    assert cand["status"] == "merged" and cand["merged_into"] == node["id"]


async def test_auto_cluster_generation_change_resets_full_alias_progress(monkeypatch):
    """Removing the alias cap must not weaken R4 generation invalidation:
    a surface change resets a parked term offset instead of resuming it."""
    monkeypatch.setattr(chain, "CLUSTER_COMPARE_BUDGET", 4)
    aliases = [f"无关长别名{i:02d}" for i in range(30)]
    node = await chain.create_node("长别名节点", "company", aliases=aliases)
    await chain.record_candidates(
        [{"name": "完全不同候选", "kind": "company"}], "research:q-generation-reset",
    )

    assert await chain._auto_cluster() == 0
    before = json.loads((await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (chain.CLUSTER_ROTATION_KEY,),
    ))["value"])
    assert before["term_offset"] == 3

    await chain.merge_aliases(node["id"], "新加入但仍不匹配")
    assert await chain._auto_cluster() == 0
    after = json.loads((await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (chain.CLUSTER_ROTATION_KEY,),
    ))["value"])
    assert after["generation"] > before["generation"]
    assert after["term_offset"] == 3                            # restarted, not 7


async def test_auto_cluster_corrupt_rotation_self_heals_twice():
    """R4 P2: non-scalar candidate_id used to reach SQLite binding and throw
    on every sweep. Invalid state is deleted, reset, and remains healthy."""
    await chain.create_node("健康节点", "company")
    await chain.record_candidates(
        [{"name": "完全不同候选", "kind": "company"}], "research:q-corrupt-cursor",
    )
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            chain.CLUSTER_ROTATION_KEY,
            json.dumps({
                "cand_cursor": 0, "candidate_id": {"bad": "type"},
                "node_cursor": 0, "matches": [],
            }),
        ),
    )

    assert await chain._auto_cluster() == 0
    assert await chain._auto_cluster() == 0


async def test_auto_cluster_rejects_forged_cursor_and_match_evidence():
    """R4 P2: a schema-shaped but impossible high node cursor plus an
    unrelated existing node id must be discarded, never trusted as sole
    match evidence for a merge."""
    unrelated = await chain.create_node("毫不相关节点", "company")
    await chain.record_candidates(
        [{"name": "完全不同候选", "kind": "company"}], "research:q-forged-cursor",
    )
    cand = await db.query_one(
        "SELECT id FROM chain_candidates WHERE name='完全不同候选'",
    )
    generation_row = await db.query_one(
        "SELECT value FROM admin_state WHERE key='chain:graph_generation'",
    )
    generation = int(generation_row["value"]) if generation_row else 0
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            chain.CLUSTER_ROTATION_KEY,
            json.dumps({
                "version": 1,
                "generation": generation,
                "cand_cursor": 0,
                "candidate_id": cand["id"],
                "node_cursor": 2**62,
                "node_id": None,
                "term_offset": 0,
                "matches": [unrelated["id"]],
            }),
        ),
    )

    assert await chain._auto_cluster() == 0
    row = await db.query_one(
        "SELECT status, merged_into FROM chain_candidates WHERE id = ?", (cand["id"],),
    )
    assert row["status"] == "pending" and row["merged_into"] is None


async def test_auto_cluster_ages_out_stale_low_mention_candidates():
    """Pending candidates below the promote threshold age out after
    CANDIDATE_TTL_DAYS (conditional bulk claim to 'rejected'), so the pending
    pool the per-tick scans iterate stays bounded by live interest. Recent
    rows and rows at the threshold are untouched by aging."""
    now = bus.now_iso()
    ancient = "2020-01-01T00:00:00+00:00"
    for cid, name, count, created in (
        ("cand-old-low", "旧冷门公司", 1, ancient),
        ("cand-new-low", "新冷门公司", 1, now),
        ("cand-old-hot", "旧热门公司", 3, ancient),     # at threshold: promotes, never ages
    ):
        await db.execute(
            "INSERT INTO chain_candidates (id, name, kind_guess, first_seen_ref, "
            "mention_count, status, created_at) VALUES (?,?,?,?,?, 'pending', ?)",
            (cid, name, "company", "research:q-1", count, created),
        )
    result = await chain.tick()
    rows = {r["id"]: r["status"] for r in await db.query(
        "SELECT id, status FROM chain_candidates")}
    assert rows["cand-old-low"] == "rejected"
    assert rows["cand-new-low"] == "pending"
    assert rows["cand-old-hot"] == "promoted" and result["auto_promoted"] == 1


# ---- P11b: auto-promotion is capped per tick ------------------------------------

async def test_auto_promote_bounded_per_tick(monkeypatch):
    """Each promotion is a transaction plus vault-export fan-out; one tick
    promotes at most AUTO_PROMOTE_BATCH candidates and drains the rest on
    later sweeps."""
    monkeypatch.setattr(chain, "AUTO_PROMOTE_BATCH", 1)
    await chain.set_promote_threshold(1)
    await chain.record_candidates([{"name": "清陶能源", "kind": "company"}], "r:1")
    await chain.record_candidates([{"name": "卫蓝新能源", "kind": "company"}], "r:2")
    assert (await chain.tick())["auto_promoted"] == 1   # capped
    assert (await chain.tick())["auto_promoted"] == 1   # remainder next sweep
    statuses = [r["status"] for r in await db.query("SELECT status FROM chain_candidates")]
    assert statuses == ["promoted", "promoted"]


# ---- P11c: artifact file reads are clamped --------------------------------------

async def test_artifact_read_clamped_to_cap(monkeypatch, tmp_path):
    """Session-workspace report files are read through a hard byte clamp:
    a runaway artifact cannot pull megabytes into the INSTR scan and the
    extraction text assembly."""
    monkeypatch.setattr(chain, "ARTIFACT_READ_CAP", 64)
    (tmp_path / "06_深度报告.md").write_text("A" * 200 + "MARKER-BEYOND", encoding="utf-8")

    async def fake_ws(session_id):
        return tmp_path

    monkeypatch.setattr(chain, "_session_workspace", fake_ws)
    event = bus.Event(id=1, type="research.completed", ref_kind="research", ref_id="q-big",
                      payload={"topic": "题目", "summary": "摘要", "session_id": "s-1"})
    kind, ref, text = await chain._artifact_from_event(event)
    assert kind == "research" and ref == "q-big"
    assert "题目" in text and "摘要" in text
    assert "MARKER-BEYOND" not in text                  # beyond the clamp: never read
    assert "A" * 64 in text and "A" * 65 not in text


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
    """REVIEW-C2 M5 reproduction: candidate work committed but the cursor
    advance (_advance_cursor) never landed (crash window). The replay
    re-processes the same event; the sighting UNIQUE key keeps mention_count
    at 1 — no phantom promotion."""
    await bus.emit("research.completed", "research", "q-1", {
        "topic": "固态电池", "summary": "ENTITY: 清陶能源 | company", "session_id": None,
    })

    async def crashed_advance_cursor(prev: int, event_id: int) -> bool:
        return True                                             # cursor write lost

    monkeypatch.setattr(chain, "_advance_cursor", crashed_advance_cursor)
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


async def test_reproject_backfills_new_exporter_kinds():
    """The four footer-bearing kinds added by the vault-projection extension
    (factcheck / paper-book-journal / research_tree / committee) are inside the
    historical backfill channel: pre-existing file-mode notes gain the footer."""
    writer = get_writer()
    rels = [
        await _mk_old_note("Inbox/Disputed Claims.md", "宁德时代产能论断存疑。",
                           kind="factcheck", artifact_id="factcheck-disputes"),
        await _mk_old_note("Book/journal/2026-07-01.md", "平仓宁德时代多头仓位。",
                           kind="paper-book-journal", artifact_id="2026-07-01"),
        await _mk_old_note("Research/固态电池/tree.md", "L0 宁德时代固态电池进度。",
                           kind="research_tree", artifact_id="research-tree:固态电池"),
        await _mk_old_note("Committee/2026-07-01 委员会裁决.md", "裁决围绕宁德时代展开。",
                           kind="committee", artifact_id="cmt-1"),
    ]
    await chain.create_node("宁德时代", "company")   # node arrives AFTER the exports

    res = await chain.reproject_footers()
    assert res["reprojected"] == 4 and res["conflicts"] == 0
    for rel in rels:
        text = (writer.root / rel).read_text(encoding="utf-8")
        assert "## Entities\n[[宁德时代]]" in text, rel
        assert text.count("## Entities") == 1, rel

    # each new kind is individually addressable through the kind filter
    for kind in ("factcheck", "paper-book-journal", "research_tree", "committee"):
        assert (await chain.reproject_footers(kind=kind))["reprojected"] == 0


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
