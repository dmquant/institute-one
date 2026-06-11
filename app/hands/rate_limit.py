"""Per-CLI rate-limit/quota signature detection.

Deliberately NO generic backstop: a false positive puts a healthy hand on a
multi-hour cooldown, which is worse than missing a signature (a missed one just
fails the task, and the loops retry).

When a signature matches but no reset time can be parsed, ``retry_after_s`` is
``None`` and the registry applies its 5h default.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .base import RateLimitInfo

log = logging.getLogger("institute.hands.rate_limit")

# Claude CLI prints reset times as local wall-clock; this deployment runs SGT.
_SGT = ZoneInfo("Asia/Singapore")

# Codex replays large chunks of the prompt/transcript; only the tail is
# trustworthy as CLI-emitted (not model-emitted) text.
_CODEX_TAIL_CHARS = 2000

_RAW_CAP = 300


def _raw_line(text: str, idx: int) -> str:
    """The full line containing position ``idx`` (for RateLimitInfo.raw)."""
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:_RAW_CAP]


# ---- claude ---------------------------------------------------------------

_CLAUDE_RESET_RE = re.compile(
    r"usage limit will reset at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE
)


def _seconds_until_wall_clock(hour: int, minute: int, ampm: str) -> int | None:
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.now(_SGT)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int((target - now).total_seconds())


def _detect_claude(text: str) -> RateLimitInfo | None:
    m = _CLAUDE_RESET_RE.search(text)
    if m:
        retry = _seconds_until_wall_clock(
            int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        )
        return RateLimitInfo("quota_exhausted", retry_after_s=retry, raw=_raw_line(text, m.start()))

    lowered = text.lower()
    for needle, reason in (("usage limit reached", "quota_exhausted"), ("rate_limit_error", "rate_limit")):
        idx = lowered.find(needle)
        if idx >= 0:
            return RateLimitInfo(reason, raw=_raw_line(text, idx))

    # Case-sensitive on purpose: bare lowercase "overloaded" appears in prose.
    for needle in ("overloaded_error", "Overloaded"):
        idx = text.find(needle)
        if idx >= 0:
            return RateLimitInfo("overloaded", raw=_raw_line(text, idx))
    return None


# ---- codex ----------------------------------------------------------------

_CODEX_TRY_AGAIN_RE = re.compile(
    r"try again in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
_CODEX_RETRY_AFTER_RE = re.compile(r"Retry-After:\s*(\d+)", re.IGNORECASE)


def _detect_codex(text: str) -> RateLimitInfo | None:
    tail = text[-_CODEX_TAIL_CHARS:]

    m = _CODEX_TRY_AGAIN_RE.search(tail)
    if m:
        unit = m.group(2).lower()
        mult = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        retry = max(1, int(float(m.group(1)) * mult))
        return RateLimitInfo("rate_limit", retry_after_s=retry, raw=_raw_line(tail, m.start()))

    m = _CODEX_RETRY_AFTER_RE.search(tail)
    if m:
        return RateLimitInfo("rate_limit", retry_after_s=max(1, int(m.group(1))), raw=_raw_line(tail, m.start()))

    lowered = tail.lower()
    for needle in ("rate_limit_exceeded", "too many requests"):
        idx = lowered.find(needle)
        if idx >= 0:
            return RateLimitInfo("rate_limit", raw=_raw_line(tail, idx))
    return None


# ---- gemini ---------------------------------------------------------------

_GEMINI_RESET_RE = re.compile(
    r"quota will reset after\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.IGNORECASE
)


def _detect_gemini(text: str) -> RateLimitInfo | None:
    m = _GEMINI_RESET_RE.search(text)
    if m:
        h, mn, s = (int(g) if g else 0 for g in m.groups())
        total = h * 3600 + mn * 60 + s
        return RateLimitInfo(
            "quota_exhausted", retry_after_s=total or None, raw=_raw_line(text, m.start())
        )

    for needle in ("RESOURCE_EXHAUSTED", "TerminalQuotaError"):  # exact API/CLI tokens
        idx = text.find(needle)
        if idx >= 0:
            return RateLimitInfo("quota_exhausted", raw=_raw_line(text, idx))

    for line in text.splitlines():
        if "429" in line:
            lowered = line.lower()
            if "quota" in lowered or "rate" in lowered:
                return RateLimitInfo("rate_limit", raw=line.strip()[:_RAW_CAP])
    return None


# ---- opencode (wraps the other providers) ----------------------------------

def _detect_opencode(text: str) -> RateLimitInfo | None:
    return _detect_claude(text) or _detect_codex(text) or _detect_gemini(text)


# ---- api hands (429 status handled in the hand itself) ----------------------

def _detect_api(text: str) -> RateLimitInfo | None:
    lowered = text.lower()
    for needle, reason in (
        ("rate limit", "rate_limit"),
        ("rate_limit", "rate_limit"),
        ("quota", "quota_exhausted"),
    ):
        idx = lowered.find(needle)
        if idx >= 0:
            return RateLimitInfo(reason, raw=_raw_line(text, idx))
    return None


_DETECTORS = {
    "claude": _detect_claude,
    "codex": _detect_codex,
    "gemini": _detect_gemini,
    "agy": _detect_gemini,  # agy is gemini-powered: same quota signatures
    "opencode": _detect_opencode,
    "claude-api": _detect_api,
    "openai-api": _detect_api,
    "gemini-api": _detect_api,
}


def detect_rate_limit(hand_name: str, text: str) -> RateLimitInfo | None:
    if not text:
        return None
    detector = _DETECTORS.get(hand_name)
    if detector is None:
        return None  # unknown hand: no generic backstop
    info = detector(text)
    if info is not None:
        log.info(
            "rate-limit signature on %s: %s (retry_after_s=%s)",
            hand_name, info.raw or info.reason, info.retry_after_s,
        )
    return info
