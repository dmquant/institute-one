"""Hand construction plus the helpers shared by every CLI hand.

The helpers live here (defined before any hand module is imported, and hand
modules are imported lazily inside ``build_hands``) so the per-CLI modules can
``from . import snapshot_workspace, finalize_cli_result`` without cycles.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import Settings
from .base import EchoHand, Hand, HandResult
from .rate_limit import detect_rate_limit

log = logging.getLogger("institute.hands")

# Long prompts are written here instead of being passed as argv (dotfile, so it
# is excluded from artifact scans automatically).
PROMPT_FILE = ".institute_prompt.md"
INLINE_PROMPT_MAX = 6000
ARTIFACT_SCAN_CAP = 200


def snapshot_workspace(workspace: Path) -> set[str]:
    """Workspace-relative paths of regular files, dotfiles excluded, capped."""
    files: set[str] = set()
    if not workspace.is_dir():
        return files
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.startswith("."):
                continue
            files.add(os.path.relpath(os.path.join(root, name), workspace))
            if len(files) >= ARTIFACT_SCAN_CAP:
                return files
    return files


def new_artifacts(workspace: Path, before: set[str]) -> list[str]:
    return sorted(snapshot_workspace(workspace) - before)


def finalize_cli_result(
    hand_name: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    workspace: Path,
    before: set[str],
) -> HandResult:
    """Shared CLI epilogue: rate-limit scan over stdout+stderr, artifact diff."""
    combined = stdout + (("\n" + stderr) if stderr.strip() else "")
    artifacts = new_artifacts(workspace, before)
    info = detect_rate_limit(hand_name, combined)
    if info is not None:
        return HandResult(output=combined, exit_code=exit_code or 1, rate_limit=info, artifacts=artifacts)
    # Clean runs return stdout only (stderr is progress noise); failures keep both.
    output = stdout if exit_code == 0 and stdout.strip() else combined
    return HandResult(output=output, exit_code=exit_code, artifacts=artifacts)


def build_hands(settings: Settings) -> list[Hand]:
    """Construct every hand. Availability filtering is the registry's job."""
    from .api_hands import ClaudeApiHand, GeminiApiHand, OpenAiApiHand
    from .claude_hand import ClaudeHand
    from .codex_hand import CodexHand
    from .gemini_hand import GeminiHand
    from .ollama_hand import OllamaHand
    from .opencode_hand import OpencodeHand

    hands: list[Hand] = [
        ClaudeHand(settings),
        CodexHand(settings),
        GeminiHand(settings),
        OpencodeHand(settings),
        OllamaHand(settings),
        ClaudeApiHand(settings),
        OpenAiApiHand(settings),
        GeminiApiHand(settings),
    ]
    if settings.enable_echo:
        hands.append(EchoHand())
    return hands
