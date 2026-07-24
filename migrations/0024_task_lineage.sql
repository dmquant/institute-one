-- M8-003: durable retry lineage + idempotency for the executor audit spine.
--
-- Before this file, POST /tasks/{id}/retry rebuilt the execution policy by
-- DERIVING it from tasks.source (app/api/tasks.py _retry_policy) and deduped
-- same-instant retries with an in-process set — both evaporate across a
-- process restart: a post-restart retry of a chain-confined task could leak
-- onto hands the original call excluded (CLAUDE.md rule 10), and a second
-- process could double-spawn a retry the first one already had in flight.
--
-- fallback_chain: JSON list of hand names — the exact chain the original
--   submit()/spawn() confined resolution + rate-limit retry to; NULL = the
--   caller used the registry-default fallback (or the row predates this
--   file). Retry replays THIS stored policy; the source derivation survives
--   only as the fallback for NULL-chain legacy rows.
-- lineage_root: id of the retry chain's ORIGINAL task. Only retry-created
--   rows carry it; a retry of a retry keeps pointing at the root, so the
--   whole audit chain is one lookup (WHERE lineage_root = <root>), never a
--   pointer walk.
ALTER TABLE tasks ADD COLUMN fallback_chain TEXT;
ALTER TABLE tasks ADD COLUMN lineage_root TEXT;

-- DB-level idempotency window: at most ONE live (queued/running) task per
-- lineage. The retry endpoint pre-checks for a friendly 409, but THIS index
-- is the arbiter — a losing concurrent INSERT (same process or another one)
-- raises IntegrityError and maps to 409, like 0022's loop-once index.
-- Originals and pre-0024 rows (lineage_root IS NULL) are unconstrained, and
-- terminal rows leave the partial index, closing the window automatically.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_lineage_active
  ON tasks(lineage_root)
  WHERE lineage_root IS NOT NULL AND status IN ('queued', 'running');
