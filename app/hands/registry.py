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
import math
import random
import time
from pathlib import Path

from ..config import Settings
from .base import Hand, RateLimitInfo

log = logging.getLogger("institute.registry")

# hand_weights.scope enum (kept in sync with migrations/0009_hand_weights.sql
# and the Literal in app/api/hands.py). 'default' is the fallback scope.
WEIGHT_SCOPES = ("whiteboard", "research", "daily", "mailbox", "default")

DEFAULT_FALLBACK_CHAINS: dict[str, list[str]] = {
    "claude": ["codex", "gemini", "claude-api"],
    "codex": ["claude", "gemini", "openai-api"],
    "gemini": ["agy", "claude", "codex", "gemini-api"],
    "agy": ["gemini", "claude", "codex", "gemini-api"],
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
        # None = never loaded (fresh process; warn once, run neutral 1.0);
        # {} = loaded and DB has no rows. Pushed by async code, see below.
        self._weights_cache: dict[str, dict[str, float]] | None = None
        self._weights_warned = False
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
        candidates = [name] + (DEFAULT_FALLBACK_CHAINS.get(name, []) if allow_fallback else [])
        return self.resolve_chain(candidates)

    def resolve_chain(self, candidates: list[str]) -> tuple[Hand | None, list[str]]:
        """Pick the first available hand from an explicit fallback chain."""
        tried: list[str] = []
        for cand in candidates:
            if cand in tried:
                continue
            tried.append(cand)
            if self.is_available(cand):
                return self._hands[cand], tried
        # last resort: accept a degraded hand rather than fail outright
        for cand in candidates:
            if cand not in tried:
                tried.append(cand)
            if self.is_available(cand, allow_degraded=True):
                return self._hands[cand], tried
        return None, tried

    # ---- weighted pick (opt-in; NOT wired into resolve/resolve_chain) --------
    #
    # Weights live in the hand_weights table (migrations/0009) but this module
    # is synchronous and resolve() runs inside executor locks, so the registry
    # never touches the DB itself. Instead it holds a process-local cache that
    # async callers push into: the weights API (app/api/hands.py) refreshes it
    # on every PUT (and opportunistically on GET), and the app lifespan
    # pre-warms it after init_registry(). The cache starts
    # as None ("never loaded", REVIEW-B2 M3): picks still work — every hand
    # weighs a neutral 1.0, exactly the DB-empty behaviour — but the first use
    # logs ONE warning so a missing pre-warm is visible instead of silently
    # ignoring persisted weights. A sync sqlite fallback read was rejected: it
    # would block the event loop and open a second DB access path.

    def set_weights_cache(self, scope_weights: dict[str, dict[str, float]]) -> None:
        """Replace the in-process weights cache: {scope: {hand: weight}}.

        Called by async code that owns the DB read (weights API PUT/GET, boot
        pre-warm). An empty dict means "loaded, no rows" and clears the
        never-loaded warning state.
        """
        self._weights_cache = {s: dict(w) for s, w in scope_weights.items()}
        self._weights_warned = False

    def weights_loaded(self) -> bool:
        """False until the first set_weights_cache() push (fresh process)."""
        return self._weights_cache is not None

    def weights_snapshot(self) -> dict[str, dict[str, float]]:
        return {s: dict(w) for s, w in (self._weights_cache or {}).items()}

    def _warn_cold_cache_once(self) -> None:
        if not self._weights_warned:
            self._weights_warned = True
            log.warning(
                "hand_weights cache never loaded in this process — picking with "
                "neutral 1.0 weights; pre-warm via refresh_weights_cache() at boot "
                "or PUT /api/hands/weights"
            )

    def weight_for(self, scope: str, hand: str) -> float:
        """Effective weight: scope row -> 'default' scope row -> 1.0."""
        if self._weights_cache is None:
            self._warn_cold_cache_once()
            return 1.0
        for s in (scope, "default"):
            w = self._weights_cache.get(s, {}).get(hand)
            if w is not None:
                return w
        return 1.0

    def pick_weighted_hand(
        self,
        scope: str,
        live_pool: list[str],
        *,
        weights: dict[str, float] | None = None,
        rng: random.Random | None = None,
    ) -> str | None:
        """Weighted-random pick of one hand from ``live_pool``.

        Opt-in scope callers (whiteboard/research/daily/mailbox rotations) call
        this explicitly. resolve()/resolve_chain() keep their deterministic
        first-available semantics and do NOT call it.

        - ``live_pool``: hand names the caller already established as usable
          (e.g. filtered through ``is_available``). Order does not matter.
        - ``weights``: explicit {hand: weight} override; when None, weights
          come from the process cache (scope row -> 'default' row -> 1.0; a
          never-loaded cache logs one warning and behaves as all-1.0).
        - Missing weight row = 1.0; an all-zero pool degrades to a uniform
          pick rather than failing. Non-finite explicit weights (inf/nan)
          clamp to 0 (the API rejects them at the boundary); huge finite
          weights are normalized by the max before sampling so the sum can't
          overflow to inf inside random.choices.
        - ``rng``: injectable for deterministic tests.
        """
        if scope not in WEIGHT_SCOPES:
            raise ValueError(f"unknown weight scope {scope!r} (expected one of {WEIGHT_SCOPES})")
        pool = [h for h in live_pool if h]
        if not pool:
            return None
        if weights is None:
            w = [self.weight_for(scope, h) for h in pool]
        else:
            w = [weights.get(h, 1.0) for h in pool]
        w = [x if math.isfinite(x) and x > 0 else 0.0 for x in (float(x) for x in w)]
        pick = rng or random
        top = max(w)
        if top <= 0:
            return pick.choice(pool)
        w = [x / top for x in w]  # normalize: sum stays finite for any finite inputs
        return pick.choices(pool, weights=w, k=1)[0]

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
