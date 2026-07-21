-- Executor depth (ROADMAP Phase 2): add 'overcommitted' to the tasks.status
-- enum. The per-hand queue-depth cap (settings.hand_queue_depth) fast-fails a
-- submit/spawn into this born-terminal status instead of queueing without
-- bound; the row itself is the audit trail (hand stays NULL — it never ran).
--
-- SQLite cannot alter a CHECK constraint, so this is the standard table
-- rebuild: create the replacement table, copy every row with an explicit
-- column list, drop the old table, rename, recreate every index (0001
-- idx_tasks_* + 0009 idx_tasks_status_finished + 0024 uq_tasks_lineage_active).
-- db.migrate() runs the whole file as ONE transaction, so a crash rolls the
-- rebuild back atomically; IF NOT EXISTS keeps a manual replay harmless.
-- Nothing references tasks by foreign key and no triggers/views exist on it,
-- so DROP + RENAME are contained. Column list verified against the live
-- schema (PRAGMA table_info(tasks)): 0001 columns in order, then the two
-- 0024 columns. fallback_chain/lineage_root stay bare TEXT so a pre-atomic
-- replay of 0024's ADD COLUMN still certifies against this stored definition
-- (app/db.py _skip_add_column proves declarations against sqlite_master).
CREATE TABLE IF NOT EXISTS tasks_rebuild_0028 (
  id             TEXT PRIMARY KEY,
  session_id     TEXT,
  hand           TEXT,                -- hand that actually ran (after fallback)
  requested_hand TEXT,                -- hand the caller asked for
  model          TEXT,
  prompt         TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','rate_limited','cancelled','expired','overcommitted')),
  source         TEXT NOT NULL DEFAULT 'api',   -- api|workflow|whiteboard|mailbox|research|daily|obsidian|mcp|test
  exit_code      INTEGER,
  output         TEXT,
  error          TEXT,
  artifacts      TEXT,                -- JSON list of workspace-relative paths
  tried          TEXT,                -- JSON list of hands attempted
  parent_run_id  TEXT,                -- workflow_runs.id when part of a workflow
  workspace_dir  TEXT,
  timeout_s      INTEGER,
  created_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT,
  fallback_chain TEXT,
  lineage_root   TEXT
);

INSERT INTO tasks_rebuild_0028 (
  id, session_id, hand, requested_hand, model, prompt, status, source,
  exit_code, output, error, artifacts, tried, parent_run_id, workspace_dir,
  timeout_s, created_at, started_at, finished_at, fallback_chain, lineage_root
)
SELECT
  id, session_id, hand, requested_hand, model, prompt, status, source,
  exit_code, output, error, artifacts, tried, parent_run_id, workspace_dir,
  timeout_s, created_at, started_at, finished_at, fallback_chain, lineage_root
FROM tasks;

DROP TABLE tasks;

ALTER TABLE tasks_rebuild_0028 RENAME TO tasks;

-- 0001 indexes
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(parent_run_id);
-- 0009 scorecard hourly-window index
CREATE INDEX IF NOT EXISTS idx_tasks_status_finished ON tasks(status, finished_at);
-- 0024 cross-process retry idempotency window (partial unique)
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_lineage_active
  ON tasks(lineage_root)
  WHERE lineage_root IS NOT NULL AND status IN ('queued', 'running');
