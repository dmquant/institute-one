"""Portfolios L1–L3 + Sunday proposer (ROADMAP Phase 5, optional row).

Prices are staged through the M4-001 PIT store (never mocked) with the
test_paper_book knowledge-time convention: bars default to
``as_known_at = <date>T12:00:00Z`` and MADE is 23:00 that evening; forecasts
default to horizon 365 so they stay unexpired against the real clock, and
expiry scenarios backdate ``expires_at`` directly. Attribution rides the 0019
provenance chain (forecast_extractions.analyst_id + items), inserted directly
so tests control conviction precisely.

Portfolio entry/exit legs price at DECISION time (latest known usable close
<= today) — deliberately not the paper book's made_at-frozen entry; all bars
here are dated June 2026, safely before any real "today".

The portfolios router is not mounted in app/main.py (mounting is outside this
card's partition — PATCH-NOTES-PORTFOLIOS.md), so API tests build a bare
FastAPI app around the router, the paper-book precedent.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import bus, db
from app.institute import forecasts, market_data, portfolios

MADE = "2026-06-01T23:00:00+00:00"
WD1, WD2, WD3 = "2026-07-12", "2026-07-19", "2026-07-26"
MACRO, EQUITY = "macro-analyst", "equity-analyst"
CASH = portfolios.DEFAULT_INITIAL_CASH


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
    sid: str, *, direction: str = "long", conviction: float | None = None,
    horizon: int = 365, claim: str = "看多",
) -> dict:
    return await forecasts.create_forecast({
        "thesis_id": "t-macro", "security_id": sid, "claim": claim,
        "direction": direction, "conviction": conviction, "horizon_days": horizon,
        "settlement_rule": {"type": "absolute_move", "threshold": 0.05},
        "made_at": MADE,
    })


async def _attribute(fc: dict, analyst_id: str) -> None:
    """0019 provenance: extraction claim row + item row -> the forecast's author."""
    now = bus.now_iso()
    eid = f"ex-{fc['id']}"
    await db.execute(
        "INSERT INTO forecast_extractions (id, source_ref, source_kind, created_at, "
        "updated_at, analyst_id) VALUES (?,?,?,?,?,?)",
        (eid, f"test:{fc['id']}", "manual", now, now, analyst_id),
    )
    await db.execute(
        "INSERT INTO forecast_extraction_items (extraction_id, security_id, forecast_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?)",
        (eid, fc["security_id"], fc["id"], now, now),
    )


async def _call(
    sid: str, analyst_id: str, *, conviction: float | None = None,
    price: float | None = 10.0, direction: str = "long", claim: str = "看多",
) -> dict:
    """Security + optional entry bar + attributed forecast in one step."""
    await _mk_security(sid)
    if price is not None:
        await _bar(sid, "2026-06-01", price)
    fc = await _forecast(sid, direction=direction, conviction=conviction, claim=claim)
    await _attribute(fc, analyst_id)
    return fc


async def _expire(forecast_id: str, at: str = "2026-06-15T00:00:00+00:00") -> None:
    await db.execute("UPDATE forecasts SET expires_at = ? WHERE id = ?", (at, forecast_id))


async def _tier_proposal(analyst_id: str, tier: str, status: str | None = None) -> dict:
    rows = await portfolios.list_proposals(analyst_id=analyst_id, status=status)
    matches = [r for r in rows if r["tier"] == tier]
    assert matches, f"no {tier} proposal for {analyst_id} (status={status})"
    return matches[0]  # newest work_date first (list order)


async def _cash(portfolio_id: str) -> float:
    row = await db.query_one("SELECT cash FROM portfolios WHERE id = ?", (portfolio_id,))
    return row["cash"]


# ==== tier trio + routing ========================================================

async def test_ensure_portfolios_three_tiers_idempotent():
    rows = await portfolios.ensure_portfolios(MACRO)
    assert [r["tier"] for r in rows] == ["L1", "L2", "L3"]
    assert all(r["analyst_id"] == MACRO for r in rows)
    assert all(r["cash"] == r["initial_cash"] == CASH for r in rows)

    again = await portfolios.ensure_portfolios(MACRO)          # idempotent: same trio
    assert [r["id"] for r in again] == [r["id"] for r in rows]
    assert len(await db.query("SELECT * FROM portfolios")) == 3

    with pytest.raises(portfolios.PortfolioError, match="not in the roster"):
        await portfolios.ensure_portfolios("nobody")


def test_tier_routing_by_conviction():
    """The layering semantics: highest tier whose floor the conviction clears;
    unknown conviction always lands in the watch book."""
    assert portfolios.tier_for_conviction(0.9) == "L1"
    assert portfolios.tier_for_conviction(0.70) == "L1"        # floor inclusive
    assert portfolios.tier_for_conviction(0.5) == "L2"
    assert portfolios.tier_for_conviction(0.40) == "L2"
    assert portfolios.tier_for_conviction(0.39) == "L3"
    assert portfolios.tier_for_conviction(0.0) == "L3"
    assert portfolios.tier_for_conviction(None) == "L3"


# ==== proposal generation ========================================================

async def test_proposer_routes_forecasts_into_tiers_and_respects_attribution():
    await _mk_thesis()
    a = await _call("TIERA.US", MACRO, conviction=0.9)         # -> macro L1
    b = await _call("TIERB.US", MACRO, conviction=0.5)         # -> macro L2
    c = await _call("TIERC.US", MACRO, conviction=None)        # -> macro L3
    await _call("NEUT.US", MACRO, conviction=0.9, direction="neutral",
                claim="中性不入组合")                            # neutral: never a candidate
    await _call("NOPX.US", MACRO, conviction=0.9, price=None)  # no PIT price: skipped
    f = await _call("EQTY.US", EQUITY, conviction=0.8)         # -> equity L1
    await _mk_security("ORPH.US")                              # unattributed: nobody's call
    await _bar("ORPH.US", "2026-06-01", 10.0)
    orphan = await _forecast("ORPH.US", conviction=0.9, claim="无归属")

    out = await portfolios.generate_proposals(WD1)
    assert out["work_date"] == WD1
    assert out["proposals"] == 4                    # macro L1/L2/L3 + equity L1
    assert out["skipped_unpriced"] == 1             # NOPX
    assert out["errors"] == 0

    l1 = await _tier_proposal(MACRO, "L1")
    assert l1["status"] == "pending" and l1["work_date"] == WD1
    assert [ch["forecast_id"] for ch in l1["changes"]] == [a["id"]]
    assert l1["changes"][0] == {
        "action": "open", "security_id": "TIERA.US", "direction": "long",
        "forecast_id": a["id"], "conviction": 0.9,
        "weight": portfolios.TIER_SPECS["L1"]["weight"], "claim": "看多",
    }
    assert "高确信集中" in l1["rationale"] and "TIERA.US" in l1["rationale"]

    l2 = await _tier_proposal(MACRO, "L2")
    assert [ch["security_id"] for ch in l2["changes"]] == ["TIERB.US"]
    assert l2["changes"][0]["weight"] == portfolios.TIER_SPECS["L2"]["weight"]
    assert b["id"] == l2["changes"][0]["forecast_id"]

    l3 = await _tier_proposal(MACRO, "L3")
    assert [ch["security_id"] for ch in l3["changes"]] == ["TIERC.US"]
    assert l3["changes"][0]["conviction"] is None
    assert c["id"] == l3["changes"][0]["forecast_id"]

    eq = await _tier_proposal(EQUITY, "L1")
    assert [ch["forecast_id"] for ch in eq["changes"]] == [f["id"]]

    # the unattributed forecast is proposed to NOBODY (fails closed)
    everything = await portfolios.list_proposals(limit=500)
    assert all(
        orphan["id"] not in [ch.get("forecast_id") for ch in p["changes"]]
        for p in everything
    )

    # same-date re-run: the INSERT is the arbiter — nothing new, nothing doubled
    again = await portfolios.generate_proposals(WD1)
    assert again["proposals"] == 0
    assert again["skipped_existing"] == 4
    assert len(await portfolios.list_proposals(limit=500)) == 4


async def test_proposer_respects_tier_caps():
    await _mk_thesis()
    for i in range(7):                              # L1 cap is 5
        await _call(f"CAP{i}.US", MACRO, conviction=0.9)

    await portfolios.generate_proposals(WD1)
    l1 = await _tier_proposal(MACRO, "L1")
    assert len(l1["changes"]) == portfolios.TIER_SPECS["L1"]["max_positions"] == 5
    assert all(ch["action"] == "open" for ch in l1["changes"])


# ==== adjudication (conditional claim) ===========================================

async def test_approve_opens_positions_debits_cash_and_claims_conditionally():
    await _mk_thesis()
    fc = await _call("OPEN.US", MACRO, conviction=0.9, price=10.0)
    await portfolios.generate_proposals(WD1)
    prop = await _tier_proposal(MACRO, "L1")

    decided = await portfolios.decide_proposal(prop["id"], "approved", note="按提案执行")
    assert decided["status"] == "approved"
    assert decided["decision_note"] == "按提案执行"
    assert decided["decided_at"] is not None
    assert [a["outcome"] for a in decided["applied"]] == ["opened"]
    applied = decided["applied"][0]
    assert applied["cost"] == pytest.approx(0.20 * CASH)       # 200k = L1 weight slice
    assert applied["quantity"] == pytest.approx(20_000.0)
    assert applied["entry_price"] == pytest.approx(10.0)

    pos = (await db.query("SELECT * FROM portfolio_positions"))[0]
    assert pos["id"] == applied["position_id"]
    assert (pos["forecast_id"], pos["proposal_id"]) == (fc["id"], prop["id"])
    assert (pos["status"], pos["direction"]) == ("open", "long")
    assert pos["entry_date"] == "2026-06-01"
    assert await _cash(prop["portfolio_id"]) == pytest.approx(CASH - 200_000.0)

    # the decided row refuses a second adjudication (lost conditional claim)
    with pytest.raises(portfolios.TransitionConflict, match="not pending"):
        await portfolios.decide_proposal(prop["id"], "approved")

    with pytest.raises(portfolios.PortfolioError, match="unknown decision"):
        await portfolios.decide_proposal(prop["id"], "maybe")
    assert await portfolios.decide_proposal("nope", "approved") is None

    # a handcrafted later proposal re-opening the SAME security is skipped at
    # apply time (consume-time re-check) and moves no cash
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO portfolio_proposals (id, portfolio_id, work_date, changes, "
        "rationale, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("dup-prop", prop["portfolio_id"], WD2, json.dumps([{
            "action": "open", "security_id": "OPEN.US", "direction": "long",
            "forecast_id": fc["id"], "conviction": 0.9, "weight": 0.20, "claim": "重复",
        }]), "手工", now, now),
    )
    dup = await portfolios.decide_proposal("dup-prop", "approved")
    assert [a["outcome"] for a in dup["applied"]] == ["skipped_duplicate_security"]
    assert await _cash(prop["portfolio_id"]) == pytest.approx(CASH - 200_000.0)
    assert len(await db.query("SELECT * FROM portfolio_positions")) == 1


async def test_reject_flips_status_and_touches_nothing():
    await _mk_thesis()
    await _call("REJ.US", MACRO, conviction=0.9)
    await portfolios.generate_proposals(WD1)
    prop = await _tier_proposal(MACRO, "L1")

    decided = await portfolios.decide_proposal(prop["id"], "rejected", note="不认可")
    assert decided["status"] == "rejected"
    assert decided["applied"] == []
    assert await db.query("SELECT * FROM portfolio_positions") == []
    assert await _cash(prop["portfolio_id"]) == pytest.approx(CASH)


async def test_approve_rechecks_live_state_per_change():
    """Consume-time re-checks (operator approve-gate precedent): a forecast
    resolved after proposing and an underfunded book each skip that ONE
    change; the rest of the proposal still applies."""
    await _mk_thesis()
    await _call("LIVEA.US", MACRO, conviction=0.9, price=10.0)
    stale = await _call("LIVEB.US", MACRO, conviction=0.9, price=20.0)
    await portfolios.generate_proposals(WD1)
    prop = await _tier_proposal(MACRO, "L1")
    assert len(prop["changes"]) == 2

    await _expire(stale["id"])                     # resolved between propose and approve
    decided = await portfolios.decide_proposal(prop["id"], "approved")
    by_sec = {a["security_id"]: a["outcome"] for a in decided["applied"]}
    assert by_sec == {"LIVEA.US": "opened", "LIVEB.US": "skipped_forecast_resolved"}
    assert await _cash(prop["portfolio_id"]) == pytest.approx(CASH - 200_000.0)

    # underfunded book: the open is refused, cash never goes negative
    poor = await _call("POOR.US", EQUITY, conviction=0.9)
    await portfolios.generate_proposals(WD2)
    eq_prop = await _tier_proposal(EQUITY, "L1")
    assert [ch["forecast_id"] for ch in eq_prop["changes"]] == [poor["id"]]
    await db.execute(
        "UPDATE portfolios SET cash = 1000.0 WHERE id = ?", (eq_prop["portfolio_id"],))
    decided = await portfolios.decide_proposal(eq_prop["id"], "approved")
    assert [a["outcome"] for a in decided["applied"]] == ["skipped_insufficient_cash"]
    assert await _cash(eq_prop["portfolio_id"]) == pytest.approx(1000.0)
    assert await db.query(
        "SELECT * FROM portfolio_positions WHERE portfolio_id = ?",
        (eq_prop["portfolio_id"],)) == []


# ==== close cycle ================================================================

async def test_close_cycle_realizes_pnl_and_returns_cash():
    await _mk_thesis()
    fc = await _call("CYCL.US", MACRO, conviction=0.9, price=10.0)
    await portfolios.generate_proposals(WD1)
    prop = await _tier_proposal(MACRO, "L1")
    await portfolios.decide_proposal(prop["id"], "approved")   # entry @ 10.0, cost 200k
    pf_id = prop["portfolio_id"]

    await _bar("CYCL.US", "2026-06-10", 11.0)                  # +10% since entry
    await _expire(fc["id"])                                    # the thesis window is over

    out = await portfolios.generate_proposals(WD2)
    assert out["proposals"] == 1                               # exactly the close
    close_prop = await _tier_proposal(MACRO, "L1", status="pending")
    assert close_prop["work_date"] == WD2
    assert len(close_prop["changes"]) == 1
    ch = close_prop["changes"][0]
    pos_id = ch["position_id"]
    assert (ch["action"], ch["security_id"], ch["reason"]) == (
        "close", "CYCL.US", "forecast_expired")

    decided = await portfolios.decide_proposal(close_prop["id"], "approved")
    assert [a["outcome"] for a in decided["applied"]] == ["closed"]
    assert decided["applied"][0]["realized_pnl"] == pytest.approx(20_000.0)

    pos = await db.query_one("SELECT * FROM portfolio_positions WHERE id = ?", (pos_id,))
    assert (pos["status"], pos["close_reason"]) == ("closed", "proposal")
    assert pos["close_price"] == pytest.approx(11.0)
    assert pos["realized_pnl"] == pytest.approx(20_000.0)
    assert pos["closed_at"] is not None
    assert await _cash(pf_id) == pytest.approx(1_020_000.0)    # 800k + 200k + 20k

    snap = await portfolios.valuation(pf_id)
    assert snap["n_open"] == 0
    assert snap["total_value"] == pytest.approx(1_020_000.0)
    assert snap["nav"] == pytest.approx(1.02)
    assert snap["realized_pnl_cum"] == pytest.approx(20_000.0)


async def test_short_position_gains_on_drop():
    await _mk_thesis()
    fc = await _call("SHRT.US", MACRO, conviction=0.9, price=10.0, direction="short",
                     claim="看空")
    await portfolios.generate_proposals(WD1)
    prop = await _tier_proposal(MACRO, "L1")
    await portfolios.decide_proposal(prop["id"], "approved")   # reserves 200k notional

    await _bar("SHRT.US", "2026-06-10", 8.0)                   # -20%: short gains +20%
    snap = await portfolios.valuation(prop["portfolio_id"])
    assert snap["positions"][0]["unrealized_pnl"] == pytest.approx(40_000.0)
    assert snap["total_value"] == pytest.approx(1_040_000.0)

    await _expire(fc["id"])
    await portfolios.generate_proposals(WD2)
    close_prop = await _tier_proposal(MACRO, "L1", status="pending")
    decided = await portfolios.decide_proposal(close_prop["id"], "approved")
    assert decided["applied"][0]["realized_pnl"] == pytest.approx(40_000.0)
    assert await _cash(prop["portfolio_id"]) == pytest.approx(1_040_000.0)


# ==== valuation ==================================================================

async def test_valuation_flags_unpriceable_as_unknown_not_zero():
    await _mk_thesis()
    await _call("VALX.US", MACRO, conviction=0.9, price=10.0)
    await portfolios.generate_proposals(WD1)
    prop = await _tier_proposal(MACRO, "L1")
    await portfolios.decide_proposal(prop["id"], "approved")
    pf_id = prop["portfolio_id"]

    await _bar("VALX.US", "2026-06-10", 12.0)                  # +20%
    snap = await portfolios.valuation(pf_id)
    assert (snap["n_open"], snap["n_unpriced"]) == (1, 0)
    assert snap["cash"] == pytest.approx(800_000.0)
    assert snap["positions_value"] == pytest.approx(240_000.0)
    assert snap["total_value"] == pytest.approx(1_040_000.0)
    assert snap["nav"] == pytest.approx(1.04)
    assert snap["positions"][0]["mark_price"] == pytest.approx(12.0)

    # security deleted (FK: SET NULL): the value becomes UNKNOWN — excluded
    # from the total and flagged, never asserted as zero (H3 posture)
    await db.execute("DELETE FROM securities WHERE id = ?", ("VALX.US",))
    snap = await portfolios.valuation(pf_id)
    assert (snap["n_open"], snap["n_unpriced"]) == (1, 1)
    assert snap["total_value"] == pytest.approx(800_000.0)     # priced subset only
    assert snap["positions"][0]["value"] is None
    assert snap["positions"][0]["unrealized_pnl"] is None

    assert await portfolios.valuation("nope") is None


# ==== Sunday job =================================================================

async def test_sunday_job_supersedes_stale_pendings_idempotently():
    await _mk_thesis()
    await _call("SUND.US", MACRO, conviction=0.9)

    out = await portfolios.sunday_proposer_job(WD1)
    assert (out["expired"], out["proposals"]) == (0, 1)
    assert (await _tier_proposal(MACRO, "L1"))["status"] == "pending"

    # a new proposal date supersedes the undecided pending
    out = await portfolios.sunday_proposer_job(WD2)
    assert out["expired"] == 1 and out["proposals"] == 1
    rows = await portfolios.list_proposals(analyst_id=MACRO)
    by_wd = {r["work_date"]: r["status"] for r in rows}
    assert by_wd == {WD1: "expired", WD2: "pending"}

    # same-date re-run: nothing expires, nothing duplicates
    again = await portfolios.sunday_proposer_job(WD2)
    assert (again["expired"], again["proposals"]) == (0, 0)
    assert again["skipped_existing"] == 1
    assert len(await portfolios.list_proposals(analyst_id=MACRO)) == 2

    # decided history is never expired; a held position proposes nothing new
    prop = await _tier_proposal(MACRO, "L1", status="pending")
    await portfolios.decide_proposal(prop["id"], "approved")
    out = await portfolios.sunday_proposer_job(WD3)
    assert (out["expired"], out["proposals"]) == (0, 0)
    by_wd = {r["work_date"]: r["status"] for r in await portfolios.list_proposals(analyst_id=MACRO)}
    assert by_wd == {WD1: "expired", WD2: "approved"}


# ==== API ========================================================================

def _make_app() -> FastAPI:
    from app.api import portfolios as api_portfolios

    app = FastAPI()
    app.include_router(api_portfolios.router)
    return app


async def test_api_roundtrip():
    await _mk_thesis()
    await _call("APIX.US", MACRO, conviction=0.9, price=10.0)
    await portfolios.generate_proposals(WD1)

    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        r = await client.get("/api/portfolios", params={"analyst_id": MACRO})
        assert r.status_code == 200
        assert [p["tier"] for p in r.json()] == ["L1", "L2", "L3"]
        l1_id = r.json()[0]["id"]

        r = await client.get(f"/api/portfolios/{l1_id}")
        assert r.status_code == 200
        assert r.json()["tier"] == "L1" and r.json()["positions"] == []
        assert (await client.get("/api/portfolios/nope")).status_code == 404
        assert (await client.get("/api/portfolios/nope/valuation")).status_code == 404

        r = await client.get("/api/portfolios/proposals", params={"status": "pending"})
        assert r.status_code == 200 and len(r.json()) == 1
        pid = r.json()[0]["id"]
        r = await client.get("/api/portfolios/proposals", params={"status": "bogus"})
        assert r.status_code == 400
        r = await client.get(f"/api/portfolios/proposals/{pid}")
        assert r.status_code == 200 and r.json()["work_date"] == WD1
        assert (await client.get("/api/portfolios/proposals/nope")).status_code == 404

        r = await client.post(f"/api/portfolios/proposals/{pid}/decide",
                              json={"decision": "approved"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved"
        assert [a["outcome"] for a in body["applied"]] == ["opened"]

        # double-decide is a lost conditional claim -> 409; unknown -> 404;
        # out-of-enum decision -> 422 (schema-level)
        r = await client.post(f"/api/portfolios/proposals/{pid}/decide",
                              json={"decision": "rejected"})
        assert r.status_code == 409
        r = await client.post("/api/portfolios/proposals/nope/decide",
                              json={"decision": "approved"})
        assert r.status_code == 404
        r = await client.post(f"/api/portfolios/proposals/{pid}/decide",
                              json={"decision": "nuke"})
        assert r.status_code == 422

        r = await client.get(f"/api/portfolios/{l1_id}/valuation")
        assert r.status_code == 200
        snap = r.json()
        assert snap["cash"] == pytest.approx(800_000.0)
        assert snap["n_open"] == 1

        r = await client.get(f"/api/portfolios/{l1_id}")
        assert len(r.json()["positions"]) == 1
        assert r.json()["positions"][0]["security_id"] == "APIX.US"
