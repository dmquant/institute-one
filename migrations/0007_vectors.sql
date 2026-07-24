-- Phase 1a: embeddings metadata (proposal §6.3, §10).
-- vector_chunks holds the chunk text + provenance for every embedded archive
-- artifact. The companion vec0 virtual table (vec_search) is created at
-- runtime by app/institute/vectors.py, because virtual tables require the
-- sqlite-vec extension to be loaded on the live connection — executescript
-- during migrate() has no extension loaded, so it must never reference it.

CREATE TABLE IF NOT EXISTS vector_chunks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- doubles as vec_search rowid
  path        TEXT NOT NULL,                      -- archive_files.path (relative to archive_dir)
  ref_kind    TEXT NOT NULL DEFAULT '',           -- research|whiteboard|daily|briefing|session
  ref_id      TEXT NOT NULL DEFAULT '',
  session_id  TEXT,
  chunk_index INTEGER NOT NULL DEFAULT 0,         -- position of the chunk within the source file
  text        TEXT NOT NULL,
  sha256      TEXT,                               -- sha256 of the source file at embed time
  model       TEXT NOT NULL DEFAULT '',           -- embedding model that produced the vector
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vector_chunks_path ON vector_chunks(path);
