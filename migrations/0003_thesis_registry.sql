-- Thesis registry (card M1-001). Contract: roadmap/07-market-thesis-data-kickoff.md.
-- Rows are truth; market-thesis-data/ is a bootstrap import source, not a live
-- dependency. Times are ISO-8601 UTC strings (bus.now_iso()).

-- ============ theses ============
-- Lanes and theses share one table: a lane is a top-level grouping node
-- (kind='lane', parent_id NULL); theses hang under a lane via parent_id.
CREATE TABLE IF NOT EXISTS theses (
  id          TEXT PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,
  parent_id   TEXT REFERENCES theses(id) ON DELETE SET NULL,
  kind        TEXT NOT NULL DEFAULT 'thesis' CHECK (kind IN ('lane','thesis')),
  title       TEXT NOT NULL,
  view        TEXT NOT NULL DEFAULT '',   -- the current thesis statement
  direction   TEXT NOT NULL DEFAULT 'neutral' CHECK (direction IN ('long','short','neutral','conflicting')),
  status      TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','active','paused','retired','invalidated')),
  tags_json   TEXT NOT NULL DEFAULT '[]',
  meta_json   TEXT NOT NULL DEFAULT '{}', -- practical metadata (import preserves it verbatim)
  source      TEXT NOT NULL DEFAULT 'manual',  -- manual|import
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theses_parent ON theses(parent_id, kind);
CREATE INDEX IF NOT EXISTS idx_theses_status ON theses(status);

-- append-only view history: version 1 is written at create, a new row lands
-- whenever title/view/direction/status changes
CREATE TABLE IF NOT EXISTS thesis_versions (
  id         TEXT PRIMARY KEY,
  thesis_id  TEXT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
  version    INTEGER NOT NULL,
  title      TEXT NOT NULL,
  view       TEXT NOT NULL DEFAULT '',
  direction  TEXT NOT NULL,
  status     TEXT NOT NULL,
  author     TEXT NOT NULL DEFAULT '',   -- operator|import|analyst id
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_versions_seq ON thesis_versions(thesis_id, version);

-- ============ market-thesis-data import provenance ============
CREATE TABLE IF NOT EXISTS market_thesis_import_batches (
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,           -- bundle directory path
  mode          TEXT NOT NULL CHECK (mode IN ('dry_run','apply')),
  status        TEXT NOT NULL CHECK (status IN ('completed','failed')),
  counts_json   TEXT NOT NULL DEFAULT '{}',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  manifest_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS market_thesis_import_items (
  id         TEXT PRIMARY KEY,
  batch_id   TEXT NOT NULL REFERENCES market_thesis_import_batches(id) ON DELETE CASCADE,
  item_type  TEXT NOT NULL CHECK (item_type IN ('lane','thesis','stock','edge')),
  source_id  TEXT NOT NULL,              -- id inside the bundle
  target_id  TEXT,                       -- local row id (NULL when skipped)
  action     TEXT NOT NULL CHECK (action IN ('created','updated','unchanged','skipped')),
  detail     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mti_items_batch ON market_thesis_import_items(batch_id, item_type);
