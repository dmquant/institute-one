-- Phase 1a: whiteboard similarity gate + diversity pick + category weights
-- (ROADMAP Phase 1a items 2-3; proposal §6.3, §10).
-- Numbered 0011: 0008/0010/0012/0013 are reserved by parallel cards;
-- db.migrate() applies files in sorted order, gaps are fine.
--
-- Times: created_at/updated_at/similarity_checked_at are bus.now_iso()
-- (UTC ISO, seconds — lexicographic order == time order).

-- ============ similarity thresholds (config row) ============
-- ROADMAP says "thresholds as config rows": they live in admin_state as ONE
-- JSON row (key='whiteboard_similarity') instead of a dedicated table —
-- admin_state is the existing operator-config surface (same idiom as the
-- 'maintenance' switch), already visible via GET /api/admin/state, and the
-- seven scalars below don't warrant a new schema concept. The code keeps
-- built-in defaults, so deleting this row degrades gracefully.
INSERT OR IGNORE INTO admin_state (key, value) VALUES (
  'whiteboard_similarity',
  '{"skip_threshold": 0.85, "skip_window_days": 14, "augment_threshold": 0.65, "augment_window_days": 30, "diversity_penalty": 0.15, "diversity_window_days": 7, "rotation_max_streak": 3}'
);

-- ============ topic category weights ============
-- Missing row == neutral weight 1.0 (kickoff multiplies topic score by the
-- category weight before subtracting the diversity penalty).
CREATE TABLE IF NOT EXISTS topic_category_weights (
  category   TEXT PRIMARY KEY CHECK (category <> ''),
  weight     REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
  updated_at TEXT NOT NULL
);

-- ============ board topic vectors ============
-- One embedding per board, written when the board opens (kickoff or manual).
-- Deliberately NOT vector_chunks: index_file/search are keyed to
-- archive_files' current sha (rebuild-by-source), which would silently drop
-- non-archive sources. The similarity gate compares cosine in Python over
-- the recent window, so this works without sqlite-vec. The model column
-- inherits A8's semantics: the gate filters on the CURRENT embed model, so
-- a model switch hides old vectors instead of comparing across spaces
-- (old boards simply stop gating — the documented degrade-open posture).
CREATE TABLE IF NOT EXISTS whiteboard_topic_vectors (
  board_id   TEXT PRIMARY KEY REFERENCES whiteboard_boards(id) ON DELETE CASCADE,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  embedding  BLOB NOT NULL,                     -- little-endian float32 array
  created_at TEXT NOT NULL
);

-- ============ topic pool: category + gate verdict cache ============
-- category: NULL == uncategorized (auto-classification is a later card).
-- similarity_state/similarity_checked_at/similar_board_id cache the gate
-- verdict so the hourly kickoff does not re-embed the same pending topics:
-- a fresh 'skip' verdict is excluded from candidate selection until the
-- cache TTL (24h) expires, then re-evaluated (windows move, boards age out).
-- similarity_fingerprint pins the verdict to the embedding model + gate
-- thresholds it was computed under (REVIEW-B4 M2): a model switch or a
-- threshold change makes every cached verdict stale immediately, without
-- waiting out the TTL.
-- topic_pool.status keeps its existing CHECK ('pending','used','expired') —
-- a skipped topic stays 'pending' and becomes eligible again naturally.
ALTER TABLE topic_pool ADD COLUMN category TEXT;
ALTER TABLE topic_pool ADD COLUMN similarity_state TEXT;       -- skip|augment|pass (last verdict)
ALTER TABLE topic_pool ADD COLUMN similarity_checked_at TEXT;  -- UTC ISO of last evaluation
ALTER TABLE topic_pool ADD COLUMN similar_board_id TEXT;       -- nearest prior board at evaluation
ALTER TABLE topic_pool ADD COLUMN similarity_fingerprint TEXT; -- hash of (embed model, gate thresholds) at evaluation

-- ============ boards: category + augment provenance ============
-- category flows from the topic at kickoff (NULL == uncategorized) and
-- feeds the diversity penalty + rotation guard.
-- prior_board_id != NULL marks an augment board: every card's prompt gets
-- the BUILD-ON-prior-work context block pointing at that board.
ALTER TABLE whiteboard_boards ADD COLUMN category TEXT;
ALTER TABLE whiteboard_boards ADD COLUMN prior_board_id TEXT;
