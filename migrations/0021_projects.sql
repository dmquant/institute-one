-- Research projects (ROADMAP Phase 7): group research runs + whiteboard
-- boards + mailbox threads (and, once the BFS Explore mode lands, research
-- trees) under one named long-running project.
-- Numbered 0021: 0019-0020 are reserved by parallel round-4 cards;
-- db.migrate() applies files in sorted order, gaps are fine (0012/0018 precedent).
--
-- Two tables + one nullable column:
--   projects       the named container. status is a two-state lifecycle
--                  (active -> archived) driven by a conditional claim in
--                  app/institute/projects.py; archived projects keep their
--                  links (history) but refuse NEW links / NEW enqueues.
--   project_links  one row per (project, kind, ref) attachment. kind 'tree'
--                  is reserved for the BFS research tree (parallel Phase 7
--                  card): the CHECK admits it now so THIS file stays final
--                  (0018 recipes precedent), domain code links it without
--                  referential validation until the tree tables exist.
--                  ref_id is deliberately NOT a foreign key: it points into
--                  four different tables depending on kind (research_queue /
--                  whiteboard_boards / mailbox_threads / research trees),
--                  and SQLite cannot express a polymorphic FK. Existence is
--                  validated in projects.link(); UNIQUE(project_id, kind,
--                  ref_id) + INSERT OR IGNORE make linking idempotent (the
--                  database is the arbiter, topic_pool content_hash idiom).
--
-- research_queue.project_id: NULLABLE, default NULL — pre-migration rows and
-- project-less enqueues keep the exact old behavior (0012 discipline: the
-- structured columns from 0012 are untouched, this file ONLY adds project_id).
-- ON DELETE SET NULL, not CASCADE: the queue is a durable work log (0012
-- precedent) — deleting a project must never erase research history.
-- Times are bus.now_iso() UTC ISO strings.

CREATE TABLE IF NOT EXISTS projects (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_links (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN ('research','board','thread','tree')),
  ref_id     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_project_links_project ON project_links(project_id, kind);

ALTER TABLE research_queue ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_research_queue_project ON research_queue(project_id);
