-- Persistent memory for analyst daily novelty gating.
--
-- Each analyst daily is decomposed into observation-level rows. The next daily
-- prompt uses recent rows as a local memory block, and a cheap/local reviewer
-- can compare the new draft against this table before closed models are asked
-- to spend judgment budget on repeated material.

CREATE TABLE IF NOT EXISTS analyst_daily_observations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  analyst_id     TEXT NOT NULL,
  work_date      TEXT NOT NULL,
  ordinal        INTEGER NOT NULL,
  title          TEXT NOT NULL,
  summary        TEXT NOT NULL DEFAULT '',
  new_delta      TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL CHECK (status IN ('main','monitor','repeat')) DEFAULT 'main',
  source_task_id TEXT NOT NULL DEFAULT '',
  content_hash   TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(analyst_id, work_date, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_analyst_daily_obs_recent
  ON analyst_daily_observations(analyst_id, work_date DESC, ordinal);

CREATE INDEX IF NOT EXISTS idx_analyst_daily_obs_hash
  ON analyst_daily_observations(analyst_id, content_hash);
