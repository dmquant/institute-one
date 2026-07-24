-- Operator favorites (ROADMAP Phase 7).
-- Heterogeneous references stay intentionally FK-free; the domain validates
-- ref_kind and list reads LEFT JOIN their owning tables for display metadata.

CREATE TABLE IF NOT EXISTS favorites (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_kind   TEXT NOT NULL,
  ref_id     TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (ref_kind, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_favorites_kind_created
  ON favorites(ref_kind, created_at DESC);
