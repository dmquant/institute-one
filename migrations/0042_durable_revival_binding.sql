-- R5 QUEUE AUDIT: durable, immutable source -> canonical retry binding.
--
-- The 0039 lease only protected the scheduler's claim window. It did not say
-- which child task that claim created, so crashes around spawn/marker/event
-- writes could lose a queued child, duplicate a completed child, or consume a
-- source with a born-terminal overcommitted child.
--
-- revival_task_id lives on the rate_limited SOURCE; revived_from_task_id lives
-- on its canonical CHILD. Domain code writes both in the same SQLite
-- transaction as the born-queued child INSERT and never clears or retargets
-- either field. Reciprocal partial UNIQUE indexes make the one-to-one binding
-- hold across every child status (unlike uq_tasks_lineage_active, whose
-- protection ends when a child becomes terminal).
ALTER TABLE tasks ADD COLUMN revival_task_id TEXT;
ALTER TABLE tasks ADD COLUMN revived_from_task_id TEXT;

-- Conservative upgrade adoption for an R4 attempt that was in-flight while
-- 0042 landed. R4 persisted a lease/attempt on the source but no child id.
-- Bind only when there is EXACTLY ONE same-lineage child created inside that
-- lease window; ambiguous histories are deliberately left untouched rather
-- than guessing and violating the canonical-child invariant. This covers
-- both report windows: marker+queued and completed-before-marker.
UPDATE tasks AS child
SET revived_from_task_id = (
  SELECT source.id
  FROM tasks AS source
  WHERE source.status = 'rate_limited'
    AND source.revival_task_id IS NULL
    AND source.revival_lease_id IS NOT NULL
    AND source.revival_leased_at IS NOT NULL
    AND source.revival_attempts > 0
    AND child.id <> source.id
    AND child.lineage_root = COALESCE(source.lineage_root, source.id)
    AND child.created_at >= source.revival_leased_at
)
WHERE child.revived_from_task_id IS NULL
  AND child.lineage_root IS NOT NULL
  AND (
    SELECT COUNT(*)
    FROM tasks AS source
    WHERE source.status = 'rate_limited'
      AND source.revival_task_id IS NULL
      AND source.revival_lease_id IS NOT NULL
      AND source.revival_leased_at IS NOT NULL
      AND source.revival_attempts > 0
      AND child.id <> source.id
      AND child.lineage_root = COALESCE(source.lineage_root, source.id)
      AND child.created_at >= source.revival_leased_at
  ) = 1;

UPDATE tasks AS source
SET revival_task_id = (
  SELECT child.id
  FROM tasks AS child
  WHERE child.revived_from_task_id = source.id
)
WHERE source.revival_task_id IS NULL
  AND (
    SELECT COUNT(*)
    FROM tasks AS child
    WHERE child.revived_from_task_id = source.id
  ) = 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_revival_task_id
  ON tasks(revival_task_id)
  WHERE revival_task_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_revived_from_task_id
  ON tasks(revived_from_task_id)
  WHERE revived_from_task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_prepared_revival_status
  ON tasks(status, revived_from_task_id)
  WHERE revived_from_task_id IS NOT NULL;
