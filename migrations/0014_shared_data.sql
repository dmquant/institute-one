-- shared_data: rendered research data bundles, keyed (topic, work_date) (Phase 1b, card B5).
-- Numbered 0014: 0008-0013 are reserved by parallel round-2 cards; db.migrate()
-- applies files in sorted order, gaps are fine.
--
-- Semantics decision (ROADMAP 1b says "(topic, work_date) upsert into shared_data";
-- 0006 never created it): this is NOT a PIT table. The point-in-time truth for
-- prices lives in price_bars / benchmark_marks (0006, immutable versions); a
-- shared_data row is a rendered PROJECTION of that truth — the latest
-- ${DATA_BUNDLE} text rendered for that topic on that work date — so plain
-- (topic, work_date) upsert-in-place is the correct semantics: re-rendering the
-- same topic on the same day refreshes the row instead of stacking versions.
-- It is the cache behind GET /api/data/{topic}/latest; the exact per-run
-- injection audit lives in workflow_runs.variables["DATA_BUNDLE"], not here
-- (a same-day re-render overwrites this row).
-- Times are ISO-8601 UTC (bus.now_iso()); work_date is the SGT work date.
CREATE TABLE IF NOT EXISTS shared_data (
  id            TEXT PRIMARY KEY,
  topic         TEXT NOT NULL,
  work_date     TEXT NOT NULL CHECK (work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  content       TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',   -- matched security ids, quote sources, byte size, ...
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (topic, work_date)
);
CREATE INDEX IF NOT EXISTS idx_shared_data_topic ON shared_data(topic, work_date);
