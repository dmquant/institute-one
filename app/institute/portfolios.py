"""Per-analyst virtual portfolios (L1–L3) + the Sunday proposer (Phase 5).

Every non-ops analyst carries three tiered portfolios (ensured idempotently,
UNIQUE(analyst_id, tier)); the tier decides how much capital a call may claim:

    L1  高确信集中   conviction >= 0.70; at most 5 slots, 20% weight each —
                     the concentrated best-ideas sleeve
    L2  分散         conviction >= 0.40; at most 15 slots, 6% weight each —
                     the diversified mid-conviction sleeve
    L3  观察仓       everything else (including NULL conviction); at most 30
                     slots, 2% token weight — the watch book, so every open
                     call is tracked somewhere

A forecast is routed to exactly ONE tier — the highest layer whose conviction
floor it clears (``tier_for_conviction``). Candidates are the analyst's OWN
open, unexpired long/short forecasts, attributed through the 0019 provenance
chain (forecast -> extraction item -> extraction.analyst_id). Unattributed
forecasts are proposed to nobody — attribution fails closed, never guesses
(the memory.py posture).

SUNDAY PROPOSER (``sunday_proposer_job``, Sun 22:00 SGT — the scheduler wiring
lives outside this partition, see PATCH-NOTES-PORTFOLIOS.md; zero model
calls). Per portfolio it drafts one proposal:

    closes  open positions whose linked forecast has resolved (settled /
            invalid) or expired — the thesis is over, exit the trade
    opens   tier-routed candidates without an open position on that security,
            up to the tier cap (closes count as freed slots — application
            runs closes first), skipping candidates with no usable PIT price
            (retried next Sunday)

One proposal per (portfolio, work_date): the INSERT is the idempotency
arbiter (ON CONFLICT DO NOTHING), so a re-run of the same date only skips.
A new proposal date supersedes older pendings — the job flips every pending
with an earlier work_date to 'expired' before generating.

ADJUDICATION (``decide_proposal``): approved/rejected is a conditional claim
on status='pending' (rowcount-checked, the house idiom) committed in ONE
transaction with the application. Approval applies changes best-effort:
every leg re-checks live state at consume time (the operator-approve
precedent) — resolved forecast, duplicate open security, breached cap,
missing price, insufficient cash each skip that ONE change with a recorded
outcome in ``applied``; nothing is ever priced by guesswork.

PRICING: entry and exit legs both read the latest KNOWN usable adjusted
close <= the decision day through ``paper_book._latest_mark`` (the B6
positive-finite whitelist over ``market_data.get_bars_pit``). Deliberately
NOT the paper book's made_at-frozen entry: the paper book measures call
quality at forecast time, portfolios measure portfolio management — you
trade at approval time. Cash accounting is symmetric for shorts (open
reserves cost = weight * initial_cash; close returns cost + realized, where
realized = signed_return * cost).

VALUATION (``valuation``) fails closed, REVIEW-C3 H3 posture: a position
with no usable mark (deleted security, no bars) has UNKNOWN value — excluded
from total_value and counted in ``n_unpriced`` (a nonzero count flags the
snapshot as a partial statement), never asserted as zero. Valuations are
computed on demand from the PIT store; no snapshot rows are persisted.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import bus, db
from ..util import new_id
from . import paper_book
from .analysts import get_analyst, roster
from .prompts import work_date

log = logging.getLogger("institute.portfolios")

TIERS = ("L1", "L2", "L3")
TIER_SPECS: dict[str, dict[str, Any]] = {
    "L1": {"label": "高确信集中", "max_positions": 5, "weight": 0.20, "min_conviction": 0.70},
    "L2": {"label": "分散", "max_positions": 15, "weight": 0.06, "min_conviction": 0.40},
    "L3": {"label": "观察仓", "max_positions": 30, "weight": 0.02, "min_conviction": None},
}
DEFAULT_INITIAL_CASH = 1_000_000.0
PROPOSAL_STATUSES = ("pending", "approved", "rejected", "expired")
DECISIONS = ("approved", "rejected")
SKIP_CATEGORIES = {"ops"}  # mirrors memory.py: editors organize, they don't make calls


class PortfolioError(ValueError):
    """Validation failure (the API maps this to 400)."""


class TransitionConflict(PortfolioError):
    """Conditional claim lost — the row changed under us (API maps to 409)."""


def _dumps(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False, allow_nan=False)


def tier_for_conviction(conviction: Any) -> str:
    """Route a call into its layer: the highest tier whose conviction floor it
    clears; unknown conviction (NULL) always lands in the L3 watch book."""
    if isinstance(conviction, (int, float)) and not isinstance(conviction, bool):
        if conviction >= TIER_SPECS["L1"]["min_conviction"]:
            return "L1"
        if conviction >= TIER_SPECS["L2"]["min_conviction"]:
            return "L2"
    return "L3"


# ---- portfolio CRUD ----------------------------------------------------------

async def ensure_portfolios(analyst_id: str) -> list[dict[str, Any]]:
    """Create the analyst's L1/L2/L3 trio idempotently and return it.

    UNIQUE(analyst_id, tier) makes the INSERT the arbiter: a concurrent or
    repeated ensure loses the ON CONFLICT DO NOTHING race and changes nothing.
    """
    if get_analyst(analyst_id) is None:
        raise PortfolioError(f"analyst {analyst_id!r} is not in the roster")
    now = bus.now_iso()
    for tier in TIERS:
        await db.execute(
            "INSERT INTO portfolios (id, analyst_id, tier, cash, initial_cash, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(analyst_id, tier) DO NOTHING",
            (new_id(), analyst_id, tier, DEFAULT_INITIAL_CASH, DEFAULT_INITIAL_CASH,
             now, now),
        )
    return await list_portfolios(analyst_id=analyst_id)


async def list_portfolios(analyst_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM portfolios"
    params: list[Any] = []
    if analyst_id:
        sql += " WHERE analyst_id = ?"
        params.append(analyst_id)
    sql += " ORDER BY analyst_id, tier"
    return await db.query(sql, params)


async def get_portfolio(portfolio_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,))
    if row is None:
        return None
    row["positions"] = await db.query(
        "SELECT * FROM portfolio_positions WHERE portfolio_id = ? "
        "ORDER BY opened_at, id",
        (portfolio_id,),
    )
    return row


# ---- proposal generation (zero model calls) -----------------------------------

async def _attributed_open_forecasts(analyst_id: str, now: str) -> list[dict[str, Any]]:
    """The analyst's own tradable open calls: long/short, unexpired, with a
    security, attributed through the 0019 provenance chain. Manual forecasts
    without an extraction row belong to nobody (fails closed)."""
    return await db.query(
        "SELECT f.* FROM forecasts f "
        "JOIN forecast_extraction_items i ON i.forecast_id = f.id "
        "JOIN forecast_extractions e ON e.id = i.extraction_id "
        "WHERE e.analyst_id = ? AND f.status = 'open' "
        "AND f.direction IN ('long','short') AND f.security_id IS NOT NULL "
        "AND f.expires_at > ? ORDER BY f.made_at, f.id",
        (analyst_id, now),
    )


async def _tier_changes(
    portfolio: dict[str, Any],
    candidates: list[dict[str, Any]],
    wd: str,
    now: str,
) -> tuple[list[dict[str, Any]], int]:
    """(changes, n skipped for missing price) for one portfolio.

    Closes come first in the list AND in application order, so the slot
    arithmetic below (closes free slots) is honest. Positions whose security
    was deleted are never proposed for close — closing needs a usable price
    (fails closed); they surface through valuation's n_unpriced instead.
    """
    spec = TIER_SPECS[portfolio["tier"]]
    open_rows = await db.query(
        "SELECT p.*, f.status AS fc_status, f.expires_at AS fc_expires_at "
        "FROM portfolio_positions p JOIN forecasts f ON f.id = p.forecast_id "
        "WHERE p.portfolio_id = ? AND p.status = 'open' ORDER BY p.opened_at, p.id",
        (portfolio["id"],),
    )
    closes: list[dict[str, Any]] = []
    for pos in open_rows:
        if not pos["security_id"]:
            continue  # unpriceable: cannot exit honestly, keep the accountability row
        if pos["fc_status"] != "open":
            reason = f"forecast_{pos['fc_status']}"  # settled | invalid
        elif pos["fc_expires_at"] <= now:
            reason = "forecast_expired"
        else:
            continue
        closes.append({
            "action": "close", "position_id": pos["id"],
            "security_id": pos["security_id"], "reason": reason,
        })

    open_secs = {p["security_id"] for p in open_rows if p["security_id"]}
    held_forecasts = {p["forecast_id"] for p in open_rows}
    slots = spec["max_positions"] - len(open_rows) + len(closes)
    opens: list[dict[str, Any]] = []
    skipped_unpriced = 0
    for fc in candidates:
        if slots <= 0:
            break
        if fc["security_id"] in open_secs or fc["id"] in held_forecasts:
            continue
        mark = await paper_book._latest_mark(fc["security_id"], wd)
        if mark is None:
            skipped_unpriced += 1  # no usable PIT price now — retry next Sunday
            continue
        opens.append({
            "action": "open", "security_id": fc["security_id"],
            "direction": fc["direction"], "forecast_id": fc["id"],
            "conviction": fc["conviction"], "weight": spec["weight"],
            "claim": " ".join(str(fc["claim"]).split())[:200],
        })
        open_secs.add(fc["security_id"])
        slots -= 1
    return closes + opens, skipped_unpriced


def _render_rationale(tier: str, changes: list[dict[str, Any]], skipped_unpriced: int) -> str:
    spec = TIER_SPECS[tier]
    floor = spec["min_conviction"]
    floor_txt = f"conviction ≥ {floor:.2f}" if floor is not None \
        else "其余全部 call（含无 conviction）"
    lines = [
        f"{tier}（{spec['label']}）：{floor_txt}；"
        f"单仓目标权重 {spec['weight']:.0%}，上限 {spec['max_positions']} 仓。"
    ]
    opens = [c for c in changes if c["action"] == "open"]
    closes = [c for c in changes if c["action"] == "close"]
    if closes:
        detail = "、".join(f"{c['security_id']}（{c['reason']}）" for c in closes)
        lines.append(f"拟平仓 {len(closes)}：{detail}")
    if opens:
        detail = "、".join(
            f"{c['security_id']}（{c['direction']}"
            + (f"，conviction {c['conviction']:.2f}" if c["conviction"] is not None else "")
            + "）"
            for c in opens
        )
        lines.append(f"拟开仓 {len(opens)}：{detail}")
    if skipped_unpriced:
        lines.append(f"另有 {skipped_unpriced} 个候选暂无可用 PIT 价格，本轮跳过，下次提案重试。")
    return "\n".join(lines)


async def generate_proposals(wd: str | None = None) -> dict[str, Any]:
    """Draft one proposal per portfolio that has something to do.

    Idempotent per (portfolio, work_date): the conditional INSERT is the
    arbiter — a same-date re-run counts skipped_existing and writes nothing.
    Empty change sets create no proposal (no operator noise). One analyst's
    failure never stops the sweep.
    """
    wd = wd or work_date()
    now = bus.now_iso()
    summary: dict[str, Any] = {
        "work_date": wd, "analysts": 0, "proposals": 0,
        "skipped_existing": 0, "skipped_empty": 0, "skipped_unpriced": 0, "errors": 0,
    }
    for analyst in roster():
        if analyst.category in SKIP_CATEGORIES:
            continue
        summary["analysts"] += 1
        try:
            await ensure_portfolios(analyst.id)
            candidates = await _attributed_open_forecasts(analyst.id, now)
            by_tier: dict[str, list[dict[str, Any]]] = {t: [] for t in TIERS}
            for fc in candidates:
                by_tier[tier_for_conviction(fc["conviction"])].append(fc)
            for pf in await list_portfolios(analyst_id=analyst.id):
                changes, skipped_unpriced = await _tier_changes(
                    pf, by_tier[pf["tier"]], wd, now)
                summary["skipped_unpriced"] += skipped_unpriced
                if not changes:
                    summary["skipped_empty"] += 1
                    continue
                pid = new_id()
                inserted = await db.execute(
                    "INSERT INTO portfolio_proposals (id, portfolio_id, work_date, "
                    "changes, rationale, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,'pending',?,?) "
                    "ON CONFLICT(portfolio_id, work_date) DO NOTHING",
                    (pid, pf["id"], wd, _dumps(changes),
                     _render_rationale(pf["tier"], changes, skipped_unpriced), now, now),
                )
                if not inserted:
                    summary["skipped_existing"] += 1
                    continue
                summary["proposals"] += 1
                await bus.emit("portfolio.proposed", "portfolio_proposal", pid, {
                    "portfolio_id": pf["id"], "analyst_id": analyst.id,
                    "tier": pf["tier"], "work_date": wd, "n_changes": len(changes),
                })
        except Exception:  # noqa: BLE001 - one analyst must not stop the sweep
            summary["errors"] += 1
            log.exception("proposal generation failed for analyst %s", analyst.id)
    return summary


async def sunday_proposer_job(wd: str | None = None) -> dict[str, Any]:
    """The Sun 22:00 SGT job body (scheduler wiring: PATCH-NOTES-PORTFOLIOS.md).

    Zero model quota — pure DB reads/writes over forecasts + the PIT store,
    so the scheduler registration stays ungated (paper-opener/paper-mtm
    class). A new proposal date supersedes older pendings: every pending with
    an earlier work_date flips to 'expired' (one conditional UPDATE) before
    generation; decided history is never touched. Safe to re-run within the
    same date — generation is idempotent per (portfolio, work_date).
    """
    wd = wd or work_date()
    expired = await db.execute(
        "UPDATE portfolio_proposals SET status = 'expired', updated_at = ? "
        "WHERE status = 'pending' AND work_date < ?",
        (bus.now_iso(), wd),
    )
    summary = await generate_proposals(wd)
    summary["expired"] = expired
    if expired:
        log.info("sunday proposer expired %d superseded pending proposal(s)", expired)
    return summary


# ---- proposal reads ------------------------------------------------------------

def _proposal_out(row: dict[str, Any]) -> dict[str, Any]:
    prop = dict(row)
    for key in ("changes", "applied"):
        try:
            prop[key] = json.loads(prop[key])
        except (TypeError, ValueError):  # defensive: rows are written canonical
            pass
    return prop


async def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    row = await db.query_one(
        "SELECT pp.*, p.analyst_id, p.tier FROM portfolio_proposals pp "
        "JOIN portfolios p ON p.id = pp.portfolio_id WHERE pp.id = ?",
        (proposal_id,),
    )
    return _proposal_out(row) if row is not None else None


async def list_proposals(
    status: str | None = None,
    portfolio_id: str | None = None,
    analyst_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if status and status not in PROPOSAL_STATUSES:
        raise PortfolioError(
            f"unknown status {status!r}; allowed: {', '.join(PROPOSAL_STATUSES)}"
        )
    clauses, params = [], []
    if status:
        clauses.append("pp.status = ?")
        params.append(status)
    if portfolio_id:
        clauses.append("pp.portfolio_id = ?")
        params.append(portfolio_id)
    if analyst_id:
        clauses.append("p.analyst_id = ?")
        params.append(analyst_id)
    sql = (
        "SELECT pp.*, p.analyst_id, p.tier FROM portfolio_proposals pp "
        "JOIN portfolios p ON p.id = pp.portfolio_id"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY pp.work_date DESC, p.analyst_id, p.tier LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return [_proposal_out(r) for r in await db.query(sql, params)]


# ---- adjudication ---------------------------------------------------------------

async def _tx_one(conn: Any, sql: str, params: tuple) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row is not None else None


async def _apply_changes(
    conn: Any,
    prop: dict[str, Any],
    changes: list[dict[str, Any]],
    marks: dict[str, tuple[str, float] | None],
    now: str,
) -> list[dict[str, Any]]:
    """Apply an approved proposal inside the caller's transaction.

    Closes run before opens so freed slots and cash are available (the
    generator's slot arithmetic assumes this order). Every leg re-checks live
    state at consume time (the operator approve-gate precedent): a resolved
    forecast, duplicate open security, breached cap, missing price, or
    insufficient cash skips that ONE change with a recorded outcome —
    approval is best-effort per change, and ``applied`` tells the operator
    exactly what landed. Pre-checks are authoritative here: the whole
    application runs in one transaction on SQLite's single writer, with the
    0032 partial unique index as the schema backstop.
    """
    pf = await _tx_one(conn, "SELECT * FROM portfolios WHERE id = ?", (prop["portfolio_id"],))
    if pf is None:  # FK-guaranteed; defensive
        raise PortfolioError(f"portfolio {prop['portfolio_id']} not found")
    spec = TIER_SPECS[pf["tier"]]
    cash = float(pf["cash"])
    row = await _tx_one(
        conn,
        "SELECT COUNT(*) AS n FROM portfolio_positions "
        "WHERE portfolio_id = ? AND status = 'open'",
        (pf["id"],),
    )
    open_count = int(row["n"]) if row else 0

    applied: list[dict[str, Any]] = []
    ordered = [c for c in changes if c.get("action") == "close"] \
        + [c for c in changes if c.get("action") == "open"]
    for ch in ordered:
        out = {k: ch[k] for k in ("action", "security_id", "forecast_id", "position_id")
               if ch.get(k) is not None}
        if ch.get("action") == "close":
            pos = await _tx_one(
                conn, "SELECT * FROM portfolio_positions WHERE id = ?",
                (ch.get("position_id"),),
            )
            mark = marks.get(pos["security_id"]) if pos and pos["security_id"] else None
            ret = paper_book._signed_return(pos["direction"], pos["entry_price"], mark[1]) \
                if pos is not None and mark is not None else None
            if pos is None or pos["status"] != "open":
                out["outcome"] = "skipped_not_open"
            elif ret is None:
                # fails closed: never close at a guessed price — the position
                # stays open and the next proposal retries
                out["outcome"] = "skipped_unpriceable"
            else:
                realized = ret * pos["cost"]
                cur = await conn.execute(
                    "UPDATE portfolio_positions SET status = 'closed', "
                    "close_reason = 'proposal', close_price = ?, realized_pnl = ?, "
                    "closed_at = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                    (mark[1], realized, now, now, pos["id"]),
                )
                closed = cur.rowcount
                await cur.close()
                if closed:
                    cash += pos["cost"] + realized
                    open_count -= 1
                    out.update(outcome="closed", close_price=mark[1], realized_pnl=realized)
                else:
                    out["outcome"] = "skipped_not_open"
        elif ch.get("action") == "open":
            sid = str(ch.get("security_id") or "")
            fc = await _tx_one(
                conn, "SELECT status, expires_at, direction FROM forecasts WHERE id = ?",
                (ch.get("forecast_id"),),
            )
            dup = await _tx_one(
                conn,
                "SELECT id FROM portfolio_positions WHERE portfolio_id = ? "
                "AND security_id = ? AND status = 'open'",
                (pf["id"], sid),
            )
            mark = marks.get(sid)
            cost = float(ch["weight"]) * float(pf["initial_cash"])
            if fc is None or fc["status"] != "open" or fc["expires_at"] <= now:
                out["outcome"] = "skipped_forecast_resolved"  # consume-time re-check
            elif dup is not None:
                out["outcome"] = "skipped_duplicate_security"
            elif open_count >= spec["max_positions"]:
                out["outcome"] = "skipped_cap"
            elif mark is None:
                out["outcome"] = "skipped_no_price"
            elif cost > cash:
                out["outcome"] = "skipped_insufficient_cash"
            else:
                entry_date, entry_price = mark
                quantity = cost / entry_price
                pos_id = new_id()
                await conn.execute(
                    "INSERT INTO portfolio_positions (id, portfolio_id, proposal_id, "
                    "forecast_id, security_id, direction, quantity, cost, entry_date, "
                    "entry_price, status, opened_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                    (pos_id, pf["id"], prop["id"], ch["forecast_id"], sid,
                     fc["direction"], quantity, cost, entry_date, entry_price, now, now),
                )
                cash -= cost
                open_count += 1
                out.update(outcome="opened", position_id=pos_id, entry_date=entry_date,
                           entry_price=entry_price, cost=cost, quantity=quantity)
        else:
            out["outcome"] = "skipped_unknown_action"
        applied.append(out)

    await conn.execute(
        "UPDATE portfolios SET cash = ?, updated_at = ? WHERE id = ?",
        (cash, now, pf["id"]),
    )
    return applied


async def decide_proposal(
    proposal_id: str, decision: str, *, note: str = "",
) -> dict[str, Any] | None:
    """Adjudicate one pending proposal (conditional claim, house idiom).

    The pending->decided flip and the application commit in ONE transaction:
    a lost claim (concurrent decider) raises TransitionConflict and rolls the
    application back, so double-application is impossible. Rejection only
    flips the status. Price legs are read before the transaction (PIT reads
    are pure); every write-side invariant is re-checked inside it.
    """
    if decision not in DECISIONS:
        raise PortfolioError(
            f"unknown decision {decision!r}; allowed: {', '.join(DECISIONS)}"
        )
    prop = await db.query_one(
        "SELECT * FROM portfolio_proposals WHERE id = ?", (proposal_id,))
    if prop is None:
        return None
    if prop["status"] != "pending":
        raise TransitionConflict(
            f"proposal {proposal_id} is {prop['status']!r}, not pending")
    try:
        changes = json.loads(prop["changes"])
    except ValueError:
        raise PortfolioError(f"proposal {proposal_id} carries corrupt changes JSON") from None
    if not isinstance(changes, list):
        raise PortfolioError(f"proposal {proposal_id} changes must be a JSON list")

    now = bus.now_iso()
    wd = work_date()
    marks: dict[str, tuple[str, float] | None] = {}
    if decision == "approved":
        for ch in changes:
            sid = str(ch.get("security_id") or "")
            if sid and sid not in marks:
                marks[sid] = await paper_book._latest_mark(sid, wd)

    applied: list[dict[str, Any]] = []
    async with db.transaction() as conn:  # NB: writes via conn; events after commit
        cur = await conn.execute(
            "UPDATE portfolio_proposals SET status = ?, decision_note = ?, "
            "decided_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (decision, (note or "").strip(), now, now, proposal_id),
        )
        claimed = cur.rowcount
        await cur.close()
        if not claimed:
            raise TransitionConflict(
                f"proposal {proposal_id} changed concurrently; reload and retry")
        if decision == "approved":
            applied = await _apply_changes(conn, prop, changes, marks, now)
            await conn.execute(
                "UPDATE portfolio_proposals SET applied = ? WHERE id = ?",
                (_dumps(applied), proposal_id),
            )
    outcomes: dict[str, int] = {}
    for item in applied:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
    await bus.emit("portfolio.proposal_decided", "portfolio_proposal", proposal_id, {
        "portfolio_id": prop["portfolio_id"], "decision": decision,
        "work_date": prop["work_date"], "outcomes": outcomes,
    })
    return await get_proposal(proposal_id)


# ---- valuation (computed on demand; fails closed, H3 posture) --------------------

async def valuation(portfolio_id: str) -> dict[str, Any] | None:
    """Point-in-time snapshot: cash + Σ priced open-position values.

    Marks are the latest KNOWN usable adjusted close <= today (settlement-time
    knowledge). An unpriceable position (deleted security, no usable bars) is
    EXCLUDED from total_value and counted in n_unpriced — unknown value is
    never asserted as zero; a nonzero n_unpriced flags the snapshot as a
    partial statement. nav = total_value / initial_cash.
    """
    pf = await db.query_one("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,))
    if pf is None:
        return None
    wd = work_date()
    positions = await db.query(
        "SELECT * FROM portfolio_positions WHERE portfolio_id = ? AND status = 'open' "
        "ORDER BY opened_at, id",
        (portfolio_id,),
    )
    total = float(pf["cash"])
    n_unpriced = 0
    detail: list[dict[str, Any]] = []
    for pos in positions:
        mark = await paper_book._latest_mark(pos["security_id"], wd) \
            if pos["security_id"] else None
        ret = paper_book._signed_return(pos["direction"], pos["entry_price"], mark[1]) \
            if mark is not None else None
        if ret is None:
            n_unpriced += 1
            detail.append({**pos, "mark_date": None, "mark_price": None,
                           "value": None, "unrealized_pnl": None})
            continue
        value = pos["cost"] * (1.0 + ret)
        total += value
        detail.append({**pos, "mark_date": mark[0], "mark_price": mark[1],
                       "value": value, "unrealized_pnl": ret * pos["cost"]})
    agg = await db.query_one(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS realized FROM portfolio_positions "
        "WHERE portfolio_id = ? AND status = 'closed' AND realized_pnl IS NOT NULL",
        (portfolio_id,),
    )
    return {
        "portfolio_id": portfolio_id,
        "analyst_id": pf["analyst_id"],
        "tier": pf["tier"],
        "work_date": wd,
        "cash": float(pf["cash"]),
        "positions_value": total - float(pf["cash"]),
        "total_value": total,
        "nav": total / float(pf["initial_cash"]),
        "n_open": len(positions),
        "n_unpriced": n_unpriced,
        "realized_pnl_cum": float(agg["realized"] if agg else 0.0),
        "positions": detail,
    }
