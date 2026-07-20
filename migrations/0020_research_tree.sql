-- BFS research tree / Explore mode (ROADMAP Phase 7, proposal §6.2).
-- Numbered 0020: 0019 is reserved by a parallel round-4 card; db.migrate()
-- applies files in sorted order, gaps are fine (0009/0018 precedent).
--
-- Two tables:
--   research_trees       one row per explore tree. status transitions are
--                        conditional claims (pending -> exploring on the first
--                        node claim; exploring -> completed/failed when every
--                        node is terminal; stop_tree() -> stopped, one
--                        transaction with its pending-node prune — REVIEW-D4
--                        H2). max_depth / max_nodes are per-tree exploration
--                        caps frozen at creation (max_nodes counts NON-pruned
--                        rows, root included). announced_at is the single-shot
--                        arbiter for the tree.completed event (REVIEW-D4 M2):
--                        set by a conditional UPDATE only when the tree is
--                        terminal AND drained (no pending/running nodes), so
--                        the event is a final snapshot and fires exactly once
--                        even for stopped trees whose running nodes finish
--                        naturally afterwards.
--   research_tree_nodes  one row per node. The root has parent_id NULL and
--                        depth 0; children land depth = parent + 1. status:
--                        pending -> running (conditional claim under the
--                        per-tree concurrency cap) -> completed/failed;
--                        'pruned' rows are born terminal -- children cut by
--                        the depth/node caps or by stop_tree(), kept so the
--                        tree viewer can show what was NOT explored.
--                        task_id links to the tasks row of the explore model
--                        call (written at node completion -- the audit spine).
--                        score REAL is RESERVED (0018 recipes precedent): the
--                        schema stays final but no code writes it this round;
--                        a later ranking card can populate it without a new
--                        migration.
-- Times: created_at/finished_at are bus.now_iso() (UTC ISO).

CREATE TABLE IF NOT EXISTS research_trees (
  id          TEXT PRIMARY KEY,
  root_topic  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                ('pending','exploring','completed','stopped','failed')),
  max_depth    INTEGER NOT NULL DEFAULT 2,
  max_nodes    INTEGER NOT NULL DEFAULT 12,
  created_at   TEXT NOT NULL,
  finished_at  TEXT,
  announced_at TEXT                       -- tree.completed emitted (single-shot arbiter)
);
CREATE INDEX IF NOT EXISTS idx_research_trees_status ON research_trees(status, created_at);

CREATE TABLE IF NOT EXISTS research_tree_nodes (
  id          TEXT PRIMARY KEY,
  tree_id     TEXT NOT NULL REFERENCES research_trees(id) ON DELETE CASCADE,
  parent_id   TEXT REFERENCES research_tree_nodes(id),   -- NULL = root
  depth       INTEGER NOT NULL DEFAULT 0,
  topic       TEXT NOT NULL,
  question    TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                ('pending','running','completed','failed','pruned')),
  task_id     TEXT,
  summary     TEXT,
  score       REAL,                                      -- reserved, unwritten this round
  created_at  TEXT NOT NULL,
  finished_at TEXT
);
-- BFS claim order (same layer first) + per-tree finality/concurrency counts.
CREATE INDEX IF NOT EXISTS idx_research_tree_nodes_claim
  ON research_tree_nodes(status, depth, created_at);
CREATE INDEX IF NOT EXISTS idx_research_tree_nodes_tree
  ON research_tree_nodes(tree_id, status);
-- Child insertion idempotency: one row per (tree, parent, topic) proposal, so
-- a crash-requeued parent re-running its explore call cannot duplicate its
-- children (INSERT OR IGNORE + this index is the arbiter). NULL parent_ids
-- (roots) are distinct under SQLite UNIQUE semantics; create_tree only ever
-- inserts one root per tree.
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_tree_children
  ON research_tree_nodes(tree_id, parent_id, topic);

-- Exploration limits (admin_state JSON row over in-code defaults; the 0015
-- factcheck_reuse_policy idiom). Deleting the row degrades to the defaults in
-- app/institute/research_tree.py. daily_tree_cap counts BOOKED create attempts
-- per SGT work date (the 'research_tree_booked:<date>' counter rows book slots
-- atomically BEFORE the tree lands; no refunds — REVIEW-D4 N2 naming);
-- node_concurrency caps concurrently RUNNING nodes per tree.
INSERT OR IGNORE INTO admin_state (key, value) VALUES
  ('research_tree_limits', '{"daily_tree_cap": 3, "node_concurrency": 2}');
