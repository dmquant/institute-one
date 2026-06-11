"""opencode CLI hand: `opencode run <prompt>` with optional provider/model.

Same long-prompt indirection as codex (argv length limits).
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from ..config import Settings
from . import INLINE_PROMPT_MAX, PROMPT_FILE, finalize_cli_result, snapshot_workspace
from .base import Hand, HandResult, OnChunk, resolve_cli_path, run_subprocess

log = logging.getLogger("institute.hands.opencode")


class OpencodeHand(Hand):
    name = "opencode"
    hand_type = "cli"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return self.settings.enable_opencode and resolve_cli_path("opencode") is not None

    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        cmd = [resolve_cli_path("opencode") or "opencode", "run"]
        effective_model = model or self.settings.opencode_model  # "provider/model"
        if effective_model:
            cmd += ["--model", effective_model]

        workspace.mkdir(parents=True, exist_ok=True)
        prompt_path: Path | None = None
        if len(prompt) < INLINE_PROMPT_MAX:
            cmd.append(prompt)
        else:
            prompt_path = workspace / PROMPT_FILE
            prompt_path.write_text(prompt, encoding="utf-8")
            cmd.append(f"Read and execute the instructions in {PROMPT_FILE}")
            log.debug("opencode prompt (%d chars) via %s", len(prompt), PROMPT_FILE)

        before = snapshot_workspace(workspace)
        try:
            stdout, stderr, code = await run_subprocess(
                cmd, cwd=workspace, timeout_s=timeout_s, on_chunk=on_chunk,
            )
        finally:
            if prompt_path is not None:
                with contextlib.suppress(OSError):
                    prompt_path.unlink()
        return finalize_cli_result(self.name, stdout, stderr, code, workspace, before)
