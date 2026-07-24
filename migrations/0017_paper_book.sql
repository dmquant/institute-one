-- Forecast extraction + paper book (Phase 5 items 1-2, card C3).
-- Numbered 0017: 0015/0016 are reserved by parallel third-round cards;
-- db.migrate() applies files in sorted order, gaps are fine (same policy as
-- 0013/0014). Times are ISO-8601 UTC strings (bus.now_iso() shape, second
-- precision); work_date / entry_date / mark-adjacent dates are calendar
-- YYYY-MM-DD strings (SGT work dates for nav_history, exchange bar dates for
-- entry_date).

-- ============ forecast extractions (source-level idempotency claim) ============
-- One row per SOURCE TEXT the regex extractor has processed (source_ref =
-- 'research:<queue_item_id>' | 'workflow:<run_id>'). The INSERT on source_ref
-- is the arbiter (A2 spirit): a handler re-fired for the same source loses the
-- ON CONFLICT DO NOTHING race and skips — re-emitted events / manual replays
-- can never double-extract forecasts from the same text. n_forecasts /
-- forecast_ids are bookkeeping written after the create_forecast() calls; a
-- crash between claim and creation leaves a claimed-but-empty row (documented:
-- re-running will NOT retry that source — operator can DELETE the row to
-- force a re-extract).
CREATE TABLE IF NOT EXISTS forecast_extractions (
  id           TEXT PRIMARY KEY,
  source_ref   TEXT NOT NULL UNIQUE,
  source_kind  TEXT NOT NULL,               -- open set (research|daily|manual|...), domain-stamped
  n_candidates INTEGER NOT NULL DEFAULT 0,  -- regex candidates found in the text
  n_forecasts  INTEGER NOT NULL DEFAULT 0,  -- forecasts actually created
  forecast_ids TEXT NOT NULL DEFAULT '[]',  -- JSON list of created forecast ids
  detail       TEXT NOT NULL DEFAULT '',    -- why candidates were dropped, etc.
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- ============ paper positions ============
-- One virtual position per forecast. Nominal size 1.0 — the book measures
-- calls, not capital. direction is long/short only: neutral forecasts are
-- never opened (nothing to trade). entry_price is the B6 knowledge-time
-- entry: the last adjusted close at or before made_at's calendar date AS
-- KNOWN AT made_at (PIT as_of = made_at) — corrections ingested after
-- made_at can never rewrite it (no look-ahead).
-- security_id mirrors 0013's posture: SET NULL on security delete — the
-- position is an accountability record; an unpriceable position stops
-- marking (fails closed) instead of blocking the delete.
-- close_reason='unpriced' (REVIEW-C3 H3) records an EXPIRED position that
-- could not be priced: close_price AND realized_pnl stay NULL — "unknown" is
-- never asserted as a number; NAV aggregation skips NULL realized_pnl and
-- surfaces the gap through nav_history.n_unpriced instead.
-- Concurrency (REVIEW-C3 M3): the database is the arbiter, not the opener's
-- pre-reads — UNIQUE(forecast_id) gives one-position-per-forecast, the
-- partial unique index below gives at-most-one OPEN position per security
-- (closed rows leave the index so the security can be re-entered), and the
-- opener's INSERT carries the cap check in its own WHERE (conditional
-- insert, B6/0012 "the INSERT is the arbiter" precedent).
CREATE TABLE IF NOT EXISTS paper_positions (
  id           TEXT PRIMARY KEY,
  forecast_id  TEXT NOT NULL REFERENCES forecasts(id),
  security_id  TEXT REFERENCES securities(id) ON DELETE SET NULL,
  direction    TEXT NOT NULL CHECK (direction IN ('long','short')),
  entry_date   TEXT NOT NULL,               -- bar_date of the frozen entry bar
  entry_price  REAL NOT NULL,               -- adjusted close, positive finite (domain-gated)
  size         REAL NOT NULL DEFAULT 1.0,   -- nominal notional
  stop_pct     REAL NOT NULL,               -- signed-return floor (fraction > 0)
  target_pct   REAL NOT NULL,               -- signed-return take-profit (fraction > 0)
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  opened_at    TEXT NOT NULL,
  closed_at    TEXT,
  close_reason TEXT CHECK (close_reason IN ('stop','target','horizon','manual','unpriced')),
  close_price  REAL,                        -- NULL only when close_reason='unpriced'
  realized_pnl REAL,                        -- signed_return * size at close; NULL = unknown (unpriced)
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_forecast ON paper_positions(forecast_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_open_security
  ON paper_positions(security_id) WHERE status = 'open' AND security_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, security_id);

-- ============ nav history ============
-- One row per SGT work date, written by the 00:00 SGT MTM job (idempotent
-- upsert — a manual re-run refreshes the same row).
--   nav = 1.0 + Σ realized_pnl over PRICED closed positions (realized_pnl
--         NOT NULL) + Σ unrealized over priceable open positions.
-- n_unpriced (REVIEW-C3 H3) is the completeness flag: how many positions
-- (open unpriceable + closed 'unpriced') carry UNKNOWN value and are
-- excluded from nav — a nonzero count means nav is a partial statement,
-- never that the unknowns were worth zero.
-- benchmark_nav is the CSI300 mark normalized to the book's benchmark base
-- (admin_state key 'paper_book:benchmark_base', pinned on first sight of a
-- usable mark); NULL whenever no usable mark is known — fails closed, the
-- column never guesses.
CREATE TABLE IF NOT EXISTS nav_history (
  work_date        TEXT PRIMARY KEY,
  nav              REAL NOT NULL,
  benchmark_nav    REAL,
  gross_exposure   REAL NOT NULL DEFAULT 0,
  n_open           INTEGER NOT NULL DEFAULT 0,
  n_unpriced       INTEGER NOT NULL DEFAULT 0,
  realized_pnl_cum REAL NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

-- ============ operator config (0011 idiom: ONE admin_state JSON row) ============
-- The code keeps built-in defaults (max_positions 20), so deleting or
-- corrupting this row degrades gracefully.
INSERT OR IGNORE INTO admin_state (key, value) VALUES (
  'paper_book',
  '{"max_positions": 20}'
);
