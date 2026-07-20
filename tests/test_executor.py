"""Executor core: submit, artifacts, cancel, orphan recovery, fallback chain."""
from __future__ import annotations

import json

from app import bus, db
from app.hands import registry as registry_mod
from app.hands.base import Hand, HandResult, RateLimitInfo
from app.hands.registry import get_registry
from app.router import executor


async def test_submit_echo_completes(tmp_path):
    task = await executor.submit("echo", "hello institute", source="test", workspace=tmp_path)
    assert task.status == "completed"
    assert task.exit_code == 0
    assert task.hand == "echo"
    assert task.requested_hand == "echo"
    assert task.output.startswith("[echo] ")
    assert "hello institute" in task.output

    events = await bus.replay(0, types=["task."])
    types = [e.type for e in events if e.ref_id == task.id]
    assert types == ["task.queued", "task.running", "task.completed"]


async def test_write_file_artifacts_land_in_workspace(tmp_path):
    prompt = "please write the report\nWRITE_FILE: notes/report.md\nrest of prompt"
    task = await executor.submit("echo", prompt, source="test", workspace=tmp_path)
    assert task.status == "completed"

    target = tmp_path / "notes" / "report.md"
    assert target.is_file()
    assert "WRITE_FILE: notes/report.md" in target.read_text(encoding="utf-8")
    assert task.artifacts == ["notes/report.md"]


async def test_cancel_queued_task(tmp_path):
    task_id = "queuedcancel1"
    await executor._create_row(
        task_id=task_id, hand="echo", prompt="never runs", source="test", model=None,
        session_id=None, parent_run_id=None, workspace=tmp_path, timeout_s=60,
    )
    assert await executor.cancel(task_id) is True
    task = await executor.get_task(task_id)
    assert task.status == "cancelled"
    # cancelling an already-terminal task is a no-op
    assert await executor.cancel(task_id) is False


async def test_recover_orphans_marks_running_row_failed():
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("orphan0000001", "echo", "interrupted by restart", "running", "test", bus.now_iso()),
    )
    n = await executor.recover_orphans()
    assert n == 1
    task = await executor.get_task("orphan0000001")
    assert task.status == "failed"
    assert task.error == "orphaned by restart"


class AlwaysRateLimitedHand(Hand):
    name = "flaky"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        return HandResult(
            output="429 too many requests", exit_code=1,
            rate_limit=RateLimitInfo(reason="rate_limit", retry_after_s=120, raw="429 too many requests"),
        )


async def test_fallback_to_echo_and_cooldown(tmp_path, monkeypatch):
    registry = get_registry()
    registry.register(AlwaysRateLimitedHand())
    monkeypatch.setitem(registry_mod.DEFAULT_FALLBACK_CHAINS, "flaky", ["echo"])

    task = await executor.submit("flaky", "fall back please", source="test", workspace=tmp_path)
    assert task.status == "completed"
    assert task.requested_hand == "flaky"
    assert task.hand == "echo"
    assert "flaky" in (task.tried or [])
    assert "[echo]" in task.output

    # the rate-limited hand went on cooldown
    assert registry.cooling_until("flaky") is not None
    assert not registry.is_available("flaky")
    snap = {s["name"]: s for s in registry.status_snapshot()}
    assert snap["flaky"]["cooldown_reason"] == "rate_limit"

    registry.clear_cooldown("flaky")
    assert registry.cooling_until("flaky") is None
    assert registry.is_available("flaky")


# ---- M8-003: execution policy + retry lineage persist on the row -------------

async def test_submit_persists_fallback_chain_and_lineage_root(tmp_path):
    """The exact chain a caller confined resolution to lands on the row (the
    retry endpoint replays it later), as does the retry lineage root."""
    task = await executor.submit(
        "echo", "persist my policy", source="test", workspace=tmp_path,
        fallback_chain=["echo"], lineage_root="roottask00001",
    )
    assert task.status == "completed"
    row = await db.query_one(
        "SELECT fallback_chain, lineage_root FROM tasks WHERE id = ?", (task.id,)
    )
    assert json.loads(row["fallback_chain"]) == ["echo"]
    assert row["lineage_root"] == "roottask00001"
    # the Task dataclass surfaces both
    assert task.fallback_chain == ["echo"]
    assert task.lineage_root == "roottask00001"


async def test_submit_without_chain_persists_null(tmp_path):
    """No explicit chain -> NULL (registry-default fallback), NOT '[]': the
    retry endpoint distinguishes 'no confinement' from an explicit chain."""
    task = await executor.submit("echo", "default policy", source="test", workspace=tmp_path)
    row = await db.query_one(
        "SELECT fallback_chain, lineage_root FROM tasks WHERE id = ?", (task.id,)
    )
    assert row["fallback_chain"] is None
    assert row["lineage_root"] is None
    assert task.fallback_chain is None
    assert task.lineage_root is None


async def test_spawn_persists_fallback_chain_and_lineage_root(tmp_path):
    """spawn() (the retry endpoint's path) persists the same policy fields;
    a tuple chain (settings.research_hand_names is a tuple) stores as JSON."""
    task_id = await executor.spawn(
        "echo", "spawned with policy", source="test", workspace=tmp_path,
        fallback_chain=("echo",), lineage_root="rootspawn0001",
    )
    atask = executor._running.get(task_id)
    if atask is not None:
        await atask
    row = await db.query_one(
        "SELECT status, fallback_chain, lineage_root FROM tasks WHERE id = ?", (task_id,)
    )
    assert row["status"] == "completed"
    assert json.loads(row["fallback_chain"]) == ["echo"]
    assert row["lineage_root"] == "rootspawn0001"
