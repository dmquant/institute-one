-- Hand weights + scorecard (ROADMAP Phase 2 "Hand weights + scorecard").
-- Numbered 0009: 0008 is reserved by a parallel card; db.migrate() applies
-- files in sorted order, gaps are fine.
--
-- Three tables, no wiring into resolve() yet (opt-in via
-- registry.pick_weighted_hand — see app/hands/registry.py):
--   hand_weights   operator-set sampling weights per (scope, hand)
--   hand_stats     hourly aggregates over terminal tasks, recomputed
--                  idempotently by scorecard.run_once (rows are overwritten)
--   hand_scorecard per-task quality verdicts from the daily scorecard job
-- Times: created_at/updated_at are bus.now_iso() (UTC ISO, seconds);
-- work_date is the SGT calendar date (prompts.work_date()); window_start is
-- a UTC ISO timestamp truncated to the hour so string order == time order.

-- ============ hand weights ============
-- 'default' is the fallback scope: pick_weighted_hand resolves a hand's
-- weight as scope row -> default row -> 1.0 (missing row = neutral weight).
CREATE TABLE IF NOT EXISTS hand_weights (
  scope      TEXT NOT NULL CHECK (scope IN ('whiteboard','research','daily','mailbox','default')),
  hand       TEXT NOT NULL CHECK (hand <> ''),
  weight     REAL NOT NULL CHECK (weight >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (scope, hand)
);

-- ============ hand stats (hourly windows) ============
-- One row per (hand, hour window). tasks_failed counts failed + expired;
-- cancelled tasks count only toward tasks_total. avg_duration_ms averages
-- finished_at - started_at over the duration_samples rows that have
-- started_at (rate-limited/cancelled rows often don't) — cross-window
-- averages must weight by duration_samples, not tasks_total.
CREATE TABLE IF NOT EXISTS hand_stats (
  hand               TEXT NOT NULL,
  window_start       TEXT NOT NULL,              -- UTC ISO, truncated to the hour
  window_hours       INTEGER NOT NULL DEFAULT 1 CHECK (window_hours > 0),
  tasks_total        INTEGER NOT NULL DEFAULT 0,
  tasks_ok           INTEGER NOT NULL DEFAULT 0,
  tasks_failed       INTEGER NOT NULL DEFAULT 0,
  tasks_rate_limited INTEGER NOT NULL DEFAULT 0,
  duration_samples   INTEGER NOT NULL DEFAULT 0, -- rows behind avg_duration_ms
  avg_duration_ms    REAL,
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (hand, window_start, window_hours)
);
CREATE INDEX IF NOT EXISTS idx_hand_stats_window ON hand_stats(window_start);

-- The daily scorecard sweep filters tasks by status + finished_at; the 0001
-- single-column status index would scan all historic terminal rows (additive
-- index on the 0001 table — old migrations stay untouched).
CREATE INDEX IF NOT EXISTS idx_tasks_status_finished ON tasks(status, finished_at);

-- ============ hand scorecard ============
-- Daily quality verdicts over completed tasks. task_id is UNIQUE so a rerun
-- of the scorecard upserts (re-judges) instead of duplicating rows.
CREATE TABLE IF NOT EXISTS hand_scorecard (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  hand       TEXT NOT NULL,
  work_date  TEXT NOT NULL CHECK (work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  task_id    TEXT NOT NULL UNIQUE,
  verdict    TEXT NOT NULL CHECK (verdict IN ('false_complete','stub','ok')),
  reason     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scorecard_date ON hand_scorecard(work_date, hand);
