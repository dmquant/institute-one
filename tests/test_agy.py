"""agy hand: availability gating, conversation-id parse, artifact capture."""
from __future__ import annotations

import time
from pathlib import Path

from app.config import get_settings
from app.hands.agy_hand import AgyHand, agy_data_root, capture_artifacts, parse_conversation_id
from app.hands.rate_limit import detect_rate_limit
from app.hands.registry import DEFAULT_FALLBACK_CHAINS


def test_available_requires_flag_and_binary(monkeypatch):
    hand = AgyHand(get_settings())
    monkeypatch.setattr(get_settings(), "enable_agy", False)
    assert hand.available() is False
    monkeypatch.setattr(get_settings(), "enable_agy", True)
    monkeypatch.setattr("app.hands.agy_hand.resolve_cli_path", lambda name: None)
    assert hand.available() is False


def test_conversation_id_parse(tmp_path: Path):
    log = tmp_path / "agy.log"
    log.write_text(
        "server.go:747] Created conversation 0a1b2c3d-1111-2222-3333-444455556666\nmore",
        encoding="utf-8",
    )
    assert parse_conversation_id(log) == "0a1b2c3d-1111-2222-3333-444455556666"
    log.write_text("no id here", encoding="utf-8")
    assert parse_conversation_id(log) is None
    assert parse_conversation_id(tmp_path / "missing.log") is None


def test_capture_artifacts_brain_and_scratch(tmp_path: Path, monkeypatch):
    data_root = tmp_path / "agy-data"
    monkeypatch.setenv("AGY_DATA_ROOT", str(data_root))
    assert agy_data_root() == data_root

    conv = "0a1b2c3d-1111-2222-3333-444455556666"
    brain = data_root / "brain" / conv
    brain.mkdir(parents=True)
    (brain / "walkthrough.md").write_text("发现与验证摘要", encoding="utf-8")
    (brain / "task.md").write_text("任务", encoding="utf-8")

    scratch = data_root / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "sub" / "report.md").write_text("scratch 成果", encoding="utf-8")
    (scratch / "old.md").write_text("旧文件", encoding="utf-8")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "sub").mkdir()
    # a pre-existing workspace copy must NOT be clobbered by the scratch mirror
    (workspace / "sub" / "report.md").write_text("authoritative", encoding="utf-8")

    cutoff = time.time() + 5
    import os
    os.utime(scratch / "old.md", (cutoff - 100, cutoff - 100))      # stale: skipped
    os.utime(scratch / "sub" / "report.md", (cutoff + 10, cutoff + 10))  # fresh

    captured, walkthrough = capture_artifacts(workspace, conv, started_at=cutoff)

    assert walkthrough == "发现与验证摘要"
    assert "agy_artifacts/walkthrough.md" in captured
    assert "agy_artifacts/task.md" in captured
    assert (workspace / "agy_artifacts" / "task.md").read_text(encoding="utf-8") == "任务"
    # skip-if-exists: the workspace copy stayed authoritative
    assert (workspace / "sub" / "report.md").read_text(encoding="utf-8") == "authoritative"
    assert "sub/report.md" not in captured
    assert not (workspace / "old.md").exists()


def test_agy_rate_limit_uses_gemini_signatures():
    info = detect_rate_limit("agy", "Error: RESOURCE_EXHAUSTED — quota exceeded")
    assert info is not None
    assert detect_rate_limit("agy", "perfectly normal output") is None


def test_agy_in_fallback_chains():
    assert DEFAULT_FALLBACK_CHAINS["agy"][0] == "gemini"
    assert DEFAULT_FALLBACK_CHAINS["gemini"][0] == "agy"
