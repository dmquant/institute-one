-- Security master (card M2-001). Canonical ids carry a market suffix:
-- "600519.CN_A", "0700.HK", "NVDA.US". market-thesis-data market labels
-- ("A-share", "US ETF", "US ADR", …) normalize into (market, instrument_type).

CREATE TABLE IF NOT EXISTS securities (
  id              TEXT PRIMARY KEY,     -- canonical: <TICKER>.<MARKET>
  ticker          TEXT NOT NULL,        -- unsuffixed exchange ticker
  market          TEXT NOT NULL CHECK (market IN ('CN_A','HK','US','KR','JP')),
  instrument_type TEXT NOT NULL DEFAULT 'stock' CHECK (instrument_type IN ('stock','etf','adr')),
  name            TEXT NOT NULL,        -- zh display name when available
  name_en         TEXT NOT NULL DEFAULT '',
  meta_json       TEXT NOT NULL DEFAULT '{}',
  source          TEXT NOT NULL DEFAULT 'manual',  -- manual|import
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_securities_ticker ON securities(ticker);
CREATE INDEX IF NOT EXISTS idx_securities_market ON securities(market, instrument_type);

-- alias -> security lookup (Chinese names, unsuffixed tickers, bundle ids …)
CREATE TABLE IF NOT EXISTS security_aliases (
  id          TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  alias       TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('name_zh','name_en','ticker','bundle_id','other')),
  created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sec_alias_unique ON security_aliases(security_id, kind, alias);
CREATE INDEX IF NOT EXISTS idx_sec_alias_lookup ON security_aliases(alias);

-- thesis <-> security edges
CREATE TABLE IF NOT EXISTS thesis_security_edges (
  id          TEXT PRIMARY KEY,
  thesis_id   TEXT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  role        TEXT NOT NULL DEFAULT 'exposure',  -- free-form: core|proxy|hedge|supplier|…
  exposure    TEXT NOT NULL DEFAULT '',          -- how the thesis expresses through this name
  confidence  REAL,                              -- 0..1 when the source provides one
  rationale   TEXT NOT NULL DEFAULT '',
  meta_json   TEXT NOT NULL DEFAULT '{}',
  source      TEXT NOT NULL DEFAULT 'manual',    -- manual|import
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tse_unique ON thesis_security_edges(thesis_id, security_id, role);
CREATE INDEX IF NOT EXISTS idx_tse_security ON thesis_security_edges(security_id);
