"""Roadmap control plane: idempotent seed import, move gates, sessions,
decisions, card create/claim, checklist/dependency CRUD, export, agent
prompts, the process overview, and the API surface."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.config import get_settings
from app.institute import roadmap

BACKLOG = Path(__file__).resolve().parent.parent / "roadmap" / "backlog.json"
SEED = json.loads(BACKLOG.read_text(encoding="utf-8"))
N_CARDS = len(SEED["cards"])
# the seed is a living board — assert against its current M7-001 status, not a snapshot
M7_STATUS = next(c for c in SEED["cards"] if c["id"] == "M7-001").get("status", "inbox")
RELEASE_GATE_NAMES = {
    "Release A", "Release B", "Release C", "Release D", "Release E", "Release F",
}


def _seed_gate(*prefixes: str) -> tuple[int, int]:
    """(total, done) computed from the living seed for the given milestone prefixes."""
    scoped = [c for c in SEED["cards"] if c.get("phase", "").split(" ")[0] in prefixes]
    return len(scoped), sum(1 for c in scoped if c.get("status") == "done")


def _seed_copy() -> dict:
    return json.loads(BACKLOG.read_text(encoding="utf-8"))


# ---- (a) idempotent import -------------------------------------------------

async def test_import_is_idempotent_and_updates_changed_fields(tmp_path):
    res = await roadmap.import_backlog()
    assert res == {"created": N_CARDS, "updated": 0, "unchanged": 0, "total": N_CARDS}
    assert len(await roadmap.list_cards()) == N_CARDS
    first_updated_at = (await roadmap.get_card("M7-001"))["updated_at"]

    # second import: no duplicates, no-op rows reported unchanged, updated_at untouched
    res2 = await roadmap.import_backlog()
    assert res2 == {"created": 0, "updated": 0, "unchanged": N_CARDS, "total": N_CARDS}
    assert len(await roadmap.list_cards()) == N_CARDS

    card = await roadmap.get_card("M7-001")
    assert card["updated_at"] == first_updated_at
    assert card["status"] == M7_STATUS
    assert card["verification"] == [".venv/bin/python -m pytest tests/test_roadmap.py -q"]
    assert card["design_links"] == ["roadmap/02-data-model.md", "roadmap/05-global-coding-process.md"]
    assert card["expected_files"] == [
        "migrations/*.sql", "app/institute/roadmap.py", "app/api/roadmap.py", "tests/test_roadmap.py",
    ]
    assert card["tags"] == []  # absent in the seed -> empty list round-trip
    acceptance = [c for c in card["checklists"] if c["kind"] == "acceptance"]
    assert len(acceptance) == 4  # merged by text, never duplicated

    # a changed seed updates fields but local status wins unless force. The flip
    # runs on a synthetic card with a pinned status: the live board's M7-001 may
    # itself drift to 'parked', where a force would emit no status_forced event.
    data = _seed_copy()
    next(c for c in data["cards"] if c["id"] == "M7-001")["title"] = "Roadmap durable backend"
    data["cards"].append({
        "id": "M7-TMPF", "title": "temp force-flip card",
        "phase": "M7 Roadmap Control Plane", "status": "ready",
    })
    seed2 = tmp_path / "backlog.json"
    seed2.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res3 = await roadmap.import_backlog(seed2)
    assert res3 == {"created": 1, "updated": 1, "unchanged": N_CARDS - 1, "total": N_CARDS + 1}
    assert (await roadmap.get_card("M7-001"))["title"] == "Roadmap durable backend"

    next(c for c in data["cards"] if c["id"] == "M7-TMPF")["status"] = "parked"
    seed2.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed2)
    assert (await roadmap.get_card("M7-TMPF"))["status"] == "ready"  # local status preserved

    await roadmap.import_backlog(seed2, force=True)
    assert (await roadmap.get_card("M7-TMPF"))["status"] == "parked"

    events = await bus.replay(0, types=["roadmap.import.completed"])
    assert len(events) == 5
    forced = await bus.replay(0, types=["roadmap.import.status_forced"])
    assert len(forced) == 1  # forced flips stay on the audit trail
    assert forced[0].ref_id == "M7-TMPF"
    assert forced[0].payload == {"from": "ready", "to": "parked"}


async def test_import_dry_run_plans_without_writes_or_events(tmp_path):
    initial = {
        "cards": [
            {
                "id": "PLAN-A", "title": "before", "status": "ready",
                "acceptance": ["base"],
            },
            {
                "id": "PLAN-B", "title": "dep target", "status": "ready",
                "acceptance": ["ready"],
            },
        ],
    }
    initial_path = tmp_path / "initial.json"
    initial_path.write_text(json.dumps(initial), encoding="utf-8")
    await roadmap.import_backlog(initial_path)
    await roadmap.add_checklist_item("PLAN-A", "acceptance", "manual-only")
    await roadmap.create_card({"id": "LIVE-ONLY", "title": "operator card"})

    desired = {
        "cards": [
            {
                "id": "PLAN-A", "title": "after", "status": "done",
                "acceptance": ["base", "seed-added"], "dependencies": ["PLAN-B"],
            },
            {
                "id": "PLAN-B", "title": "dep target", "status": "ready",
                "acceptance": ["ready"],
            },
            {
                "id": "PLAN-C", "title": "new finished card", "status": "done",
                "acceptance": ["new card"],
            },
        ],
    }
    desired_path = tmp_path / "desired.json"
    desired_path.write_text(json.dumps(desired), encoding="utf-8")

    before_events = (await db.query_one("SELECT COUNT(*) AS n FROM roadmap_events"))["n"]
    before_bus = len(await bus.replay(0))
    before_cards = await roadmap.list_cards()
    plan = await roadmap.import_backlog(
        desired_path, dry_run=True, new_card_status_policy="inbox"
    )

    assert plan["dry_run"] is True
    assert plan["new_card_status_policy"] == "inbox"
    assert (plan["created"], plan["updated"], plan["unchanged"], plan["total"]) == (1, 1, 1, 3)
    assert plan["created_cards"] == [{
        "card_id": "PLAN-C", "seed_status": "done", "applied_status": "inbox",
    }]
    assert plan["status_drift"] == [{
        "card_id": "PLAN-A", "live_status": "ready", "seed_status": "done",
        "action": "preserve_live",
    }]
    assert plan["live_only"] == [{
        "card_id": "LIVE-ONLY", "title": "operator card", "status": "inbox",
    }]
    assert plan["dependency_changes"] == [{
        "card_id": "PLAN-A", "added": ["PLAN-B"], "removed": [],
    }]
    checklist = {change["card_id"]: change for change in plan["checklist_changes"]}
    assert checklist["PLAN-A"] == {
        "card_id": "PLAN-A", "added": ["seed-added"],
        "preserved_live_only": ["manual-only"],
    }
    assert checklist["PLAN-C"]["added"] == ["new card"]
    assert {item["card_id"] for item in plan["updated_cards"]} == {"PLAN-A"}
    assert set(plan["updated_cards"][0]["fields"]) == {
        "title", "acceptance", "dependencies",
    }

    # Dry-run means no cards, child rows, audit rows, or in-memory bus events.
    assert await roadmap.list_cards() == before_cards
    assert await roadmap.get_card("PLAN-C") is None
    assert (await db.query_one("SELECT COUNT(*) AS n FROM roadmap_events"))["n"] == before_events
    assert len(await bus.replay(0)) == before_bus

    applied = await roadmap.import_backlog(
        desired_path, new_card_status_policy="inbox"
    )
    assert applied == {"created": 1, "updated": 1, "unchanged": 1, "total": 3}
    assert (await roadmap.get_card("PLAN-C"))["status"] == "inbox"
    assert (await roadmap.get_card("PLAN-A"))["status"] == "ready"  # local status still wins


async def test_import_rejects_self_dependency_and_cycles_atomically(tmp_path):
    self_seed = tmp_path / "self.json"
    self_seed.write_text(json.dumps({"cards": [
        {"id": "SELF", "title": "self", "dependencies": ["SELF"]},
    ]}), encoding="utf-8")
    with pytest.raises(roadmap.RoadmapError, match="cannot depend on itself"):
        await roadmap.import_backlog(self_seed, dry_run=True)
    with pytest.raises(roadmap.RoadmapError, match="cannot depend on itself"):
        await roadmap.import_backlog(self_seed)

    cycle_seed = tmp_path / "cycle.json"
    cycle_seed.write_text(json.dumps({"cards": [
        {"id": "CYCLE-A", "title": "a", "dependencies": ["CYCLE-B"]},
        {"id": "CYCLE-B", "title": "b", "dependencies": ["CYCLE-A"]},
    ]}), encoding="utf-8")
    with pytest.raises(roadmap.RoadmapError, match=r"dependency cycle: .*CYCLE"):
        await roadmap.import_backlog(cycle_seed)

    assert await roadmap.list_cards() == []
    assert (await db.query_one("SELECT COUNT(*) AS n FROM roadmap_events"))["n"] == 0
    assert await bus.replay(0) == []


async def test_import_new_card_status_policy_validation_and_seed_default(tmp_path):
    seed = tmp_path / "status.json"
    seed.write_text(json.dumps({"cards": [
        {"id": "STATUS-DONE", "title": "done in seed", "status": "done"},
    ]}), encoding="utf-8")

    # Compatible default keeps the seed state for a freshly built board.
    await roadmap.import_backlog(seed)
    assert (await roadmap.get_card("STATUS-DONE"))["status"] == "done"

    with pytest.raises(roadmap.RoadmapError, match="new card status policy"):
        await roadmap.import_backlog(seed, new_card_status_policy="unsafe")


# ---- (b) unknown dependency ids --------------------------------------------

async def test_unknown_dependency_ids_are_rejected(tmp_path):
    data = _seed_copy()
    data["cards"][0]["dependencies"] = ["ZZ-999"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(roadmap.RoadmapError, match="unknown card 'ZZ-999'"):
        await roadmap.import_backlog(bad)
    # validation happens before any write: nothing was imported
    assert await roadmap.list_cards() == []


async def test_seed_shape_validation_is_atomic(tmp_path):
    """Malformed non-enum fields raise RoadmapError up front — zero rows written."""
    bad_shapes = [
        ("sort_order", "not-a-number", "sort_order"),
        ("design_links", "roadmap/02-data-model.md", "list of strings"),  # bare string, not list
        ("acceptance", [123], "list of strings"),  # non-string checklist item
        ("dependencies", ["M7-001", 7], "list of strings"),
        ("title", ["nested"], "must be a string"),
    ]
    for field, value, match in bad_shapes:
        data = _seed_copy()
        data["cards"][-1][field] = value  # last card: a partial import would be visible
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(roadmap.RoadmapError, match=match):
            await roadmap.import_backlog(bad)
        assert await roadmap.list_cards() == []  # all-or-nothing

    # the same shape check guards the domain update path (not just pydantic)
    await roadmap.import_backlog()
    with pytest.raises(roadmap.RoadmapError, match="list of strings"):
        await roadmap.update_card("M7-001", {"design_links": "roadmap/02-data-model.md"})
    with pytest.raises(roadmap.RoadmapError, match="number"):
        await roadmap.update_card("M7-001", {"sort_order": "top"})


async def test_reimport_reconciles_dropped_dependencies(tmp_path):
    await roadmap.import_backlog()
    card = await roadmap.get_card("M7-003")
    assert [d["depends_on_id"] for d in card["dependencies"]] == ["M7-001"]

    data = _seed_copy()
    next(c for c in data["cards"] if c["id"] == "M7-003")["dependencies"] = []
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)

    card = await roadmap.get_card("M7-003")
    assert card["dependencies"] == []  # stale dep no longer blocks move-to-done


async def test_import_dry_run_is_zero_write_zero_event_and_inbox_policy_stages_new_cards(tmp_path):
    plan = await roadmap.import_backlog(dry_run=True, new_card_status_policy="inbox")
    assert plan["dry_run"] is True
    assert plan["created"] == N_CARDS
    assert plan["updated"] == 0
    assert plan["unchanged"] == 0
    assert await roadmap.list_cards() == []
    assert await bus.replay(0, types=["roadmap.import.completed"]) == []
    assert await bus.replay(0, types=["roadmap.import.status_forced"]) == []

    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMPQ", "title": "temp staged import card",
        "phase": "M7 Roadmap Control Plane", "status": "done", "acceptance": ["captured by inbox staging"],
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res = await roadmap.import_backlog(seed, new_card_status_policy="inbox")
    assert res == {"created": N_CARDS + 1, "updated": 0, "unchanged": 0, "total": N_CARDS + 1}
    staged = await roadmap.get_card("M7-TMPQ")
    assert staged is not None
    assert staged["status"] == "inbox"  # import did NOT bypass move/evidence gates
    assert staged["completed_at"] is None
    assert len(await bus.replay(0, types=["roadmap.import.completed"])) == 1


async def test_release_gates_extend_through_m10():
    await roadmap.import_backlog()
    gates = {g["name"]: g for g in await roadmap.release_gates()}
    assert set(gates) == {"Release A", "Release B", "Release C", "Release D", "Release E", "Release F"}
    for name, prefixes in {
        "Release D": ("M8",),
        "Release E": ("M9",),
        "Release F": ("M10",),
    }.items():
        total, done = _seed_gate(*prefixes)
        assert gates[name]["prefixes"] == list(prefixes)
        assert (gates[name]["total"], gates[name]["done"]) == (total, done)
        assert gates[name]["remaining"] == sorted(
            c["id"] for c in SEED["cards"]
            if c.get("phase", "").split(" ")[0] in prefixes and c.get("status") != "done"
        )


# ---- (c) move rules ----------------------------------------------------------

async def test_move_to_done_gated_by_dependencies_and_override(tmp_path):
    # self-contained pair with pinned statuses: the gate assertions must not
    # depend on the live board (any non-done dependency blocks done)
    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMPD", "title": "temp dependency card",
        "phase": "M7 Roadmap Control Plane", "status": "ready",
    })
    data["cards"].append({
        "id": "M7-TMPG", "title": "temp gated card",
        "phase": "M7 Roadmap Control Plane", "dependencies": ["M7-TMPD"],
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)

    # M7-TMPG depends on M7-TMPD which is not 'done' -> done is blocked
    with pytest.raises(roadmap.RoadmapError, match="M7-TMPD"):
        await roadmap.move("M7-TMPG", "done")
    assert (await roadmap.get_card("M7-TMPG"))["status"] == "inbox"

    forced = await roadmap.move("M7-TMPG", "done", override=True, reason="operator override")
    assert forced["status"] == "done"
    assert forced["completed_at"]

    events = await bus.replay(0, types=["roadmap.card.moved"])
    assert events[-1].ref_id == "M7-TMPG"
    assert events[-1].payload == {
        "from": "inbox", "to": "done", "override": True, "reason": "operator override",
    }


async def test_plain_moves_and_conditional_claim(tmp_path):
    # temp card with a pinned status: plain-move assertions must not depend on
    # the live board's M7-001 status
    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMPM", "title": "temp move card",
        "phase": "M7 Roadmap Control Plane", "status": "ready", "acceptance": ["it works"],
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)

    card = await roadmap.move("M7-TMPM", "in_progress", owner="claude")
    assert card["status"] == "in_progress"
    assert card["owner"] == "claude"

    # conditional claim: a stale mover loses and the row stays untouched
    with pytest.raises(roadmap.MoveConflict):
        await roadmap.move("M7-TMPM", "review", expected_status="ready")
    assert (await roadmap.get_card("M7-TMPM"))["status"] == "in_progress"

    # review needs a coding session with a summary (M7-005)
    sess = await roadmap.create_session("M7-TMPM", actor="claude", goal="do the work")
    await roadmap.update_session(sess["id"], {"status": "completed", "summary": "did the work"})
    card = await roadmap.move("M7-TMPM", "review")
    assert card["status"] == "review"

    # done needs evidence even with zero dependencies (02-data-model.md)
    with pytest.raises(roadmap.RoadmapError, match="evidence"):
        await roadmap.move("M7-TMPM", "done")
    await roadmap.add_evidence("M7-TMPM", "test", "pytest tests/test_roadmap.py", status="pass")
    card = await roadmap.move("M7-TMPM", "done")
    assert card["status"] == "done"
    assert card["completed_at"]

    # in_progress requires an owner unless one is provided
    with pytest.raises(roadmap.RoadmapError, match="owner"):
        await roadmap.move("M3-001", "in_progress")

    assert await roadmap.move("no-such-card", "ready") is None


async def test_move_gates_empty_acceptance_and_blocked(tmp_path):
    await roadmap.import_backlog()

    # a card with no acceptance checklist cannot move to ready
    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMP", "title": "temp card without acceptance",
        "phase": "M7 Roadmap Control Plane",
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)
    with pytest.raises(roadmap.RoadmapError, match="acceptance checklist is empty"):
        await roadmap.move("M7-TMP", "ready")
    assert (await roadmap.get_card("M7-TMP"))["status"] == "inbox"

    # a blocked card cannot move forward unless override (self-contained card:
    # the real seed's M7-001 status drifts as the board advances)
    data["cards"].append({
        "id": "M7-TMP2", "title": "temp blocked card", "phase": "M7 Roadmap Control Plane",
        "status": "ready", "acceptance": ["it works"],
    })
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)
    await roadmap.update_card("M7-TMP2", {"blocked_reason": "waiting on upstream schema"})
    with pytest.raises(roadmap.RoadmapError, match="blocked"):
        await roadmap.move("M7-TMP2", "in_progress", owner="claude")
    assert (await roadmap.get_card("M7-TMP2"))["status"] == "ready"
    card = await roadmap.move("M7-TMP2", "in_progress", owner="claude", override=True)
    assert card["status"] == "in_progress"


async def test_move_to_review_gated_by_session_summary(tmp_path):
    # self-contained card with a pinned status: the gate assertions must not
    # depend on the live board (review requires a session summary, M7-005)
    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMPR", "title": "temp review-gate card",
        "phase": "M7 Roadmap Control Plane", "status": "in_progress", "owner": "claude",
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)

    # no sessions at all -> review is blocked
    with pytest.raises(roadmap.RoadmapError, match="session summary"):
        await roadmap.move("M7-TMPR", "review")
    assert (await roadmap.get_card("M7-TMPR"))["status"] == "in_progress"

    # a session without a summary does not open the gate; whitespace doesn't count
    sess = await roadmap.create_session("M7-TMPR", actor="claude", goal="implement the thing")
    with pytest.raises(roadmap.RoadmapError, match="session summary"):
        await roadmap.move("M7-TMPR", "review")
    await roadmap.update_session(sess["id"], {"summary": "   "})
    with pytest.raises(roadmap.RoadmapError, match="session summary"):
        await roadmap.move("M7-TMPR", "review")

    # a cancelled session never opens the gate, even with a summary
    ghost = await roadmap.create_session("M7-TMPR", actor="claude", goal="abandoned attempt")
    await roadmap.update_session(ghost["id"], {"status": "cancelled", "summary": "went nowhere"})
    with pytest.raises(roadmap.RoadmapError, match="session summary"):
        await roadmap.move("M7-TMPR", "review")

    # override is the single escape hatch, and it lands on the audit trail
    card = await roadmap.move("M7-TMPR", "review", override=True, reason="operator says go")
    assert card["status"] == "review"
    events = await bus.replay(0, types=["roadmap.card.moved"])
    assert events[-1].ref_id == "M7-TMPR"
    assert events[-1].payload == {
        "from": "in_progress", "to": "review", "override": True, "reason": "operator says go",
    }

    # back to in_progress, then a real summary opens the gate without override
    await roadmap.move("M7-TMPR", "in_progress")
    await roadmap.update_session(sess["id"], {"status": "completed", "summary": "implemented + tested"})
    card = await roadmap.move("M7-TMPR", "review")
    assert card["status"] == "review"


# ---- (d) invalid values --------------------------------------------------------

async def test_invalid_status_type_priority_rejected(tmp_path):
    await roadmap.import_backlog()

    with pytest.raises(roadmap.RoadmapError, match="unknown status"):
        await roadmap.move("M7-001", "doing")
    with pytest.raises(roadmap.RoadmapError, match="unknown priority"):
        await roadmap.update_card("M7-001", {"priority": "P9"})
    with pytest.raises(roadmap.RoadmapError, match="unknown type"):
        await roadmap.update_card("M7-001", {"type": "bananas"})
    with pytest.raises(roadmap.RoadmapError, match="move"):
        await roadmap.update_card("M7-001", {"status": "done"})
    # explicit null on a text field is a 400-shaped error, not a NOT NULL 500
    with pytest.raises(roadmap.RoadmapError, match="title must be a string"):
        await roadmap.update_card("M7-001", {"title": None})

    data = _seed_copy()
    data["cards"][0]["priority"] = "P9"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(roadmap.RoadmapError, match="unknown priority"):
        await roadmap.import_backlog(bad)


# ---- (e) sessions ---------------------------------------------------------------

async def test_session_create_and_command_append_roundtrip():
    await roadmap.import_backlog()

    sess = await roadmap.create_session(
        "M7-001", actor="claude", goal="implement roadmap backend",
        planned_files=["app/institute/roadmap.py"],
    )
    assert sess["status"] == "active"
    assert sess["started_at"] and not sess["finished_at"]
    assert sess["planned_files"] == ["app/institute/roadmap.py"]

    await roadmap.append_command(
        sess["id"], "pytest", ".venv/bin/python -m pytest tests/test_roadmap.py -q",
        exit_code=0, output_excerpt="all green",
    )
    await roadmap.append_command(sess["id"], "compileall", ".venv/bin/python -m compileall app -q", exit_code=0)

    got = await roadmap.get_session(sess["id"])
    assert [c["command_label"] for c in got["commands"]] == ["pytest", "compileall"]
    assert got["commands"][0]["exit_code"] == 0
    assert got["commands"][0]["output_excerpt"] == "all green"

    # full field round-trip: goal + planned files stay updatable mid-session
    updated = await roadmap.update_session(
        sess["id"],
        {"goal": "implement roadmap backend + api",
         "planned_files": ["app/institute/roadmap.py", "app/api/roadmap.py"]},
    )
    assert updated["actor"] == "claude"
    assert updated["goal"] == "implement roadmap backend + api"
    assert updated["planned_files"] == ["app/institute/roadmap.py", "app/api/roadmap.py"]
    assert updated["status"] == "active"  # field edits never claim the status

    # explicit null on a text field is a 400-shaped error, not a NOT NULL 500
    with pytest.raises(roadmap.RoadmapError, match="summary must be a string"):
        await roadmap.update_session(sess["id"], {"summary": None})

    done = await roadmap.update_session(
        sess["id"], {"status": "completed", "summary": "schema + api + tests", "touched_files": ["app/main.py"]}
    )
    assert done["status"] == "completed"
    assert done["finished_at"]
    assert done["touched_files"] == ["app/main.py"]

    # conditional claim: a finished session cannot be finished twice
    with pytest.raises(roadmap.MoveConflict):
        await roadmap.update_session(sess["id"], {"status": "completed"})

    # reopen is a conditional claim too, and lands on the audit trail
    reopened = await roadmap.update_session(sess["id"], {"status": "active"})
    assert reopened["status"] == "active"
    assert reopened["finished_at"] is None

    assert await roadmap.create_session("NOPE-1", actor="x", goal="y") is None
    listed = await roadmap.list_sessions(card_id="M7-001")
    assert len(listed) == 1
    assert listed[0]["n_commands"] == 2

    events = await bus.replay(0, types=["roadmap.session"])
    types = [e.type for e in events]
    assert "roadmap.session.started" in types
    assert "roadmap.session.completed" in types
    assert "roadmap.session.reopened" in types


# ---- (f) session command as evidence (M7-005 acceptance c) -------------------

async def test_session_command_attaches_as_evidence():
    await roadmap.import_backlog()
    sess = await roadmap.create_session("M7-005", actor="claude", goal="close out the card")

    # plain append never touches evidence
    await roadmap.append_command(sess["id"], "ls", "ls -la", exit_code=0)
    card = await roadmap.get_card("M7-005")
    assert card["evidence"] == []

    # as_evidence promotes the command onto the card: exit 0 -> pass
    cmd = await roadmap.append_command(
        sess["id"], "pytest", ".venv/bin/python -m pytest tests/test_roadmap.py -q",
        exit_code=0, output_excerpt="all green", as_evidence=True,
    )
    assert cmd["evidence_id"]
    card = await roadmap.get_card("M7-005")
    assert len(card["evidence"]) == 1
    ev = card["evidence"][0]
    assert ev["id"] == cmd["evidence_id"]
    assert ev["kind"] == "command"
    assert ev["title"] == "pytest"
    assert ev["status"] == "pass"
    assert ".venv/bin/python -m pytest" in ev["body"] and "all green" in ev["body"]
    assert ev["artifact_ref"] == f"session_command:{cmd['id']}"

    # non-zero exit -> fail; unknown exit -> info
    failed = await roadmap.append_command(sess["id"], "build", "npm run build", exit_code=2, as_evidence=True)
    unknown = await roadmap.append_command(sess["id"], "note", "manual check", as_evidence=True)
    by_id = {e["id"]: e for e in (await roadmap.get_card("M7-005"))["evidence"]}
    assert by_id[failed["evidence_id"]]["status"] == "fail"
    assert by_id[unknown["evidence_id"]]["status"] == "info"

    # the evidence trail satisfies the move-to-done evidence gate
    events = await bus.replay(0, types=["roadmap.evidence.added"])
    assert len(events) == 3


# ---- (g) decisions (M7-008) ---------------------------------------------------

async def test_decisions_open_and_resolve():
    await roadmap.import_backlog()

    dec = await roadmap.open_decision(
        "Pick session evidence shape", "Promote commands or duplicate rows?",
        card_id="M7-005", options=["promote", "duplicate"],
    )
    assert dec["status"] == "open"
    assert dec["card_id"] == "M7-005"
    assert dec["options"] == ["promote", "duplicate"]
    assert dec["created_at"] and not dec["resolved_at"]

    # board-level decision: no card
    board = await roadmap.open_decision("Board cadence", "Weekly or biweekly?")
    assert board["card_id"] is None

    with pytest.raises(roadmap.RoadmapError, match="unknown card"):
        await roadmap.open_decision("t", "q", card_id="NOPE-1")
    with pytest.raises(roadmap.RoadmapError, match="title and a question"):
        await roadmap.open_decision("", "q")

    # field edits stay open; resolving requires the decision text
    edited = await roadmap.update_decision(dec["id"], {"question": "Promote or duplicate?"})
    assert edited["question"] == "Promote or duplicate?"
    assert edited["status"] == "open"
    with pytest.raises(roadmap.RoadmapError, match="decision text"):
        await roadmap.update_decision(dec["id"], {"status": "resolved"})
    with pytest.raises(roadmap.RoadmapError, match="only be patched to resolved"):
        await roadmap.update_decision(dec["id"], {"status": "open"})
    with pytest.raises(roadmap.RoadmapError, match="unknown decision status"):
        await roadmap.update_decision(dec["id"], {"status": "later"})

    resolved = await roadmap.update_decision(dec["id"], {"status": "resolved", "decision": "promote"})
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "promote"
    assert resolved["resolved_at"]

    # conditional claim: a resolved decision cannot resolve twice
    with pytest.raises(roadmap.MoveConflict):
        await roadmap.update_decision(dec["id"], {"status": "resolved", "decision": "duplicate"})

    # a resolved decision is immutable: field-only PATCHes are rejected too,
    # and the row + its decision.resolved event keep the original value
    for attempt in ({"decision": "duplicate"}, {"title": "rewrite"}, {"options": ["x"]}):
        with pytest.raises(roadmap.MoveConflict, match="immutable"):
            await roadmap.update_decision(dec["id"], attempt)
    unchanged = await roadmap.get_decision(dec["id"])
    assert unchanged["decision"] == "promote"
    assert unchanged["title"] == "Pick session evidence shape"

    listed = await roadmap.list_decisions(card_id="M7-005")
    assert [d["id"] for d in listed] == [dec["id"]]
    assert len(await roadmap.list_decisions(status="open")) == 1  # the board one
    assert await roadmap.update_decision("nope", {"decision": "x"}) is None

    opened = await bus.replay(0, types=["roadmap.decision.opened"])
    assert len(opened) == 2
    assert opened[0].ref_id == "M7-005"
    resolved_ev = await bus.replay(0, types=["roadmap.decision.resolved"])
    assert len(resolved_ev) == 1
    assert resolved_ev[0].payload == {"decision_id": dec["id"], "decision": "promote"}


# ---- (h) card create + claim (M7-008) ------------------------------------------

async def test_create_card_validates_and_seeds_acceptance():
    await roadmap.import_backlog()

    card = await roadmap.create_card({
        "id": "M7-NEW", "title": "New card", "phase": "M7 Roadmap Control Plane",
        "status": "ready", "acceptance": ["it works", "tests pass"],
        "tags": ["ops"], "sort_order": 5,
    })
    assert card["status"] == "ready"
    assert card["tags"] == ["ops"]
    assert [c["text"] for c in card["checklists"]] == ["it works", "tests pass"]
    assert not card["completed_at"]

    # server-generated id when omitted
    anon = await roadmap.create_card({"title": "anon card"})
    assert anon["id"] and anon["status"] == "inbox"

    with pytest.raises(roadmap.RoadmapError, match="already exists"):
        await roadmap.create_card({"id": "M7-NEW", "title": "dup"})
    with pytest.raises(roadmap.RoadmapError, match="needs a title"):
        await roadmap.create_card({"title": "  "})
    with pytest.raises(roadmap.RoadmapError, match="acceptance checklist is empty"):
        await roadmap.create_card({"title": "no acceptance", "status": "ready"})
    with pytest.raises(roadmap.RoadmapError, match="unknown create status"):
        await roadmap.create_card({"title": "jump the funnel", "status": "in_progress"})
    with pytest.raises(roadmap.RoadmapError, match="unknown priority"):
        await roadmap.create_card({"title": "bad", "priority": "P9"})
    with pytest.raises(roadmap.RoadmapError, match="must be unique"):
        await roadmap.create_card({"title": "dup acceptance", "acceptance": ["same", "same"]})
    # P2-1: whitespace acceptance items must not sneak a card past the ready gate
    with pytest.raises(roadmap.RoadmapError, match="need text"):
        await roadmap.create_card({"title": "blank acceptance", "status": "ready", "acceptance": ["   "]})
    # normalization happens before the duplicate check
    with pytest.raises(roadmap.RoadmapError, match="must be unique"):
        await roadmap.create_card({"title": "padded dup", "acceptance": ["same", " same "]})
    # duplicate-id failure is atomic: no checklist rows leaked
    dup_items = await db.query(
        "SELECT * FROM roadmap_checklists WHERE card_id = 'M7-NEW' AND kind = 'acceptance'"
    )
    assert len(dup_items) == 2

    events = await bus.replay(0, types=["roadmap.card.created"])
    assert {e.ref_id for e in events} == {"M7-NEW", anon["id"]}


async def test_claim_card_is_a_conditional_claim():
    await roadmap.import_backlog()

    # self-contained claim target: the live board's cards drift to done as the
    # roadmap advances, and done cards are not claimable
    await roadmap.create_card({
        "id": "M7-TMPC", "title": "temp claim card", "phase": "M7 Roadmap Control Plane",
    })
    card = await roadmap.claim_card("M7-TMPC", "agent-a6")
    assert card["status"] == "in_progress"
    assert card["owner"] == "agent-a6"

    # second claim loses: already owned (and no longer inbox/ready)
    with pytest.raises(roadmap.MoveConflict, match="already owned"):
        await roadmap.claim_card("M7-TMPC", "agent-b")

    # owned-but-ready card is not claimable either
    await roadmap.update_card("M7-006", {"owner": "someone"})
    with pytest.raises(roadmap.MoveConflict, match="already owned"):
        await roadmap.claim_card("M7-006", "agent-b")

    # a card past ready cannot be claimed (it went through move/claim already)
    await roadmap.create_card({"id": "M7-TMPC2", "title": "temp past-ready card"})
    await roadmap.move("M7-TMPC2", "verify")
    with pytest.raises(roadmap.RoadmapError, match="only inbox/ready"):
        await roadmap.claim_card("M7-TMPC2", "agent-b")

    # blocked cards need the move() override path, not claim.
    # pinned temp card — live seed cards drift to done as the roadmap advances
    await roadmap.create_card({"id": "M7-TMPC3", "title": "temp blocked card"})
    await roadmap.update_card("M7-TMPC3", {"blocked_reason": "waiting on schema"})
    with pytest.raises(roadmap.RoadmapError, match="blocked"):
        await roadmap.claim_card("M7-TMPC3", "agent-b")

    with pytest.raises(roadmap.RoadmapError, match="needs an owner"):
        await roadmap.claim_card("M4-001", "  ")
    assert await roadmap.claim_card("NOPE-1", "agent-a6") is None

    events = await bus.replay(0, types=["roadmap.card.claimed"])
    assert len(events) == 1
    assert events[0].ref_id == "M7-TMPC"
    assert events[0].payload == {"owner": "agent-a6", "from": "inbox", "to": "in_progress"}


async def test_concurrent_claim_and_create_single_winner():
    """P3-1: real concurrency — both racers pass the pre-checks, the DB write
    decides. Exactly one claim wins the rowcount, the loser gets MoveConflict;
    duplicate creates collapse to one card."""
    import asyncio

    await roadmap.import_backlog()

    # self-contained claim target (live-board cards drift to done over time)
    await roadmap.create_card({
        "id": "M7-TMPW", "title": "temp race card", "phase": "M7 Roadmap Control Plane",
    })
    results = await asyncio.gather(
        roadmap.claim_card("M7-TMPW", "racer-1"),
        roadmap.claim_card("M7-TMPW", "racer-2"),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, roadmap.MoveConflict)]
    assert len(winners) == 1 and len(losers) == 1
    card = await roadmap.get_card("M7-TMPW")
    assert card["status"] == "in_progress"
    assert card["owner"] == winners[0]["owner"]
    assert len(await bus.replay(0, types=["roadmap.card.claimed"])) == 1

    results = await asyncio.gather(
        roadmap.create_card({"id": "M7-RACE", "title": "first"}),
        roadmap.create_card({"id": "M7-RACE", "title": "second"}),
        return_exceptions=True,
    )
    created = [r for r in results if isinstance(r, dict)]
    rejected = [r for r in results if isinstance(r, roadmap.RoadmapError)]
    assert len(created) == 1 and len(rejected) == 1
    assert (await roadmap.get_card("M7-RACE"))["title"] == created[0]["title"]


# ---- (i) checklist CRUD (M7-008) ------------------------------------------------

async def test_checklist_add_check_delete():
    await roadmap.import_backlog()

    item = await roadmap.add_checklist_item("M7-008", "review", "scope did not drift")
    assert item["checked"] == 0
    assert item["sort_order"] == 10.0

    # sibling items keep appending to the tail of their kind
    item2 = await roadmap.add_checklist_item("M7-008", "review", "tests are meaningful")
    assert item2["sort_order"] == 20.0

    with pytest.raises(roadmap.RoadmapError, match="already exists"):
        await roadmap.add_checklist_item("M7-008", "review", "scope did not drift")
    with pytest.raises(roadmap.RoadmapError, match="unknown checklist kind"):
        await roadmap.add_checklist_item("M7-008", "vibes", "nope")
    with pytest.raises(roadmap.RoadmapError, match="needs text"):
        await roadmap.add_checklist_item("M7-008", "review", "   ")
    assert await roadmap.add_checklist_item("NOPE-1", "review", "x") is None

    checked = await roadmap.update_checklist_item(item["id"], {"checked": True})
    assert checked["checked"] == 1
    unchecked = await roadmap.update_checklist_item(item["id"], {"checked": False})
    assert unchecked["checked"] == 0

    # renaming re-derives the deterministic id (the seed merge key is the text)
    renamed = await roadmap.update_checklist_item(item["id"], {"text": "scope stayed put", "sort_order": 5})
    assert renamed["text"] == "scope stayed put"
    assert renamed["sort_order"] == 5.0
    assert renamed["id"] != item["id"]
    assert renamed["id"] == roadmap._det_id("M7-008", "review", "scope stayed put")
    assert await roadmap.update_checklist_item(item["id"], {"checked": True}) is None  # old id is gone
    rename_ev = (await bus.replay(0, types=["roadmap.checklist.renamed"]))[-1]
    assert rename_ev.payload == {
        "from_id": item["id"], "to_id": renamed["id"],
        "from_text": "scope did not drift", "to_text": "scope stayed put",
    }

    with pytest.raises(roadmap.RoadmapError, match="already exists"):
        await roadmap.update_checklist_item(renamed["id"], {"text": "tests are meaningful"})
    with pytest.raises(roadmap.RoadmapError, match="unknown checklist fields"):
        await roadmap.update_checklist_item(renamed["id"], {"kind": "acceptance"})
    assert await roadmap.update_checklist_item("nope", {"checked": True}) is None

    assert await roadmap.delete_checklist_item(renamed["id"]) is True
    assert await roadmap.delete_checklist_item(renamed["id"]) is False
    texts = [c["text"] for c in (await roadmap.get_card("M7-008"))["checklists"] if c["kind"] == "review"]
    assert texts == ["tests are meaningful"]

    check_events = await bus.replay(0, types=["roadmap.checklist.checked"])
    assert [e.payload["checked"] for e in check_events] == [True, False]
    assert (await bus.replay(0, types=["roadmap.checklist.added"]))[-1].ref_id == "M7-008"
    assert (await bus.replay(0, types=["roadmap.checklist.removed"]))[-1].payload["text"] == "scope stayed put"


async def test_checklist_rename_then_reimport_merges_by_text():
    """P1-3 regression: after a rename the next seed import must treat the OLD
    text as missing (recreate it) instead of colliding with the renamed row."""
    await roadmap.import_backlog()

    card = await roadmap.get_card("M7-008")
    first = next(c for c in card["checklists"] if c["kind"] == "acceptance")
    old_id, old_text = first["id"], first["text"]
    await roadmap.update_checklist_item(old_id, {"checked": True})

    renamed = await roadmap.update_checklist_item(old_id, {"text": "renamed acceptance line"})
    assert renamed["id"] == roadmap._det_id("M7-008", "acceptance", "renamed acceptance line")
    assert renamed["checked"] == 1  # rename keeps checked state

    # re-import the original seed: the old text comes back as a NEW row with its
    # deterministic id (no primary-key collision with the renamed row), and the
    # renamed row survives untouched
    await roadmap.import_backlog()
    items = {c["text"]: c for c in (await roadmap.get_card("M7-008"))["checklists"]}
    assert old_text in items
    assert items[old_text]["id"] == old_id
    assert items[old_text]["checked"] == 0  # rebuilt seed item starts unchecked
    assert items["renamed acceptance line"]["id"] == renamed["id"]
    assert items["renamed acceptance line"]["checked"] == 1
    # id == det(text) invariant holds for every row on the card
    for c in (await roadmap.get_card("M7-008"))["checklists"]:
        assert c["id"] == roadmap._det_id("M7-008", c["kind"], c["text"])


# ---- (j) dependency add/remove (M7-008) ----------------------------------------

async def test_dependency_add_remove_and_cycle_guard():
    await roadmap.import_backlog()

    # M7-008 -> M7-005 is NOT in the seed (M7-008 only depends on M7-001)
    dep = await roadmap.add_dependency("M7-008", "M7-005")
    assert dep["depends_on_id"] == "M7-005"
    assert dep["relation"] == "blocks"
    assert len(await bus.replay(0, types=["roadmap.dependency.added"])) == 1

    # idempotent re-add returns the same edge and emits no duplicate event —
    # including with unnormalized relation whitespace (P2-2: relation is
    # stripped BEFORE the deterministic id is derived)
    again = await roadmap.add_dependency("M7-008", "M7-005")
    assert again["id"] == dep["id"]
    padded = await roadmap.add_dependency("M7-008", "M7-005", relation=" blocks ")
    assert padded is not None and padded["id"] == dep["id"]
    assert len(await bus.replay(0, types=["roadmap.dependency.added"])) == 1

    with pytest.raises(roadmap.RoadmapError, match="cannot depend on itself"):
        await roadmap.add_dependency("M7-008", "M7-008")
    with pytest.raises(roadmap.RoadmapError, match="unknown card"):
        await roadmap.add_dependency("M7-008", "ZZ-999")
    assert await roadmap.add_dependency("NOPE-1", "M7-005") is None

    # direct cycle: M7-005 -> M7-008 closes the loop we just added
    with pytest.raises(roadmap.RoadmapError, match="cycle"):
        await roadmap.add_dependency("M7-005", "M7-008")
    # transitive cycle: the seed carries M7-006 -> M7-003 -> M7-001, so
    # M7-001 -> M7-006 would close a multi-hop loop
    with pytest.raises(roadmap.RoadmapError, match="cycle"):
        await roadmap.add_dependency("M7-001", "M7-006")

    # the new edge participates in the move-to-done gate: pin a not-done
    # dependency of our own (live-board statuses drift toward done)
    await roadmap.create_card({"id": "M7-TMPY", "title": "temp open dependency"})
    tmp_edge = await roadmap.add_dependency("M7-008", "M7-TMPY")
    with pytest.raises(roadmap.RoadmapError, match="M7-TMPY"):
        await roadmap.move("M7-008", "done")
    assert await roadmap.remove_dependency(tmp_edge["id"]) is True

    assert await roadmap.remove_dependency(dep["id"]) is True
    assert await roadmap.remove_dependency(dep["id"]) is False
    deps = (await roadmap.get_card("M7-008"))["dependencies"]
    assert [d["depends_on_id"] for d in deps] == ["M7-001"]  # seed edge untouched
    removed = await bus.replay(0, types=["roadmap.dependency.removed"])
    assert removed[-1].payload["dependency_id"] == dep["id"]


# ---- (k) export snapshot (M7-008) ------------------------------------------------

async def test_export_roundtrip_is_idempotent_and_rebuilds():
    await roadmap.import_backlog()

    # mutate the live board so the snapshot carries real state
    await roadmap.create_card({
        "id": "M7-EXP", "title": "export fixture", "phase": "M7 Roadmap Control Plane",
        "status": "ready", "acceptance": ["exports cleanly"], "verification": ["pytest -q"],
        "tags": ["fixture"], "sort_order": 999,
    })
    await roadmap.claim_card("M7-EXP", "agent-a6")
    await roadmap.add_dependency("M7-EXP", "M7-001")
    # a blocked card must survive the roundtrip (the blocker gates claim/move).
    # pinned temp card — live seed cards drift to done as the roadmap advances
    await roadmap.create_card({"id": "M7-EXPB", "title": "export blocked fixture"})
    await roadmap.update_card("M7-EXPB", {"blocked_reason": "external blocker"})

    snapshot = await roadmap.export_backlog()
    assert snapshot["version"] == 1
    assert snapshot["columns"] == list(roadmap.STATUSES)
    by_id = {c["id"]: c for c in snapshot["cards"]}
    exp = by_id["M7-EXP"]
    assert exp["status"] == "in_progress"
    assert exp["owner"] == "agent-a6"
    assert exp["dependencies"] == ["M7-001"]
    assert exp["acceptance"] == ["exports cleanly"]
    assert exp["verification"] == ["pytest -q"]
    assert exp["tags"] == ["fixture"]
    assert by_id["M7-EXPB"]["blocked_reason"] == "external blocker"

    # 1) re-importing the snapshot into the same db is a no-op
    tmp = Path(get_settings().home_dir) / "export-snapshot.json"
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    res = await roadmap.import_backlog(tmp)
    assert res["created"] == 0 and res["updated"] == 0
    assert res["unchanged"] == len(snapshot["cards"])

    # 2) importing the snapshot into an empty db rebuilds the board
    await db.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(get_settings().db_path) + suffix).unlink(missing_ok=True)
    await db.init()
    res = await roadmap.import_backlog(tmp)
    assert res["created"] == len(snapshot["cards"])
    rebuilt = await roadmap.get_card("M7-EXP")
    assert rebuilt["status"] == "in_progress"  # status/owner survive the rebuild
    assert rebuilt["owner"] == "agent-a6"
    assert [d["depends_on_id"] for d in rebuilt["dependencies"]] == ["M7-001"]
    assert [c["text"] for c in rebuilt["checklists"]] == ["exports cleanly"]
    # the rebuilt blocker still gates claim and forward moves
    blocked = await roadmap.get_card("M7-EXPB")
    assert blocked["blocked_reason"] == "external blocker"
    with pytest.raises(roadmap.RoadmapError, match="blocked"):
        await roadmap.claim_card("M7-EXPB", "agent-b")

    # 3) export-of-the-rebuild equals the original snapshot, byte for byte
    re_export = await roadmap.export_backlog()
    assert re_export == snapshot
    assert (
        json.dumps(re_export, ensure_ascii=False).encode("utf-8")
        == json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    )


# ---- (l) retry-safe create mutations (M7-010) -------------------------------

async def test_concurrent_idempotent_card_create_has_one_winner():
    request = {"title": "concurrent retry", "acceptance": ["one card"]}
    first, second = await asyncio.gather(
        roadmap.create_card(request, idempotency_key="concurrent-key"),
        roadmap.create_card(request, idempotency_key="concurrent-key"),
    )
    assert first == second
    assert (await db.query_one(
        "SELECT COUNT(*) AS n FROM roadmap_cards WHERE title = 'concurrent retry'"
    ))["n"] == 1
    events = await bus.replay(0, types=["roadmap.card.created"])
    assert len(events) == 1

async def test_idempotency_record_and_create_mutation_are_atomic(monkeypatch):
    async def fail_store(*_args, **_kwargs):
        raise RuntimeError("simulated ledger write failure")

    monkeypatch.setattr(roadmap, "_store_idempotency_result", fail_store)
    with pytest.raises(RuntimeError, match="ledger write failure"):
        await roadmap.create_card(
            {"id": "IDEM-ROLLBACK", "title": "must roll back"},
            idempotency_key="atomic-key",
        )

    # The resource insert happened before the injected failure, but both are
    # inside one transaction: no card, checklist, ledger row, or event survives.
    assert await roadmap.get_card("IDEM-ROLLBACK") is None
    assert (await db.query_one(
        "SELECT COUNT(*) AS n FROM roadmap_idempotency_keys"
    ))["n"] == 0
    assert (await db.query_one(
        "SELECT COUNT(*) AS n FROM roadmap_events"
    ))["n"] == 0


async def test_api_create_idempotency_replay_conflict_and_scope_isolation():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Parent fixtures are ordinary creates; all seven create-style roadmap
        # endpoints below reuse one key to prove route isolation.
        for card_id in ("IDEM-P1", "IDEM-P2"):
            response = await client.post(
                "/api/roadmap/cards", json={"id": card_id, "title": card_id}
            )
            assert response.status_code == 200

        # Generated-id card create: exact replay returns the original serialized
        # response, not a second card and not the resource's later edited state.
        retry_headers = {"Idempotency-Key": "card-retry"}
        create_body = {"title": "original title", "acceptance": ["works"]}
        first = await client.post(
            "/api/roadmap/cards", json=create_body, headers=retry_headers
        )
        replay = await client.post(
            "/api/roadmap/cards", json=create_body, headers=retry_headers
        )
        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        generated_id = first.json()["id"]
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM roadmap_cards WHERE title = 'original title'"
        ))["n"] == 1
        assert (await client.patch(
            f"/api/roadmap/cards/{generated_id}", json={"title": "edited later"}
        )).status_code == 200
        replay_after_edit = await client.post(
            "/api/roadmap/cards", json=create_body, headers=retry_headers
        )
        assert replay_after_edit.json() == first.json()
        assert replay_after_edit.json()["title"] == "original title"

        conflict = await client.post(
            "/api/roadmap/cards", json={"title": "different request"},
            headers=retry_headers,
        )
        assert conflict.status_code == 409
        assert "different request" in conflict.json()["detail"]

        shared = {"Idempotency-Key": "shared-across-routes"}

        async def post_twice(path: str, payload: dict) -> dict:
            one = await client.post(path, json=payload, headers=shared)
            two = await client.post(path, json=payload, headers=shared)
            assert one.status_code == two.status_code == 200, (path, one.text, two.text)
            assert two.json() == one.json()
            return one.json()

        evidence = await post_twice(
            "/api/roadmap/cards/IDEM-P1/evidence",
            {"kind": "test", "title": "pytest", "status": "pass"},
        )
        checklist = await post_twice(
            "/api/roadmap/cards/IDEM-P1/checklists",
            {"kind": "acceptance", "text": "one item"},
        )
        dependency = await post_twice(
            "/api/roadmap/cards/IDEM-P1/dependencies",
            {"depends_on_id": "IDEM-P2"},
        )
        decision = await post_twice(
            "/api/roadmap/decisions",
            {"card_id": "IDEM-P1", "title": "choose", "question": "A or B?"},
        )
        session = await post_twice(
            "/api/roadmap/cards/IDEM-P1/sessions",
            {"actor": "codex", "goal": "test retries"},
        )
        command = await post_twice(
            f"/api/roadmap/sessions/{session['id']}/commands",
            {
                "command_label": "pytest", "command_text": "pytest -q",
                "exit_code": 0, "as_evidence": True,
            },
        )
        assert command["evidence_id"]

        # Same route + same key is also isolated by parent resource.
        other_parent = await client.post(
            "/api/roadmap/cards/IDEM-P2/evidence",
            json={"kind": "doc", "title": "different parent"}, headers=shared,
        )
        assert other_parent.status_code == 200
        assert other_parent.json()["id"] != evidence["id"]

        # Every keyed resource was created once; replay did not duplicate rows.
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM roadmap_checklists WHERE id = ?", (checklist["id"],)
        ))["n"] == 1
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM roadmap_dependencies WHERE id = ?", (dependency["id"],)
        ))["n"] == 1
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM roadmap_decisions WHERE id = ?", (decision["id"],)
        ))["n"] == 1
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM roadmap_coding_sessions WHERE id = ?", (session["id"],)
        ))["n"] == 1
        assert (await db.query_one(
            "SELECT COUNT(*) AS n FROM roadmap_session_commands WHERE id = ?", (command["id"],)
        ))["n"] == 1

        # Invalid keys are rejected before mutation.
        empty = await client.post(
            "/api/roadmap/cards", json={"title": "empty key"},
            headers={"Idempotency-Key": "   "},
        )
        assert empty.status_code == 400
        too_long = await client.post(
            "/api/roadmap/cards", json={"title": "long key"},
            headers={"Idempotency-Key": "x" * 201},
        )
        assert too_long.status_code == 400


# ---- (l) agent prompt (M7-007) ----------------------------------------------------

async def test_agent_prompt_is_deterministic_and_complete():
    await roadmap.import_backlog()

    p1 = await roadmap.generate_agent_prompt("M7-007")
    p2 = await roadmap.generate_agent_prompt("M7-007")
    assert p1.encode("utf-8") == p2.encode("utf-8")  # byte-identical for the same card state

    # every required element is present: id, title, design links, expected
    # files, acceptance criteria, verification commands, and constraints
    assert p1.startswith("You are implementing roadmap card M7-007: Generate agent prompts from roadmap cards.")
    assert "Design links:" in p1 and "- roadmap/06-agent-protocol.md" in p1
    assert "Expected files:" in p1 and "- app/institute/roadmap.py" in p1
    assert "Acceptance criteria:" in p1
    assert "- prompt generation is deterministic for the same card state" in p1
    assert "Verification:" in p1
    assert "- .venv/bin/python -m pytest tests/test_roadmap.py -q" in p1
    assert "Constraints:" in p1 and "read CLAUDE.md first" in p1
    assert "migrations are additive only" in p1
    # dependencies render with their live status
    assert f"- M7-001 ({M7_STATUS})" in p1

    # updated_at alone must not leak into the prompt (no timestamps rendered)
    await roadmap.update_card("M7-007", {"sort_order": 123.0})
    assert await roadmap.generate_agent_prompt("M7-007") == p1

    # a real card edit changes the prompt
    await roadmap.update_card("M7-007", {"title": "Generate deterministic prompts"})
    p3 = await roadmap.generate_agent_prompt("M7-007")
    assert p3 != p1
    assert "Generate deterministic prompts" in p3

    # checklist edits flow through too
    await roadmap.add_checklist_item("M7-007", "acceptance", "prompt covers dependencies")
    p4 = await roadmap.generate_agent_prompt("M7-007")
    assert p4 != p3
    assert "- prompt covers dependencies" in p4

    assert await roadmap.generate_agent_prompt("NOPE-1") is None


# ---- (m) process overview (M7-006) -------------------------------------------------

async def test_process_overview_aggregates_sessions_decisions_gates_blocked(tmp_path):
    # self-contained fixtures with pinned statuses: one done M7 card (with pass
    # evidence) and one blocked M7 card, on top of the living seed
    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMPP1", "title": "temp process done card",
        "phase": "M7 Roadmap Control Plane", "status": "done",
    })
    data["cards"].append({
        "id": "M7-TMPP2", "title": "temp process blocked card",
        "phase": "M7 Roadmap Control Plane", "status": "ready",
        "acceptance": ["works"], "dependencies": ["M7-TMPP1"],
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)

    # one active session; a finished session must not show up as active
    sess = await roadmap.create_session("M7-TMPP2", actor="agent-b7", goal="build the process view")
    finished = await roadmap.create_session("M7-TMPP1", actor="human", goal="already finished")
    await roadmap.update_session(finished["id"], {"status": "completed", "summary": "done"})

    # one open decision; a resolved decision must not show up
    dec = await roadmap.open_decision("Pick view layout", "Tabs or split?", card_id="M7-TMPP2")
    settled = await roadmap.open_decision("Old question", "Settled?")
    await roadmap.update_decision(settled["id"], {"status": "resolved", "decision": "yes"})

    # operator block + evidence: pass counts toward readiness, fail does not
    await roadmap.update_card("M7-TMPP2", {"blocked_reason": "waiting on design sign-off"})
    await roadmap.add_evidence("M7-TMPP1", "test", "pytest green", status="pass")
    await roadmap.add_evidence("M7-TMPP2", "command", "npm run build", status="fail")

    view = await roadmap.process_overview()
    assert set(view) == {"active_sessions", "open_decisions", "release_gates", "blocked_cards"}

    # active sessions carry actor/goal and the card link
    assert [s["id"] for s in view["active_sessions"]] == [sess["id"]]
    active = view["active_sessions"][0]
    assert active["actor"] == "agent-b7"
    assert active["goal"] == "build the process view"
    assert active["card_id"] == "M7-TMPP2"
    assert active["card_title"] == "temp process blocked card"
    assert active["n_commands"] == 0

    # only open decisions, with the card title joined in
    assert [d["id"] for d in view["open_decisions"]] == [dec["id"]]
    assert view["open_decisions"][0]["card_title"] == "temp process blocked card"

    # release gates: shape + counts scoped by milestone prefix
    gates = {g["gate"]: g for g in view["release_gates"]}
    assert set(gates) == RELEASE_GATE_NAMES
    gate_c = gates["Release C"]
    assert set(gate_c) == {
        "gate", "description", "prefixes", "cards_total", "cards_done",
        "evidence_ready", "blockers", "ready",
    }
    total_c, done_c = _seed_gate("M7")
    assert gate_c["cards_total"] == total_c + 2
    assert gate_c["cards_done"] == done_c + 1
    assert gate_c["evidence_ready"] == 1  # the fail verdict does not count
    assert "M7-TMPP2" in gate_c["blockers"]
    assert gate_c["ready"] is False
    # gates without evidence never report ready even if all cards were done
    assert gates["Release A"]["evidence_ready"] == 0
    assert gates["Release A"]["ready"] is False

    # blocked cards are visible without opening each card
    blocked = {c["id"]: c for c in view["blocked_cards"]}
    assert "M7-TMPP2" in blocked
    assert blocked["M7-TMPP2"]["blocked_reason"] == "waiting on design sign-off"
    assert blocked["M7-TMPP2"]["open_dependencies"] == []  # its dependency is done
    assert "M7-TMPP1" not in blocked  # done cards are not process blockers

    # an open dependency also surfaces as a blocker (no blocked_reason needed).
    # pinned temp card, not a live seed card: seed statuses drift as the roadmap
    # progresses and must not flip this assertion
    await roadmap.create_card({
        "id": "M7-TMPP3", "title": "pinned open dep", "type": "feature",
        "phase": "M7 Roadmap Control Plane", "status": "inbox",
        "summary": "stays open for the blocker assertion",
    })
    await roadmap.update_card("M7-TMPP2", {"blocked_reason": ""})
    await roadmap.add_dependency("M7-TMPP2", "M7-TMPP3")
    view2 = await roadmap.process_overview()
    blocked2 = {c["id"]: c for c in view2["blocked_cards"]}
    assert blocked2["M7-TMPP2"]["open_dependencies"] == ["M7-TMPP3"]


# ---- API surface ------------------------------------------------------------------

async def test_api_roundtrip_and_release_gates():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # the import endpoint only reads seeds inside the repository
        r = await client.post("/api/roadmap/import", json={"path": "/etc/hosts"})
        assert r.status_code == 400

        # The HTTP dry-run exposes the policy and full plan without seeding the DB.
        r = await client.post("/api/roadmap/import", json={
            "dry_run": True, "new_card_status_policy": "inbox",
        })
        assert r.status_code == 200
        assert r.json()["dry_run"] is True
        assert r.json()["new_card_status_policy"] == "inbox"
        assert r.json()["created"] == N_CARDS
        assert await roadmap.list_cards() == []
        assert (await client.post(
            "/api/roadmap/import", json={"new_card_status_policy": "unsafe"},
        )).status_code == 422

        r = await client.post("/api/roadmap/import", json={})
        assert r.status_code == 200
        assert r.json()["created"] == N_CARDS

        r = await client.get("/api/roadmap/cards", params={"phase": "M7", "priority": "P1"})
        assert r.status_code == 200
        ids = {c["id"] for c in r.json()}
        assert "M7-001" in ids and "M7-003" not in ids  # M7-003 is P2

        r = await client.get("/api/roadmap/cards/M7-001")
        assert r.status_code == 200
        assert r.json()["checklists"]
        assert (await client.get("/api/roadmap/cards/NOPE")).status_code == 404

        # move gate: dependency not done -> 400; override -> 200
        r = await client.post("/api/roadmap/cards/M7-003/move", json={"status": "done"})
        assert r.status_code == 400
        r = await client.post("/api/roadmap/cards/M7-003/move", json={"status": "done", "override": True})
        assert r.status_code == 200
        assert r.json()["status"] == "done"

        # stale expected_status -> 409
        r = await client.post(
            "/api/roadmap/cards/M7-001/move", json={"status": "review", "expected_status": "inbox"}
        )
        assert r.status_code == 409

        r = await client.patch("/api/roadmap/cards/M7-001", json={"owner": "claude"})
        assert r.status_code == 200
        assert r.json()["owner"] == "claude"
        r = await client.patch("/api/roadmap/cards/M7-001", json={"priority": "P9"})
        assert r.status_code == 400

        # review gate (M7-005): no session summary -> 400, override -> allowed
        r = await client.post("/api/roadmap/cards/M7-006/move", json={"status": "review"})
        assert r.status_code == 400
        assert "session summary" in r.json()["detail"]

        r = await client.post(
            "/api/roadmap/cards/M7-001/sessions",
            json={"actor": "claude", "goal": "wire the api", "planned_files": ["app/api/roadmap.py"]},
        )
        assert r.status_code == 200
        assert r.json()["planned_files"] == ["app/api/roadmap.py"]
        sid = r.json()["id"]
        r = await client.post(
            f"/api/roadmap/sessions/{sid}/commands",
            json={"command_label": "build", "command_text": "npm run build", "exit_code": 0},
        )
        assert r.status_code == 200
        # explicit JSON null on a text field -> 400 (was an unhandled NOT NULL 500)
        r = await client.patch(f"/api/roadmap/sessions/{sid}", json={"summary": None})
        assert r.status_code == 400
        assert "summary must be a string" in r.json()["detail"]
        r = await client.patch(
            f"/api/roadmap/sessions/{sid}",
            json={"status": "completed", "summary": "ok", "touched_files": ["app/api/roadmap.py"]},
        )
        assert r.status_code == 200
        assert r.json()["finished_at"]
        assert r.json()["touched_files"] == ["app/api/roadmap.py"]
        r = await client.get("/api/roadmap/sessions", params={"card_id": "M7-001"})
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["n_commands"] == 1

        r = await client.get("/api/roadmap/release-gates")
        assert r.status_code == 200
        gates = {g["name"]: g for g in r.json()}
        assert set(gates) == RELEASE_GATE_NAMES
        assert gates["Release A"]["prefixes"] == ["M0", "M1", "M2", "M3"]
        # counts come from the living seed, not a snapshot of board statuses
        total_a, done_a = _seed_gate("M0", "M1", "M2", "M3")
        assert (gates["Release A"]["total"], gates["Release A"]["done"]) == (total_a, done_a)
        assert gates["Release B"]["total"] == _seed_gate("M4", "M5", "M6")[0]
        total_c, _ = _seed_gate("M7")
        assert gates["Release C"]["total"] == total_c
        # living seed dones plus the forced M7-003 move above (a no-op if the
        # board has already marked M7-003 done)
        done_ids = {
            c["id"] for c in SEED["cards"]
            if c.get("phase", "").split(" ")[0] == "M7" and c.get("status") == "done"
        }
        assert gates["Release C"]["done"] == len(done_ids | {"M7-003"})
        assert gates["Release D"]["prefixes"] == ["M8"]
        assert gates["Release E"]["prefixes"] == ["M9"]
        assert gates["Release F"]["prefixes"] == ["M10"]
        assert gates["Release D"]["description"] == "Post-Audit Hardening"
        assert gates["Release E"]["description"] == "North Star R1"
        assert gates["Release F"]["description"] == "Bounded-Autonomy Loop"
        assert gates["Release D"]["total"] == _seed_gate("M8")[0]
        assert gates["Release E"]["total"] == _seed_gate("M9")[0]
        assert gates["Release F"]["total"] == _seed_gate("M10")[0]


async def test_api_prompt_and_process_endpoints():
    """HTTP face of M7-007 (GET /cards/{id}/prompt) and M7-006 (GET /process)."""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/roadmap/import", json={})).status_code == 200

        # prompt: {"prompt": str}, byte-stable across calls, 404 for unknown cards
        r1 = await client.get("/api/roadmap/cards/M7-007/prompt")
        assert r1.status_code == 200
        r2 = await client.get("/api/roadmap/cards/M7-007/prompt")
        assert r1.json() == r2.json()
        prompt = r1.json()["prompt"]
        assert prompt.startswith("You are implementing roadmap card M7-007")
        assert "Acceptance criteria:" in prompt
        assert "Constraints:" in prompt and "read CLAUDE.md first" in prompt
        assert (await client.get("/api/roadmap/cards/NOPE/prompt")).status_code == 404

        # process: the aggregate shape the plugin's 流程 tab renders
        r = await client.get("/api/roadmap/process")
        assert r.status_code == 200
        view = r.json()
        assert set(view) == {"active_sessions", "open_decisions", "release_gates", "blocked_cards"}
        assert {g["gate"] for g in view["release_gates"]} == RELEASE_GATE_NAMES
        for gate in view["release_gates"]:
            assert gate["cards_total"] >= gate["cards_done"]
            assert isinstance(gate["blockers"], list)
            assert isinstance(gate["ready"], bool)


async def test_api_m7_008_surface():
    """One HTTP-level check per new route: create/claim, checklist CRUD,
    dependency add/remove, decisions, export, and command-as-evidence."""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/roadmap/import", json={})).status_code == 200

        # create: happy path + funnel guard
        r = await client.post("/api/roadmap/cards", json={
            "id": "M7-API", "title": "api created", "phase": "M7 Roadmap Control Plane",
            "status": "ready", "acceptance": ["works over http"],
        })
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
        r = await client.post("/api/roadmap/cards", json={"title": "cheater", "status": "done"})
        assert r.status_code == 400
        r = await client.post("/api/roadmap/cards", json={"id": "M7-API", "title": "dup"})
        assert r.status_code == 400

        # claim: happy path + double claim -> 409 + 404
        r = await client.post("/api/roadmap/cards/M7-API/claim", json={"owner": "agent-a6"})
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"
        assert r.json()["owner"] == "agent-a6"
        r = await client.post("/api/roadmap/cards/M7-API/claim", json={"owner": "agent-b"})
        assert r.status_code == 409
        assert (await client.post("/api/roadmap/cards/NOPE/claim", json={"owner": "x"})).status_code == 404

        # checklist add/check/delete
        r = await client.post(
            "/api/roadmap/cards/M7-API/checklists", json={"kind": "review", "text": "http checked"}
        )
        assert r.status_code == 200
        item_id = r.json()["id"]
        r = await client.patch(f"/api/roadmap/checklists/{item_id}", json={"checked": True})
        assert r.status_code == 200
        assert r.json()["checked"] == 1
        assert (await client.delete(f"/api/roadmap/checklists/{item_id}")).status_code == 200
        assert (await client.delete(f"/api/roadmap/checklists/{item_id}")).status_code == 404

        # dependencies: add (with cycle guard) / remove
        r = await client.post("/api/roadmap/cards/M7-API/dependencies", json={"depends_on_id": "M7-001"})
        assert r.status_code == 200
        dep_id = r.json()["id"]
        r = await client.post("/api/roadmap/cards/M7-API/dependencies", json={"depends_on_id": "M7-API"})
        assert r.status_code == 400
        assert (await client.delete(f"/api/roadmap/dependencies/{dep_id}")).status_code == 200
        assert (await client.delete(f"/api/roadmap/dependencies/{dep_id}")).status_code == 404

        # decisions: open -> list -> get -> resolve (twice -> 409)
        r = await client.post("/api/roadmap/decisions", json={
            "title": "http decision", "question": "does the api work?", "card_id": "M7-API",
        })
        assert r.status_code == 200
        did = r.json()["id"]
        r = await client.get("/api/roadmap/decisions", params={"card_id": "M7-API"})
        assert r.status_code == 200
        assert [d["id"] for d in r.json()] == [did]
        assert (await client.get(f"/api/roadmap/decisions/{did}")).status_code == 200
        assert (await client.get("/api/roadmap/decisions/nope")).status_code == 404
        r = await client.patch(f"/api/roadmap/decisions/{did}", json={"status": "resolved", "decision": "yes"})
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"
        r = await client.patch(f"/api/roadmap/decisions/{did}", json={"status": "resolved", "decision": "no"})
        assert r.status_code == 409
        # P1-2 regression: a status-less PATCH cannot rewrite a resolved decision
        r = await client.patch(f"/api/roadmap/decisions/{did}", json={"decision": "no"})
        assert r.status_code == 409
        assert "immutable" in r.json()["detail"]
        assert (await client.get(f"/api/roadmap/decisions/{did}")).json()["decision"] == "yes"

        # session command attaches as evidence over http (M7-005 acceptance c)
        r = await client.post(
            "/api/roadmap/cards/M7-API/sessions", json={"actor": "agent-a6", "goal": "verify http"}
        )
        sid = r.json()["id"]
        r = await client.post(
            f"/api/roadmap/sessions/{sid}/commands",
            json={"command_label": "pytest", "command_text": "pytest -q", "exit_code": 0, "as_evidence": True},
        )
        assert r.status_code == 200
        assert r.json()["evidence_id"]
        card = (await client.get("/api/roadmap/cards/M7-API")).json()
        assert [e["kind"] for e in card["evidence"]] == ["command"]
        assert card["evidence"][0]["status"] == "pass"

        # export: seed-shaped snapshot that the import endpoint accepts as a shape
        r = await client.get("/api/roadmap/export")
        assert r.status_code == 200
        snap = r.json()
        assert snap["version"] == 1
        assert {c["id"] for c in snap["cards"]} >= {"M7-001", "M7-API"}
        api_card = next(c for c in snap["cards"] if c["id"] == "M7-API")
        assert api_card["owner"] == "agent-a6"
        assert api_card["status"] == "in_progress"
        assert api_card["acceptance"] == ["works over http"]
