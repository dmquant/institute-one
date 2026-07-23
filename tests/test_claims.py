"""Direct tests for the shared admin_state conditional-claim idiom
(app/institute/claims.py).

The four call sites (analyst_daily sweep, memory compact, whiteboard kickoff,
committee week) exercise it indirectly; after the copy-paste convergence this
suite locks the algorithm itself: one winner per key, stale-only takeover via
CAS, CAS release that never erases a successor's claim, the lease staleness
rules (expired / future / corrupt), and heartbeat renew/lose semantics.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app import db
from app.institute.claims import (
    claim_admin_state,
    heartbeat_admin_state,
    lease_stale_checker,
    release_admin_state,
)

KEY = "test_claim:2026-07-23"


def _token(owner: str, claimed_at: str | None = None) -> str:
    return json.dumps({
        "owner": owner,
        "claimed_at": claimed_at or datetime.now(timezone.utc).isoformat(),
    })


async def _never_stale(value: str, key: str) -> bool:
    return False


async def _always_stale(value: str, key: str) -> bool:
    return True


async def _held(key: str = KEY) -> str | None:
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    return row["value"] if row else None


# ---- claim / takeover / release ----------------------------------------------

async def test_first_claim_wins_and_live_claim_refuses():
    claim = await claim_admin_state(KEY, make_token=lambda: _token("a"), is_stale=_never_stale)
    assert claim is not None
    key, token = claim
    assert (key, await _held()) == (KEY, token)

    # a second claimant against a LIVE token loses without touching the row
    assert await claim_admin_state(KEY, make_token=lambda: _token("b"), is_stale=_never_stale) is None
    assert await _held() == token


async def test_stale_takeover_is_cas_and_concurrent_takeovers_get_one_winner():
    stale = _token("dead")
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?, ?)", (KEY, stale))

    winners = await asyncio.gather(
        claim_admin_state(KEY, make_token=lambda: _token("b"), is_stale=_always_stale),
        claim_admin_state(KEY, make_token=lambda: _token("c"), is_stale=_always_stale),
    )
    won = [w for w in winners if w is not None]
    assert len(won) == 1  # the CAS on the exact stale value arbitrates
    assert await _held() == won[0][1]


async def test_release_is_cas_scoped_to_the_own_token():
    claim = await claim_admin_state(KEY, make_token=lambda: _token("a"), is_stale=_never_stale)
    assert claim is not None
    _, old_token = claim

    # the lease is taken over; the ORIGINAL owner's late release must no-op
    takeover = await claim_admin_state(KEY, make_token=lambda: _token("b"), is_stale=_always_stale)
    assert takeover is not None
    await release_admin_state(KEY, old_token)
    assert await _held() == takeover[1]  # successor's claim survives

    # the live owner's release does delete the row
    await release_admin_state(KEY, takeover[1])
    assert await _held() is None


# ---- lease_stale_checker -------------------------------------------------------

async def test_lease_staleness_rules_expired_future_corrupt():
    is_stale = lease_stale_checker(60.0, label="test claim")
    now = datetime.now(timezone.utc)

    fresh = _token("a", now.isoformat())
    expired = _token("a", (now - timedelta(seconds=61)).isoformat())
    future = _token("a", (now + timedelta(seconds=5)).isoformat())

    assert await is_stale(fresh, KEY) is False
    assert await is_stale(expired, KEY) is True
    assert await is_stale(future, KEY) is True      # clock jump fails stale, not live
    assert await is_stale("not json", KEY) is True  # corrupt must not wedge the row
    assert await is_stale(json.dumps({"owner": "a"}), KEY) is True  # missing claimed_at

    # an injectable clock is honored (the whiteboard bus.now_iso site)
    pinned = lease_stale_checker(60.0, now=lambda: now + timedelta(seconds=120))
    assert await pinned(fresh, KEY) is True


# ---- heartbeat -----------------------------------------------------------------

async def test_heartbeat_renews_with_same_owner_then_stops_on_release():
    claim = await claim_admin_state(KEY, make_token=lambda: _token("hb"), is_stale=_never_stale)
    assert claim is not None
    holder = {"token": claim[1]}
    stop = asyncio.Event()

    beat = asyncio.create_task(heartbeat_admin_state(
        KEY, holder, stop, interval_s=0.02, renew_token=lambda owner: _token(owner),
    ))
    for _ in range(200):  # wait for at least one renewal
        await asyncio.sleep(0.01)
        if holder["token"] != claim[1]:
            break
    assert holder["token"] != claim[1]
    renewed = json.loads(holder["token"])
    assert renewed["owner"] == "hb"                 # renewals keep the owner
    assert await _held() == holder["token"]         # holder always mirrors the row

    stop.set()
    await asyncio.wait_for(beat, timeout=1)
    await release_admin_state(KEY, holder["token"])
    assert await _held() is None


async def test_heartbeat_stops_after_losing_the_row():
    claim = await claim_admin_state(KEY, make_token=lambda: _token("hb"), is_stale=_never_stale)
    assert claim is not None
    holder = {"token": claim[1]}
    stop = asyncio.Event()

    # simulate a takeover: the stored value no longer matches holder["token"]
    await db.execute("UPDATE admin_state SET value = ? WHERE key = ?", (_token("thief"), KEY))

    beat = asyncio.create_task(heartbeat_admin_state(
        KEY, holder, stop, interval_s=0.02, renew_token=lambda owner: _token(owner),
    ))
    await asyncio.wait_for(beat, timeout=1)  # renewal CAS misses -> the beat exits by itself

    thief = await _held()
    assert thief is not None and json.loads(thief)["owner"] == "thief"
    await release_admin_state(KEY, holder["token"])  # late release no-ops on the stale token
    assert await _held() == thief
