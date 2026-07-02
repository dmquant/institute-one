-- Roadmap control plane (card M7-001). Contract: roadmap/02-data-model.md.
-- Rows are truth; roadmap/backlog.json is a seed/import/export artifact.
-- Times are ISO-8601 UTC strings (bus.now_iso()).

-- ============ cards ============
CREATE TABLE IF NOT EXISTS roadmap_cards (
  id                  TEXT PRIMARY KEY,
  title               TEXT NOT NULL,
  type                TEXT NOT NULL,
  phase               TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('inbox','ready','in_progress','review','verify','done','parked')),
  priority            TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
  risk                TEXT NOT NULL DEFAULT 'medium' CHECK (risk IN ('low','medium','high')),
  owner               TEXT,
  summary             TEXT NOT NULL DEFAULT '',
  problem             TEXT NOT NULL DEFAULT '',
  implementation      TEXT NOT NULL DEFAULT '',
  agent_prompt        TEXT NOT NULL DEFAULT '',
  design_links_json   TEXT NOT NULL DEFAULT '[]',
  expected_files_json TEXT NOT NULL DEFAULT '[]',
  verification_json   TEXT NOT NULL DEFAULT '[]',  -- seed field `verification` (list of commands)
  tags_json           TEXT NOT NULL DEFAULT '[]',
  blocked_reason      TEXT,
  sort_order          REAL NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  completed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_roadmap_cards_status ON roadmap_cards(status, sort_order);
CREATE INDEX IF NOT EXISTS idx_roadmap_cards_phase ON roadmap_cards(phase);

CREATE TABLE IF NOT EXISTS roadmap_checklists (
  id           TEXT PRIMARY KEY,
  card_id      TEXT NOT NULL REFERENCES roadmap_cards(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,           -- acceptance|implementation|review
  text         TEXT NOT NULL,
  checked      INTEGER NOT NULL DEFAULT 0,
  sort_order   REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
-- import merges checklist items by text (INSERT OR IGNORE keeps local checked state)
CREATE UNIQUE INDEX IF NOT EXISTS idx_roadmap_checklists_text ON roadmap_checklists(card_id, kind, text);

CREATE TABLE IF NOT EXISTS roadmap_dependencies (
  id             TEXT PRIMARY KEY,
  card_id        TEXT NOT NULL REFERENCES roadmap_cards(id) ON DELETE CASCADE,
  depends_on_id  TEXT NOT NULL REFERENCES roadmap_cards(id) ON DELETE CASCADE,
  relation       TEXT NOT NULL DEFAULT 'blocks',
  created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_roadmap_deps_pair ON roadmap_dependencies(card_id, depends_on_id, relation);
CREATE INDEX IF NOT EXISTS idx_roadmap_deps_on ON roadmap_dependencies(depends_on_id);

CREATE TABLE IF NOT EXISTS roadmap_evidence (
  id           TEXT PRIMARY KEY,
  card_id      TEXT NOT NULL REFERENCES roadmap_cards(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,           -- command|test|screenshot|diff|doc|operator
  title        TEXT NOT NULL,
  body         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL CHECK (status IN ('pass','fail','info','override')),
  artifact_ref TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roadmap_evidence_card ON roadmap_evidence(card_id, created_at);

-- roadmap-scoped audit trail (the bus `events` table additionally receives
-- namespaced `roadmap.*` events for SSE / vault projections)
CREATE TABLE IF NOT EXISTS roadmap_events (
  id           TEXT PRIMARY KEY,
  card_id      TEXT,
  event_type   TEXT NOT NULL,           -- card.moved, import.completed, ... (02-data-model.md)
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roadmap_events_card ON roadmap_events(card_id, created_at);

-- ============ coding sessions ============
CREATE TABLE IF NOT EXISTS roadmap_coding_sessions (
  id                  TEXT PRIMARY KEY,
  card_id             TEXT NOT NULL REFERENCES roadmap_cards(id) ON DELETE CASCADE,
  actor               TEXT NOT NULL,    -- human|codex|claude|gemini|other
  goal                TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('active','completed','partial','blocked','cancelled')),
  planned_files_json  TEXT NOT NULL DEFAULT '[]',
  touched_files_json  TEXT NOT NULL DEFAULT '[]',
  summary             TEXT NOT NULL DEFAULT '',
  started_at          TEXT NOT NULL,
  finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_roadmap_sessions_card ON roadmap_coding_sessions(card_id, started_at);
CREATE INDEX IF NOT EXISTS idx_roadmap_sessions_status ON roadmap_coding_sessions(status);

CREATE TABLE IF NOT EXISTS roadmap_session_commands (
  id             TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL REFERENCES roadmap_coding_sessions(id) ON DELETE CASCADE,
  command_label  TEXT NOT NULL,
  command_text   TEXT NOT NULL,
  exit_code      INTEGER,
  output_excerpt TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roadmap_session_cmds ON roadmap_session_commands(session_id, created_at);

CREATE TABLE IF NOT EXISTS roadmap_decisions (
  id             TEXT PRIMARY KEY,
  card_id        TEXT,
  title          TEXT NOT NULL,
  question       TEXT NOT NULL,
  options_json   TEXT NOT NULL DEFAULT '[]',
  decision       TEXT,
  status         TEXT NOT NULL DEFAULT 'open',
  created_at     TEXT NOT NULL,
  resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_roadmap_decisions_card ON roadmap_decisions(card_id, created_at);

-- persisted gate rows are optional; GET /api/roadmap/release-gates computes
-- gate progress from card phases (Release A = M0-M3, B = M4-M6, C = M7)
CREATE TABLE IF NOT EXISTS roadmap_release_gates (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  scope_json      TEXT NOT NULL DEFAULT '[]',
  criteria_json   TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'open',
  evidence_json   TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
