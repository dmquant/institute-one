"""Thesis registry schema (card M1-001): lifecycle CHECKs, version history, indices.

Schema-level tests only — the domain module and API are card M1-002. The autouse
``app_runtime`` fixture applies migrations (db.init()) with foreign_keys=ON.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import bus, db


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
