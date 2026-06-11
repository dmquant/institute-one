"""Single configuration object for the whole system.

Everything lives under ``home`` (default ``~/.institute-one``).  All settings can
be overridden via environment variables prefixed ``INSTITUTE_`` or a ``.env``
file in the working directory, e.g. ``INSTITUTE_PORT=8200``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

VERSION = "0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INSTITUTE_", env_file=".env", extra="ignore")

    home: Path = Path("~/.institute-one")
    host: str = "127.0.0.1"
    port: int = 8100
    timezone: str = "Asia/Singapore"

    # Obsidian vault export. None disables the vault layer entirely.
    # Point at the *subtree the institute owns*, e.g. ~/Obsidian/Main/Institute
    vault_dir: Path | None = None

    # Execution
    max_concurrent: int = 3
    default_hand: str = "claude"
    default_timeout_s: int = 1800
    output_cap_bytes: int = 200_000  # tasks.output column cap

    # Hand enable flags (CLI hands are additionally gated on the binary existing)
    enable_claude: bool = True
    enable_codex: bool = True
    enable_gemini: bool = True
    enable_opencode: bool = True
    enable_ollama: bool = False
    enable_echo: bool = True  # trivial built-in hand used by tests/smoke checks

    # Per-hand default models (None -> the CLI's own default)
    claude_model: str | None = None
    codex_model: str | None = None
    gemini_model: str | None = None
    opencode_model: str | None = None
    ollama_model: str = "llama3.2"
    ollama_host: str = "http://localhost:11434"

    # Direct-API fallback hands (only registered when the key is present)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None
    anthropic_api_model: str = "claude-sonnet-4-6"
    openai_api_model: str = "gpt-5.2"
    google_api_model: str = "gemini-2.5-pro"

    # Scheduler (cron-ish times, SGT). Set to "" to disable a job.
    briefing_time: str = "08:30"        # 晨会简报
    daily_time: str = "23:00"           # 每日日报
    analyst_daily_time: str = "19:00"   # 分析师观察日报（跟进项喂白板与信箱）
    whiteboard_kickoff_minutes: int = 60   # try to open a new board every N minutes
    whiteboard_tick_seconds: int = 60      # advance running boards
    mailbox_sweep_seconds: int = 120
    research_tick_minutes: int = 30
    research_daily_cap: int = 4
    research_cooldown_days: int = 30
    janitor_minutes: int = 60

    # ---- derived paths -------------------------------------------------
    @property
    def home_dir(self) -> Path:
        return self.home.expanduser()

    @property
    def db_path(self) -> Path:
        return self.home_dir / "institute.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.home_dir / "workspaces"

    @property
    def archive_dir(self) -> Path:
        return self.home_dir / "archive"

    @property
    def rate_limits_path(self) -> Path:
        return self.home_dir / "rate_limits.json"

    @property
    def logs_dir(self) -> Path:
        return self.home_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.home_dir / "backups"

    def ensure_dirs(self) -> None:
        for p in (self.home_dir, self.workspaces_dir, self.archive_dir, self.logs_dir, self.backups_dir):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def workflows_dir(self) -> Path:
        return self.repo_root / "workflows"

    @property
    def catalog_path(self) -> Path:
        return self.repo_root / "catalog" / "analysts.json"

    @property
    def frontend_dist(self) -> Path:
        return self.repo_root / "frontend" / "dist"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests point INSTITUTE_HOME at a tmpdir, then call this."""
    get_settings.cache_clear()
    os.environ.setdefault("TZ", "Asia/Singapore")
