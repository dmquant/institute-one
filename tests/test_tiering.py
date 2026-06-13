"""Task-tier routing keeps cheap work on local hands."""
from __future__ import annotations

import json

from app import bus, db
from app.config import Settings, get_settings
from app.hands.base import Hand, HandResult
from app.hands.registry import get_registry
from app.institute import workflows
from app.router import executor
from app.router.tiering import route_for_tier


class CheapHand(Hand):
    name = "cheap-test"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None):
        return HandResult(output=f"cheap:{model or ''}", exit_code=0)


def test_route_for_tier_uses_cheap_hand_only_for_cheap_work():
    settings = Settings(cheap_hand="ollama", cheap_model="qwen3.6:35b-a3b", default_hand="pool")

    cheap = route_for_tier(settings, "cheap")
    assert cheap.hand == "ollama"
    assert cheap.model == "qwen3.6:35b-a3b"

    frontier = route_for_tier(settings, None)
    assert frontier.hand == "pool"
    assert frontier.model is None

    explicit = route_for_tier(settings, "cheap", fallback_hand="codex", fallback_model="gpt-5.2")
    assert explicit.hand == "codex"
    assert explicit.model == "gpt-5.2"


async def test_workflow_step_tier_cheap_routes_to_cheap_hand(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "cheap_hand", "cheap-test")
    monkeypatch.setattr(settings, "cheap_model", "local-model")
    get_registry().register(CheapHand())

    await db.execute(
        """INSERT INTO workflows (id, name, description, variables, steps, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (
            "cheap-flow",
            "Cheap Flow",
            "",
            "[]",
            json.dumps([
                {
                    "id": "compress",
                    "title": "Compress",
                    "analyst": "chief-strategist",
                    "tier": "cheap",
                    "prompt": "compress this",
                }
            ], ensure_ascii=False),
            bus.now_iso(),
        ),
    )

    run = await workflows.run_workflow_and_wait("cheap-flow", source="test")
    assert run["status"] == "completed"
    assert run["results"][0]["requested_hand"] == "cheap-test"

    tasks = await executor.list_tasks(parent_run_id=run["id"])
    assert len(tasks) == 1
    assert tasks[0]["requested_hand"] == "cheap-test"
    assert tasks[0]["hand"] == "cheap-test"
    assert tasks[0]["model"] == "local-model"
