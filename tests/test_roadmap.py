"""Roadmap control plane: idempotent seed import, move gates, sessions, API."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import bus
from app.institute import roadmap

BACKLOG = Path(__file__).resolve().parent.parent / "roadmap" / "backlog.json"
SEED = json.loads(BACKLOG.read_text(encoding="utf-8"))
N_CARDS = len(SEED["cards"])


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
    assert card["status"] == "review"  # seed status (M7-001 is under review)
    assert card["verification"] == [".venv/bin/python -m pytest tests/test_roadmap.py -q"]
    assert card["design_links"] == ["roadmap/02-data-model.md", "roadmap/05-global-coding-process.md"]
    assert card["expected_files"] == [
        "migrations/*.sql", "app/institute/roadmap.py", "app/api/roadmap.py", "tests/test_roadmap.py",
    ]
    assert card["tags"] == []  # absent in the seed -> empty list round-trip
    acceptance = [c for c in card["checklists"] if c["kind"] == "acceptance"]
    assert len(acceptance) == 4  # merged by text, never duplicated

    # a changed seed updates fields but local status wins unless force
    data = _seed_copy()
    target = next(c for c in data["cards"] if c["id"] == "M7-001")
    target["title"] = "Roadmap durable backend"
    target["status"] = "parked"
    seed2 = tmp_path / "backlog.json"
    seed2.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res3 = await roadmap.import_backlog(seed2)
    assert res3 == {"created": 0, "updated": 1, "unchanged": N_CARDS - 1, "total": N_CARDS}
    card = await roadmap.get_card("M7-001")
    assert card["title"] == "Roadmap durable backend"
    assert card["status"] == "review"  # local status preserved

    await roadmap.import_backlog(seed2, force=True)
    card = await roadmap.get_card("M7-001")
    assert card["status"] == "parked"

    events = await bus.replay(0, types=["roadmap.import.completed"])
    assert len(events) == 4
    forced = await bus.replay(0, types=["roadmap.import.status_forced"])
    assert len(forced) == 1  # forced flips stay on the audit trail
    assert forced[0].ref_id == "M7-001"
    assert forced[0].payload == {"from": "review", "to": "parked"}


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


# ---- (c) move rules ----------------------------------------------------------

async def test_move_to_done_gated_by_dependencies_and_override():
    await roadmap.import_backlog()

    # M7-003 depends on M7-001 which is not 'done' -> done is blocked
    before = (await roadmap.get_card("M7-003"))["status"]
    with pytest.raises(roadmap.RoadmapError, match="M7-001"):
        await roadmap.move("M7-003", "done")
    assert (await roadmap.get_card("M7-003"))["status"] == before

    forced = await roadmap.move("M7-003", "done", override=True, reason="operator override")
    assert forced["status"] == "done"
    assert forced["completed_at"]

    events = await bus.replay(0, types=["roadmap.card.moved"])
    assert events[-1].ref_id == "M7-003"
    assert events[-1].payload == {
        "from": before, "to": "done", "override": True, "reason": "operator override",
    }


async def test_plain_moves_and_conditional_claim():
    await roadmap.import_backlog()

    card = await roadmap.move("M7-001", "in_progress", owner="claude")
    assert card["status"] == "in_progress"
    assert card["owner"] == "claude"

    # conditional claim: a stale mover loses and the row stays untouched
    with pytest.raises(roadmap.MoveConflict):
        await roadmap.move("M7-001", "review", expected_status="ready")
    assert (await roadmap.get_card("M7-001"))["status"] == "in_progress"

    sess = await roadmap.create_session(
        "M7-001", actor="claude", goal="finish the roadmap backend",
    )
    await roadmap.update_session(
        sess["id"], {"status": "completed", "summary": "backend complete and verified"},
    )
    card = await roadmap.move("M7-001", "review")
    assert card["status"] == "review"

    # done needs evidence even with zero dependencies (02-data-model.md)
    with pytest.raises(roadmap.RoadmapError, match="evidence"):
        await roadmap.move("M7-001", "done")
    await roadmap.add_evidence("M7-001", "test", "pytest tests/test_roadmap.py", status="pass")
    card = await roadmap.move("M7-001", "done")
    assert card["status"] == "done"
    assert card["completed_at"]

    # in_progress requires an owner unless one is provided
    with pytest.raises(roadmap.RoadmapError, match="owner"):
        await roadmap.move("M3-001", "in_progress")

    assert await roadmap.move("no-such-card", "ready") is None


async def test_review_requires_completed_session_summary_unless_overridden():
    await roadmap.import_backlog()
    await roadmap.move("M7-005", "in_progress", owner="cursor")

    with pytest.raises(roadmap.RoadmapError, match="completed coding session"):
        await roadmap.move("M7-005", "review")

    sess = await roadmap.create_session(
        "M7-005", actor="cursor", goal="add coding session tracking",
    )
    with pytest.raises(roadmap.RoadmapError, match="summary"):
        await roadmap.update_session(sess["id"], {"status": "completed"})

    await roadmap.update_session(sess["id"], {"summary": "implementation and tests complete"})
    with pytest.raises(roadmap.RoadmapError, match="completed coding session"):
        await roadmap.move("M7-005", "review")

    await roadmap.update_session(sess["id"], {"status": "completed"})
    card = await roadmap.move("M7-005", "review")
    assert card["status"] == "review"

    await roadmap.move("M7-007", "in_progress", owner="cursor")
    forced = await roadmap.move("M7-007", "review", override=True, reason="operator reviewed manually")
    assert forced["status"] == "review"


async def test_review_gate_cannot_be_starved_or_raced():
    await roadmap.import_backlog()
    await roadmap.move("M7-005", "in_progress", owner="cursor")
    sess = await roadmap.create_session("M7-005", actor="cursor", goal="session tracking")
    await roadmap.update_session(sess["id"], {"status": "completed", "summary": "done and verified"})

    # a terminal session's summary cannot be blanked afterwards (whitespace included)
    for blank in ("", "   ", "\n", "\t\n"):
        with pytest.raises(roadmap.RoadmapError, match="needs a summary"):
            await roadmap.update_session(sess["id"], {"summary": blank})

    # TOCTOU: completing WITHOUT a summary in the same request must re-check
    # the DB summary inside the claim — a concurrently blanked summary (legal
    # while active) cannot ride into completed
    await roadmap.update_session(sess["id"], {"status": "active"})
    await roadmap.update_session(sess["id"], {"summary": "\n"})  # active drafts may be blank
    with pytest.raises(roadmap.RoadmapError, match="needs a summary"):
        # validation sees the blank too, but force the claim path as well:
        # pre-read row lies about a non-blank summary
        from unittest.mock import patch as _patch

        real_query_one = roadmap.db.query_one

        async def lying_row(sql, params=()):
            row = await real_query_one(sql, params)
            if row is not None and sql.startswith("SELECT * FROM roadmap_coding_sessions") and "summary" in row:
                row = dict(row, summary="looks fine")
            return row

        with _patch.object(roadmap.db, "query_one", side_effect=lying_row):
            await roadmap.update_session(sess["id"], {"status": "completed"})
    assert (await roadmap.get_session(sess["id"]))["status"] == "active"
    await roadmap.update_session(sess["id"], {"summary": "done and verified"})
    await roadmap.update_session(sess["id"], {"status": "completed"})
    assert (await roadmap.get_session(sess["id"]))["summary"] == "done and verified"
    await roadmap.update_session(sess["id"], {"status": "active"})

    # reopening the only eligible session starves the gate again
    await roadmap.update_session(sess["id"], {"status": "active"})
    with pytest.raises(roadmap.RoadmapError, match="completed coding session"):
        await roadmap.move("M7-005", "review")
    assert (await roadmap.get_card("M7-005"))["status"] == "in_progress"

    # TOCTOU: even if the pre-check is fooled, the conditional claim itself
    # carries the gate — the card must never land in review without an
    # eligible session (the lie makes the failure surface as a conflict)
    from unittest.mock import patch

    real_query_one = roadmap.db.query_one

    async def lying_query_one(sql, params=()):
        if sql.startswith("SELECT 1 AS ok WHERE"):
            return {"ok": 1}
        return await real_query_one(sql, params)

    with patch.object(roadmap.db, "query_one", side_effect=lying_query_one):
        with pytest.raises(roadmap.RoadmapError):
            await roadmap.move("M7-005", "review")
    assert (await roadmap.get_card("M7-005"))["status"] == "in_progress"

    # with an eligible session the same claim goes through
    await roadmap.update_session(sess["id"], {"status": "completed", "summary": "done and verified"})
    assert (await roadmap.move("M7-005", "review"))["status"] == "review"


async def test_command_evidence_survives_bus_failure():
    """A committed command+evidence write must not surface as an error when the
    post-commit bus mirror fails (a retry would duplicate both rows)."""
    from unittest.mock import patch

    from app import bus as bus_mod

    await roadmap.import_backlog()
    sess = await roadmap.create_session("M7-001", actor="cursor", goal="verify evidence path")

    async def exploding_emit(*args, **kwargs):
        raise RuntimeError("bus down")

    with patch.object(bus_mod, "emit", side_effect=exploding_emit):
        command = await roadmap.append_command(
            sess["id"], "pytest", "pytest -q", exit_code=0, attach_as_evidence=True,
        )
    assert command is not None and command["evidence_id"]

    card = await roadmap.get_card("M7-001")
    assert len([e for e in card["evidence"] if e["id"] == command["evidence_id"]]) == 1
    n_cmds = await roadmap.db.query_one(
        "SELECT COUNT(*) AS n FROM roadmap_session_commands WHERE session_id = ?", (sess["id"],)
    )
    assert n_cmds["n"] == 1
    audit = await roadmap.db.query(
        "SELECT * FROM roadmap_events WHERE event_type = 'evidence.added' AND card_id = 'M7-001'"
    )
    assert len(audit) == 1  # audit row committed with the business rows


async def test_move_gates_empty_acceptance_and_blocked(tmp_path):
    await roadmap.import_backlog()

    # a card with no acceptance checklist cannot move to ready; a second temp
    # card seeds as ready so the blocked gate is independent of real statuses
    data = _seed_copy()
    data["cards"].append({
        "id": "M7-TMP", "title": "temp card without acceptance",
        "phase": "M7 Roadmap Control Plane",
    })
    data["cards"].append({
        "id": "M7-TMP2", "title": "temp ready card", "status": "ready",
        "phase": "M7 Roadmap Control Plane", "acceptance": ["it works"],
    })
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)
    with pytest.raises(roadmap.RoadmapError, match="acceptance checklist is empty"):
        await roadmap.move("M7-TMP", "ready")
    assert (await roadmap.get_card("M7-TMP"))["status"] == "inbox"

    # a blocked card cannot move forward unless override
    await roadmap.update_card("M7-TMP2", {"blocked_reason": "waiting on upstream schema"})
    with pytest.raises(roadmap.RoadmapError, match="blocked"):
        await roadmap.move("M7-TMP2", "in_progress", owner="claude")
    assert (await roadmap.get_card("M7-TMP2"))["status"] == "ready"
    card = await roadmap.move("M7-TMP2", "in_progress", owner="claude", override=True)
    assert card["status"] == "in_progress"


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

    command = await roadmap.append_command(
        sess["id"], "pytest", ".venv/bin/python -m pytest tests/test_roadmap.py -q",
        exit_code=0, output_excerpt="all green", attach_as_evidence=True,
    )
    assert command["evidence_id"]
    await roadmap.append_command(sess["id"], "compileall", ".venv/bin/python -m compileall app -q", exit_code=0)

    got = await roadmap.get_session(sess["id"])
    assert [c["command_label"] for c in got["commands"]] == ["pytest", "compileall"]
    assert got["commands"][0]["exit_code"] == 0
    assert got["commands"][0]["output_excerpt"] == "all green"
    card = await roadmap.get_card("M7-001")
    evidence = next(e for e in card["evidence"] if e["id"] == command["evidence_id"])
    assert evidence["kind"] == "command"
    assert evidence["status"] == "pass"
    assert evidence["artifact_ref"] == f"roadmap-session:{sess['id']}/command:{command['id']}"
    assert "pytest tests/test_roadmap.py" in evidence["body"]

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


# ---- (f) M7-008: create, claim, checklists, dependencies, decisions, export ----

async def test_create_card_validates_and_lands_with_children():
    await roadmap.import_backlog()

    card = await roadmap.create_card({
        "id": "M7-NEW", "title": "brand new card", "phase": "M7 Roadmap Control Plane",
        "status": "ready", "acceptance": ["it works", "it is tested"],
        "dependencies": ["M7-001"], "verification": ["pytest -q"],
    })
    assert card["status"] == "ready"
    assert [c["text"] for c in card["checklists"]] == ["it works", "it is tested"]
    assert [d["depends_on_id"] for d in card["dependencies"]] == ["M7-001"]
    assert card["dependencies"][0]["source"] == "manual"
    assert card["sort_order"] > 0

    with pytest.raises(roadmap.RoadmapError, match="already exists"):
        await roadmap.create_card({"id": "M7-NEW", "title": "dup"})
    with pytest.raises(roadmap.RoadmapError, match="unknown card"):
        await roadmap.create_card({"id": "M7-NEW2", "title": "x", "dependencies": ["ZZ-1"]})
    with pytest.raises(roadmap.RoadmapError, match="acceptance checklist"):
        await roadmap.create_card({"id": "M7-NEW3", "title": "x", "status": "ready"})
    with pytest.raises(roadmap.RoadmapError, match="creation status"):
        await roadmap.create_card({"id": "M7-NEW4", "title": "x", "status": "done"})
    # nothing from the failed creates leaked (transaction rolled back)
    assert await roadmap.get_card("M7-NEW2") is None

    events = await bus.replay(0, types=["roadmap.card.created"])
    assert [e.ref_id for e in events] == ["M7-NEW"]


async def test_claim_is_atomic_and_respects_blocked():
    await roadmap.import_backlog()

    card = await roadmap.claim_card("M5-001", "cursor")  # seeds as inbox
    assert card["status"] == "in_progress"
    assert card["owner"] == "cursor"
    events = await bus.replay(0, types=["roadmap.card.claimed"])
    assert events[-1].ref_id == "M5-001"
    assert events[-1].payload == {"owner": "cursor", "from": "inbox"}

    # already in_progress -> second claim by someone else conflicts
    with pytest.raises(roadmap.MoveConflict):
        await roadmap.claim_card("M5-001", "codex")

    # blocked cards cannot be claimed
    await roadmap.update_card("M3-001", {"blocked_reason": "waiting on schema"})
    with pytest.raises(roadmap.RoadmapError, match="blocked"):
        await roadmap.claim_card("M3-001", "cursor")

    # stale expected_status -> conflict; unknown card -> None
    with pytest.raises(roadmap.MoveConflict):
        await roadmap.claim_card("M4-001", "cursor", expected_status="ready")
    assert await roadmap.claim_card("NOPE", "cursor") is None
    with pytest.raises(roadmap.RoadmapError, match="owner"):
        await roadmap.claim_card("M4-001", "  ")


async def test_checklist_crud_and_import_merge():
    await roadmap.import_backlog()

    item = await roadmap.add_checklist_item("M7-001", "review", "scope did not drift")
    assert item["kind"] == "review"
    with pytest.raises(roadmap.RoadmapError, match="already exists"):
        await roadmap.add_checklist_item("M7-001", "review", "scope did not drift")
    with pytest.raises(roadmap.RoadmapError, match="unknown checklist kind"):
        await roadmap.add_checklist_item("M7-001", "vibes", "x")
    assert await roadmap.add_checklist_item("NOPE", "review", "x") is None

    checked = await roadmap.set_checklist_item(item["id"], checked=True)
    assert checked["checked"] == 1
    renamed = await roadmap.set_checklist_item(item["id"], text="scope stayed put")
    assert renamed["text"] == "scope stayed put"
    assert renamed["checked"] == 1  # text edit does not reset checked

    # re-import keeps manual items and their checked state
    await roadmap.import_backlog()
    card = await roadmap.get_card("M7-001")
    mine = [c for c in card["checklists"] if c["id"] == item["id"]]
    assert len(mine) == 1 and mine[0]["checked"] == 1

    assert await roadmap.remove_checklist_item(item["id"]) is True
    assert await roadmap.remove_checklist_item(item["id"]) is False

    events = await bus.replay(0, types=["roadmap.checklist"])
    types = [e.type for e in events]
    assert "roadmap.checklist.added" in types
    assert "roadmap.checklist.checked" in types
    assert "roadmap.checklist.removed" in types


async def test_dependency_crud_cycles_and_import_reconciliation(tmp_path):
    await roadmap.import_backlog()

    dep = await roadmap.add_dependency("M7-007", "M7-005")
    assert dep["source"] == "manual"
    with pytest.raises(roadmap.RoadmapError, match="already depends"):
        await roadmap.add_dependency("M7-007", "M7-005")
    with pytest.raises(roadmap.RoadmapError, match="itself"):
        await roadmap.add_dependency("M7-007", "M7-007")
    with pytest.raises(roadmap.RoadmapError, match="unknown card"):
        await roadmap.add_dependency("M7-007", "ZZ-9")
    assert await roadmap.add_dependency("NOPE", "M7-001") is None

    # direct and transitive cycles are rejected
    with pytest.raises(roadmap.RoadmapError, match="cycle"):
        await roadmap.add_dependency("M7-005", "M7-007")
    with pytest.raises(roadmap.RoadmapError, match="cycle"):
        await roadmap.add_dependency("M7-001", "M7-007")  # M7-007 -> M7-005 -> M7-001

    # manual dependencies survive a re-import; import-owned rows still reconcile
    data = _seed_copy()
    next(c for c in data["cards"] if c["id"] == "M7-003")["dependencies"] = []
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)
    card = await roadmap.get_card("M7-007")
    assert "M7-005" in [d["depends_on_id"] for d in card["dependencies"]]
    assert (await roadmap.get_card("M7-003"))["dependencies"] == []  # import row reconciled away

    assert await roadmap.remove_dependency(dep["id"]) is True
    assert await roadmap.remove_dependency(dep["id"]) is False


async def test_decisions_open_resolve_exactly_once():
    await roadmap.import_backlog()

    decision = await roadmap.open_decision(
        "hand policy", "which hand runs research?", card_id="M7-005",
        options=["codex", "claude"],
    )
    assert decision["status"] == "open"
    assert decision["options"] == ["codex", "claude"]
    with pytest.raises(roadmap.RoadmapError, match="unknown card"):
        await roadmap.open_decision("x", "y", card_id="NOPE")
    with pytest.raises(roadmap.RoadmapError, match="title and a question"):
        await roadmap.open_decision("", "y")

    listed = await roadmap.list_decisions(card_id="M7-005", status="open")
    assert [d["id"] for d in listed] == [decision["id"]]

    resolved = await roadmap.resolve_decision(decision["id"], "codex")
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "codex"
    assert resolved["resolved_at"]
    with pytest.raises(roadmap.MoveConflict):  # resolves exactly once
        await roadmap.resolve_decision(decision["id"], "claude")
    assert await roadmap.resolve_decision("NOPE", "x") is None

    events = await bus.replay(0, types=["roadmap.decision"])
    types = [e.type for e in events]
    assert "roadmap.decision.opened" in types and "roadmap.decision.resolved" in types


async def test_agent_prompt_is_deterministic_and_complete():
    await roadmap.import_backlog()

    first = await roadmap.generate_agent_prompt("M7-005")
    second = await roadmap.generate_agent_prompt("M7-005")
    assert first == second  # deterministic for the same card state
    assert first["generated"] is True
    prompt = first["prompt"]
    card = await roadmap.get_card("M7-005")
    assert f"roadmap card {card['id']}: {card['title']}" in prompt
    for link in card["design_links"]:
        assert link in prompt
    for f in card["expected_files"]:
        assert f in prompt
    for c in card["checklists"]:
        assert c["text"] in prompt
    for v in card["verification"]:
        assert v in prompt
    assert "M7-001" in prompt  # dependency
    assert "do not push" in prompt  # constraints block

    # card state change changes the prompt; operator override wins verbatim
    await roadmap.add_checklist_item("M7-005", "acceptance", "one more criterion")
    third = await roadmap.generate_agent_prompt("M7-005")
    assert third["prompt"] != prompt
    assert "one more criterion" in third["prompt"]

    await roadmap.update_card("M7-005", {"agent_prompt": "只做这一件事。"})
    overridden = await roadmap.generate_agent_prompt("M7-005")
    assert overridden == {"card_id": "M7-005", "prompt": "只做这一件事。", "generated": False}

    assert await roadmap.generate_agent_prompt("NOPE") is None


async def test_export_restores_faithfully_into_fresh_db(tmp_path):
    await roadmap.import_backlog()
    # make live state diverge from the seed: a move, a manual dep with a custom
    # relation, operator process state (blocker, checked boxes, review items)
    sess = await roadmap.create_session("M7-003", actor="cursor", goal="kanban ui")
    await roadmap.update_session(sess["id"], {"status": "completed", "summary": "ui done"})
    await roadmap.move("M7-003", "in_progress", owner="cursor")
    await roadmap.create_card({
        "id": "M9-001", "title": "exported newcomer", "phase": "M9 Future",
        "acceptance": ["exists"],
    })
    await roadmap.add_dependency("M9-001", "M7-003", relation="informs")
    await roadmap.update_card("M9-001", {"blocked_reason": "waiting on operator"})
    item = await roadmap.add_checklist_item("M9-001", "review", "diff reviewed")
    await roadmap.set_checklist_item(item["id"], checked=True)
    acceptance_item = next(
        c for c in (await roadmap.get_card("M9-001"))["checklists"] if c["kind"] == "acceptance"
    )
    await roadmap.set_checklist_item(acceptance_item["id"], checked=True)

    snapshot = await roadmap.export_backlog()
    assert snapshot["columns"] == list(roadmap.STATUSES)
    by_id = {c["id"]: c for c in snapshot["cards"]}
    assert by_id["M7-003"]["status"] == "in_progress"
    assert by_id["M9-001"]["dependencies"] == ["M7-003"]
    assert by_id["M9-001"]["dependencies_meta"] == [
        {"depends_on_id": "M7-003", "relation": "informs", "source": "manual"},
    ]
    assert by_id["M9-001"]["blocked_reason"] == "waiting on operator"
    assert by_id["M9-001"]["acceptance_checked"] == ["exists"]
    assert by_id["M9-001"]["checklists_extra"] == [
        {"kind": "review", "text": "diff reviewed", "checked": True},
    ]
    assert by_id["M7-001"]["acceptance"]  # checklist texts round-trip
    assert "M9 Future" in snapshot["phases"]

    # same-DB re-import: no duplicates, nothing created
    out = tmp_path / "export.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    res = await roadmap.import_backlog(out)
    assert res["created"] == 0

    # FRESH-DB restore: wipe the roadmap tables and import the snapshot —
    # ownership, relation, blocker, and checked state must all come back
    for table in (
        "roadmap_session_commands", "roadmap_coding_sessions", "roadmap_evidence",
        "roadmap_events", "roadmap_dependencies", "roadmap_checklists",
        "roadmap_decisions", "roadmap_cards",
    ):
        await roadmap.db.execute(f"DELETE FROM {table}")
    res = await roadmap.import_backlog(out)
    assert res["created"] == len(snapshot["cards"])

    restored = await roadmap.get_card("M9-001")
    assert restored["status"] == "inbox"
    assert restored["blocked_reason"] == "waiting on operator"
    dep = restored["dependencies"][0]
    assert (dep["depends_on_id"], dep["relation"], dep["source"]) == ("M7-003", "informs", "manual")
    by_kind = {(c["kind"], c["text"]): c for c in restored["checklists"]}
    assert by_kind[("acceptance", "exists")]["checked"] == 1
    assert by_kind[("review", "diff reviewed")]["checked"] == 1
    # manual ownership survives, so a later seed reconcile cannot delete it
    await roadmap.import_backlog(out)
    assert (await roadmap.get_card("M9-001"))["dependencies"]


async def test_import_rejects_cycles_in_final_graph(tmp_path):
    await roadmap.import_backlog()
    await roadmap.add_dependency("M7-003", "M7-007")  # manual edge A -> B

    # a seed shipping B -> A would close the loop through the manual edge:
    # the import must fail atomically (nothing written)
    data = _seed_copy()
    next(c for c in data["cards"] if c["id"] == "M7-007")["dependencies"] = ["M7-003"]
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = await roadmap.get_card("M7-007")
    with pytest.raises(roadmap.RoadmapError, match="cycle"):
        await roadmap.import_backlog(seed)
    after = await roadmap.get_card("M7-007")
    assert [d["depends_on_id"] for d in after["dependencies"]] == [
        d["depends_on_id"] for d in before["dependencies"]
    ]


async def test_create_and_checklist_edge_inputs_are_400_not_500():
    await roadmap.import_backlog()

    # explicit null sort_order means "auto", not float(None)
    card = await roadmap.create_card({"id": "M9-EDGE", "title": "edge", "sort_order": None})
    assert card["sort_order"] > 0
    with pytest.raises(roadmap.RoadmapError, match="number"):
        await roadmap.create_card({"id": "M9-EDGE2", "title": "edge", "sort_order": "top"})
    # non-finite floats would hit the NOT NULL column as a 500
    for bad in ("NaN", float("nan"), "Infinity", float("inf")):
        with pytest.raises(roadmap.RoadmapError, match="finite"):
            await roadmap.create_card({"id": "M9-EDGE3", "title": "edge", "sort_order": bad})
        with pytest.raises(roadmap.RoadmapError, match="finite"):
            await roadmap.update_card("M9-EDGE", {"sort_order": bad})
        with pytest.raises(roadmap.RoadmapError, match="finite"):
            await roadmap.move("M9-EDGE", "ready", sort_order=bad)  # move() path too

    # \x1f is the det-id/reconcile-pair separator: ids and relations carrying
    # it could alias two different (id, relation) tuples onto one encoding.
    # Leading/trailing positions matter too — str.strip() eats \x1f, so a
    # post-strip check would silently normalize instead of rejecting
    for bad_id in ("M9\x1fALIAS", "M9-EDGE\x1f", "\x1fM9-EDGE"):
        with pytest.raises(roadmap.RoadmapError, match="reserved"):
            await roadmap.create_card({"id": bad_id, "title": "x"})
    for bad_rel in ("informs\x1fblocks", "\x1finforms", "informs\x1f"):
        with pytest.raises(roadmap.RoadmapError, match="reserved"):
            await roadmap.add_dependency("M9-EDGE", "M7-001", relation=bad_rel)

    # renaming a checklist item onto an existing text is a validation error
    a = await roadmap.add_checklist_item("M9-EDGE", "review", "first")
    await roadmap.add_checklist_item("M9-EDGE", "review", "second")
    with pytest.raises(roadmap.RoadmapError, match="already exists"):
        await roadmap.set_checklist_item(a["id"], text="second")


async def test_import_reconciles_relation_changes(tmp_path):
    await roadmap.import_backlog()
    deps = (await roadmap.get_card("M7-003"))["dependencies"]
    assert [(d["depends_on_id"], d["relation"]) for d in deps] == [("M7-001", "blocks")]

    # the seed changes the edge's relation: the old pair must be REPLACED,
    # not accumulated next to the new one
    data = _seed_copy()
    card = next(c for c in data["cards"] if c["id"] == "M7-003")
    card["dependencies_meta"] = [
        {"depends_on_id": "M7-001", "relation": "informs", "source": "import"},
    ]
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    await roadmap.import_backlog(seed)

    deps = (await roadmap.get_card("M7-003"))["dependencies"]
    assert [(d["depends_on_id"], d["relation"]) for d in deps] == [("M7-001", "informs")]

    # seeds cannot smuggle the pair separator through dependencies_meta
    data = _seed_copy()
    next(c for c in data["cards"] if c["id"] == "M7-003")["dependencies_meta"] = [
        {"depends_on_id": "M7-001", "relation": "a\x1fb", "source": "import"},
    ]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(roadmap.RoadmapError, match="reserved"):
        await roadmap.import_backlog(bad)


# ---- API surface ------------------------------------------------------------------

async def test_api_roundtrip_and_release_gates():
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # the import endpoint only reads seeds inside the repository
        r = await client.post("/api/roadmap/import", json={"path": "/etc/hosts"})
        assert r.status_code == 400

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
        r = await client.post("/api/roadmap/cards/M7-001/move", json={"status": "in_progress"})
        assert r.status_code == 200
        r = await client.patch("/api/roadmap/cards/M7-001", json={"priority": "P9"})
        assert r.status_code == 400

        r = await client.post(
            "/api/roadmap/cards/M7-001/sessions", json={"actor": "claude", "goal": "wire the api"}
        )
        assert r.status_code == 200
        sid = r.json()["id"]
        r = await client.post(
            f"/api/roadmap/sessions/{sid}/commands",
            json={
                "command_label": "build", "command_text": "npm run build",
                "exit_code": 0, "attach_as_evidence": True,
            },
        )
        assert r.status_code == 200
        assert r.json()["evidence_id"]
        r = await client.post("/api/roadmap/cards/M7-001/move", json={"status": "review"})
        assert r.status_code == 400
        assert "completed coding session" in r.json()["detail"]
        r = await client.patch(f"/api/roadmap/sessions/{sid}", json={"status": "completed", "summary": "ok"})
        assert r.status_code == 200
        assert r.json()["finished_at"]
        r = await client.post("/api/roadmap/cards/M7-001/move", json={"status": "review"})
        assert r.status_code == 200
        r = await client.get("/api/roadmap/sessions", params={"card_id": "M7-001"})
        assert r.status_code == 200
        assert len(r.json()) == 1

        r = await client.get("/api/roadmap/release-gates")
        assert r.status_code == 200
        gates = {g["name"]: g for g in r.json()}
        assert set(gates) == {"Release A", "Release B", "Release C"}
        assert gates["Release A"]["prefixes"] == ["M0", "M1", "M2", "M3"]
        assert gates["Release A"]["total"] == 8
        assert gates["Release A"]["done"] == 3  # M0-001, M0-002, M1-000 seeded done
        assert gates["Release B"]["total"] == 2
        assert gates["Release C"]["total"] == 8
        assert gates["Release C"]["done"] == 2  # M7-009 seeded done + M7-003 forced done above
        # evidence readiness: M7-001 got command evidence above and is not done
        assert "M7-001" in gates["Release C"]["evidence_ready"]
        assert "M7-001" in gates["Release C"]["remaining"]

        # M7-008/M7-007 API surface: create, claim, checklists, deps, decisions,
        # prompt, export
        r = await client.post("/api/roadmap/cards", json={
            "id": "API-1", "title": "api-born card", "acceptance": ["works"], "status": "ready",
        })
        assert r.status_code == 200
        r = await client.post("/api/roadmap/cards", json={"id": "API-1", "title": "dup"})
        assert r.status_code == 400
        r = await client.post("/api/roadmap/cards/API-1/claim", json={"owner": "cursor"})
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"
        r = await client.post("/api/roadmap/cards/API-1/claim", json={"owner": "codex"})
        assert r.status_code == 409

        r = await client.post("/api/roadmap/cards/API-1/checklists", json={"text": "one more"})
        assert r.status_code == 200
        item_id = r.json()["id"]
        r = await client.patch(f"/api/roadmap/checklists/{item_id}", json={"checked": True})
        assert r.status_code == 200
        assert r.json()["checked"] == 1
        r = await client.delete(f"/api/roadmap/checklists/{item_id}")
        assert r.status_code == 200
        assert (await client.delete(f"/api/roadmap/checklists/{item_id}")).status_code == 404

        r = await client.post("/api/roadmap/cards/API-1/dependencies", json={"depends_on_id": "M7-005"})
        assert r.status_code == 200
        dep_id = r.json()["id"]
        r = await client.post("/api/roadmap/cards/M7-005/dependencies", json={"depends_on_id": "API-1"})
        assert r.status_code == 400  # cycle
        assert (await client.delete(f"/api/roadmap/dependencies/{dep_id}")).status_code == 200

        r = await client.post("/api/roadmap/decisions", json={
            "title": "naming", "question": "keep API-1?", "card_id": "API-1", "options": ["yes", "no"],
        })
        assert r.status_code == 200
        decision_id = r.json()["id"]
        r = await client.get("/api/roadmap/decisions", params={"status": "open"})
        assert decision_id in [d["id"] for d in r.json()]
        r = await client.patch(f"/api/roadmap/decisions/{decision_id}", json={"decision": "yes"})
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"
        r = await client.patch(f"/api/roadmap/decisions/{decision_id}", json={"decision": "no"})
        assert r.status_code == 409

        r = await client.get("/api/roadmap/cards/API-1/prompt")
        assert r.status_code == 200
        assert "API-1" in r.json()["prompt"]
        assert (await client.get("/api/roadmap/cards/NOPE/prompt")).status_code == 404

        r = await client.get("/api/roadmap/export")
        assert r.status_code == 200
        snapshot = r.json()
        assert "API-1" in {c["id"] for c in snapshot["cards"]}
