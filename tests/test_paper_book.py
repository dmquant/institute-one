"""Paper book (Phase 5 card C3): opener, MTM close paths, NAV, manual close.

Prices are staged through the card M4-001 PIT store (never mocked), with the
test_forecasts knowledge-time convention: bars default to
``as_known_at = <date>T12:00:00Z`` and MADE is 23:00 that evening, so the
entry leg (frozen at made_at — the B6 anti-look-ahead contract) sees the
entry-day close, and look-ahead probes write corrections known AFTER made_at
and assert they cannot move the entry. Forecasts default to horizon 365 so
they are still unexpired against the real clock; horizon-close scenarios
backdate ``expires_at`` directly.

The paper-book router is not mounted in app/main.py (mounting is outside this
card's partition — PATCH-NOTES-C3.md), so API tests build a bare FastAPI app
around the router.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import forecasts, market_data, paper_book

MADE = "2026-06-01T23:00:00+00:00"


def _known(date: str) -> str:
    return f"{date}T12:00:00+00:00"


async def _mk_thesis(tid: str = "t-macro") -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO theses (id, kind, slug, name_zh, status, current_view, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tid, "thesis", tid, "宏观论点", "active", "unknown", now, now),
    )


async def _mk_security(sid: str) -> None:
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO securities (id, symbol, market, name_en, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, sid.split(".")[0], "US", f"{sid} Co", now, now),
    )


async def _bar(sid: str, bar_date: str, close: float, *, known: str | None = None) -> None:
    await market_data.upsert_bar({
        "security_id": sid, "bar_date": bar_date,
        "open": close, "high": close, "low": close, "close": close,
        "as_known_at": known or _known(bar_date),
    })


async def _forecast(
    sid: str, *, direction: str = "long", horizon: int = 365, claim: str = "看多",
) -> dict:
    return await forecasts.create_forecast({
        "thesis_id": "t-macro", "security_id": sid, "claim": claim,
        "direction": direction, "horizon_days": horizon,
        "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
        "made_at": MADE,
    })


async def _expire(forecast_id: str, at: str = "2026-06-15T00:00:00+00:00") -> None:
    await db.execute("UPDATE forecasts SET expires_at = ? WHERE id = ?", (at, forecast_id))


async def _set_cap(n: int) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (paper_book.ADMIN_KEY, json.dumps({"max_positions": n})),
    )


async def _positions(status: str | None = None) -> list[dict]:
    return await paper_book.list_positions(status=status)


# ==== opener ===================================================================

async def test_opener_entry_frozen_at_made_at():
    """The B6 look-ahead probe, position edition: a correction of the entry-day
    close ingested AFTER made_at must never move the position's entry."""
    await _mk_thesis()
    await _mk_security("LOOK.US")
    await _bar("LOOK.US", "2026-06-01", 10.0)                       # known noon < MADE
    await _bar("LOOK.US", "2026-06-01", 20.0, known="2026-06-02T09:00:00+00:00")
    fc = await _forecast("LOOK.US")

    out = await paper_book.opener_tick()
    assert out["opened"] == 1
    pos = (await _positions("open"))[0]
    assert pos["forecast_id"] == fc["id"]
    assert pos["entry_price"] == pytest.approx(10.0)                # basis 10, never 20
    assert pos["entry_date"] == "2026-06-01"
    assert (pos["direction"], pos["size"]) == ("long", 1.0)
    assert (pos["stop_pct"], pos["target_pct"]) == (
        paper_book.DEFAULT_STOP_PCT, paper_book.DEFAULT_TARGET_PCT)

    # idempotent: the forecast already has its position
    out = await paper_book.opener_tick()
    assert out["opened"] == 0
    assert len(await _positions()) == 1
    events = await bus.replay(0, types=["paper_book.opened"])
    assert len(events) == 1


async def test_opener_skips_no_price_then_retries_and_filters():
    await _mk_thesis()
    await _mk_security("NOPX.US")
    fc = await _forecast("NOPX.US")
    await _mk_security("NEUT.US")
    await _bar("NEUT.US", "2026-06-01", 10.0)
    await _forecast("NEUT.US", direction="neutral", claim="中性不开仓")
    await _mk_security("EXPD.US")
    await _bar("EXPD.US", "2026-06-01", 10.0)
    expired = await _forecast("EXPD.US", horizon=10, claim="已到期")   # expired long ago

    out = await paper_book.opener_tick()
    assert out["opened"] == 0
    assert out["skipped_no_price"] == 1          # NOPX: no usable entry knowledge yet
    assert await _positions() == []              # neutral + expired never considered

    # entry knowledge backfilled AS OF before made_at -> next tick opens
    await _bar("NOPX.US", "2026-06-01", 10.0)    # known noon, <= MADE
    out = await paper_book.opener_tick()
    assert out["opened"] == 1
    assert [p["forecast_id"] for p in await _positions("open")] == [fc["id"]]
    # the expired forecast stays positionless forever
    assert await db.query_one(
        "SELECT 1 AS x FROM paper_positions WHERE forecast_id = ?", (expired["id"],)) is None


async def test_opener_cap_and_per_security_dedup():
    await _mk_thesis()
    for sid in ("CAPA.US", "CAPB.US"):
        await _mk_security(sid)
        await _bar(sid, "2026-06-01", 10.0)
        await _forecast(sid, claim=f"cap {sid}")

    await _set_cap(1)
    out = await paper_book.opener_tick()
    assert (out["cap"], out["opened"]) == (1, 1)
    assert len(await _positions("open")) == 1
    out = await paper_book.opener_tick()                 # cap full: nothing more
    assert out["opened"] == 0

    await _set_cap(20)                                   # admin_state raise takes effect
    out = await paper_book.opener_tick()
    assert out["opened"] == 1
    assert len(await _positions("open")) == 2

    # two open forecasts on ONE security -> a single open position
    await _mk_security("DUPS.US")
    await _bar("DUPS.US", "2026-06-01", 10.0)
    f1 = await _forecast("DUPS.US", claim="第一条")
    f2 = await _forecast("DUPS.US", claim="第二条")
    await paper_book.opener_tick()
    await paper_book.opener_tick()
    dups = [p for p in await _positions("open") if p["security_id"] == "DUPS.US"]
    assert len(dups) == 1
    assert dups[0]["forecast_id"] in {f1["id"], f2["id"]}


# ==== MTM: stop / target / horizon =============================================

async def test_mtm_stop_and_target_close_without_settling():
    await _mk_thesis()
    for sid in ("STOPX.US", "TGTX.US", "SHRT.US"):
        await _mk_security(sid)
        await _bar(sid, "2026-06-01", 10.0)
    await _forecast("STOPX.US")
    await _forecast("TGTX.US")
    await _forecast("SHRT.US", direction="short", claim="看空")
    assert (await paper_book.opener_tick())["opened"] == 3

    await _bar("STOPX.US", "2026-06-10", 9.4)    # -6%  <= -5%  -> stop
    await _bar("TGTX.US", "2026-06-10", 11.5)    # +15% >= +10% -> target
    await _bar("SHRT.US", "2026-06-10", 8.5)     # short signed +15% -> target

    out = await paper_book.mark_to_market()
    assert out["closed"] == 3 and out["n_open"] == 0
    by_sec = {p["security_id"]: p for p in await _positions("closed")}
    assert by_sec["STOPX.US"]["close_reason"] == "stop"
    assert by_sec["STOPX.US"]["close_price"] == pytest.approx(9.4)
    assert by_sec["STOPX.US"]["realized_pnl"] == pytest.approx(-0.06)
    assert by_sec["TGTX.US"]["close_reason"] == "target"
    assert by_sec["TGTX.US"]["realized_pnl"] == pytest.approx(0.15)
    assert by_sec["SHRT.US"]["close_reason"] == "target"     # short gains on the drop
    assert by_sec["SHRT.US"]["realized_pnl"] == pytest.approx(0.15)

    # stop/target closes never settle an UNEXPIRED forecast (B6 refuses pre-expiry)
    assert await db.query("SELECT * FROM forecast_settlements") == []
    assert all(f["status"] == "open" for f in await forecasts.list_forecasts())

    nav = await db.query_one("SELECT * FROM nav_history")
    assert nav["nav"] == pytest.approx(1.24)                 # 1 - 0.06 + 0.15 + 0.15
    assert nav["realized_pnl_cum"] == pytest.approx(0.24)
    assert (nav["n_open"], nav["benchmark_nav"]) == (0, None)  # no marks: fails closed

    # idempotent re-run: same single row, closed rows unchanged
    out = await paper_book.mark_to_market()
    assert out["closed"] == 0
    assert len(await db.query("SELECT * FROM nav_history")) == 1
    assert len(await _positions("closed")) == 3


async def test_mtm_horizon_close_settles_conditionally_never_twice():
    await _mk_thesis()
    for sid, exit_close in (("HZN.US", 10.3), ("HZN2.US", 8.0), ("HZN3.US", 10.3)):
        await _mk_security(sid)
        await _bar(sid, "2026-06-01", 10.0)
        await _bar(sid, "2026-06-11", exit_close)
    f1 = await _forecast("HZN.US", claim="到期平仓")
    f2 = await _forecast("HZN2.US", claim="到期且触发止损")
    f3 = await _forecast("HZN3.US", claim="已被人工结算")
    assert (await paper_book.opener_tick())["opened"] == 3
    for f in (f1, f2, f3):
        await _expire(f["id"])
    settled_first = await forecasts.settle_forecast(f3["id"])   # a racing settler won f3
    assert settled_first["status"] == "settled"

    out = await paper_book.mark_to_market()
    assert out["closed"] == 3
    by_fc = {p["forecast_id"]: p for p in await _positions("closed")}

    # +3%: inside the band, closed only because the horizon arrived; settle fires
    assert by_fc[f1["id"]]["close_reason"] == "horizon"
    assert by_fc[f1["id"]]["close_price"] == pytest.approx(10.3)
    assert by_fc[f1["id"]]["realized_pnl"] == pytest.approx(0.03)
    s1 = await forecasts.get_forecast(f1["id"])
    assert s1["status"] == "settled"
    assert s1["settlement"]["verdict"] == "partial"             # 0 < 0.03 < 0.05

    # -20%: stop takes precedence over horizon, and the expired forecast still settles
    assert by_fc[f2["id"]]["close_reason"] == "stop"
    s2 = await forecasts.get_forecast(f2["id"])
    assert s2["status"] == "settled"
    assert s2["settlement"]["verdict"] == "miss"

    # pre-settled forecast: the close succeeds, settlement stays EXACTLY one row
    assert by_fc[f3["id"]]["close_reason"] == "horizon"
    rows = await db.query(
        "SELECT id FROM forecast_settlements WHERE forecast_id = ?", (f3["id"],))
    assert len(rows) == 1


async def test_mtm_post_expiry_prices_never_leak_into_closes():
    """REVIEW-C3 H2 probe: a dramatic bar AFTER expiry must neither set the
    close price nor flip a horizon close into target — the paper close and
    the B6 settlement must price from the same window (<= expiry date)."""
    await _mk_thesis()
    await _mk_security("LATEX.US")
    await _bar("LATEX.US", "2026-06-01", 10.0)
    await _bar("LATEX.US", "2026-06-11", 10.2)   # inside the window: +2%
    fc = await _forecast("LATEX.US")
    assert (await paper_book.opener_tick())["opened"] == 1
    await _expire(fc["id"])                       # expires 2026-06-15
    await _bar("LATEX.US", "2026-07-01", 20.0)    # post-expiry moonshot: must be invisible

    out = await paper_book.mark_to_market()       # runs "today", long after expiry
    assert out["closed"] == 1
    pos = (await _positions("closed"))[0]
    assert pos["close_reason"] == "horizon"                      # NOT target
    assert pos["close_price"] == pytest.approx(10.2)             # window bar, not 20
    assert pos["realized_pnl"] == pytest.approx(0.02)
    settled = await forecasts.get_forecast(fc["id"])
    assert settled["settlement"]["actual_return"] == pytest.approx(0.02)  # same endpoint
    assert settled["settlement"]["verdict"] == "partial"
    nav = await db.query_one("SELECT * FROM nav_history")
    assert nav["nav"] == pytest.approx(1.02)

    # manual close obeys the same clamp: still priced off the window bar
    await _mk_security("LATEM.US")
    await _bar("LATEM.US", "2026-06-01", 10.0)
    await _bar("LATEM.US", "2026-06-11", 10.2)
    fc2 = await _forecast("LATEM.US", claim="手动平仓晚于到期")
    assert (await paper_book.opener_tick())["opened"] == 1
    await _expire(fc2["id"])
    await _bar("LATEM.US", "2026-07-01", 20.0)
    pos2 = (await _positions("open"))[0]
    closed2 = await paper_book.close_position(pos2["id"])
    assert closed2["close_price"] == pytest.approx(10.2)
    assert closed2["realized_pnl"] == pytest.approx(0.02)


async def test_mtm_unpriceable_is_unknown_not_zero():
    """REVIEW-C3 H3: unknown value is excluded and flagged (n_unpriced),
    never asserted as a zero return."""
    await _mk_thesis()
    await _mk_security("GONE.US")
    await _bar("GONE.US", "2026-06-01", 10.0)
    await _bar("GONE.US", "2026-06-10", 10.8)    # real unrealized +8% first
    fc = await _forecast("GONE.US")
    assert (await paper_book.opener_tick())["opened"] == 1
    out = await paper_book.mark_to_market()
    assert out["nav"] == pytest.approx(1.08)
    assert out["n_unpriced"] == 0

    # security deleted (FK: SET NULL): the value becomes UNKNOWN — the nav
    # over the priced subset drops it, and n_unpriced flags the row as a
    # partial statement (the +8% was knowledge about a security that no
    # longer exists; 0 is never asserted in its place)
    await db.execute("DELETE FROM securities WHERE id = ?", ("GONE.US",))
    out = await paper_book.mark_to_market()
    assert (out["closed"], out["n_open"]) == (0, 1)              # stays open, unpriced
    assert out["n_unpriced"] == 1
    assert out["nav"] == pytest.approx(1.0)                      # priced subset only
    nav = await db.query_one("SELECT * FROM nav_history")
    assert nav["n_unpriced"] == 1

    # expired + unpriceable: closes as 'unpriced' with NULL price AND NULL
    # realized — the unknown stays unknown forever, excluded from realized
    # aggregates, still counted in n_unpriced; the forecast settles
    # 'invalid' through B6's own fails-closed path
    await _expire(fc["id"])
    out = await paper_book.mark_to_market()
    assert out["closed"] == 1
    pos = (await _positions("closed"))[0]
    assert (pos["close_reason"], pos["close_price"], pos["realized_pnl"]) == (
        "unpriced", None, None)
    assert out["realized_pnl_cum"] == pytest.approx(0.0)         # NULL never summed as 0
    assert out["n_unpriced"] == 1
    assert out["nav"] == pytest.approx(1.0)
    settled = await forecasts.get_forecast(fc["id"])
    assert settled["status"] == "invalid"
    assert settled["settlement"]["verdict"] == "invalid"

    # the journal reports the gap instead of a fake 0
    journal = await paper_book.render_journal(out["work_date"])
    assert "未知" in journal and "unpriced" in journal


async def test_opener_concurrency_database_is_the_arbiter():
    """REVIEW-C3 M3: per-security uniqueness and the cap are enforced by the
    INSERT itself (partial unique index + conditional insert), not by the
    opener's pre-reads."""
    await _mk_thesis()
    await _mk_security("RACE.US")
    await _bar("RACE.US", "2026-06-01", 10.0)
    f1 = await _forecast("RACE.US", claim="并发一")
    f2 = await _forecast("RACE.US", claim="并发二")
    now = bus.now_iso()

    # deterministic race core: two inserts for the same security — the 0017
    # partial unique index picks exactly one winner
    r1 = await paper_book._insert_position(f1, "2026-06-01", 10.0, 20, now)
    r2 = await paper_book._insert_position(f2, "2026-06-01", 10.0, 20, now)
    assert (r1, r2) == ("opened", "lost_race")
    open_rows = await db.query(
        "SELECT * FROM paper_positions WHERE security_id = 'RACE.US' AND status = 'open'")
    assert len(open_rows) == 1
    # ... and the same forecast can never open twice (UNIQUE(forecast_id))
    assert await paper_book._insert_position(f1, "2026-06-01", 10.0, 20, now) == "lost_race"

    # cap is a database fact too: the conditional INSERT refuses at the cap
    await _mk_security("CAPX.US")
    await _bar("CAPX.US", "2026-06-01", 10.0)
    f3 = await _forecast("CAPX.US", claim="超出上限")
    assert await paper_book._insert_position(f3, "2026-06-01", 10.0, 1, now) == "cap"

    # whole-tick integration: concurrent sweeps agree on one total winner
    await _set_cap(2)
    import asyncio
    outs = await asyncio.gather(paper_book.opener_tick(), paper_book.opener_tick())
    assert sum(o["opened"] for o in outs) == 1                   # CAPX opened once
    assert len(await _positions("open")) == 2


# ==== REVIEW-C3 M5: closed-position attribution -> analyst memory ===============

async def test_close_attribution_flows_into_analyst_memory():
    """The full flywheel: an attributed extraction opens a position, the close
    event carries the author's analyst_id, and the memory compact consumes the
    outcome as its fourth material source (id-cursored like the other three)."""
    from app.institute import forecast_extract as fx
    from app.institute import memory

    await _mk_thesis()
    await _mk_security("ATTR.US")
    await _bar("ATTR.US", "2026-06-01", 10.0)

    # 一年内: horizon 365 keeps the forecast unexpired against the real clock
    # (the module-wide convention — see the module docstring)
    out = await fx.process_source(
        "research:attr-flow", "research", "一年内看多 ATTR.US",
        made_at=MADE, analyst_id="macro-analyst",
    )
    assert len(out["created"]) == 1
    assert (await paper_book.opener_tick())["opened"] == 1

    await _bar("ATTR.US", "2026-06-10", 11.5)          # +15% -> target close
    assert (await paper_book.mark_to_market())["closed"] == 1

    events = await bus.replay(0, types=["paper_book.closed"])
    assert len(events) == 1
    assert events[0].payload["analyst_id"] == "macro-analyst"
    assert events[0].payload["reason"] == "target"

    # the compact's material carries the outcome (echo hand reflects the prompt)
    r = await memory.compact_one("macro-analyst")
    assert r["status"] == "completed"
    md = (await memory.latest("macro-analyst"))["compact_md"]
    assert "纸面账本结果" in md
    assert "ATTR.US" in md and "target" in md and "+15.00%" in md
    assert "看多 ATTR.US" in md                         # the original claim rides along

    # consumed exactly once: the cursor advanced past the event
    again = await memory.compact_one("macro-analyst")
    assert again.get("skipped") == "no new material"
    cursors = memory._parse_cursors((await memory.latest("macro-analyst"))["cursors"])
    assert cursors["outcome_event"] >= events[0].id

    # other analysts never see someone else's outcome
    assert (await memory._outcome_items("equity-analyst", 0))[0] == []


async def test_close_without_attribution_stays_unattributed():
    """Manually-created forecasts (no extraction row) close with a NULL
    analyst_id — no memory ever consumes them (fails closed, never a guess)."""
    from app.institute import memory

    await _mk_thesis()
    await _mk_security("NOAT.US")
    await _bar("NOAT.US", "2026-06-01", 10.0)
    await _forecast("NOAT.US")                          # direct create: no provenance
    assert (await paper_book.opener_tick())["opened"] == 1
    await _bar("NOAT.US", "2026-06-10", 11.5)
    assert (await paper_book.mark_to_market())["closed"] == 1

    events = await bus.replay(0, types=["paper_book.closed"])
    assert events[0].payload["analyst_id"] is None
    for analyst_id in ("macro-analyst", "chief-strategist"):
        items, _ = await memory._outcome_items(analyst_id, 0)
        assert items == []


# ==== NAV curve + benchmark =====================================================

async def test_nav_unrealized_and_benchmark_base():
    await _mk_thesis()
    await _mk_security("NAVX.US")
    await _bar("NAVX.US", "2026-06-01", 10.0)
    await _forecast("NAVX.US")
    assert (await paper_book.opener_tick())["opened"] == 1
    await _bar("NAVX.US", "2026-06-10", 10.5)    # +5%: inside the band, stays open

    out = await paper_book.mark_to_market()
    assert (out["closed"], out["n_open"]) == (0, 1)
    assert out["nav"] == pytest.approx(1.05)
    assert out["gross_exposure"] == pytest.approx(1.0)
    assert out["benchmark_nav"] is None          # no CSI300 marks yet: NULL, no guessing

    # first usable mark pins the benchmark base (that day reads 1.0) ...
    await market_data.upsert_benchmark({"id": "CSI300", "name_zh": "沪深300"})
    await market_data.upsert_benchmark_mark("CSI300", {"mark_date": "2026-06-01", "value": 4000.0})
    out = await paper_book.mark_to_market()
    assert out["benchmark_nav"] == pytest.approx(1.0)
    base = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (paper_book.BENCH_BASE_KEY,))
    assert json.loads(base["value"])["value"] == pytest.approx(4000.0)

    # ... later marks are normalized to it; the upsert stays one row per day
    await market_data.upsert_benchmark_mark("CSI300", {"mark_date": "2026-06-11", "value": 4200.0})
    out = await paper_book.mark_to_market()
    assert out["benchmark_nav"] == pytest.approx(1.05)
    assert len(await db.query("SELECT * FROM nav_history")) == 1

    # an unusable newest mark (0.0 is storable by 0006 design) fails closed to NULL
    await market_data.upsert_benchmark_mark("CSI300", {"mark_date": "2026-06-12", "value": 0.0})
    out = await paper_book.mark_to_market()
    assert out["benchmark_nav"] is None

    series = await paper_book.nav_series(days=30)
    assert len(series) == 1
    assert series[0]["nav"] == pytest.approx(1.05)
    assert series[0]["benchmark_nav"] is None    # the re-run refreshed the same row

    # journal renders the day's NAV block (opens happened on the real 'today')
    journal = await paper_book.render_journal(series[0]["work_date"])
    assert "纸面交易日志" in journal and "NAV" in journal
    assert "无基准数据" in journal


# ==== API: positions / nav / manual close =======================================

def _make_app() -> FastAPI:
    from app.api import paper_book as api_paper_book

    app = FastAPI()
    app.include_router(api_paper_book.router)
    return app


async def test_manual_close_api_roundtrip():
    await _mk_thesis()
    await _mk_security("POSX.US")
    await _bar("POSX.US", "2026-06-01", 10.0)
    await _bar("POSX.US", "2026-06-10", 10.8)
    fc = await _forecast("POSX.US")
    await _mk_security("DELX.US")
    await _bar("DELX.US", "2026-06-01", 10.0)
    await _forecast("DELX.US", claim="将被删标的")
    assert (await paper_book.opener_tick())["opened"] == 2
    await db.execute("DELETE FROM securities WHERE id = ?", ("DELX.US",))

    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.get("/api/book/positions", params={"status": "open"})
        assert r.status_code == 200 and len(r.json()) == 2
        pid = next(p["id"] for p in r.json() if p["forecast_id"] == fc["id"])
        del_pid = next(p["id"] for p in r.json() if p["forecast_id"] != fc["id"])

        assert (await client.get(f"/api/book/positions/{pid}")).status_code == 200
        assert (await client.get("/api/book/positions/nope")).status_code == 404
        r = await client.get("/api/book/positions", params={"status": "bogus"})
        assert r.status_code == 400

        r = await client.post(f"/api/book/positions/{pid}/close")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "closed"
        assert body["close_reason"] == "manual"
        assert body["close_price"] == pytest.approx(10.8)
        assert body["realized_pnl"] == pytest.approx(0.08)
        # manual close of an unexpired forecast never settles it
        assert (await forecasts.get_forecast(fc["id"]))["status"] == "open"

        # double close is a lost conditional claim -> 409; unknown -> 404
        assert (await client.post(f"/api/book/positions/{pid}/close")).status_code == 409
        assert (await client.post("/api/book/positions/nope/close")).status_code == 404

        # unpriceable position (security deleted): fails closed -> 400, stays open
        r = await client.post(f"/api/book/positions/{del_pid}/close")
        assert r.status_code == 400
        assert "no usable price" in r.json()["detail"]
        r = await client.get("/api/book/positions", params={"status": "open"})
        assert [p["id"] for p in r.json()] == [del_pid]

        await paper_book.mark_to_market()
        r = await client.get("/api/book/nav", params={"days": 30})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["nav"] == pytest.approx(1.08)    # realized 0.08 + unpriceable flat
        assert rows[0]["n_open"] == 1
