"""The admin_state conditional-claim idiom, shared by every mutual-exclusion user.

One concurrency-critical algorithm, four call sites (analyst_daily sweep,
memory compact, whiteboard topic kickoff, committee week): INSERT ... ON
CONFLICT DO NOTHING decides the winner by rowcount; a loser takes the row
over only when the held token is stale, via a CAS UPDATE on the exact stale
value, so two concurrent takeovers also get one winner; release is a CAS
delete of the winner's own token. Centralized here because the algorithm
used to be copy-pasted — the "future claimed_at counts as stale"
correctness fix once had to be hand-copied to every site.

Per-site parameters stay per-site on purpose: the token JSON shape
(``{"owner": ...}`` vs ``{"status": "claimed", ...}`` — tokens stored in
live databases must keep parsing), the clock (``bus.now_iso()`` vs
``datetime.now(timezone.utc)``), and the staleness rule itself (a plain
lease vs the committee's run-status lookup). Only the sweep renews its
claim, via :func:`heartbeat_admin_state`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .. import db
from ..util import new_id

log = logging.getLogger("institute.claims")


async def claim_admin_state(
    key: str,
    *,
    make_token: Callable[[], str],
    is_stale: Callable[[str, str], Awaitable[bool]],
) -> tuple[str, str] | None:
    """Conditionally claim an admin_state row; (key, token) for the winner, else None.

    INSERT ... ON CONFLICT DO NOTHING decides the winner by rowcount (the
    conditional-claim idiom). A loser takes over only when
    ``is_stale(held_value, key)`` agrees the held token is stale; the
    takeover is a CAS UPDATE on the exact stale value, so two concurrent
    takeovers also get one winner. A row released between the INSERT and the
    SELECT is skipped, not raced.
    """
    token = make_token()
    n = await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (key, token),
    )
    if n:
        return key, token

    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    if row is None:  # released between INSERT and SELECT — don't race the release, just skip
        return None
    if not await is_stale(row["value"], key):
        return None  # another owner holds a live claim
    n = await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
        (token, key, row["value"]),
    )
    return (key, token) if n else None  # n == 0: lost the takeover race — exactly one retryer wins


def lease_stale_checker(
    lease_seconds: float,
    *,
    now: Callable[[], datetime] | None = None,
    label: str = "claim",
) -> Callable[[str, str], Awaitable[bool]]:
    """Build an is_stale predicate for ``{"claimed_at": ...}`` lease tokens.

    Stale = the lease expired, OR claimed_at lies in the future (a clock
    jump / garbage would otherwise stay "live" until that future time plus
    the lease), OR the token is unparseable (a corrupt claim must not wedge
    the row forever). ``now`` defaults to ``datetime.now(timezone.utc)``;
    pass a bus.now_iso-based clock where the site already uses one. ``label``
    prefixes the future-claimed_at warning ("sweep claim", ...).
    """
    clock = now or (lambda: datetime.now(timezone.utc))

    async def is_stale(value: str, key: str) -> bool:
        try:
            claimed_at = datetime.fromisoformat(json.loads(value)["claimed_at"])
            age_s = (clock() - claimed_at).total_seconds()
            if age_s < 0:
                log.warning("%s %s has a future claimed_at (%s); treating as stale", label, key, claimed_at)
            return not 0 <= age_s < lease_seconds
        except (ValueError, KeyError, TypeError):
            return True  # corrupt claim must not wedge the row forever

    return is_stale


async def release_admin_state(key: str, token: str) -> None:
    """CAS delete — only our own claim: a late-finishing timed-out owner must
    not erase the claim of whoever took over its lease."""
    await db.execute("DELETE FROM admin_state WHERE key = ? AND value = ?", (key, token))


async def heartbeat_admin_state(
    key: str,
    holder: dict[str, str],
    stop: asyncio.Event,
    *,
    interval_s: float,
    renew_token: Callable[[str], str],
) -> None:
    """Renew a claim every interval_s until told to stop.

    Each renewal is a CAS on the previous token (same owner, fresh
    claimed_at via ``renew_token(owner)``); holder["token"] always carries
    the value the release must CAS against. If a renewal loses (claim
    deleted or taken over after missed beats), stop beating — the takeover
    already happened, and the release will safely no-op on the stale token.
    """
    try:
        owner = json.loads(holder["token"])["owner"]
    except (ValueError, KeyError, TypeError):  # unreachable with our own tokens
        owner = new_id()
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return  # released normally: no further renewals
        except asyncio.TimeoutError:
            pass
        new_token = renew_token(owner)
        try:
            n = await db.execute(
                "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
                (new_token, key, holder["token"]),
            )
        except Exception:  # noqa: BLE001 - a transient DB error must not kill the beat
            log.exception("claim heartbeat renewal errored; will retry")
            continue
        if not n:
            log.warning("claim heartbeat lost %s (taken over or force-released)", key)
            return
        holder["token"] = new_token
