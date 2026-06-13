-- Evidence source ledger.
--
-- This is intentionally source-first rather than crawler-first: first record
-- where URLs appeared in analyst artifacts, then future research can decide
-- whether a source needs refresh/fetching.

CREATE TABLE IF NOT EXISTS evidence_sources (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_url  TEXT NOT NULL UNIQUE,
  url            TEXT NOT NULL,
  host           TEXT NOT NULL DEFAULT '',
  title          TEXT NOT NULL DEFAULT '',
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  source_count   INTEGER NOT NULL DEFAULT 1,
  metadata       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_evidence_sources_host ON evidence_sources(host, last_seen_at);

CREATE TABLE IF NOT EXISTS claim_evidence_links (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id      INTEGER NOT NULL REFERENCES evidence_sources(id) ON DELETE CASCADE,
  artifact_kind  TEXT NOT NULL,
  artifact_id    TEXT NOT NULL,
  artifact_path  TEXT NOT NULL DEFAULT '',
  topic          TEXT NOT NULL DEFAULT '',
  analyst_id     TEXT NOT NULL DEFAULT '',
  work_date      TEXT NOT NULL DEFAULT '',
  claim_text     TEXT NOT NULL DEFAULT '',
  context_text   TEXT NOT NULL DEFAULT '',
  context_hash   TEXT NOT NULL,
  link_type      TEXT NOT NULL DEFAULT 'cited_url',
  created_at     TEXT NOT NULL,
  UNIQUE(source_id, artifact_kind, artifact_id, context_hash)
);

CREATE INDEX IF NOT EXISTS idx_claim_links_artifact ON claim_evidence_links(artifact_kind, artifact_id);
CREATE INDEX IF NOT EXISTS idx_claim_links_topic ON claim_evidence_links(topic, work_date);
CREATE INDEX IF NOT EXISTS idx_claim_links_source ON claim_evidence_links(source_id, created_at);
