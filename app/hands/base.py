"""The Hand abstraction.

A hand is anything that takes a prompt and returns output: a CLI agent run as a
subprocess inside a workspace directory (claude/codex/agy/opencode), a local
HTTP service (ollama), or a direct provider API (anthropic/openai/google).

Design notes carried over from the previous system:
- ``get_cli_env()`` captures the user's *login shell* environment once, so CLIs
  spawned from a daemon still find their PATH and auth state.
- Rate limits are returned as a structured ``RateLimitInfo`` on the result, not
  as an in-band string protocol.
- Subprocesses run with ``start_new_session=True`` and are killed by process
  group on timeout/cancel so no orphan CLIs linger.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal

log = logging.getLogger("institute.hands")

Chunk = dict  # {"type": "stdout"|"stderr"|"status", "text": str}
OnChunk = Callable[[Chunk], None]


@dataclass
class RateLimitInfo:
    reason: str                     # quota_exhausted | rate_limit | overloaded
    retry_after_s: int | None = None
    raw: str = ""                   # the matched signature line


@dataclass
class HandResult:
    output: str
    exit_code: int
    rate_limit: RateLimitInfo | None = None
    artifacts: list[str] = field(default_factory=list)  # workspace-relative paths created by the hand


class Hand(ABC):
    name: str = "hand"
    hand_type: Literal["cli", "http", "api"] = "cli"

    @abstractmethod
    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        """Run the prompt. Must not raise on hand-level failure — return exit_code != 0.

        May raise asyncio.TimeoutError/CancelledError (the executor handles those).
        """

    def available(self) -> bool:
        """Static availability: binary installed / key configured. Cooldowns are the registry's job."""
        return True

    async def health_check(self) -> bool:
        return self.available()


class EchoHand(Hand):
    """Built-in trivial hand used by tests and smoke checks. Always available.

    Echoes the prompt back; if the prompt contains a line ``WRITE_FILE: <name>``
    it writes the rest of the prompt to that file in the workspace (lets tests
    exercise the artifact path without any real CLI).
    """

    name = "echo"
    hand_type = "cli"

    async def execute(self, prompt, workspace, *, model=None, timeout_s=1800, on_chunk=None) -> HandResult:
        artifacts: list[str] = []
        targets: list[str] = []
        for line in prompt.splitlines():
            if line.startswith("WRITE_FILE: "):
                targets.append(line.split("WRITE_FILE: ", 1)[1].strip())
        targets.extend(re.findall(r"DONE:\s*([^\s]+\.md)", prompt))
        for fname in targets:
            if not fname or fname in artifacts:
                continue
            target = workspace / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(prompt, encoding="utf-8")
            artifacts.append(fname)
        out = f"[echo] {prompt[:4000]}"
        if on_chunk:
            on_chunk({"type": "stdout", "text": out})
        return HandResult(output=out, exit_code=0, artifacts=artifacts)


# ---- shared helpers -----------------------------------------------------

@lru_cache(maxsize=1)
def get_cli_env() -> dict[str, str]:
    """Capture the user's login-shell environment (once per process).

    Daemon-spawned CLIs need the user's real PATH and auth-related vars; a bare
    ``os.environ`` from launchd is missing both.
    """
    env = dict(os.environ)
    shell = os.environ.get("SHELL", "/bin/zsh")
    try:
        proc = subprocess.run(
            [shell, "-l", "-c", "env"], capture_output=True, text=True, timeout=10,
        )
        for line in proc.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                if k and k.isidentifier() is False and not k.replace("_", "").isalnum():
                    continue
                env.setdefault(k, v)
        # PATH from the login shell wins outright (it's the point of the exercise)
        for line in proc.stdout.splitlines():
            if line.startswith("PATH="):
                env["PATH"] = line[5:]
                break
    except Exception:  # noqa: BLE001 - best effort; fall back to os.environ
        log.warning("login-shell env capture failed; using os.environ")
    return env


def resolve_cli_path(name: str) -> str | None:
    """Find a CLI binary using the login-shell PATH."""
    return shutil.which(name, path=get_cli_env().get("PATH"))


async def run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    on_chunk: OnChunk | None = None,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str, int]:
    """Run a subprocess streaming stdout. Returns (stdout, stderr, exit_code).

    Kills the whole process group on timeout (raises asyncio.TimeoutError) and
    on cancellation (re-raises CancelledError).
    """
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env or get_cli_env(),
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    async def _pump(stream: asyncio.StreamReader, parts: list[str], kind: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            parts.append(text)
            if on_chunk:
                try:
                    on_chunk({"type": kind, "text": text})
                except Exception:  # noqa: BLE001
                    pass

    async def _run() -> int:
        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        await asyncio.gather(
            _pump(proc.stdout, stdout_parts, "stdout"),
            _pump(proc.stderr, stderr_parts, "stderr"),
        )
        return await proc.wait()

    def _kill() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    try:
        code = await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill()
        raise
    except asyncio.CancelledError:
        _kill()
        raise
    return "".join(stdout_parts), "".join(stderr_parts), code
