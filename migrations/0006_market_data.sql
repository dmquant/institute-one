-- Market calendar, PIT price bars, benchmarks, corporate actions (card M4-001).
-- Contract: roadmap/backlog.json M4-001 acceptance (the design doc
-- design/local-thesis-alpha/06-market-data-pit.md is gitignored and absent in
-- this clone). Storage only — fetchers are later cards.
-- Numbered 0006: 0005 is reserved by a parallel card; db.migrate() applies
-- files in sorted order, gaps are fine.
--
-- PIT (point-in-time) convention, shared by price_bars / benchmark_marks /
-- corporate_actions:
--   valid_time   = when the fact is ABOUT (bar's trading day, action's ex-date)
--   as_known_at  = when we LEARNED it (defaults to ingest time unless the
--                  caller backfills a historical revision stream)
-- Version rows are IMMUTABLE. A correction never overwrites: it appends a row
-- with the same natural key and a later as_known_at. On a full version-key
-- collision the domain layer allows only an exact-payload replay (idempotent
-- no-op); different facts under the same key are rejected (409). "What did we
-- know at time T" = for each natural key take the row with MAX(as_known_at)
-- among as_known_at <= T. The UNIQUE index (natural key + as_known_at) is both
-- the insert conflict target and that query's scan index.
-- Times: created_at/updated_at are bus.now_iso() (seconds); PIT columns
-- (valid_time/as_known_at) are domain-normalized to microsecond-precision UTC
-- ISO ('YYYY-MM-DDTHH:MM:SS.ffffff+00:00') — one fixed-width shape, so string
-- order == time order and same-second corrections still get distinct version
-- keys. Calendar-day columns (cal_date/bar_date/mark_date/ex_date/start_date/
-- end_date) are YYYY-MM-DD and GLOB-checked so lexicographic comparison ==
-- date comparison.

-- ============ trading calendar ============
-- Market-level open/closed days (acceptance: "calendar can represent closed
-- and suspended days" — market closure lives here; per-security suspension is
-- security_suspensions below). Natural composite key, no uuid needed.
-- market set mirrors securities.market so every security can find its calendar.
CREATE TABLE IF NOT EXISTS trading_calendar (
  market     TEXT NOT NULL CHECK (market IN ('CN_A','HK','US','GLOBAL_CONTEXT')),
  cal_date   TEXT NOT NULL CHECK (cal_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  is_open    INTEGER NOT NULL CHECK (is_open IN (0,1)),
  note       TEXT,                    -- holiday name / half-day annotation
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (market, cal_date)
);

-- ============ security suspensions ============
-- Per-security halt intervals (inclusive dates). end_date NULL = still
-- suspended. securities.listing_status='suspended' is the current snapshot;
-- this table is the dated history that PIT queries need.
CREATE TABLE IF NOT EXISTS security_suspensions (
  id          TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  start_date  TEXT NOT NULL CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  end_date    TEXT CHECK (end_date IS NULL OR end_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  reason      TEXT NOT NULL DEFAULT '',
  source      TEXT,                   -- free-form origin tag (open set)
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  CHECK (end_date IS NULL OR end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS idx_suspensions_sec ON security_suspensions(security_id, start_date);

-- ============ price bars (PIT) ============
-- freq is an OPEN set ('1d' at launch; intraday later) — additive-only
-- migrations cannot widen a CHECK, so the domain module validates instead.
-- OHLC prices carry no sign CHECK on purpose (futures can print negative);
-- high >= low is the only invariant safe to enforce.
CREATE TABLE IF NOT EXISTS price_bars (
  id            TEXT PRIMARY KEY,
  security_id   TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  freq          TEXT NOT NULL DEFAULT '1d',
  bar_date      TEXT NOT NULL CHECK (bar_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  open          REAL NOT NULL,
  high          REAL NOT NULL,
  low           REAL NOT NULL,
  close         REAL NOT NULL,
  volume        REAL CHECK (volume IS NULL OR volume >= 0),
  adj_factor    REAL NOT NULL DEFAULT 1.0 CHECK (adj_factor > 0),  -- cumulative adjustment; raw*adj_factor = adjusted
  valid_time    TEXT NOT NULL,        -- see PIT convention above
  as_known_at   TEXT NOT NULL,
  source        TEXT,                 -- free-form origin tag (fetcher cards fill this)
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  CHECK (high >= low)
);
-- Natural key + as_known_at: one row per known version of a bar, immutable.
-- Doubles as the insert conflict target (exact-replay dedup) and the PIT scan
-- index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_bars_version
  ON price_bars(security_id, freq, bar_date, as_known_at);

-- ============ benchmarks ============
-- Benchmarks are NOT securities (acceptance: "benchmark marks are separate
-- from securities"): no row in securities is required or implied, and
-- benchmark_marks reference this table only. Forecast settlement (M5) reads
-- marks from here so a thesis can settle against an index without polluting
-- the security master.
CREATE TABLE IF NOT EXISTS benchmarks (
  id            TEXT PRIMARY KEY,     -- local handle, e.g. CSI300 / HSI / SPX
  name_zh       TEXT,
  name_en       TEXT,
  market        TEXT CHECK (market IS NULL OR market IN ('CN_A','HK','US','GLOBAL_CONTEXT')),
  currency      TEXT,
  source        TEXT NOT NULL DEFAULT 'manual',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  CHECK (COALESCE(name_zh, '') <> '' OR COALESCE(name_en, '') <> '')
);

-- Benchmark level per day, PIT-versioned like price_bars (index closes do get
-- restated; settlement must be able to ask "what level did we know then").
CREATE TABLE IF NOT EXISTS benchmark_marks (
  id            TEXT PRIMARY KEY,
  benchmark_id  TEXT NOT NULL REFERENCES benchmarks(id) ON DELETE CASCADE,
  mark_date     TEXT NOT NULL CHECK (mark_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  value         REAL NOT NULL,        -- closing level / mark
  valid_time    TEXT NOT NULL,
  as_known_at   TEXT NOT NULL,
  source        TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_benchmark_marks_version
  ON benchmark_marks(benchmark_id, mark_date, as_known_at);

-- ============ corporate actions (PIT) ============
-- Splits / dividends / etc. with the same dual-time treatment: an announced
-- dividend that later changes amount appends a new as_known_at version.
-- 'other' keeps the type set from locking out exotic actions (additive-only
-- migrations cannot widen the CHECK); details go in metadata_json.
-- Natural key (security, type, ex_date): two same-type actions on one ex-date
-- must be folded into one row (or 'other') — accepted simplification.
CREATE TABLE IF NOT EXISTS corporate_actions (
  id            TEXT PRIMARY KEY,
  security_id   TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  action_type   TEXT NOT NULL CHECK (action_type IN
                  ('split','reverse_split','dividend','bonus_issue','rights_issue','spin_off','merger','delisting','other')),
  ex_date       TEXT NOT NULL CHECK (ex_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  ratio         REAL CHECK (ratio IS NULL OR ratio > 0),  -- shares-out per share-in (split/bonus/rights)
  cash_amount   REAL,                 -- per-share cash (dividend), in `currency`
  currency      TEXT,
  valid_time    TEXT NOT NULL,        -- defaults to ex_date at the domain layer
  as_known_at   TEXT NOT NULL,
  source        TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_corporate_actions_version
  ON corporate_actions(security_id, action_type, ex_date, as_known_at);
