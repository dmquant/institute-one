"""Gemini CLI hand: `gemini --yolo`, prompt piped via stdin.

The gemini CLI runs non-interactively when stdin is not a TTY and reads the
piped input as the prompt; we pass no ``-p`` value at all (a bare ``-p`` is
rejected by its arg parser, and stdin is length-safe for big prompts).
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from . import finalize_cli_result, snapshot_workspace
from .base import Hand, HandResult, OnChunk, resolve_cli_path, run_subprocess

log = logging.getLogger("institute.hands.gemini")


class GeminiHand(Hand):
    name = "gemini"
    hand_type = "cli"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return self.settings.enable_gemini and resolve_cli_path("gemini") is not None

    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        cmd = [resolve_cli_path("gemini") or "gemini", "--yolo"]
        effective_model = model or self.settings.gemini_model
        if effective_model:
            cmd += ["--model", effective_model]

        workspace.mkdir(parents=True, exist_ok=True)
        before = snapshot_workspace(workspace)
        log.debug("gemini run in %s (model=%s)", workspace, effective_model)
        stdout, stderr, code = await run_subprocess(
            cmd, cwd=workspace, timeout_s=timeout_s, on_chunk=on_chunk, stdin_data=prompt,
        )
        return finalize_cli_result(self.name, stdout, stderr, code, workspace, before)
