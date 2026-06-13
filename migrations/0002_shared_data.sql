-- Shared market/financial data cache.

CREATE TABLE IF NOT EXISTS shared_data (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  topic       TEXT NOT NULL,
  data_type   TEXT NOT NULL,
  work_date   TEXT NOT NULL,
  provider    TEXT NOT NULL,
  confidence  REAL NOT NULL DEFAULT 0.0,
  payload     TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(topic, data_type, work_date)
);

CREATE INDEX IF NOT EXISTS idx_shared_data_topic ON shared_data(topic, data_type, updated_at);
