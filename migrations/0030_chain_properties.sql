-- Chain entity properties + hybrid supersede/conflict history (ROADMAP Phase 4).
-- Domain module: app/institute/chain.py; API: app/api/chain.py.
--
-- Normal updates leave the newest assertion active and mark the prior active
-- row superseded. Assertions for the same entity/key/as_of from different
-- sources with materially different values instead share a conflict_group and
-- remain conflicted until an operator chooses a winner. Resolution retires the
-- losing assertions; supersedes_id and conflict_group remain as audit links.
-- Times are ISO-8601 UTC strings from bus.now_iso().

CREATE TABLE IF NOT EXISTS chain_properties (
  id                 TEXT PRIMARY KEY,
  entity_id          TEXT NOT NULL REFERENCES chain_nodes(id) ON DELETE CASCADE,
  prop_key           TEXT NOT NULL CHECK (length(prop_key) BETWEEN 1 AND 80),
  value              TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 2000),
  as_of              TEXT NOT NULL CHECK (length(as_of) BETWEEN 1 AND 64),
  source_ref          TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 300),
  status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                        ('active','superseded','conflicted','retired')),
  supersedes_id       TEXT REFERENCES chain_properties(id) ON DELETE SET NULL,
  conflict_group      TEXT,
  operator_action_id  INTEGER REFERENCES operator_actions(id) ON DELETE SET NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  CHECK (status <> 'conflicted' OR conflict_group IS NOT NULL),
  UNIQUE (entity_id, prop_key, value, as_of, source_ref)
);

-- A resolved/non-conflicting key has one current assertion. The domain moves
-- the old active row before inserting/activating its successor; this index is
-- the final concurrency backstop.
CREATE UNIQUE INDEX IF NOT EXISTS uq_chain_properties_active_key
  ON chain_properties(entity_id, prop_key) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_chain_properties_entity_history
  ON chain_properties(entity_id, prop_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chain_properties_conflicts
  ON chain_properties(status, conflict_group, created_at);

CREATE INDEX IF NOT EXISTS idx_chain_properties_period
  ON chain_properties(entity_id, prop_key, as_of);

-- Durable staging for model-extracted property assertions. tick() persists a
-- paid-for extraction's PROPERTY lines here BEFORE advancing that event's
-- cursor, so a later failure can neither lose the assertions nor force the
-- model call to replay. Applied after the promotion/cluster sweep: pending ->
-- applied (landed in chain_properties, or was an exact replay), pending ->
-- skipped (a deterministically invalid assertion, or the entity stayed
-- unknown for STAGING_UNKNOWN_ATTEMPTS sweeps — attempts counts those
-- unknown-entity misses, and the skip decision is one atomic UPDATE so a
-- promotion racing the check can never terminally swallow the row);
-- application errors keep the row pending for the next tick's retry.
-- event_id is a soft reference (the janitor prunes old events rows). The
-- UNIQUE key makes a crash-replayed event's re-staging a no-op.
CREATE TABLE IF NOT EXISTS chain_property_staging (
  id          TEXT PRIMARY KEY,
  event_id    INTEGER NOT NULL,
  entity      TEXT NOT NULL,
  prop_key    TEXT NOT NULL,
  value       TEXT NOT NULL,
  as_of       TEXT NOT NULL,
  source_ref  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                ('pending','applied','skipped')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (event_id, entity, prop_key, value, as_of)
);

CREATE INDEX IF NOT EXISTS idx_chain_property_staging_pending
  ON chain_property_staging(status, id);
