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
# the seed is a living board — assert against its current M7-001 status, not a snapshot
M7_STATUS = next(c for c in SEED["cards"] if c["id"] == "M7-001").get("status", "inbox")


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

    await roadmap.append_command(
        sess["id"], "pytest", ".venv/bin/python -m pytest tests/test_roadmap.py -q",
        exit_code=0, output_excerpt="all green",
    )
    await roadmap.append_command(sess["id"], "compileall", ".venv/bin/python -m compileall app -q", exit_code=0)

    got = await roadmap.get_session(sess["id"])
    assert [c["command_label"] for c in got["commands"]] == ["pytest", "compileall"]
    assert got["commands"][0]["exit_code"] == 0
    assert got["commands"][0]["output_excerpt"] == "all green"

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
        r = await client.patch("/api/roadmap/cards/M7-001", json={"priority": "P9"})
        assert r.status_code == 400

        r = await client.post(
            "/api/roadmap/cards/M7-001/sessions", json={"actor": "claude", "goal": "wire the api"}
        )
        assert r.status_code == 200
        sid = r.json()["id"]
        r = await client.post(
            f"/api/roadmap/sessions/{sid}/commands",
            json={"command_label": "build", "command_text": "npm run build", "exit_code": 0},
        )
        assert r.status_code == 200
        r = await client.patch(f"/api/roadmap/sessions/{sid}", json={"status": "completed", "summary": "ok"})
        assert r.status_code == 200
        assert r.json()["finished_at"]
        r = await client.get("/api/roadmap/sessions", params={"card_id": "M7-001"})
        assert r.status_code == 200
        assert len(r.json()) == 1

        r = await client.get("/api/roadmap/release-gates")
        assert r.status_code == 200
        gates = {g["name"]: g for g in r.json()}
        assert set(gates) == {"Release A", "Release B", "Release C"}
        assert gates["Release A"]["prefixes"] == ["M0", "M1", "M2", "M3"]
        # counts come from the living seed, not a snapshot of board statuses
        total_a, done_a = _seed_gate("M0", "M1", "M2", "M3")
        assert (gates["Release A"]["total"], gates["Release A"]["done"]) == (total_a, done_a)
        assert gates["Release B"]["total"] == _seed_gate("M4", "M5", "M6")[0]
        total_c, done_c = _seed_gate("M7")
        assert gates["Release C"]["total"] == total_c
        assert gates["Release C"]["done"] == done_c + 1  # M7-003 forced done above
