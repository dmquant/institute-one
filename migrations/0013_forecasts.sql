-- Forecast ledger + settlements (card M5-001).
-- Contract: roadmap/backlog.json M5-001 acceptance (the design doc
-- design/local-thesis-alpha/04-alpha-portfolio-loop.md is gitignored and
-- absent in this clone). Numbered 0013: 0008-0012 are reserved by parallel
-- second-round cards; db.migrate() applies files in sorted order, gaps are
-- fine. Times are ISO-8601 UTC strings (bus.now_iso() shape, second
-- precision); made_at/expires_at share that shape so string order == time
-- order for the settle-after-expiry gate.

-- ============ forecasts ============
-- A forecast is a falsifiable call: thesis + claim + direction + horizon +
-- a deterministic settlement_rule. settlement_rule is canonical JSON,
-- validated and normalized by the domain layer (app/institute/forecasts.py):
--   {"type": "absolute_move",      "threshold": <fraction > 0>}
--   {"type": "price_vs_benchmark", "threshold": <fraction > 0>, "benchmark_id": "CSI300"}
-- No CHECK on the rule payload or on conviction range (convention 0..1,
-- probability-style) — additive-only migrations cannot widen a CHECK, so open
-- sets are domain-validated (same policy as price_bars.freq in 0006).
-- thesis_id has NO ON DELETE action on purpose: the ledger is an
-- accountability record — deleting a thesis with forecasts must fail (FK
-- violation) rather than silently erase or orphan the track record.
-- security_id is nullable (a thesis-level macro claim may not name a single
-- security; current rule types require it — enforced at create) and SET NULL
-- on delete: settlement then fails closed to 'invalid', never guesses.
CREATE TABLE IF NOT EXISTS forecasts (
  id              TEXT PRIMARY KEY,
  thesis_id       TEXT NOT NULL REFERENCES theses(id),
  security_id     TEXT REFERENCES securities(id) ON DELETE SET NULL,
  claim           TEXT NOT NULL,
  direction       TEXT NOT NULL CHECK (direction IN ('long','short','neutral')),
  conviction      REAL,
  horizon_days    INTEGER NOT NULL CHECK (horizon_days > 0),
  settlement_rule TEXT NOT NULL,
  made_at         TEXT NOT NULL,
  expires_at      TEXT NOT NULL,      -- made_at + horizon_days (domain-computed)
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','settled','invalid')),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecasts_thesis ON forecasts(thesis_id, status);
CREATE INDEX IF NOT EXISTS idx_forecasts_status ON forecasts(status, expires_at);

-- ============ forecast settlements ============
-- Exactly one settlement per forecast. The domain claims status='open'
-- conditionally and inserts the settlement in the SAME transaction, so a lost
-- claim (concurrent settler) rolls back cleanly; the UNIQUE index backstops
-- that invariant at the schema level. verdict='invalid' records a settlement
-- that FAILED CLOSED (required price/benchmark data missing or unusable) —
-- benchmark_return/actual_return stay NULL for whatever could not be
-- computed, and note says why.
CREATE TABLE IF NOT EXISTS forecast_settlements (
  id               TEXT PRIMARY KEY,
  forecast_id      TEXT NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
  verdict          TEXT NOT NULL CHECK (verdict IN ('hit','miss','partial','invalid')),
  settled_at       TEXT NOT NULL,
  benchmark_return REAL,
  actual_return    REAL,
  note             TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_settlements_forecast
  ON forecast_settlements(forecast_id);
