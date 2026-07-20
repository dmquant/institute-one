"""Analyst dailies: guard, sweep, follow-up application with self-mail drop."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import bus, db
from app.institute import analyst_daily
from app.institute.analysts import get_analyst, roster

SAMPLE_WITH_SELF_MAIL = """## 观察日报

1. 测试观察一（来源：test）。

## 后续跟进

```json
{
  "whiteboard_topics": [
    {"topic": "利率与权益估值的传导", "question": "10Y 上行 50bp 对成长股估值的弹性?"}
  ],
  "mailbox_followups": [
    {"analyst_id": "macro-analyst", "subject": "自问自答", "body": "写给自己的应被丢弃。"},
    {"analyst_id": "equity-analyst", "subject": "估值弹性", "body": "请测算 10Y+50bp 情景下的估值压缩。"}
  ]
}
```
"""


async def test_run_one_completes_and_guards():
    result = await analyst_daily.run_one("macro-analyst")
    assert result["status"] == "completed"

    record = await analyst_daily._get_record()
    assert record["macro-analyst"] == "completed"

    # second run same day is skipped
    again = await analyst_daily.run_one("macro-analyst")
    assert again.get("skipped")

    events = [e for e in await bus.replay(0, types=["analyst_daily.completed"])
              if e.ref_id == "macro-analyst"]
    assert len(events) == 1
    # the shared per-day session exists
    st = await analyst_daily.status()
    assert st["session_id"]
    assert st["analysts"]["macro-analyst"] == "completed"


async def test_run_all_skips_ops_and_done():
    await analyst_daily.run_one("macro-analyst")
    summary = await analyst_daily.run_all()
    ids = [r["analyst_id"] for r in summary["results"]]
    assert "ops-editor" not in ids          # ops category excluded
    assert "macro-analyst" not in ids       # already done today
    assert summary["completed"] == len(ids)  # echo completes everything


async def test_followups_applied_with_self_mail_dropped():
    analyst = get_analyst("macro-analyst")
    session = await analyst_daily._today_session()
    ws = Path(session["workspace_dir"])
    (ws / "macro-analyst.md").write_text(SAMPLE_WITH_SELF_MAIL, encoding="utf-8")

    n_topics, n_mails = await analyst_daily._apply_followups(analyst, ws, "macro-analyst.md")
    assert n_topics == 1
    assert n_mails == 1  # self-mail dropped, equity-analyst kept

    pool = await db.query("SELECT * FROM topic_pool WHERE source = 'analyst-daily'")
    assert len(pool) == 1 and "利率" in pool[0]["topic"]

    threads = await db.query("SELECT * FROM mailbox_threads")
    assert len(threads) == 1
    assert threads[0]["analyst_id"] == "equity-analyst"
    assert analyst.name in threads[0]["subject"]


def test_rotation_skips_to_default_when_no_clis():
    # tests run with all CLI hands disabled -> rotation falls back to default (echo)
    a = roster()[0]
    assert analyst_daily._pick_hand(a, 0) in ("echo", a.hand or "echo")


async def test_mark_concurrent_no_lost_updates():
    """Concurrent _mark under asyncio.gather must keep every analyst's mark.

    The old one-blob-per-day read-modify-write lost updates: each _mark read
    the same (empty) record and the last write erased the others.
    """
    ids = [a.id for a in roster() if a.category not in analyst_daily.SKIP_CATEGORIES]
    assert len(ids) >= 2  # need real concurrency for the regression to bite

    await asyncio.gather(*(analyst_daily._mark(aid, "completed") for aid in ids))

    record = await analyst_daily._get_record()
    assert {aid: record.get(aid) for aid in ids} == {aid: "completed" for aid in ids}


async def test_get_record_merges_legacy_blob():
    """A pre-upgrade per-day blob still counts; per-analyst rows win on conflict."""
    legacy_key = analyst_daily._guard_prefix()
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?)",
        (legacy_key, json.dumps({"macro-analyst": "completed", "equity-analyst": "failed"})),
    )
    await analyst_daily._mark("equity-analyst", "completed")

    record = await analyst_daily._get_record()
    assert record["macro-analyst"] == "completed"   # from the legacy blob
    assert record["equity-analyst"] == "completed"  # per-analyst row wins

    # the guard keeps honoring legacy-blob completions after the upgrade
    result = await analyst_daily.run_one("macro-analyst")
    assert result.get("skipped")


async def test_get_record_prefix_is_literal_no_cross_day_pollution():
    """The per-analyst scan is a literal prefix compare, not a GLOB/LIKE pattern.

    Similar prefixes (2026-07-2 vs 2026-07-20) must not cross-match, and GLOB
    metacharacters in an externally supplied date (status API) match nothing.
    """
    async def put(key: str, status: str) -> None:
        await db.execute(
            "INSERT INTO admin_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(status)),
        )

    await put("analyst_daily:2026-07-2:macro-analyst", "failed")    # short neighbour
    await put("analyst_daily:2026-07-19:macro-analyst", "failed")
    await put("analyst_daily:2026-07-20:macro-analyst", "completed")
    await put("analyst_daily:2026-07-21:equity-analyst", "failed")

    # each day sees exactly its own rows
    assert await analyst_daily._get_record("2026-07-20") == {"macro-analyst": "completed"}
    assert await analyst_daily._get_record("2026-07-19") == {"macro-analyst": "failed"}
    assert await analyst_daily._get_record("2026-07-2") == {"macro-analyst": "failed"}

    # metacharacter dates are treated as literals -> no cross-day matches
    for probe in ("2026-07-??", "2026-07-2*", "2026-07-[12]0", "2026-07-%", "*"):
        assert await analyst_daily._get_record(probe) == {}, probe


# ---- F1-1: concurrent double-run protection ----------------------------------

def _working_ids() -> list[str]:
    return [a.id for a in roster() if a.category not in analyst_daily.SKIP_CATEGORIES]


async def test_concurrent_run_all_single_sweep_single_session():
    """Two overlapping run_all (cron + manual run-now) must yield exactly one
    task per analyst and ONE shared daily session — the F1-1 probe measured
    9 analysts x 2 = 18 tasks / 18 sessions before the sweep claim."""
    results = await asyncio.gather(analyst_daily.run_all(), analyst_daily.run_all())

    tasks = await db.query("SELECT id FROM tasks WHERE source = 'analyst-daily'")
    assert len(tasks) == len(_working_ids())  # one task per working analyst

    sessions = await db.query("SELECT id FROM sessions WHERE kind = 'daily'")
    assert len(sessions) == 1  # the one-shared-session-per-day invariant

    # exactly one sweep actually ran; the other skipped (either as claim loser
    # or, if the winner already finished, on the all-done guard)
    ran = [r for r in results if r["ran"]]
    skipped = [r for r in results if r.get("skipped")]
    assert len(ran) == 1 and len(skipped) == 1
    assert ran[0]["completed"] == len(_working_ids())

    # completion events emitted once per analyst, not twice
    events = await bus.replay(0, types=["analyst_daily.completed"])
    assert len(events) == len(_working_ids())


async def test_today_session_concurrent_creates_one():
    """The SELECT-then-INSERT window inside one sweep's gather used to mint a
    session per analyst; under the lock all callers converge on one row."""
    sessions = await asyncio.gather(*(analyst_daily._today_session() for _ in range(6)))
    assert len({s["id"] for s in sessions}) == 1
    rows = await db.query("SELECT id FROM sessions WHERE kind = 'daily'")
    assert len(rows) == 1


async def test_sweep_claim_one_winner_and_release():
    first = await analyst_daily._claim_sweep()
    assert first is not None
    assert await analyst_daily._claim_sweep() is None  # live claim: loser skips

    key, token = first
    await analyst_daily._release_sweep(key, token)
    second = await analyst_daily._claim_sweep()  # released -> claimable again
    assert second is not None
    await analyst_daily._release_sweep(*second)


async def test_sweep_claim_takes_over_expired_and_corrupt_claims():
    key = analyst_daily._sweep_key()
    stale = json.dumps({
        "owner": "deadbeef",
        "claimed_at": (datetime.now(timezone.utc)
                       - timedelta(seconds=analyst_daily.SWEEP_LEASE_S + 60)
                       ).isoformat(timespec="seconds"),
    })
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?, ?)", (key, stale))
    claim = await analyst_daily._claim_sweep()  # expired lease: taken over
    assert claim is not None
    await analyst_daily._release_sweep(*claim)

    await db.execute("INSERT INTO admin_state (key, value) VALUES (?, ?)", (key, "not json"))
    claim = await analyst_daily._claim_sweep()  # corrupt claim must not wedge the day
    assert claim is not None
    await analyst_daily._release_sweep(*claim)


async def test_sweep_release_is_cas_only_own_claim():
    """A late-finishing owner whose lease was taken over must not erase the
    new owner's claim."""
    claim = await analyst_daily._claim_sweep()
    assert claim is not None
    key, token = claim

    await analyst_daily._release_sweep(key, "someone else's token")  # no-op
    assert await analyst_daily._claim_sweep() is None  # our claim still holds

    await analyst_daily._release_sweep(key, token)
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    assert row is None


async def test_run_all_releases_claim_and_reruns_failures():
    summary = await analyst_daily.run_all()
    assert summary["ran"] == len(_working_ids())

    # claim released -> a later sweep is not locked out, and sees all-done
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (analyst_daily._sweep_key(),)
    )
    assert row is None
    again = await analyst_daily.run_all()
    assert again["skipped"] == "all done"

    # a failed analyst stays claimable: the next sweep retries exactly it
    await analyst_daily._mark(_working_ids()[0], "failed")
    retry = await analyst_daily.run_all()
    assert retry["ran"] == 1
    assert retry["results"][0]["analyst_id"] == _working_ids()[0]


async def test_sweep_claim_key_never_pollutes_daily_record():
    claim = await analyst_daily._claim_sweep()
    assert claim is not None
    record = await analyst_daily._get_record()
    assert record == {}  # the claim row lives outside the per-analyst namespace
    st = await analyst_daily.status()
    assert all(v == "pending" for v in st["analysts"].values())
    await analyst_daily._release_sweep(*claim)


async def test_sweep_heartbeat_renews_claim_live_owner_never_taken_over(monkeypatch):
    """REVIEW-B1 M1: a live sweep outlasting the lease must NOT be taken over —
    the heartbeat keeps renewing claimed_at, so takeover only ever applies to
    a dead (hard-killed) owner (that path is covered by the expired-claim
    takeover test above).

    claimed_at is second-precision (bus.now_iso() truncates), so the observed
    age can read up to ~1s older than reality; the lease here stays above
    heartbeat + 1s so renewals reliably register as live.
    """
    monkeypatch.setattr(analyst_daily, "SWEEP_LEASE_S", 1.2)
    monkeypatch.setattr(analyst_daily, "SWEEP_HEARTBEAT_S", 0.05)

    claim = await analyst_daily._claim_sweep()
    assert claim is not None
    key, token = claim
    holder = {"token": token}
    stop = asyncio.Event()
    beat = asyncio.create_task(analyst_daily._heartbeat_loop(key, holder, stop))
    try:
        await asyncio.sleep(1.8)  # well past the 1.2s lease
        # the ORIGINAL claimed_at is long expired, but renewals keep it live
        assert await analyst_daily._claim_sweep() is None
        assert holder["token"] != token  # renewals actually happened
        assert json.loads(holder["token"])["owner"] == json.loads(token)["owner"]
    finally:
        stop.set()
        await beat
    await analyst_daily._release_sweep(key, holder["token"])
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    assert row is None  # the renewed token still releases cleanly


async def test_sweep_heartbeat_stops_when_claim_is_lost(monkeypatch):
    """A heartbeat that loses its CAS (claim force-deleted / taken over) must
    exit instead of fighting the new owner; the stale release then no-ops."""
    monkeypatch.setattr(analyst_daily, "SWEEP_HEARTBEAT_S", 0.05)

    claim = await analyst_daily._claim_sweep()
    assert claim is not None
    key, token = claim
    # operator escape hatch: force-delete the row, then a new sweep claims it
    await db.execute("DELETE FROM admin_state WHERE key = ?", (key,))
    new_claim = await analyst_daily._claim_sweep()
    assert new_claim is not None

    holder = {"token": token}
    stop = asyncio.Event()
    beat = asyncio.create_task(analyst_daily._heartbeat_loop(key, holder, stop))
    await asyncio.wait_for(beat, timeout=2.0)  # exits by itself on the lost CAS
    assert holder["token"] == token  # no renewal happened

    await analyst_daily._release_sweep(key, holder["token"])  # stale: no-op
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    assert row is not None and row["value"] == new_claim[1]  # new owner intact
    await analyst_daily._release_sweep(*new_claim)


async def test_sweep_claim_future_timestamp_treated_as_stale():
    """REVIEW-B1 L3: a claimed_at in the future (clock jump / garbage) must
    not block the day until that future time plus the lease."""
    key = analyst_daily._sweep_key()
    future = json.dumps({
        "owner": "clockjump",
        "claimed_at": (datetime.now(timezone.utc) + timedelta(hours=6)
                       ).isoformat(timespec="seconds"),
    })
    await db.execute("INSERT INTO admin_state (key, value) VALUES (?, ?)", (key, future))

    claim = await analyst_daily._claim_sweep()
    assert claim is not None  # taken over, not wedged for 6h+lease
    await analyst_daily._release_sweep(*claim)


async def test_run_all_cancellation_releases_claim(monkeypatch):
    """Drain-style cancellation of a mid-flight run_all must release the sweep
    claim via finally (REVIEW-B1 L3 asked for this as a pinned regression)."""
    started = asyncio.Event()

    async def slow_run_one(analyst_id: str, *, force: bool = False, rotation_index: int = 0):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(analyst_daily, "run_one", slow_run_one)

    sweep = asyncio.create_task(analyst_daily.run_all())
    await asyncio.wait_for(started.wait(), timeout=2.0)
    sweep.cancel()
    await asyncio.gather(sweep, return_exceptions=True)

    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (analyst_daily._sweep_key(),)
    )
    assert row is None  # claim released on the cancellation path
    assert (await analyst_daily._claim_sweep()) is not None  # and re-claimable
