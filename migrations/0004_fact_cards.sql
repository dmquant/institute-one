-- Lightweight post-run claim audit.
--
-- Fact cards are extracted from generated artifacts after writing. The first
-- version is deliberately conservative: it records checkable claims and source
-- attachment status, without pretending that a cited URL fully verifies the
-- claim.

CREATE TABLE IF NOT EXISTS fact_cards (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_kind  TEXT NOT NULL,
  artifact_id    TEXT NOT NULL,
  artifact_path  TEXT NOT NULL DEFAULT '',
  topic          TEXT NOT NULL DEFAULT '',
  analyst_id     TEXT NOT NULL DEFAULT '',
  work_date      TEXT NOT NULL DEFAULT '',
  claim_text     TEXT NOT NULL,
  category       TEXT NOT NULL DEFAULT 'other',
  verdict        TEXT NOT NULL CHECK (verdict IN (
                   'source_attached',
                   'weak_source',
                   'unsupported',
                   'declared_unverified'
                 )),
  confidence     REAL NOT NULL DEFAULT 0.0,
  rationale      TEXT NOT NULL DEFAULT '',
  source_urls    TEXT NOT NULL DEFAULT '[]',
  context_text   TEXT NOT NULL DEFAULT '',
  claim_hash     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(artifact_kind, artifact_id, claim_hash)
);

CREATE INDEX IF NOT EXISTS idx_fact_cards_artifact ON fact_cards(artifact_kind, artifact_id);
CREATE INDEX IF NOT EXISTS idx_fact_cards_topic ON fact_cards(topic, work_date);
CREATE INDEX IF NOT EXISTS idx_fact_cards_verdict ON fact_cards(verdict, updated_at);
