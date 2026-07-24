"""Single configuration object for the whole system.

Everything lives under ``home`` (default ``~/.institute-one``).  All settings can
be overridden via environment variables prefixed ``INSTITUTE_`` or a ``.env``
file in the working directory, e.g. ``INSTITUTE_PORT=8200``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VERSION = "0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INSTITUTE_", env_file=".env", extra="ignore")

    home: Path = Path("~/.institute-one")
    host: str = "127.0.0.1"
    port: int = 8100
    # IANA zone name, pre-checked at load time by _timezone_must_resolve below.
    timezone: str = "Asia/Singapore"
    # Optional bearer auth (ROADMAP Phase 0). None/empty = auth disabled.
    token: str | None = None            # INSTITUTE_TOKEN

    # Obsidian vault export. None disables the vault layer entirely.
    # Point at the *subtree the institute owns*, e.g. ~/Obsidian/Main/Institute
    vault_dir: Path | None = None

    # Execution
    # ge=1 — asyncio.Semaphore(0) is perfectly legal yet silently starves
    # every task; the validator below rejects <1 with a pointer to the
    # supported pause mechanism (the maintenance switch).
    max_concurrent: int = Field(default=3, ge=1)
    # Per-hand queued-depth cap (ROADMAP Phase 2 executor depth): a submit/spawn
    # finding MORE THAN this many queued rows on its hand fast-fails as the
    # terminal status 'overcommitted' instead of queueing without bound (a
    # backlog of exactly the cap is still admitted — normal fan-out bursts,
    # e.g. the analyst-daily sweep, park roster-sized backlogs briefly).
    # <=0 disables the check.
    hand_queue_depth: int = 8           # INSTITUTE_HAND_QUEUE_DEPTH
    default_hand: str = "claude"
    research_hands: str = "codex,agy"
    # gt=0 — 0/negative would make every task expire the moment it starts.
    default_timeout_s: int = Field(default=1800, gt=0)
    # tasks.output column cap; gt=0 — 0 would silently blank all task output.
    output_cap_bytes: int = Field(default=200_000, gt=0)

    # Hand enable flags (CLI hands are additionally gated on the binary existing)
    enable_claude: bool = True
    enable_codex: bool = True
    enable_gemini: bool = True
    enable_agy: bool = True       # Google Antigravity CLI (gemini successor)
    enable_opencode: bool = True
    enable_ollama: bool = False
    enable_echo: bool = True  # trivial built-in hand used by tests/smoke checks

    # Weighted hand selection (ROADMAP Phase 2; hand_weights table, migrations/0009).
    # Opt-in: False (default) keeps every call site's pre-weights behaviour unchanged.
    enable_hand_weights: bool = False

    # Per-hand default models (None -> the CLI's own default)
    claude_model: str | None = None
    codex_model: str | None = None
    gemini_model: str | None = None
    opencode_model: str | None = None
    ollama_model: str = "llama3.2"
    ollama_host: str = "http://localhost:11434"

    # Vector search (Phase 1a). Off by default: without Ollama + sqlite-vec the
    # system runs the documented FTS5-only degradation path.
    enable_vectors: bool = False
    embed_model: str = "bge-m3"

    # Market data fetchers (Phase 1b). The ladder is FMP -> Stooq -> Sina;
    # Stooq/Sina are keyless, so fetching works with no key at all.
    fmp_api_key: str | None = None          # INSTITUTE_FMP_API_KEY
    fetch_proxy: str | None = None          # INSTITUTE_FETCH_PROXY, e.g. http://127.0.0.1:7897 (mihomo)
    market_fetch_enabled: bool = True       # INSTITUTE_MARKET_FETCH_ENABLED — kill switch for the hourly job
    market_refresh_minutes: int = 60        # hourly per ROADMAP; 0/negative disables
    market_refresh_limit: int = 20          # securities per sweep (stalest first)

    # Direct-API fallback hands (only registered when the key is present)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None
    anthropic_api_model: str = "claude-sonnet-4-6"
    openai_api_model: str = "gpt-5.2"
    google_api_model: str = "gemini-2.5-pro"
    # Base URLs — override to point a hand at an OpenAI/Anthropic/Gemini-compatible
    # local gateway (e.g. CLIProxyAPI / litellm) instead of the official endpoint.
    anthropic_api_base_url: str = "https://api.anthropic.com"
    openai_api_base_url: str = "https://api.openai.com/v1"
    google_api_base_url: str = "https://generativelanguage.googleapis.com"

    # Scheduler (cron-ish times, SGT). Set to "" to disable a job.
    briefing_time: str = "08:30"        # 晨会简报
    daily_time: str = "23:00"           # 每日日报
    analyst_daily_time: str = "19:00"   # 分析师观察日报（跟进项喂白板与信箱）
    scorecard_time: str = "00:05"       # previous-day hand quality settlement
    memory_compact_time: str = "23:30"  # 常备记忆压缩（analyst memory nightly compact）
    committee_time: str = "20:00"       # 每周委员会（仅周五触发；"" 禁用）
    whiteboard_kickoff_minutes: int = 60   # try to open a new board every N minutes
    whiteboard_tick_seconds: int = 60      # advance running boards
    mailbox_sweep_seconds: int = 120
    research_tick_minutes: int = 30
    research_daily_cap: int = 4       # SGT work-day cap on completed runs; 0/negative disables the research queue
    research_cooldown_days: int = 30
    janitor_minutes: int = 60
    events_retention_days: int = 90       # durable SSE/audit replay window
    # Phase 3 fact-check (scheduler.py reads the tick minutes directly;
    # only the daily cap is read defensively, by factcheck.py)
    factcheck_tick_minutes: int = 30    # 0/negative disables the job
    # Verification ATTEMPTS per SGT work date. None -> factcheck's built-in
    # default (10); a concrete value here would shadow the module constant the
    # factcheck tests monkeypatch, so only the env override materialises one.
    factcheck_daily_cap: int | None = None  # INSTITUTE_FACTCHECK_DAILY_CAP

    # ---- field guards --------------------------------------------------
    @field_validator("max_concurrent", mode="before")
    @classmethod
    def _max_concurrent_is_not_a_pause(cls, v: object) -> object:
        # A bare ge=1 error would not tell the operator what 0 was meant to
        # achieve; say it explicitly. Runs before pydantic's int coercion, so
        # env-var strings are parsed here for the range check only.
        try:
            n = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return v  # not a number — let pydantic's own int parsing report it
        if n < 1:
            raise ValueError(
                "max_concurrent must be >= 1: asyncio.Semaphore(0) is legal and "
                "would silently starve every task. To pause the system use the "
                "maintenance switch (POST /api/admin/maintenance) instead."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone_must_resolve(cls, v: str) -> str:
        # Fail at config load with a clear message instead of crashing the
        # lifespan later (scheduler/prompts build ZoneInfo from this string).
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown IANA timezone {v!r} (expected e.g. 'Asia/Singapore', 'UTC')"
            ) from exc
        return v

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

    @property
    def research_hand_names(self) -> tuple[str, ...]:
        names = tuple(h.strip() for h in self.research_hands.split(",") if h.strip())
        return names or (self.default_hand,)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests point INSTITUTE_HOME at a tmpdir, then call this."""
    get_settings.cache_clear()
    os.environ.setdefault("TZ", "Asia/Singapore")
