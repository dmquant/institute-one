-- Paper book / forecast extraction hardening (round 4, card D2).
-- Numbered 0019 by pre-allocation: 0017 is ALREADY APPLIED to the production
-- DB and must never be edited — every schema change here rides a NEW file.
-- db.migrate() applies files in sorted order (0013/0014 gap policy).
-- Times are ISO-8601 UTC strings (bus.now_iso() shape).

-- ============ M2: crash-consistent extraction state machine ============
-- 0017 documented the crash hole itself: the source claim committed first,
-- forecasts were created one by one afterwards, and a crash in between left a
-- claimed-but-empty row that an ordinary replay could never resume (duplicate)
-- while the documented DELETE-and-re-extract escape hatch DUPLICATED the
-- already-created part. The claim row now carries an explicit state:
--   pending   claimed, candidate work possibly unfinished — a replay of the
--             same source_ref RESUMES it (skipping candidates already
--             recorded in forecast_extraction_items below)
--   complete  every candidate has been decided; replays are duplicates again
-- Pre-0019 rows default to 'complete': they were written by the old code
-- path and must not be re-extracted by the new resume logic.
ALTER TABLE forecast_extractions ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'
  CHECK (status IN ('pending','complete'));

-- ============ M5: extraction provenance (analyst attribution) ============
-- The author of the SOURCE ARTIFACT the forecasts were extracted from — the
-- analyst of the last non-ops step of the source workflow run (compilers/
-- editors organize, they do not originate calls; ops analysts carry no field
-- memory — memory.SKIP_CATEGORIES). NULL = attribution unknown (manual
-- sources, vanished runs): paper outcomes then simply do not flow back.
ALTER TABLE forecast_extractions ADD COLUMN analyst_id TEXT;

-- ============ M2: per-candidate idempotency claims ============
-- One row per (extraction, candidate security): the INSERT on the primary
-- key is the per-candidate arbiter (A2 spirit), so a resumed or concurrent
-- processing of the same source can never double-create a candidate's
-- forecast. forecast_id is back-filled after create_forecast() succeeds:
--   forecast_id NOT NULL   candidate done — resume returns it, never re-creates
--   forecast_id NULL       claimed but IN DOUBT (crash landed exactly between
--                          create and back-fill) — resume SKIPS it (fails
--                          closed, never risks a duplicate) and reports it in
--                          detail; the operator escape hatch is now surgical:
--                          check the forecasts table, DELETE this ONE item
--                          row (not the whole claim), flip the claim back to
--                          'pending' and replay.
-- Validation-refused candidates (ForecastError) release their claim row so
-- the refusal is re-evaluated on a later resume instead of reading as doubt.
CREATE TABLE IF NOT EXISTS forecast_extraction_items (
  extraction_id TEXT NOT NULL REFERENCES forecast_extractions(id) ON DELETE CASCADE,
  security_id   TEXT NOT NULL,
  forecast_id   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (extraction_id, security_id)
);
-- reverse lookup: paper_book.closed attribution resolves forecast -> analyst
-- through items -> extraction. Partial UNIQUE: a forecast is created by
-- exactly one candidate slot, ever (NULL in-flight rows stay out).
CREATE UNIQUE INDEX IF NOT EXISTS idx_extraction_items_forecast
  ON forecast_extraction_items(forecast_id) WHERE forecast_id IS NOT NULL;
