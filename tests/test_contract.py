"""/api/contract + /api/artifacts (agent C6's partition, ROADMAP Phase 8).

The router is mounted in app/main.py with the other API routers; tests still
build a bare FastAPI app around just this router (the test_digests idiom) so
they run without the full app lifespan. DB + tmp vault come from the autouse
``app_runtime`` fixture.

Status enums: the contract imports the canonical constants from the owning
state-machine modules (REVIEW-C6 M4). To catch code and contract drifting
TOGETHER, tests cross-check the API response against the CHECK constraints
parsed straight out of migrations/0001_init.sql text — an independent source,
not the module under test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.api.contract import NOTE_CAP_BYTES
from app.config import get_settings
from app.institute import research, whiteboard, workflows
from app.router import executor

REPO = Path(__file__).resolve().parent.parent


def _make_app() -> FastAPI:
    from app.api import contract as api_contract

    app = FastAPI()
    app.include_router(api_contract.router)
    return app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test")


# ---- /api/contract -------------------------------------------------------------

def _enum_from_0001(table: str) -> list[str]:
    """Independently parse a table's status CHECK enum out of the 0001 SQL
    text — deliberately NOT the module under test, so a constant drifting
    away from the schema fails here even if code and contract drift together."""
    sql = (REPO / "migrations" / "0001_init.sql").read_text(encoding="utf-8")
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\b.*?status\s+TEXT\s+NOT\s+NULL\s+"
        rf"CHECK \(status IN \(([^)]*)\)\)",
        sql, re.DOTALL,
    )
    assert m, f"no status CHECK found for {table} in 0001_init.sql"
    return sorted(v.strip().strip("'") for v in m.group(1).split(","))


async def test_contract_enums_come_from_code_constants():
    async with _client() as client:
        body = (await client.get("/api/contract")).json()

    assert body["version"] == 1
    assert body["status_source"] == "code_constants"
    # the live DB carries the same CHECK constraints -> every table verifies
    assert set(body["schema_cross_check"].values()) == {"ok"}

    # response equals the importable state-machine constants...
    assert body["statuses"]["tasks"] == sorted(set(executor.ACTIVE) | executor.TERMINAL)
    assert body["statuses"]["workflow_runs"] == sorted(workflows.RUN_STATUSES)
    assert body["statuses"]["research_queue"] == sorted(research.QUEUE_STATUSES)
    assert body["statuses"]["whiteboard_boards"] == sorted(whiteboard.BOARD_STATUSES)
    assert body["terminal_task_statuses"] == sorted(executor.TERMINAL)

    # ...and the constants match the 0001 schema text, independently parsed
    for table in ("tasks", "workflow_runs", "research_queue", "whiteboard_boards"):
        assert body["statuses"][table] == _enum_from_0001(table), table


async def test_contract_caps_and_ref_grammar():
    async with _client() as client:
        body = (await client.get("/api/contract")).json()

    caps = body["caps"]
    settings = get_settings()
    assert caps["output_cap_bytes"] == settings.output_cap_bytes
    assert caps["note_content_cap_bytes"] == NOTE_CAP_BYTES
    assert caps["default_timeout_s"] == settings.default_timeout_s
    assert caps["max_concurrent"] == settings.max_concurrent
    assert caps["output_truncation_marker"] == executor.TRUNCATION_MARKER

    refs = body["refs"]
    assert refs["endpoint"] == "/api/artifacts?ref="
    assert set(refs["kinds"]) == {"task", "note", "fact_card"}
    for kind in ("task:", "note:", "fact_card:"):
        assert kind in refs["grammar"]


# ---- /api/artifacts: task refs ---------------------------------------------------

async def _seed_task(task_id: str = "abc123def456") -> None:
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, hand, model, prompt, status, source, "
        " exit_code, output, artifacts, tried, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, "echo", "echo", None, "hello", "completed", "api",
         0, "[echo] hello", json.dumps(["report.md"]), json.dumps(["echo"]), bus.now_iso()),
    )


async def test_artifact_task_roundtrip():
    await _seed_task()
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "task:abc123def456"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "task" and body["ref"] == "task:abc123def456"
    task = body["task"]
    assert task["id"] == "abc123def456"
    assert task["status"] == "completed"
    assert task["artifacts"] == ["report.md"]  # JSON columns decoded
    assert task["tried"] == ["echo"]


async def test_artifact_task_unknown_404():
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "task:nope"})
    assert resp.status_code == 404


# ---- /api/artifacts: note refs -----------------------------------------------------

async def test_artifact_note_content_and_ledger():
    settings = get_settings()
    root = settings.vault_dir.expanduser()
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "brief.md").write_text("# 晨会\n内容", encoding="utf-8")
    await db.execute(
        "INSERT INTO vault_index (path, artifact_kind, artifact_id, sha256, state, written_at) "
        "VALUES (?,?,?,?,?,?)",
        ("reports/brief.md", "briefing", "run1", "deadbeef", "clean", bus.now_iso()),
    )
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "note:reports/brief.md"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "note" and body["path"] == "reports/brief.md"
    assert body["content"] == "# 晨会\n内容"
    assert body["truncated"] is False
    assert body["size_bytes"] == len("# 晨会\n内容".encode("utf-8"))
    assert body["ledger"]["artifact_kind"] == "briefing"


async def test_artifact_note_truncates_at_8kb():
    settings = get_settings()
    root = settings.vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    big = "研究" * 6000  # 36KB utf-8, multi-byte to exercise the boundary cut
    (root / "big.md").write_text(big, encoding="utf-8")
    async with _client() as client:
        body = (await client.get("/api/artifacts", params={"ref": "note:big.md"})).json()
    assert body["truncated"] is True
    assert body["size_bytes"] == len(big.encode("utf-8"))
    assert len(body["content"].encode("utf-8")) <= NOTE_CAP_BYTES
    assert body["content"]  # ignore-errors decode kept the head, not nothing
    assert body["ledger"] is None  # never written by the writer


async def test_artifact_note_rejects_escape_and_missing():
    async with _client() as client:
        assert (await client.get("/api/artifacts", params={"ref": "note:../secrets.md"})).status_code == 400
        assert (await client.get("/api/artifacts", params={"ref": "note:/etc/passwd"})).status_code == 400
        assert (await client.get("/api/artifacts", params={"ref": "note:missing.md"})).status_code == 404


# ---- note refs: symlink escapes (REVIEW-C6 H2) ---------------------------------

async def test_artifact_note_file_symlink_escape_403(tmp_path):
    """A symlink INSIDE the vault pointing at a file OUTSIDE it passed the
    lexical checks and read out of jail before the realpath re-check."""
    root = get_settings().vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.md"
    secret.write_text("outside the vault", encoding="utf-8")
    (root / "leak.md").symlink_to(secret)
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "note:leak.md"})
    assert resp.status_code == 403
    assert "escapes the vault root" in resp.json()["detail"]


async def test_artifact_note_directory_symlink_escape_403(tmp_path):
    """Same escape via a symlinked intermediate directory."""
    root = get_settings().vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "doc.md").write_text("outside via dir", encoding="utf-8")
    (root / "sub").symlink_to(outside, target_is_directory=True)
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "note:sub/doc.md"})
    assert resp.status_code == 403


async def test_artifact_note_internal_symlink_still_served():
    """Symlinks whose real target stays under the vault root remain legal."""
    root = get_settings().vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    (root / "real.md").write_text("in-vault target", encoding="utf-8")
    (root / "alias.md").symlink_to(root / "real.md")
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "note:alias.md"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "in-vault target"


async def test_artifact_note_dangling_symlink_404():
    root = get_settings().vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    (root / "dangling.md").symlink_to(root / "never-existed.md")
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "note:dangling.md"})
    assert resp.status_code == 404


async def test_artifact_note_unconfigured_vault_400(monkeypatch):
    monkeypatch.setattr(get_settings(), "vault_dir", None)
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "note:x.md"})
    assert resp.status_code == 400
    assert "vault_dir" in resp.json()["detail"]


# ---- /api/artifacts: fact_card refs ------------------------------------------------
# The Phase 3 migration (0015_fact_check.sql, C1's partition) has landed on this
# checkout, so the table exists after a full migrate. Pin BOTH states explicitly
# (the test_digests analyst_memory idiom): a deployment running an older
# checkout must get 501, a current one must get row JSON.

async def _drop_fact_tables() -> None:
    # referencing tables first (ON DELETE CASCADE FKs point at fact_cards)
    for table in ("fact_claim_vectors", "verified_facts", "fact_cards"):
        await db.execute(f"DROP TABLE IF EXISTS {table}")


async def test_artifact_fact_card_501_when_table_absent():
    await _drop_fact_tables()
    async with _client() as client:
        resp = await client.get("/api/artifacts", params={"ref": "fact_card:42"})
    assert resp.status_code == 501
    assert "Phase 3" in resp.json()["detail"]


async def test_artifact_fact_card_serves_rows_from_real_schema():
    # real 0015 schema: NOT NULL source_kind/source_ref/claim/category/created_at
    await db.execute(
        "INSERT INTO fact_cards (id, source_kind, source_ref, analyst_id, claim, category, created_at) "
        "VALUES ('fc1', 'research_report', 'rq1', 'macro-analyst', '营收同比 +30%', 'financial', ?)",
        (bus.now_iso(),),
    )
    async with _client() as client:
        ok = await client.get("/api/artifacts", params={"ref": "fact_card:fc1"})
        missing = await client.get("/api/artifacts", params={"ref": "fact_card:nope"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["kind"] == "fact_card"
    assert body["fact_card"]["claim"] == "营收同比 +30%"
    assert body["fact_card"]["status"] == "pending"  # 0015 default
    assert missing.status_code == 404


# ---- /api/artifacts: ref grammar edges ----------------------------------------------

async def test_artifact_unknown_and_malformed_refs_400():
    async with _client() as client:
        assert (await client.get("/api/artifacts", params={"ref": "bogus:1"})).status_code == 400
        assert (await client.get("/api/artifacts", params={"ref": "no-colon"})).status_code == 400
        assert (await client.get("/api/artifacts", params={"ref": "task:"})).status_code == 400
        assert (await client.get("/api/artifacts", params={"ref": ""})).status_code == 400
        # missing param entirely -> FastAPI validation
        assert (await client.get("/api/artifacts")).status_code == 422
