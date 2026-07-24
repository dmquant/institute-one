-- Portfolios L1-L3 + Sunday proposer (ROADMAP Phase 5, "Portfolios L1–L3 +
-- Sunday proposer" row). Numbered 0032 by pre-allocation: 0026-0031 are taken
-- by parallel cards; db.migrate() applies files in sorted order, gaps are fine
-- (0013/0014 policy). Times are ISO-8601 UTC strings (bus.now_iso() shape);
-- work_date is an SGT calendar date (prompts.work_date()); entry_date /
-- close-adjacent dates are exchange bar dates (YYYY-MM-DD).

-- ============ portfolios (one row per analyst per tier) ============
-- Three VIRTUAL layers per analyst — the tier is the risk posture, enforced by
-- code constants (portfolios.TIER_SPECS), not by schema:
--   L1  高确信集中: few concentrated slots, high conviction floor, big weight
--   L2  分散:       diversified mid-conviction sleeve, small weights
--   L3  观察仓:     watch book — every remaining call gets a token weight
-- analyst_id is a roster id (catalog/analysts.json) — the roster lives outside
-- the DB, so no FK (analyst_daily / memory precedent). cash only moves through
-- proposal application (open debits cost; close credits cost + realized_pnl);
-- initial_cash is the NAV denominator and the weight-sizing base. The trio is
-- created idempotently (UNIQUE(analyst_id, tier); the INSERT is the arbiter).
CREATE TABLE IF NOT EXISTS portfolios (
  id           TEXT PRIMARY KEY,
  analyst_id   TEXT NOT NULL,
  tier         TEXT NOT NULL CHECK (tier IN ('L1','L2','L3')),
  cash         REAL NOT NULL,
  initial_cash REAL NOT NULL,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  UNIQUE (analyst_id, tier)
);

-- ============ proposals (the Sunday 22:00 SGT rebalance suggestions) ============
-- One proposal per portfolio per proposal date: UNIQUE(portfolio_id, work_date)
-- makes the Sunday job idempotent — a re-run the same date loses the
-- ON CONFLICT DO NOTHING race and skips (A2 spirit: the INSERT is the arbiter).
-- changes is the 拟调仓清单 (canonical JSON list written by the generator):
--   {"action":"open",  "security_id","direction","forecast_id","conviction","weight","claim"}
--   {"action":"close", "position_id","security_id","reason"}
-- Lifecycle: pending -> approved | rejected (operator, conditional claim on
-- status='pending', rowcount-checked) or -> expired (a NEWER proposal date
-- supersedes: the Sunday job flips every pending with an older work_date).
-- applied records the per-change application outcome JSON, written inside the
-- approve transaction — approval is best-effort per change (a change that
-- fails its consume-time re-check is skipped and reported, never guessed).
CREATE TABLE IF NOT EXISTS portfolio_proposals (
  id            TEXT PRIMARY KEY,
  portfolio_id  TEXT NOT NULL REFERENCES portfolios(id),
  work_date     TEXT NOT NULL,
  changes       TEXT NOT NULL DEFAULT '[]',
  rationale     TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','expired')),
  decision_note TEXT NOT NULL DEFAULT '',
  applied       TEXT NOT NULL DEFAULT '[]',
  decided_at    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (portfolio_id, work_date)
);
CREATE INDEX IF NOT EXISTS idx_portfolio_proposals_status
  ON portfolio_proposals(status, work_date);

-- ============ positions (cash-accounted, unlike the nominal paper book) ============
-- Born ONLY from approved proposals at launch (proposal_id/forecast_id NOT
-- NULL — provenance is part of the record). Entry/close prices come from the
-- PIT store through the B6 positive-finite whitelist (paper_book._latest_mark)
-- at DECISION time — portfolios measure portfolio MANAGEMENT (you trade when
-- the operator approves), while the paper book's made_at-frozen entries
-- measure call quality; the two deliberately differ.
--   cost = weight * initial_cash; quantity = cost / entry_price
--   close: realized_pnl = signed_return * cost; cash += cost + realized_pnl
--          (shorts reserve the notional at open, same cash symmetry)
-- security_id mirrors 0013/0017: SET NULL on security delete — the position is
-- an accountability record; an unpriceable position stops valuing (fails
-- closed, excluded from valuation and counted in n_unpriced) instead of
-- blocking the delete. Closing needs a usable price: an unpriceable close is
-- REFUSED at apply time (never closed at a guessed price), so a deleted
-- security's slot stays occupied until data returns (documented limitation).
-- close_reason is an open set, domain-stamped ('proposal' at launch) — no
-- CHECK, because additive migrations cannot widen one (forecasts precedent).
-- The partial unique index gives at-most-one OPEN position per security per
-- portfolio (closed rows leave the index so the security can be re-entered);
-- apply-time pre-checks run inside one transaction (single-writer SQLite), so
-- the index is the schema backstop, not the arbiter.
CREATE TABLE IF NOT EXISTS portfolio_positions (
  id           TEXT PRIMARY KEY,
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
  proposal_id  TEXT NOT NULL REFERENCES portfolio_proposals(id),
  forecast_id  TEXT NOT NULL REFERENCES forecasts(id),
  security_id  TEXT REFERENCES securities(id) ON DELETE SET NULL,
  direction    TEXT NOT NULL CHECK (direction IN ('long','short')),
  quantity     REAL NOT NULL,
  cost         REAL NOT NULL,
  entry_date   TEXT NOT NULL,
  entry_price  REAL NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  opened_at    TEXT NOT NULL,
  closed_at    TEXT,
  close_reason TEXT,
  close_price  REAL,
  realized_pnl REAL,
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_positions_open_security
  ON portfolio_positions(portfolio_id, security_id)
  WHERE status = 'open' AND security_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_portfolio_positions_status
  ON portfolio_positions(portfolio_id, status);
