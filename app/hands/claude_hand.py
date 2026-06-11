"""Claude Code CLI hand: non-interactive `claude -p`, prompt via stdin."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from . import finalize_cli_result, snapshot_workspace
from .base import Hand, HandResult, OnChunk, resolve_cli_path, run_subprocess

log = logging.getLogger("institute.hands.claude")


class ClaudeHand(Hand):
    name = "claude"
    hand_type = "cli"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return self.settings.enable_claude and resolve_cli_path("claude") is not None

    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        cmd = [
            resolve_cli_path("claude") or "claude",
            "-p",
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        effective_model = model or self.settings.claude_model
        if effective_model:
            cmd += ["--model", effective_model]

        workspace.mkdir(parents=True, exist_ok=True)
        before = snapshot_workspace(workspace)
        log.debug("claude run in %s (model=%s)", workspace, effective_model)
        stdout, stderr, code = await run_subprocess(
            cmd, cwd=workspace, timeout_s=timeout_s, on_chunk=on_chunk, stdin_data=prompt,
        )
        return finalize_cli_result(self.name, stdout, stderr, code, workspace, before)
