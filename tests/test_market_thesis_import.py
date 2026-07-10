"""market_thesis_import provenance schema (card M1-001): batches, idempotency, items.

Schema-level tests only — the importer itself is a later card (M1-003). Values
mirror the documented bundle contract (roadmap/07-market-thesis-data-kickoff.md)
without reading market-thesis-data/ (intentionally untracked input).
"""
from __future__ import annotations

import sqlite3

import pytest

from app import bus, db

MANIFEST = {
    "schema": "researchos.market_thesis_export.manifest.v1",
    "generated_at": "2026-07-01T01:45:20.376Z",
    "source_schema": "vibe.ai_institute.public_research_network.v1",
    "source_generated_at": "2026-06-30T13:20:12.832Z",
    "source_first_date": "2026-04-23",
    "source_last_date": "2026-06-30",
    "thesis_count": 74,
    "lane_count": 55,
    "stock_count": 236,
    "edge_count": 1888,
    "thesis_stock_edge_count": 1020,
}


async def _mk_import(
    iid: str,
    *,
    mode: str = "apply",
    status: str = "running",
    key: str | None = None,
    sha256: str = "deadbeef" * 8,
) -> None:
    await db.execute(
        "INSERT INTO market_thesis_imports (id, schema, generated_at, source_schema, source_generated_at, "
        "source_first_date, source_last_date, thesis_count, lane_count, stock_count, edge_count, "
        "thesis_stock_edge_count, bundle_sha256, idempotency_key, mode, status, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            iid, MANIFEST["schema"], MANIFEST["generated_at"], MANIFEST["source_schema"],
            MANIFEST["source_generated_at"], MANIFEST["source_first_date"], MANIFEST["source_last_date"],
            MANIFEST["thesis_count"], MANIFEST["lane_count"], MANIFEST["stock_count"],
            MANIFEST["edge_count"], MANIFEST["thesis_stock_edge_count"],
            sha256, key, mode, status, bus.now_iso(),
        ),
    )


async def _mk_item(
    item_id: str,
    import_id: str,
    item_type: str,
    external_id: str,
    *,
    local_id: str | None = None,
    status: str = "inserted",
    message: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO market_thesis_import_items (id, import_id, item_type, external_id, local_id, status, "
        "message, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (item_id, import_id, item_type, external_id, local_id, status, message, bus.now_iso()),
    )


# ---- batches -----------------------------------------------------------------

async def test_batch_row_holds_manifest_counts():
    await _mk_import("imp-1", key="v1:2026-07-01:deadbeef")
    row = await db.query_one("SELECT * FROM market_thesis_imports WHERE id=?", ("imp-1",))
    assert row["schema"] == MANIFEST["schema"]
    assert (row["thesis_count"], row["lane_count"], row["stock_count"]) == (74, 55, 236)
    assert (row["edge_count"], row["thesis_stock_edge_count"]) == (1888, 1020)
    assert row["source_first_date"] == "2026-04-23"
    assert row["finished_at"] is None


async def test_idempotency_key_unique_for_completed_and_null_repeats():
    await _mk_import("imp-1", key="v1:2026-07-01:deadbeef", status="completed")
    with pytest.raises(sqlite3.IntegrityError):
        # same bundle re-applied after a COMPLETED run is blocked
        await _mk_import("imp-2", key="v1:2026-07-01:deadbeef", status="completed")

    # dry-runs leave the key NULL and may repeat freely
    await _mk_import("dry-1", mode="dry_run", key=None)
    await _mk_import("dry-2", mode="dry_run", key=None)
    rows = await db.query("SELECT id FROM market_thesis_imports WHERE mode='dry_run'")
    assert len(rows) == 2


async def test_failed_apply_does_not_block_retry():
    """Idempotency is a partial unique index over status='completed' only — a
    failed apply must never brick a re-run of the same bundle."""
    await _mk_import("imp-1", key="v1:2026-07-01:deadbeef", status="failed")

    # retry with the SAME key: insert succeeds, and the completion claim lands
    await _mk_import("imp-2", key="v1:2026-07-01:deadbeef", status="running")
    claimed = await db.execute(
        "UPDATE market_thesis_imports SET status='completed', finished_at=? WHERE id=? AND status='running'",
        (bus.now_iso(), "imp-2"),
    )
    assert claimed == 1

    # once the retry completes, the key IS occupied: a further apply conflicts
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_import("imp-3", key="v1:2026-07-01:deadbeef", status="completed")


async def test_mode_and_status_checks_enforced():
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_import("imp-bad", mode="preview")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_import("imp-bad", status="pending")


async def test_completion_is_a_conditional_claim():
    await _mk_import("imp-1", status="running")
    now = bus.now_iso()
    claimed = await db.execute(
        "UPDATE market_thesis_imports SET status='completed', finished_at=? WHERE id=? AND status='running'",
        (now, "imp-1"),
    )
    assert claimed == 1
    again = await db.execute(
        "UPDATE market_thesis_imports SET status='completed', finished_at=? WHERE id=? AND status='running'",
        (now, "imp-1"),
    )
    assert again == 0  # a second claimer (restart recovery) must see 0 rows


# ---- items -------------------------------------------------------------------

async def test_item_rows_unique_per_batch_and_counted():
    await _mk_import("imp-1")
    await _mk_item("it-1", "imp-1", "lane", "ai", local_id="ai")
    await _mk_item("it-2", "imp-1", "thesis", "thesis-05c3f6f33c", local_id="thesis-05c3f6f33c")
    await _mk_item("it-3", "imp-1", "stock", "NVDA", local_id="NVDA.US", status="updated")
    await _mk_item("it-4", "imp-1", "edge", "edge-19a5fd38e40f", status="failed",
                   message="edge references unknown ticker")  # failed items carry no local_id

    # replaying the same bundle record into the same batch is a conflict
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-5", "imp-1", "lane", "ai", local_id="ai")

    # the same external id in a NEW batch is fine (re-import creates a new batch)
    await _mk_import("imp-2")
    await _mk_item("it-6", "imp-2", "lane", "ai", local_id="ai", status="skipped")

    counts = {
        r["status"]: r["n"]
        for r in await db.query(
            "SELECT status, COUNT(*) AS n FROM market_thesis_import_items WHERE import_id=? GROUP BY status",
            ("imp-1",),
        )
    }
    assert counts == {"inserted": 2, "updated": 1, "failed": 1}


async def test_item_type_and_status_checks_and_fk():
    await _mk_import("imp-1")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-bad", "imp-1", "security", "NVDA")  # not lane|thesis|stock|edge
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-bad", "imp-1", "stock", "NVDA", status="imported")
    with pytest.raises(sqlite3.IntegrityError):
        await _mk_item("it-orphan", "no-such-import", "stock", "NVDA")


async def test_items_cascade_with_batch():
    await _mk_import("imp-1")
    await _mk_item("it-1", "imp-1", "thesis", "thesis-029ce03da1", local_id="thesis-029ce03da1")
    await db.execute("DELETE FROM market_thesis_imports WHERE id=?", ("imp-1",))
    assert await db.query("SELECT id FROM market_thesis_import_items WHERE import_id=?", ("imp-1",)) == []
