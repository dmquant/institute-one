-- Forecast integrity hardening (audit fixes: settlement evidence chain,
-- extraction exactly-once, backfill provenance).
-- Numbered 0033: 0032 is the latest applied file; db.migrate() applies files
-- in sorted order, gaps are fine. Additive only — no BEGIN/COMMIT/ATTACH/
-- VACUUM (each file runs as ONE transaction, tests/test_db_migrate.py
-- enforces this). Times: created_at/updated_at stay bus.now_iso() (seconds);
-- PIT knowledge timestamps (knowledge_as_of / *_as_known_at) use the 0006
-- microsecond-precision UTC shape so string order == time order against the
-- price_bars/benchmark_marks version keys.

-- ============ forecasts: creation provenance ============
-- origin marks HOW the row entered the ledger. 'standard' = recorded at (or
-- within the API's now±24h tolerance of) knowledge time; 'backfill' = the
-- caller explicitly declared a historical import through the privileged
-- backfill=true API field. Backfill rows are an accountability record, NOT
-- performance: list_forecasts excludes them by default (the SPA dashboard and
-- Obsidian plugin hit-rate aggregations read that default), and the paper
-- book never opens them. No CHECK on purpose: additive-only migrations cannot
-- widen a CHECK, so the open set is domain-validated (0006 `source` policy);
-- pre-0033 rows default to 'standard' (they were created live).
ALTER TABLE forecasts ADD COLUMN origin TEXT NOT NULL DEFAULT 'standard';

-- ============ forecast_settlements: evidence chain ============
-- knowledge_as_of is the settlement's single knowledge cutoff: a
-- microsecond-precision UTC timestamp fixed by the SYSTEM at settle time
-- (callers can no longer choose it) and passed explicitly to every exit-leg
-- PIT read, so all price reads of one settlement answer "what did we know at
-- exactly this instant" from one consistent snapshot. Entry legs stay frozen
-- at made_at (the anti-look-ahead contract) — their evidence columns record
-- which version that was. Each leg persists the version identity of the
-- exact PIT row used: (date, as_known_at) is the 0006 version key
-- (security_id/freq resp. benchmark_id come from the forecast row/rule).
-- NULL = that leg was never resolved (fails-closed 'invalid' settlements,
-- absolute_move rules without a benchmark) or the row predates 0033.
ALTER TABLE forecast_settlements ADD COLUMN knowledge_as_of TEXT;
ALTER TABLE forecast_settlements ADD COLUMN entry_bar_date TEXT;
ALTER TABLE forecast_settlements ADD COLUMN entry_as_known_at TEXT;
ALTER TABLE forecast_settlements ADD COLUMN exit_bar_date TEXT;
ALTER TABLE forecast_settlements ADD COLUMN exit_as_known_at TEXT;
ALTER TABLE forecast_settlements ADD COLUMN bench_entry_date TEXT;
ALTER TABLE forecast_settlements ADD COLUMN bench_entry_as_known_at TEXT;
ALTER TABLE forecast_settlements ADD COLUMN bench_exit_date TEXT;
ALTER TABLE forecast_settlements ADD COLUMN bench_exit_as_known_at TEXT;

-- ============ forecast_extractions: content-bound idempotency ============
-- text_sha256 binds the source_ref claim to the exact bytes it was extracted
-- from: a replay carrying DIFFERENT content under the same source_ref is
-- refused with a readable error instead of silently resuming/duplicating.
-- made_at freezes the knowledge time for EVERY candidate of the source at
-- first claim, so a crash-resume can never re-create a candidate with a
-- different made_at than its siblings got before the crash.
-- Both NULL for pre-0033 rows (unverifiable — resume then accepts and
-- back-fills the hash rather than bricking legacy claims).
--
-- NOTE (no DDL of its own): forecast_extraction_items.forecast_id (0019)
-- changes meaning with this release. It is now written AT CLAIM TIME with a
-- pre-generated deterministic id (sha256 of extraction_id|security_id,
-- 12 hex) and create_forecast() is called WITH that id — the forecasts
-- primary key arbitrates replays, so the 0019 "claimed-but-NULL = in doubt,
-- skip" crash window is gone. A NULL forecast_id can only be a pre-0033
-- legacy claim; the resume path still fails closed on those.
ALTER TABLE forecast_extractions ADD COLUMN text_sha256 TEXT;
ALTER TABLE forecast_extractions ADD COLUMN made_at TEXT;
