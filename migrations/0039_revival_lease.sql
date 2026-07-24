-- LOOP R4 P1: crash-safe claim for the rate-limit revival job.
--
-- The old protocol persisted the permanent [rate-limit-revival:claimed]
-- marker in tasks.error BEFORE the retry generation existed; a hard crash
-- (SIGKILL / power loss) inside respawn left the marker with no retry — the
-- candidate scan excludes marked rows, so that lineage was dead forever with
-- no stale reclaim. A permanent marker cannot double as a crash-safe claim.
--
-- revival_lease_id / revival_leased_at: random token + timestamp written by
-- the conditional pre-respawn claim (fact_cards 0034 idiom). Every later
-- write of that attempt carries "AND revival_lease_id = ?"; the candidate
-- scan reclaims rows whose lease is older than the TTL (a crashed attempt).
-- The permanent marker is now written ONLY after the retry generation really
-- exists, carrying the lease.
--
-- revival_attempts: incremented by each claim, so crash/failure loops are
-- bounded (RATE_LIMIT_REVIVAL_MAX_ATTEMPTS); rows that keep failing are
-- parked instead of burning a claim every firing.
ALTER TABLE tasks ADD COLUMN revival_lease_id TEXT;
ALTER TABLE tasks ADD COLUMN revival_leased_at TEXT;
ALTER TABLE tasks ADD COLUMN revival_attempts INTEGER NOT NULL DEFAULT 0;
