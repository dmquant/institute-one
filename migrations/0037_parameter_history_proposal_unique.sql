-- R3 loop-fix close-out (P6a replay hardening): at most ONE parameter_history
-- row per applied proposal.
--
-- approve_proposal()'s replay path (approved AND applied=0, PATCH-NOTES-LOOP-
-- P6) applies a set_parameter proposal at-least-once. Two concurrent replays
-- could both miss the per-proposal history lookup; the second, reading
-- admin_state after the first committed, passed its byte-CAS (SET v WHERE
-- value = v succeeds) and appended a second, no-op history row for the same
-- proposal_id. set_parameter now re-checks per proposal INSIDE its write
-- transaction; this partial unique index is the DB-level backstop (the 0022 /
-- 0026 dedupe-index idiom): the loser's INSERT raises IntegrityError, its
-- transaction rolls back whole, and the caller converges on the winner's row.
--
-- Rollback rows are exempt by construction: rollback_parameter() always
-- inserts proposal_id = NULL (rollback_of carries the lineage), so the
-- partial index never constrains them.
--
-- Pre-index cleanup (narrowed per R4 review — history is an AUDIT LOG, the
-- migration must never guess): only rows that are PROVABLY no-op echoes of
-- the pre-0037 replay race are pruned — old_value = new_value (the echo's
-- signature; SQL NULL never compares equal, so NULL-valued rows are always
-- kept) AND an earlier row for the same proposal exists. A later duplicate
-- that REALLY moved the value (an old replay interleaved with a human change,
-- e.g. 0.7→0.75 then 0.8→0.75) is a real state transition: it stays, and the
-- CREATE UNIQUE INDEX below then fails LOUDLY (the file's transaction rolls
-- back whole, boot reports the failed migration) so a human reconciles the
-- genuine duplicate instead of the migration silently rewriting history.
-- The live deployment's parameter_history is empty (verified at review time),
-- so both statements are no-ops there and apply cleanly.
--
-- Numbered 0037: 0035/0036 are reserved by parallel loop-fix executors;
-- gaps are fine (0009 precedent).

DELETE FROM parameter_history
WHERE proposal_id IS NOT NULL
  AND old_value = new_value
  AND EXISTS (
    SELECT 1 FROM parameter_history earlier
    WHERE earlier.proposal_id = parameter_history.proposal_id
      AND earlier.id < parameter_history.id
  );

CREATE UNIQUE INDEX IF NOT EXISTS uq_parameter_history_proposal
  ON parameter_history(proposal_id) WHERE proposal_id IS NOT NULL;
