"""Hand registry: availability, cooldowns, fallback chains, circuit breaker.

Rules carried over from the previous system (battle-tested, do not relitigate):
- Cooldowns persist to ``rate_limits.json`` and survive restarts.
- Cooldown merges never SHORTEN an existing cooldown; minimum 60s floor.
- A rate limit is breaker-NEUTRAL. Only >=2 consecutive *real* failures degrade
  a hand; one success resets. Degraded hands are skipped during resolve unless
  nothing else is available.
- Fallback chains: the coding CLIs are interchangeable; API hands sit at the
  tail of their family chain. Explicit ``model`` should be dropped by callers
  on cross-family fallback (the executor does this).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..config import Settings
from .base import Hand, RateLimitInfo

log = logging.getLogger("institute.registry")

DEFAULT_FALLBACK_CHAINS: dict[str, list[str]] = {
    "claude": ["codex", "gemini", "claude-api"],
    "codex": ["claude", "gemini", "openai-api"],
    "gemini": ["claude", "codex", "gemini-api"],
    "opencode": ["claude", "codex"],
    "claude-api": [],
    "openai-api": [],
    "gemini-api": [],
    "ollama": [],
    "echo": [],
}

DEFAULT_COOLDOWN_S = 5 * 3600  # quota signature with no parseable reset time
MIN_COOLDOWN_S = 60


class HandRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._hands: dict[str, Hand] = {}
        self._cooldowns: dict[str, dict] = {}   # name -> {until: epoch, reason, marked_at}
        self._consecutive_failures: dict[str, int] = {}
        self._degraded: set[str] = set()
        self._load_cooldowns()

    # ---- registration ----------------------------------------------------
    def register(self, hand: Hand) -> None:
        self._hands[hand.name] = hand

    def get(self, name: str) -> Hand | None:
        return self._hands.get(name)

    def names(self) -> list[str]:
        return list(self._hands.keys())

    # ---- cooldown persistence ---------------------------------------------
    def _load_cooldowns(self) -> None:
        path: Path = self.settings.rate_limits_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                now = time.time()
                self._cooldowns = {k: v for k, v in data.items() if v.get("until", 0) > now}
            except Exception:  # noqa: BLE001
                log.warning("could not parse %s; starting clean", path)

    def _save_cooldowns(self) -> None:
        try:
            self.settings.rate_limits_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings.rate_limits_path.write_text(
                json.dumps(self._cooldowns, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to persist cooldowns")

    # ---- state transitions -------------------------------------------------
    def mark_rate_limited(self, name: str, info: RateLimitInfo) -> None:
        retry = info.retry_after_s if info.retry_after_s and info.retry_after_s > 0 else DEFAULT_COOLDOWN_S
        retry = max(retry, MIN_COOLDOWN_S)
        until = time.time() + retry
        existing = self._cooldowns.get(name)
        if existing and existing.get("until", 0) >= until:
            return  # never shorten
        self._cooldowns[name] = {"until": until, "reason": info.reason, "marked_at": time.time()}
        self._save_cooldowns()
        log.info("hand %s on cooldown for %ds (%s)", name, retry, info.reason)

    def clear_cooldown(self, name: str) -> None:
        if name in self._cooldowns:
            del self._cooldowns[name]
            self._save_cooldowns()

    def record_result(self, name: str, *, ok: bool, rate_limited: bool = False) -> None:
        if rate_limited:
            return  # breaker-neutral
        if ok:
            self._consecutive_failures[name] = 0
            self._degraded.discard(name)
        else:
            n = self._consecutive_failures.get(name, 0) + 1
            self._consecutive_failures[name] = n
            if n >= 2:
                self._degraded.add(name)
                log.warning("hand %s degraded after %d consecutive failures", name, n)

    # ---- availability / resolution -----------------------------------------
    def cooling_until(self, name: str) -> float | None:
        cd = self._cooldowns.get(name)
        if cd and cd.get("until", 0) > time.time():
            return cd["until"]
        return None

    def is_available(self, name: str, *, allow_degraded: bool = False) -> bool:
        hand = self._hands.get(name)
        if hand is None or not hand.available():
            return False
        if self.cooling_until(name):
            return False
        if name in self._degraded and not allow_degraded:
            return False
        return True

    def resolve(self, name: str, *, allow_fallback: bool = True) -> tuple[Hand | None, list[str]]:
        """Pick the hand to run. Returns (hand_or_None, tried_names)."""
        tried: list[str] = []
        candidates = [name] + (DEFAULT_FALLBACK_CHAINS.get(name, []) if allow_fallback else [])
        for cand in candidates:
            tried.append(cand)
            if self.is_available(cand):
                return self._hands[cand], tried
        # last resort: accept a degraded hand rather than fail outright
        for cand in candidates:
            if self.is_available(cand, allow_degraded=True):
                return self._hands[cand], tried
        return None, tried

    # ---- introspection -------------------------------------------------------
    def status_snapshot(self) -> list[dict]:
        out = []
        now = time.time()
        for name, hand in self._hands.items():
            cd = self._cooldowns.get(name)
            cooling = cd and cd.get("until", 0) > now
            out.append({
                "name": name,
                "type": hand.hand_type,
                "installed": hand.available(),
                "available": self.is_available(name),
                "degraded": name in self._degraded,
                "cooldown_until": cd["until"] if cooling else None,
                "cooldown_reason": cd.get("reason") if cooling else None,
                "consecutive_failures": self._consecutive_failures.get(name, 0),
                "fallback_chain": DEFAULT_FALLBACK_CHAINS.get(name, []),
            })
        return out


_registry: HandRegistry | None = None


def init_registry(settings: Settings) -> HandRegistry:
    """Build the singleton registry. Hand construction lives in hands/__init__.py."""
    global _registry
    from . import build_hands  # late import: hands/__init__ imports this module

    _registry = HandRegistry(settings)
    for hand in build_hands(settings):
        _registry.register(hand)
    log.info("registered hands: %s", _registry.names())
    return _registry


def get_registry() -> HandRegistry:
    if _registry is None:
        raise RuntimeError("init_registry() has not been called")
    return _registry
