"""Task-tier hand selection.

Frontier work stays on the default hand/pool. Cheap/local work can be routed to
the local Ollama hand without putting Ollama into the main analyst pool.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings

CHEAP_TIERS = {"cheap", "local", "cheap_local"}


@dataclass(frozen=True)
class RouteChoice:
    hand: str
    model: str | None


def route_for_tier(
    settings: Settings,
    tier: str | None,
    *,
    fallback_hand: str | None = None,
    fallback_model: str | None = None,
) -> RouteChoice:
    """Return the hand/model for a task tier.

    Explicit caller hand/model should be passed as fallback_*; cheap tiers only
    override them when the caller did not already choose a hand.
    """
    normalized = (tier or "").strip().lower()
    if normalized in CHEAP_TIERS and not fallback_hand:
        return RouteChoice(settings.cheap_hand, settings.cheap_model)
    return RouteChoice(fallback_hand or settings.default_hand, fallback_model)
