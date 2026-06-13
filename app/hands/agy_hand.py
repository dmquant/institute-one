"""Antigravity (agy) CLI hand — Google's Gemini agent, successor to gemini-cli.

Ported from agent-route-node's battle-tested agy_hand. The quirks that matter
(learned the hard way there — keep them):

- agy does NOT tolerate concurrent invocations: instances share one
  ``~/.gemini/antigravity-cli`` data dir, a language-server port, and the OS
  keyring. Two at once → one stalls with an empty log and/or abandons
  ``--add-dir``. All runs serialize on a module lock (belt) in addition to the
  executor's per-hand mutex (suspenders) — the lock also scopes the scratch
  artifact scan to exactly one run.
- ``--print <prompt>`` is value-taking and must be the LAST flag; everything
    else goes before it or agy swallows a flag as the prompt.
- Print mode ignores cwd for file ops: pass ``--add-dir <workspace>``.
- stdin must be /dev/null or agy's startup read blocks forever.
- Per-call ``--model`` is supported in current agy releases; it must be placed
  before ``--print``.
- Agentic runs write artifacts OUTSIDE the workspace:
  brain/<conversation-id>/*.md (conversation id parsed from --log-file) is
  copied into ``workspace/agy_artifacts/`` with the walkthrough inlined into
  the result text, and run-fresh files in scratch/ are mirrored to the
  workspace ROOT (skip-if-exists; agy sometimes ignores --add-dir under load
  and drops the real deliverable there).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from ..config import Settings
from . import INLINE_PROMPT_MAX, PROMPT_FILE, finalize_cli_result, snapshot_workspace
from .base import Hand, HandResult, OnChunk, RateLimitInfo, resolve_cli_path, run_subprocess

log = logging.getLogger("institute.hands.agy")

_AGY_LOCK = asyncio.Lock()

_CONV_ID_RE = re.compile(r"Created conversation ([0-9a-fA-F-]{36})")


def agy_data_root() -> Path:
    """Antigravity CLI data dir — holds scratch/ and brain/<conv-id>/."""
    return Path(os.environ.get("AGY_DATA_ROOT", "~/.gemini/antigravity-cli")).expanduser()


def parse_conversation_id(log_path: Path) -> str | None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _CONV_ID_RE.search(text)
    return m.group(1) if m else None


def capture_artifacts(workspace: Path, conv_id: str | None, started_at: float) -> tuple[list[str], str | None]:
    """Pull agy's out-of-workspace outputs into the workspace.

    Returns (captured_relpaths, walkthrough_text). Must run while the agy lock
    is still held so the scratch mtime scan sees only this run's files.
    """
    captured: list[str] = []
    walkthrough: str | None = None
    root = agy_data_root()

    # 1) brain artifacts (task.md, implementation_plan.md, walkthrough.md, …)
    if conv_id:
        brain = root / "brain" / conv_id
        if brain.is_dir():
            dest = workspace / "agy_artifacts"
            for src in sorted(brain.glob("*.md")):
                try:
                    content = src.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if src.name == "walkthrough.md":
                    walkthrough = content
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / src.name
                # skip-if-unchanged keeps mtimes stable for downstream sync/archive
                try:
                    if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != content:
                        target.write_text(content, encoding="utf-8")
                    captured.append(f"agy_artifacts/{src.name}")
                except OSError:
                    pass

    # 2) scratch safety net — mirror run-fresh files to the workspace ROOT
    #    (NOT a subdir: downstream readers expect deliverables at their
    #    declared paths; skip-if-exists keeps the --add-dir copy authoritative).
    scratch = root / "scratch"
    if scratch.is_dir():
        for dirpath, dirnames, filenames in os.walk(scratch):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                src = Path(dirpath) / fn
                try:
                    if src.stat().st_mtime < started_at:
                        continue
                except OSError:
                    continue
                rel = src.relative_to(scratch)
                dst = workspace / rel
                if dst.exists():
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    captured.append(str(rel))
                except OSError:
                    pass

    return captured, walkthrough


class AgyHand(Hand):
    name = "agy"
    hand_type = "cli"
    default_model_attr: str | None = None

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return self.settings.enable_agy and resolve_cli_path("agy") is not None

    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        workspace.mkdir(parents=True, exist_ok=True)
        await self._ensure_git(workspace)
        before = snapshot_workspace(workspace)

        # long prompts go through the workspace file (argv stays short)
        prompt_file: Path | None = None
        if len(prompt) >= INLINE_PROMPT_MAX:
            prompt_file = workspace / PROMPT_FILE
            prompt_file.write_text(prompt, encoding="utf-8")
            prompt_arg = f"Read and execute the instructions in {PROMPT_FILE}"
        else:
            prompt_arg = prompt

        log_fd, log_path_s = tempfile.mkstemp(prefix="agy-", suffix=".log")
        os.close(log_fd)
        log_path = Path(log_path_s)

        cmd = [
            resolve_cli_path("agy") or "agy",
            "--add-dir", str(workspace),
            "--print-timeout", f"{timeout_s}s",
            "--log-file", str(log_path),
        ]
        effective_model = model
        if effective_model is None and self.default_model_attr:
            effective_model = getattr(self.settings, self.default_model_attr)
        if effective_model:
            cmd += ["--model", effective_model]
        cmd += ["--print", prompt_arg]  # MUST stay last (value-taking flag)

        if on_chunk and _AGY_LOCK.locked():
            on_chunk({"type": "status", "text": "agy busy — queued (agy runs serially)"})

        async with _AGY_LOCK:
            started_at = time.time()
            try:
                stdout, stderr, code = await run_subprocess(
                    cmd, cwd=workspace, timeout_s=timeout_s, on_chunk=on_chunk,
                )
            finally:
                # capture BEFORE releasing the lock (scratch scan must not race
                # the next serialized run) and BEFORE deleting the log (conv id)
                conv_id = parse_conversation_id(log_path)
                try:
                    captured, walkthrough = capture_artifacts(workspace, conv_id, started_at)
                except Exception:  # noqa: BLE001 - capture is best-effort
                    log.exception("agy artifact capture failed")
                    captured, walkthrough = [], None
                log_path.unlink(missing_ok=True)
                if prompt_file is not None:
                    prompt_file.unlink(missing_ok=True)

        result = finalize_cli_result(self.name, stdout, stderr, code, workspace, before)
        if walkthrough and walkthrough.strip() and result.rate_limit is None:
            result.output = (
                f"{result.output}\n\n---\n\n## Agy Walkthrough\n\n{walkthrough.strip()}"
            ).strip()
        for rel in captured:
            if rel not in result.artifacts:
                result.artifacts.append(rel)
        if code == 0 and not result.output.strip() and not result.artifacts:
            raw = "agy returned exit code 0 with no stdout, stderr, or artifacts"
            return HandResult(
                output=raw,
                exit_code=1,
                rate_limit=RateLimitInfo("quota_exhausted", raw=raw),
                artifacts=[],
            )
        return result

    async def _ensure_git(self, workspace: Path) -> None:
        """agy behaves better in a git workspace; init quietly if missing."""
        if (workspace / ".git").exists():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "init", "-q", cwd=str(workspace),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:  # noqa: BLE001 - best effort
            pass


class AgyOpusHand(AgyHand):
    """Antigravity CLI pinned to Claude Opus for the main hand pool."""

    name = "agy-opus"
    default_model_attr = "agy_opus_model"
