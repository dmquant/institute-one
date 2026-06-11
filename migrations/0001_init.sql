-- institute-one initial schema.
-- Times are stored as ISO-8601 UTC strings (created_at etc.) unless noted.
-- work_date columns are SGT calendar dates (YYYY-MM-DD).

-- ============ router spine ============
CREATE TABLE IF NOT EXISTS tasks (
  id             TEXT PRIMARY KEY,
  session_id     TEXT,
  hand           TEXT,                -- hand that actually ran (after fallback)
  requested_hand TEXT,                -- hand the caller asked for
  model          TEXT,
  prompt         TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','rate_limited','cancelled','expired')),
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
  finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(parent_run_id);

-- ============ events (audit + SSE cursor + vault trigger) ============
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  type       TEXT NOT NULL,           -- e.g. task.completed, research.completed, whiteboard.board_completed
  ref_kind   TEXT NOT NULL DEFAULT '',
  ref_id     TEXT NOT NULL DEFAULT '',
  payload    TEXT NOT NULL DEFAULT '{}',  -- JSON
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, id);

-- ============ sessions ============
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL DEFAULT '',
  kind          TEXT NOT NULL DEFAULT 'chat',  -- chat|workflow|whiteboard|mailbox|research|daily
  analyst_id    TEXT,
  workspace_dir TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_kind ON sessions(kind, updated_at);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role       TEXT NOT NULL,           -- user|assistant|system
  content    TEXT NOT NULL,
  hand       TEXT,
  task_id    TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- ============ workflows ============
CREATE TABLE IF NOT EXISTS workflows (
  id          TEXT PRIMARY KEY,        -- e.g. 'research', 'briefing', 'daily'
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  variables   TEXT NOT NULL DEFAULT '[]',  -- JSON list of variable names
  steps       TEXT NOT NULL,               -- JSON list (see workflows/*.json)
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id           TEXT PRIMARY KEY,
  workflow_id  TEXT NOT NULL,
  session_id   TEXT,
  status       TEXT NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
  variables    TEXT NOT NULL DEFAULT '{}',  -- JSON dict
  current_step INTEGER NOT NULL DEFAULT 0,
  results      TEXT NOT NULL DEFAULT '[]',  -- JSON list of {step_id, title, task_id, status, summary, output_file}
  error        TEXT,
  source       TEXT NOT NULL DEFAULT 'api',
  started_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_wf ON workflow_runs(workflow_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status);

-- ============ whiteboard ============
CREATE TABLE IF NOT EXISTS whiteboard_boards (
  id         TEXT PRIMARY KEY,
  topic      TEXT NOT NULL,
  question   TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL CHECK (status IN ('active','completed','stopped','failed')),
  max_cards  INTEGER NOT NULL DEFAULT 5,
  session_id TEXT,
  work_date  TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_boards_status ON whiteboard_boards(status, updated_at);

CREATE TABLE IF NOT EXISTS whiteboard_cards (
  id          TEXT PRIMARY KEY,
  board_id    TEXT NOT NULL REFERENCES whiteboard_boards(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  analyst_id  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed')),
  question    TEXT NOT NULL DEFAULT '',
  summary     TEXT,
  output_file TEXT,                   -- workspace-relative path of the card report
  task_id     TEXT,
  created_at  TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_board ON whiteboard_cards(board_id, idx);
CREATE INDEX IF NOT EXISTS idx_cards_status ON whiteboard_cards(status);

CREATE TABLE IF NOT EXISTS topic_pool (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  topic        TEXT NOT NULL,
  question     TEXT NOT NULL DEFAULT '',
  source       TEXT NOT NULL DEFAULT 'manual',  -- manual|api|obsidian|harvest
  score        REAL NOT NULL DEFAULT 1.0,
  status       TEXT NOT NULL CHECK (status IN ('pending','used','expired')) DEFAULT 'pending',
  content_hash TEXT UNIQUE,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_status ON topic_pool(status, score);

-- ============ mailbox ============
CREATE TABLE IF NOT EXISTS mailbox_threads (
  id         TEXT PRIMARY KEY,
  subject    TEXT NOT NULL,
  analyst_id TEXT NOT NULL,            -- the analyst this thread converses with
  status     TEXT NOT NULL CHECK (status IN ('open','closed')) DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_status ON mailbox_threads(status, updated_at);

CREATE TABLE IF NOT EXISTS mailbox_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id  TEXT NOT NULL REFERENCES mailbox_threads(id) ON DELETE CASCADE,
  author     TEXT NOT NULL,            -- 'operator' or an analyst_id
  kind       TEXT NOT NULL CHECK (kind IN ('note','dispatch','reply')) DEFAULT 'note',
  body       TEXT NOT NULL,
  task_id    TEXT,                     -- for dispatch: the executor task producing the reply
  status     TEXT NOT NULL DEFAULT 'done',  -- dispatch: pending|done|failed
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mbox_thread ON mailbox_messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_mbox_status ON mailbox_messages(status);

-- ============ deep research ============
CREATE TABLE IF NOT EXISTS research_queue (
  id          TEXT PRIMARY KEY,
  topic       TEXT NOT NULL,           -- ticker or research question
  priority    INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled')),
  source      TEXT NOT NULL DEFAULT 'api',
  run_id      TEXT,                    -- workflow_runs.id
  error       TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_status ON research_queue(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS research_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  topic        TEXT NOT NULL,
  run_id       TEXT,
  summary      TEXT,
  completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_log_topic ON research_log(topic, completed_at);

-- ============ archive + search ============
CREATE TABLE IF NOT EXISTS archive_files (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  ref_kind   TEXT NOT NULL DEFAULT '',  -- research|whiteboard|daily|briefing|session
  ref_id     TEXT NOT NULL DEFAULT '',
  path       TEXT NOT NULL UNIQUE,      -- relative to archive_dir
  size       INTEGER NOT NULL DEFAULT 0,
  sha256     TEXT,
  created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
  content, path UNINDEXED, ref_kind UNINDEXED, ref_id UNINDEXED, session_id UNINDEXED
);

-- ============ vault bridge ============
CREATE TABLE IF NOT EXISTS vault_index (
  path          TEXT PRIMARY KEY,      -- vault-relative path
  artifact_kind TEXT NOT NULL,
  artifact_id   TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('clean','conflict')) DEFAULT 'clean',
  written_at    TEXT NOT NULL
);

-- ============ misc ============
CREATE TABLE IF NOT EXISTS admin_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                  -- JSON
);
