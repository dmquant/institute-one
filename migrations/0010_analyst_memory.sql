-- Analyst memory (ROADMAP Phase 2): versioned standing-memory compacts.
--
-- One row per compact. version increments per analyst; UNIQUE(analyst_id,
-- version) doubles as the concurrency claim: two overlapping compacts both
-- try INSERT OR IGNORE for version N+1 and the rowcount decides the winner
-- (same conditional-claim idiom as everywhere else).
--
-- cursors: JSON {"daily_event": N, "card_event": N, "mail_msg": N} — the
-- per-source monotonic id high-water marks of the material THIS version
-- actually consumed. The next compact resumes strictly after these ids, so
-- every output row is consumed by exactly one version: no same-second
-- timestamp ambiguity, no loss for material that lands while the model runs,
-- and per-source LIMIT overflow is picked up by later compacts instead of
-- being dropped (REVIEW-B3 B3-H1 / B3-M3).
CREATE TABLE IF NOT EXISTS analyst_memory (
  id         TEXT PRIMARY KEY,
  analyst_id TEXT NOT NULL,
  version    INTEGER NOT NULL,
  work_date  TEXT NOT NULL,             -- SGT calendar date of the compact
  compact_md TEXT NOT NULL,             -- the dense standing memory (Markdown)
  supersedes TEXT,                      -- analyst_memory.id of the previous version (NULL for v1)
  cursors    TEXT NOT NULL DEFAULT '{}', -- per-source consumption cursors (JSON, see above)
  created_at TEXT NOT NULL,             -- UTC ISO (bus.now_iso)
  UNIQUE (analyst_id, version)
);
CREATE INDEX IF NOT EXISTS idx_analyst_memory_latest ON analyst_memory(analyst_id, version DESC);

-- VaultWriter rule 4 (managed regions): a vault_index row must say what its
-- sha256 covers — the whole file ('file', the historical behavior) or only
-- the managed region between %% institute:begin %% / %% institute:end %%
-- markers ('region'). Region-mode notes let human annotations outside the
-- markers survive regeneration, so doctor() and the write path compare the
-- region hash instead of the whole-file hash for those rows.
ALTER TABLE vault_index ADD COLUMN mode TEXT NOT NULL DEFAULT 'file' CHECK (mode IN ('file', 'region'));
