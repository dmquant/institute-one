"""POST /api/tasks/{id}/retry — requeue failed/orphaned tasks as new rows.

Exercised through the real HTTP route (httpx ASGITransport, no lifespan — the
conftest fixture owns DB/registry setup) so routing, status codes and response
shapes are covered, not just the handler function.

M8-003 additions: retry replays the row's PERSISTED fallback_chain (source
derivation only for NULL-chain legacy rows), lineage_root roots every retry
generation at the original task, and the idempotency window is the 0024
partial unique index — DB-level, so it survives process restarts.
"""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from app import bus, db
from app.config import get_settings
from app.hands.base import EchoHand
from app.main import create_app
from app.router import executor


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _insert_failed(task_id: str, *, source: str = "test", hand: str = "echo",
                         session_id: str | None = None, workspace: str = "",
                         error: str = "orphaned by restart",
                         fallback_chain: str | None = None,
                         lineage_root: str | None = None) -> None:
    await db.execute(
        "INSERT INTO tasks (id, session_id, requested_hand, prompt, status, source, error,"
        "                   workspace_dir, timeout_s, fallback_chain, lineage_root, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, session_id, hand, "retry me please", "failed", source, error,
         workspace, 60, fallback_chain, lineage_root, bus.now_iso()),
    )


async def _insert_live(task_id: str, *, lineage_root: str, status: str = "queued",
                       workspace: str = "") -> None:
    """A live (queued/running) retry row, as another process would leave it."""
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, workspace_dir,"
        "                   timeout_s, lineage_root, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "echo", "retry me please", status, "test", workspace, 60,
         lineage_root, bus.now_iso()),
    )


async def _wait_done(task_id: str) -> None:
    atask = executor._running.get(task_id)
    if atask is not None:
        await asyncio.gather(atask, return_exceptions=True)


async def test_retry_unknown_task_is_404():
    async with _client() as client:
        resp = await client.post("/api/tasks/nope00000000/retry")
    assert resp.status_code == 404


async def test_retry_refuses_non_failed_task(tmp_path):
    task = await executor.submit("echo", "already fine", source="test", workspace=tmp_path)
    assert task.status == "completed"
    async with _client() as client:
        resp = await client.post(f"/api/tasks/{task.id}/retry")
    assert resp.status_code == 409
    assert "completed" in resp.json()["detail"]


async def test_retry_refuses_running_task(tmp_path):
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, workspace_dir,"
        "                   timeout_s, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("stillrunning1", "echo", "in flight", "running", "test", str(tmp_path), 60, bus.now_iso()),
    )
    async with _client() as client:
        resp = await client.post("/api/tasks/stillrunning1/retry")
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"]


async def test_retry_orphaned_task_spawns_new_row(tmp_path):
    await _insert_failed("orphanretry01", session_id="sess-orig", workspace=str(tmp_path))

    async with _client() as client:
        resp = await client.post("/api/tasks/orphanretry01/retry")
    assert resp.status_code == 200
    body = resp.json()
    new_id = body["task_id"]
    assert body["retried_from"] == "orphanretry01"
    assert body["lineage_root"] == "orphanretry01"
    assert new_id != "orphanretry01"

    await _wait_done(new_id)
    new = await executor.get_task(new_id)
    assert new.status == "completed"
    assert new.prompt == "retry me please"
    assert new.requested_hand == "echo"
    assert new.session_id == "sess-orig"
    assert new.workspace_dir == str(tmp_path)
    assert new.lineage_root == "orphanretry01"  # the retry roots at the original

    # the old row is untouched audit history (and carries no lineage_root:
    # it IS the root)
    old = await executor.get_task("orphanretry01")
    assert old.status == "failed"
    assert old.error == "orphaned by restart"
    assert old.lineage_root is None


async def test_retry_null_chain_research_row_falls_back_to_derivation(tmp_path):
    """NULL-chain legacy rows (pre-0024) still get the source derivation:
    CLAUDE.md rule 10 — a research retry must stay inside research_hand_names.

    The stored hand ('codex', disabled in tests) fell out of the configured
    chain (tests pin research hands to 'echo'); without the policy rebuild the
    default registry fallback would wander to claude/gemini — here everything
    but echo is disabled, so the old behaviour would end 'rate_limited' with
    tried=[codex, ...]. The derived policy pins the chain head instead.
    """
    await _insert_failed("researchretry", source="research", hand="codex", workspace=str(tmp_path))

    async with _client() as client:
        resp = await client.post("/api/tasks/researchretry/retry")
    assert resp.status_code == 200
    new_id = resp.json()["task_id"]

    await _wait_done(new_id)
    new = await executor.get_task(new_id)
    assert new.requested_hand == "echo"  # chain head replaces the out-of-chain hand
    assert new.hand == "echo"
    assert new.status == "completed"
    assert new.tried == ["echo"]  # resolution confined to the research chain


# ---- M8-003: the stored chain is the policy -----------------------------------

async def test_retry_replays_stored_chain_not_live_settings(tmp_path, monkeypatch):
    """The persisted chain wins over a source derivation from LIVE settings:
    changing research_hands between the original run and the retry must not
    leak in. Derivation would rebuild ('claude','codex') — both disabled in
    tests, ending rate_limited; the stored ["echo"] chain completes instead.
    """
    await _insert_failed(
        "storedchain01", source="research", hand="echo", workspace=str(tmp_path),
        fallback_chain='["echo"]',
    )
    monkeypatch.setattr(get_settings(), "research_hands", "claude,codex")

    async with _client() as client:
        resp = await client.post("/api/tasks/storedchain01/retry")
    assert resp.status_code == 200
    new_id = resp.json()["task_id"]

    await _wait_done(new_id)
    new = await executor.get_task(new_id)
    assert new.status == "completed"
    assert new.hand == "echo"
    assert new.tried == ["echo"]
    assert new.fallback_chain == ["echo"]  # replayed AND re-persisted on the new row
    assert new.lineage_root == "storedchain01"


async def test_retry_confinement_survives_process_restart(tmp_path, monkeypatch):
    """M8-003 core acceptance: a fresh DB connection (= a new process — no
    in-memory state survives) still replays the persisted chain."""
    await _insert_failed(
        "restartchain1", source="research", hand="echo", workspace=str(tmp_path),
        fallback_chain='["echo"]',
    )
    await db.close()
    await db.init()  # "process restart": only the database carries over
    monkeypatch.setattr(get_settings(), "research_hands", "claude,codex")

    async with _client() as client:
        resp = await client.post("/api/tasks/restartchain1/retry")
    assert resp.status_code == 200
    new_id = resp.json()["task_id"]

    await _wait_done(new_id)
    new = await executor.get_task(new_id)
    assert new.status == "completed"
    assert new.hand == "echo"
    assert new.fallback_chain == ["echo"]
    assert new.lineage_root == "restartchain1"


# ---- M8-003: lineage_root audit chain -----------------------------------------

async def test_retry_lineage_three_generations_share_one_root(tmp_path):
    """A retry of a retry keeps pointing at the ORIGINAL task: the audit
    chain is one lookup (WHERE lineage_root = root), never a pointer walk."""
    await _insert_failed("lineageroot01", workspace=str(tmp_path))

    async with _client() as client:
        resp = await client.post("/api/tasks/lineageroot01/retry")
        assert resp.status_code == 200
        gen2 = resp.json()["task_id"]
        assert resp.json()["lineage_root"] == "lineageroot01"
        await _wait_done(gen2)
        # fail the finished generation so the next retry is allowed
        await db.execute("UPDATE tasks SET status='failed' WHERE id = ?", (gen2,))

        resp = await client.post(f"/api/tasks/{gen2}/retry")
        assert resp.status_code == 200
        gen3 = resp.json()["task_id"]
        assert resp.json()["lineage_root"] == "lineageroot01"  # root, not gen2
        await _wait_done(gen3)
        await db.execute("UPDATE tasks SET status='failed' WHERE id = ?", (gen3,))

        resp = await client.post(f"/api/tasks/{gen3}/retry")
        assert resp.status_code == 200
        gen4 = resp.json()["task_id"]
        assert resp.json()["lineage_root"] == "lineageroot01"
        await _wait_done(gen4)

    rows = await db.query(
        "SELECT id FROM tasks WHERE lineage_root = 'lineageroot01'"
    )
    assert {r["id"] for r in rows} == {gen2, gen3, gen4}


# ---- M8-003: DB-level idempotency window --------------------------------------

async def test_retry_409_while_lineage_has_live_task(tmp_path):
    """While ANY task of the lineage is still queued/running, another retry
    answers 409; a terminal state closes the window and retry works again."""
    await _insert_failed("liveroot00001", workspace=str(tmp_path))
    await _insert_live("liveretry0001", lineage_root="liveroot00001", workspace=str(tmp_path))

    async with _client() as client:
        resp = await client.post("/api/tasks/liveroot00001/retry")
        assert resp.status_code == 409
        assert "liveretry0001" in resp.json()["detail"]

        # the live retry reaches a terminal state -> the window closes
        await db.execute(
            "UPDATE tasks SET status='failed', error='x' WHERE id='liveretry0001'"
        )
        resp = await client.post("/api/tasks/liveroot00001/retry")
        assert resp.status_code == 200
        await _wait_done(resp.json()["task_id"])


async def test_retry_idempotency_is_db_level_across_restart(tmp_path):
    """The idempotency window lives in the DATABASE (0024 partial unique
    index), not process memory: a fresh connection (= another process or a
    post-restart one) still refuses to double-spawn a lineage's retry."""
    await _insert_failed("dbwindowroot1", workspace=str(tmp_path))
    await _insert_live("dbwindowlive1", lineage_root="dbwindowroot1",
                       status="running", workspace=str(tmp_path))

    await db.close()
    await db.init()  # new connection: any in-memory guard would be gone

    async with _client() as client:
        resp = await client.post("/api/tasks/dbwindowroot1/retry")
    assert resp.status_code == 409
    assert "already live" in resp.json()["detail"]


async def test_lineage_unique_index_is_the_arbiter(tmp_path):
    """Bypassing the endpoint's pre-check (the lost-race window), the 0024
    partial unique index itself refuses a second live row per lineage —
    while terminal rows stay out of the window (the index is partial)."""
    await _insert_live("arbiterlive01", lineage_root="arbiterroot01", workspace=str(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        await _insert_live("arbiterlive02", lineage_root="arbiterroot01", workspace=str(tmp_path))

    # terminal generations of the same lineage coexist freely (audit history)
    await _insert_failed("arbiterdead01", lineage_root="arbiterroot01", workspace=str(tmp_path))
    await _insert_failed("arbiterdead02", lineage_root="arbiterroot01", workspace=str(tmp_path))
    # and a DIFFERENT lineage's live retry is unaffected
    await _insert_live("arbiterlive03", lineage_root="otherroot0001", workspace=str(tmp_path))


async def test_retry_same_instant_duplicate_is_409(tmp_path, monkeypatch):
    """Two same-instant retries of one task: exactly one spawns, the loser
    answers 409 — via the pre-check or, losing the race to it, the unique
    index (IntegrityError mapped to 409). The echo hand is gated so the
    winner's task stays live for the whole race."""
    await _insert_failed("racingretry01", workspace=str(tmp_path))

    gate = asyncio.Event()
    orig = EchoHand.execute

    async def gated_execute(self, prompt, workspace, **kwargs):
        await gate.wait()
        return await orig(self, prompt, workspace, **kwargs)

    monkeypatch.setattr(EchoHand, "execute", gated_execute)

    try:
        async with _client() as client:
            r1, r2 = await asyncio.gather(
                client.post("/api/tasks/racingretry01/retry"),
                client.post("/api/tasks/racingretry01/retry"),
            )
        assert sorted([r1.status_code, r2.status_code]) == [200, 409]
        winner = r1 if r1.status_code == 200 else r2
        new_id = winner.json()["task_id"]
    finally:
        gate.set()
    await _wait_done(new_id)
    new = await executor.get_task(new_id)
    assert new.status == "completed"
