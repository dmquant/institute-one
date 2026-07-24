-- Chain graph foundation (ROADMAP Phase 4, round-3 partition C2; reworked in
-- review round C2b — this file has never been applied to production, so the
-- rework edits it in place instead of adding a follow-up migration).
-- Proposal §6.2 chain row + §8.1: the Obsidian vault IS the graph browser —
-- nodes project to Chain/<entity>.md notes, backlinks replace a dedicated UI.
-- Domain module: app/institute/chain.py; API: app/api/chain.py.
-- Times are ISO-8601 UTC strings (bus.now_iso()). Additive only (B1 migration
-- discipline: no BEGIN/COMMIT/ATTACH/VACUUM — db.migrate wraps the file in one
-- transaction).

-- ============ chain_nodes ============
-- One row per tracked entity. `name` is UNIQUE because the INSTR backstop must
-- resolve a hit to exactly one node; cross-listings/aka forms live in
-- `aliases` (JSON array of strings, each resolving to this node).
-- `slug` is the PERSISTED vault projection path segment (Chain/<slug>.md),
-- assigned at insert and UNIQUE: _slug() is not injective ("A/B" and "A:B"
-- both normalize to "A-B"), so colliding names get a stable node-id suffix
-- instead of silently overwriting each other's note (REVIEW-C2 M3).
-- security_id ties an entity to the 0004 security master when it is listed;
-- SET NULL on security deletion — the graph node outlives the listing row.
CREATE TABLE IF NOT EXISTS chain_nodes (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL CHECK (length(name) >= 2),
  kind        TEXT NOT NULL CHECK (kind IN ('company','product','technology','commodity','person','org','other')),
  security_id TEXT REFERENCES securities(id) ON DELETE SET NULL,
  aliases     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(aliases) AND json_type(aliases) = 'array'),  -- JSON array of strings
  slug        TEXT NOT NULL CHECK (length(slug) >= 1),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chain_nodes_name ON chain_nodes(name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chain_nodes_slug ON chain_nodes(slug);
CREATE INDEX IF NOT EXISTS idx_chain_nodes_kind ON chain_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_chain_nodes_security ON chain_nodes(security_id);

-- ============ chain_edges ============
-- Typed directed relations. `relation` is an OPEN set (suggested vocabulary:
-- supplier_of / customer_of / competitor_of / subsidiary_of / produces —
-- rendered as Dataview inline fields `relation:: [[dst]]`), so no CHECK on it
-- beyond non-emptiness. UNIQUE(src,dst,relation) makes edge assertion
-- idempotent. Self-loops carry no chain information — rejected by CHECK.
CREATE TABLE IF NOT EXISTS chain_edges (
  id           TEXT PRIMARY KEY,
  src_id       TEXT NOT NULL REFERENCES chain_nodes(id) ON DELETE CASCADE,
  dst_id       TEXT NOT NULL REFERENCES chain_nodes(id) ON DELETE CASCADE,
  relation     TEXT NOT NULL CHECK (length(relation) > 0),
  confidence   REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
  evidence_ref TEXT,                     -- artifact pointer, e.g. research:<id>
  created_at   TEXT NOT NULL,
  CHECK (src_id <> dst_id),
  UNIQUE (src_id, dst_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_chain_edges_src ON chain_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_chain_edges_dst ON chain_edges(dst_id);

-- ============ chain_mentions ============
-- "Entity X was mentioned in artifact Y" — written by the INSTR backstop
-- tagger over new artifacts AND backfilled from candidate sightings when a
-- candidate is promoted/clustered into a node (REVIEW-C2 M2). UNIQUE(node_id,
-- artifact_kind, artifact_ref) IS the idempotency contract: re-tagging the
-- same artifact (bus handler + the hourly catch-up tick both run it) can
-- never duplicate a mention.
-- artifact_kind/artifact_ref mirror the archive/vault conventions:
-- ('research', <queue item id>), ('whiteboard', <board id>),
-- ('analyst-daily', '<analyst_id>:<date>').
CREATE TABLE IF NOT EXISTS chain_mentions (
  id            TEXT PRIMARY KEY,
  node_id       TEXT NOT NULL REFERENCES chain_nodes(id) ON DELETE CASCADE,
  artifact_kind TEXT NOT NULL,
  artifact_ref  TEXT NOT NULL,
  snippet       TEXT,                   -- text around the first hit
  created_at    TEXT NOT NULL,
  UNIQUE (node_id, artifact_kind, artifact_ref)
);
CREATE INDEX IF NOT EXISTS idx_chain_mentions_node ON chain_mentions(node_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chain_mentions_ref ON chain_mentions(artifact_kind, artifact_ref);

-- ============ chain_candidates ============
-- Entity-extraction output awaiting promotion. UNIQUE(name): re-sighting a
-- known candidate never duplicates the row; kind_guess/first_seen_ref keep
-- the FIRST sighting. mention_count is DERIVED: it aggregates the DISTINCT
-- sources in chain_candidate_sightings (REVIEW-C2 M5 — a crash-replayed
-- artifact must not double-count toward the auto-promote threshold).
-- status transitions use the conditional-claim idiom
-- (UPDATE … WHERE status='pending'); promoted/rejected/merged rows stay for
-- audit. 'merged' = the periodic auto-cluster folded this candidate into an
-- existing node (REVIEW-C2 M1); merged_into records the absorbing node for
-- both the promoted and merged outcomes.
CREATE TABLE IF NOT EXISTS chain_candidates (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL CHECK (length(name) >= 2),
  kind_guess     TEXT,
  first_seen_ref TEXT,                  -- artifact_kind:artifact_ref of the first sighting
  mention_count  INTEGER NOT NULL DEFAULT 1,
  status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','promoted','rejected','merged')),
  merged_into    TEXT REFERENCES chain_nodes(id) ON DELETE SET NULL,
  created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chain_candidates_name ON chain_candidates(name);
CREATE INDEX IF NOT EXISTS idx_chain_candidates_status ON chain_candidates(status, mention_count);

-- ============ chain_candidate_sightings ============
-- One row per DISTINCT (candidate, source artifact) sighting. This is the
-- idempotency unit the events-cursor crash-replay window needs (REVIEW-C2
-- M5): re-processing the same artifact hits the UNIQUE key and adds nothing,
-- and mention_count is recomputed as COUNT(*) over these rows. It is also the
-- full source set that promotion backfills into chain_mentions (REVIEW-C2
-- M2) — first_seen_ref alone only remembered one artifact; snippet (captured
-- at extraction time when the artifact text is at hand) makes the backfilled
-- mention as useful as a live backstop one.
CREATE TABLE IF NOT EXISTS chain_candidate_sightings (
  id            TEXT PRIMARY KEY,
  candidate_id  TEXT NOT NULL REFERENCES chain_candidates(id) ON DELETE CASCADE,
  artifact_kind TEXT NOT NULL,
  artifact_ref  TEXT NOT NULL,
  snippet       TEXT,                   -- text around the first hit, if known
  created_at    TEXT NOT NULL,
  UNIQUE (candidate_id, artifact_kind, artifact_ref)
);
