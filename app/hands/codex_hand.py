"""Codex CLI hand: `codex exec` with workspace-write sandbox.

Short prompts go as the final positional argument; long prompts are written to
a dotfile in the workspace and replaced by a one-line indirection (argv has OS
length limits and codex has no stdin prompt mode).
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from ..config import Settings
from . import INLINE_PROMPT_MAX, PROMPT_FILE, finalize_cli_result, snapshot_workspace
from .base import Hand, HandResult, OnChunk, resolve_cli_path, run_subprocess

log = logging.getLogger("institute.hands.codex")


class CodexHand(Hand):
    name = "codex"
    hand_type = "cli"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return self.settings.enable_codex and resolve_cli_path("codex") is not None

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
            resolve_cli_path("codex") or "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox", "workspace-write",
        ]
        effective_model = model or self.settings.codex_model
        if effective_model:
            cmd += ["-m", effective_model]

        workspace.mkdir(parents=True, exist_ok=True)
        prompt_path: Path | None = None
        if len(prompt) < INLINE_PROMPT_MAX:
            cmd.append(prompt)
        else:
            prompt_path = workspace / PROMPT_FILE
            prompt_path.write_text(prompt, encoding="utf-8")
            cmd.append(f"Read and execute the instructions in {PROMPT_FILE}")
            log.debug("codex prompt (%d chars) via %s", len(prompt), PROMPT_FILE)

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
