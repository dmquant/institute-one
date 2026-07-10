-- Thesis registry + market-thesis-data import provenance (card M1-001).
-- Contracts: design/local-thesis-alpha/02-thesis-stock-model.md (lifecycle,
-- version history) and design/local-thesis-alpha/10-market-thesis-data-bootstrap.md
-- (field mapping, provenance tables, validation policy).
-- Securities, security_aliases, and thesis_security_edges are card M2-001 and are
-- deliberately NOT created here; imported stockUniverse / practical blocks persist
-- as JSON (metadata_json / stock_map_json) until the edge tables land.
-- Times are ISO-8601 UTC strings (bus.now_iso()); first_seen/last_seen carry the
-- bundle-provided calendar dates (YYYY-MM-DD).

-- ============ theses ============
-- Lanes are theses with kind='lane' (10-market-thesis-data-bootstrap.md: "keep it
-- simple"); a thesis references its lane (or parent thesis) via parent_id.
-- Mapping: lanes.id/theses.id -> id, laneId -> parent_id, title/lane -> name_zh,
-- titleEn/laneEn -> name_en, direction -> current_view, conviction -> conviction_score,
-- practical.score -> alpha_prior_score, href -> source_href, networkHref ->
-- source_network_href. Every bundle field not mapped to a column above (practical
-- block, directionLabel, analystCount, lane aggregates, topTerms, stockUniverse,
-- investableFocus, counts, ...) is kept verbatim in metadata_json — the importer
-- must never drop fields.
CREATE TABLE IF NOT EXISTS theses (
  id                  TEXT PRIMARY KEY,       -- manual: path-like (ai/gpu); import: bundle id (thesis-029ce03da1, lane id)
  parent_id           TEXT REFERENCES theses(id) ON DELETE SET NULL,
  kind                TEXT NOT NULL DEFAULT 'thesis' CHECK (kind IN ('lane','thesis')),
  slug                TEXT NOT NULL UNIQUE,   -- human handle; importer sets slug = bundle id
  name_zh             TEXT NOT NULL,          -- thesis.title / lanes.lane
  name_en             TEXT,                   -- thesis.titleEn / lanes.laneEn
  status              TEXT NOT NULL CHECK (status IN ('candidate','active','watch','dormant','retired')),
  scope               TEXT NOT NULL DEFAULT '',
  exclusions          TEXT NOT NULL DEFAULT '',
  owner_analyst       TEXT,
  priority            REAL NOT NULL DEFAULT 0,
  confidence          TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('low','medium','high')),
  current_view        TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (current_view IN ('bullish','bearish','neutral','avoid','conflicting','unknown')),
  conviction_score    REAL,                   -- thesis.conviction (0-100)
  alpha_prior_score   REAL,                   -- thesis.practical.score
  first_seen          TEXT,                   -- thesis.firstSeen / lanes.firstSeen
  last_seen           TEXT,                   -- thesis.lastSeen / lanes.lastSeen
  source              TEXT NOT NULL DEFAULT 'manual',  -- free-form origin tag (open set), e.g. manual|market_thesis_import|research
  source_href         TEXT,                   -- thesis.href / lanes.href / stocks.href style public link
  source_network_href TEXT,                   -- thesis.networkHref
  metadata_json       TEXT NOT NULL DEFAULT '{}',
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theses_status ON theses(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_theses_parent ON theses(parent_id);
CREATE INDEX IF NOT EXISTS idx_theses_kind ON theses(kind, status);

-- ============ thesis versions (view history) ============
-- Projection fields on `theses` are the living view; every revision appends a row
-- here (version increments per thesis, supersedes_id -> the row it replaces), so
-- history is never lost. Import seeds version 1 with summary = thesis.coreView.
CREATE TABLE IF NOT EXISTS thesis_versions (
  id             TEXT PRIMARY KEY,
  thesis_id      TEXT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
  version        INTEGER NOT NULL,
  supersedes_id  TEXT REFERENCES thesis_versions(id),
  run_id         TEXT,                        -- workflow_runs.id that produced the revision
  view           TEXT NOT NULL CHECK (view IN ('bullish','bearish','neutral','avoid','conflicting','unknown')),
  confidence     TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('low','medium','high')),
  summary        TEXT NOT NULL,
  drivers_json   TEXT NOT NULL DEFAULT '[]',
  risks_json     TEXT NOT NULL DEFAULT '[]',
  kpis_json      TEXT NOT NULL DEFAULT '[]',
  catalysts_json TEXT NOT NULL DEFAULT '[]',
  stock_map_json TEXT NOT NULL DEFAULT '[]',  -- import: thesis.stockUniverse until M2-001 edges land
  created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_versions_ver ON thesis_versions(thesis_id, version);

-- ============ market-thesis-data import provenance ============
-- One row per import attempt (dry-run or apply) so later bundles can be refreshed
-- or diffed. idempotency_key is set only by applied imports (e.g. derived from
-- schema + generatedAt + bundle_sha256); dry-runs leave it NULL so they can repeat.
-- Uniqueness is enforced by a partial index over COMPLETED rows only (idx_mti_idem):
-- idempotency blocks re-running a completed bundle, never the retry of a failed one.
CREATE TABLE IF NOT EXISTS market_thesis_imports (
  id                      TEXT PRIMARY KEY,
  schema                  TEXT NOT NULL,      -- manifest.schema
  generated_at            TEXT NOT NULL,      -- manifest.generatedAt
  source_schema           TEXT,               -- manifest.sourceSchema
  source_generated_at     TEXT,               -- manifest.sourceGeneratedAt
  source_first_date       TEXT,               -- manifest.stats.sourceDateRange.first
  source_last_date        TEXT,               -- manifest.stats.sourceDateRange.last
  thesis_count            INTEGER NOT NULL DEFAULT 0,
  lane_count              INTEGER NOT NULL DEFAULT 0,
  stock_count             INTEGER NOT NULL DEFAULT 0,
  edge_count              INTEGER NOT NULL DEFAULT 0,
  thesis_stock_edge_count INTEGER NOT NULL DEFAULT 0,
  bundle_sha256           TEXT,
  idempotency_key         TEXT,
  mode                    TEXT NOT NULL DEFAULT 'apply' CHECK (mode IN ('dry_run','apply')),
  status                  TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
  manifest_json           TEXT NOT NULL DEFAULT '{}',  -- full manifest (sourceSummary, files, ...)
  warnings_json           TEXT NOT NULL DEFAULT '[]',  -- warn-but-continue findings
  error                   TEXT,
  imported_at             TEXT NOT NULL,
  finished_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_mti_status ON market_thesis_imports(status, imported_at);
-- Partial unique: only a COMPLETED apply occupies the key, so a failed apply can
-- be retried with the same bundle.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mti_idem
  ON market_thesis_imports(idempotency_key) WHERE status = 'completed';

-- Per-item provenance: what each bundle record became locally.
-- local_id is NULL for failed items (contract relaxation: a failed edge referencing
-- an unknown thesis/ticker has no local counterpart to point at).
CREATE TABLE IF NOT EXISTS market_thesis_import_items (
  id          TEXT PRIMARY KEY,
  import_id   TEXT NOT NULL REFERENCES market_thesis_imports(id) ON DELETE CASCADE,
  item_type   TEXT NOT NULL CHECK (item_type IN ('lane','thesis','stock','edge')),
  external_id TEXT NOT NULL,                  -- bundle id / ticker / edge id
  local_id    TEXT,
  status      TEXT NOT NULL CHECK (status IN ('inserted','updated','skipped','failed')),
  message     TEXT,
  created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mti_items_ext ON market_thesis_import_items(import_id, item_type, external_id);
CREATE INDEX IF NOT EXISTS idx_mti_items_status ON market_thesis_import_items(import_id, status);
