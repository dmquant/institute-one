-- Persistent multi-agent groups and run records (M8-012, from S4-P2-06:
-- multi-agent runs had no durable group/run rows — no reconnect, no
-- partial-spawn recovery — and committee had no persistent run record or
-- Committee Vault export).
-- Numbered 0027: 0026 is reserved by a parallel card; db.migrate() applies
-- files in sorted order, gaps are fine (0020/0021 precedent).
--
-- Two tables:
--   multi_agent_groups  named standing panels: member analysts (JSON array of
--                       roster ids, fan-out order) plus the routing strategy
--                       (join mode + optional hand override). CRUD lives in
--                       app/institute/multi_agent.py; deleting a group keeps
--                       its runs (group_id SET NULL) — run rows freeze their
--                       own agents/mode at spawn time, so history never
--                       depends on the live group definition.
--                       The 'committee' row is system-maintained: it is
--                       upserted from workflows/committee.json step analysts
--                       every time a committee run is recorded.
--   multi_agent_runs    one row per fan-out run (and per weekly committee
--                       run). prompt is the INPUT SNAPSHOT (the fan-out
--                       prompt; for committee: the ${WEEK_DISPUTES} whiteboard
--                       digest the agenda step saw). task_ids is the JSON
--                       array of executor task ids in agents order — per-step
--                       outputs stay in the tasks rows (the audit spine);
--                       spawned tasks also carry parent_run_id = this run id,
--                       so a crash between spawn and the task_ids write is
--                       recoverable from the tasks table (partial-spawn
--                       recovery). verdict is the STRUCTURED join record
--                       (mode/ok/votes/ballots + per-task status refs, no
--                       full output text) written by the settle claim.
--                       status: running -> completed/failed via conditional
--                       claims (hard rule 2); 'completed' means every spawned
--                       task reached a terminal state and the verdict was
--                       recorded — verdict.ok says whether the join converged.
--                       workflow_run_id bridges committee records to their
--                       workflow_runs row; the UNIQUE index (NULLs distinct)
--                       is the idempotency arbiter for INSERT OR IGNORE
--                       re-records (scheduler kickoff vs exporter finalize).
-- Times are bus.now_iso() UTC ISO strings.

CREATE TABLE IF NOT EXISTS multi_agent_groups (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  agents      TEXT NOT NULL,               -- JSON array of analyst ids, fan-out order
  mode        TEXT NOT NULL DEFAULT 'all' CHECK (mode IN ('all','first_success','majority_vote','best_effort')),
  hand        TEXT,                        -- optional hand override for every member
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS multi_agent_runs (
  id              TEXT PRIMARY KEY,
  group_id        TEXT REFERENCES multi_agent_groups(id) ON DELETE SET NULL,
  workflow_run_id TEXT,                    -- committee bridge; NULL for ad-hoc fan-outs
  agents          TEXT NOT NULL,           -- JSON array frozen at spawn
  mode            TEXT NOT NULL CHECK (mode IN ('all','first_success','majority_vote','best_effort')),
  prompt          TEXT NOT NULL DEFAULT '',-- input snapshot (see header)
  status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
  task_ids        TEXT NOT NULL DEFAULT '[]',  -- JSON array, agents order
  verdict         TEXT,                    -- JSON structured verdict (settle claim)
  error           TEXT,
  created_at      TEXT NOT NULL,
  finished_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_multi_agent_runs_workflow
  ON multi_agent_runs(workflow_run_id);
CREATE INDEX IF NOT EXISTS idx_multi_agent_runs_group
  ON multi_agent_runs(group_id, created_at);
CREATE INDEX IF NOT EXISTS idx_multi_agent_runs_status
  ON multi_agent_runs(status, created_at);
