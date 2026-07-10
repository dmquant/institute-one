-- Security master + aliases + thesis-security edges (card M2-001).
-- Contracts: design/local-thesis-alpha/02-thesis-stock-model.md (security model,
-- alias layer, thesis-security edge, role taxonomy) and
-- design/local-thesis-alpha/10-market-thesis-data-bootstrap.md (market
-- normalization table, bucket mapping, provenance).
-- Schema only — the domain module, API, and importer are later cards.
-- Times are ISO-8601 UTC strings (bus.now_iso()).

-- ============ securities ============
-- Canonical id = suffixed ticker (02-thesis-stock-model.md): 688256.SH /
-- 000001.SZ / 830799.BJ (CN_A), 0700.HK (HK), NVDA.US (US — the bundle ships US
-- tickers unsuffixed; the importer appends .US). Context-only markets keep their
-- native vendor suffix (005930.KS, 6954.T) and land as GLOBAL_CONTEXT.
-- Market normalization (10-market-thesis-data-bootstrap.md; exact bundle values
-- observed in market-thesis-data/stocks.json):
--   A-share     -> CN_A                          (75 rows)
--   A-share ETF -> CN_A + instrument_type 'ETF'   (5)
--   HK          -> HK                             (26)
--   HK ETF      -> HK   + instrument_type 'ETF'   (2)
--   US          -> US                             (96)
--   US ETF      -> US   + instrument_type 'ETF'   (21)
--   US ADR      -> US   + instrument_type 'ADR'   (8)
--   Korea/Japan -> GLOBAL_CONTEXT                 (3)
-- Raw bundle market strings must NOT be stored — the CHECK rejects them.
-- The bundle only carries ticker/name/market, so exchange/currency/board are
-- nullable derived enrichments (02 doc has them NOT NULL; relaxed for import).
CREATE TABLE IF NOT EXISTS securities (
  id              TEXT PRIMARY KEY,       -- canonical suffixed ticker (see above)
  symbol          TEXT NOT NULL,          -- unsuffixed ticker: 688256, 0700, NVDA
  market          TEXT NOT NULL CHECK (market IN ('CN_A','HK','US','GLOBAL_CONTEXT')),
  instrument_type TEXT NOT NULL DEFAULT 'stock' CHECK (instrument_type IN ('stock','ETF','ADR')),
  exchange        TEXT,                   -- SSE/SZSE/BSE/HKEX/Nasdaq/NYSE/...
  name_zh         TEXT,                   -- bundle `name` is zh or en depending on script
  name_en         TEXT,
  currency        TEXT,                   -- CNY/HKD/USD/... derivable from market
  board           TEXT,                   -- STAR, ChiNext, Main, ...
  listing_status  TEXT NOT NULL DEFAULT 'active' CHECK (listing_status IN ('active','suspended','delisted')),
  company_key     TEXT,                   -- groups cross-listings of one company
  source          TEXT NOT NULL DEFAULT 'manual',  -- free-form origin tag (open set), e.g. manual|market_thesis_import
  source_href     TEXT,                   -- stocks[].href
  metadata_json   TEXT NOT NULL DEFAULT '{}',      -- thesisCount, laneCount, lanes, ... never drop bundle fields
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  CHECK (COALESCE(name_zh, '') <> '' OR COALESCE(name_en, '') <> ''),  -- at least one non-empty name
  CHECK (id GLOB (symbol || '.?*')),  -- id must be symbol + a suffix
  -- Canonical suffix must agree with market (GLOB: case-sensitive, whole-string).
  -- Only the suffix is case-enforced — 'nvda.US' passes; the importer owns
  -- ticker normalization. GLOBAL_CONTEXT is the open-set catch-all for native
  -- vendor suffixes, but the reserved canonical suffixes are excluded so a
  -- mislabeled CN_A/HK/US id cannot hide under it.
  CHECK (
    (market = 'CN_A' AND (id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].SH'
                       OR id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].SZ'
                       OR id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].BJ'))
    OR (market = 'HK' AND id GLOB '[0-9]*.HK')
    OR (market = 'US' AND id GLOB '?*.US')
    OR (market = 'GLOBAL_CONTEXT' AND id GLOB '?*.?*'
        AND id NOT GLOB '*.SH' AND id NOT GLOB '*.SZ' AND id NOT GLOB '*.BJ'
        AND id NOT GLOB '*.HK' AND id NOT GLOB '*.US')
  )
);
CREATE INDEX IF NOT EXISTS idx_securities_market ON securities(market, instrument_type);
CREATE INDEX IF NOT EXISTS idx_securities_symbol ON securities(symbol);

-- ============ security aliases ============
-- Lookup layer for Chinese names, English names, unsuffixed tickers, ADR /
-- H-share handles, and common abbreviations (02-thesis-stock-model.md).
-- Contract: within one kind an alias resolves to exactly one security
-- (UNIQUE(alias, kind)); the same alias TEXT may recur under a different kind
-- (e.g. "0700" as an unsuffixed ticker and as an abbreviation elsewhere), so
-- the unique key is (alias, kind), not alias alone.
-- IMPORTER WARNING: the bundle ships duplicate zh names for cross-listings —
-- 中芯国际 (688981.SH and 0981.HK) and 中远海控 (601919.SH and 1919.HK) — so a
-- naive per-row name_zh alias insert hits UNIQUE(alias, kind) mid-import. The
-- importer must warn-and-skip the duplicate (or attach the alias to only one
-- listing); company_key on securities is the intended cross-listing grouper.
CREATE TABLE IF NOT EXISTS security_aliases (
  id          TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  alias       TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('name_zh','name_en','ticker','adr','h_share','abbreviation')),
  source      TEXT,                       -- free-form origin tag, e.g. market_thesis_import
  created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_security_aliases_alias ON security_aliases(alias, kind);
CREATE INDEX IF NOT EXISTS idx_security_aliases_sec ON security_aliases(security_id);

-- ============ thesis-security edges ============
-- The investable map: "this security is relevant to this thesis and should be
-- tracked" — never "buy" (10-market-thesis-data-bootstrap.md).
-- role is an OPEN set: local taxonomy values (pure_play|supplier|customer|proxy|
-- hedge|global_leader|read_through|competitor per 02-thesis-stock-model.md) AND
-- raw bundle labels (free-text Chinese such as 中长久期代理) — the bootstrap doc
-- says warn-but-keep labels that do not map cleanly, so no CHECK on role.
-- bucket CHECK set = exact values observed in thesis_stock_edges.csv
-- (core 940 / peer 49 / watch 28 / hedge 3); NULL for manual edges.
-- weight keeps the raw bundle weight; exposure is its normalized 0..1 projection.
CREATE TABLE IF NOT EXISTS thesis_security_edges (
  id            TEXT PRIMARY KEY,
  thesis_id     TEXT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
  security_id   TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  role          TEXT NOT NULL,
  bucket        TEXT CHECK (bucket IN ('core','watch','peer','hedge')),
  exposure      REAL NOT NULL DEFAULT 0.5 CHECK (exposure >= 0.0 AND exposure <= 1.0),
  confidence    TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('low','medium','high')),
  rationale     TEXT NOT NULL DEFAULT '',
  weight        REAL,                     -- raw thesis_stock_edges.csv weight (exposure proxy input)
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  source_run_id TEXT,                     -- workflow_runs.id that asserted the edge
  import_id     TEXT REFERENCES market_thesis_imports(id) ON DELETE SET NULL,  -- provenance (0003)
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tse_unique ON thesis_security_edges(thesis_id, security_id, role);
CREATE INDEX IF NOT EXISTS idx_tse_thesis ON thesis_security_edges(thesis_id, status);
CREATE INDEX IF NOT EXISTS idx_tse_security ON thesis_security_edges(security_id, status);
CREATE INDEX IF NOT EXISTS idx_tse_import ON thesis_security_edges(import_id);  -- per-bundle provenance (diff/refresh), like idx_mti_items in 0003
