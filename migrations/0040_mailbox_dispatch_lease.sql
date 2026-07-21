-- LOOP R4 P1: durable dispatch claim for the mailbox.
--
-- _run_dispatch used to submit the model call with NO db-side claim (the
-- in-process _inflight set is not a correctness mechanism) and then wrote
-- task_id unconditionally — a late worker that lost the final
-- status='done' conditional claim could still overwrite task_id, leaving
-- dispatch.task_id pointing at a different task than the reply that landed.
--
-- lease_id / leased_at: random token + timestamp written by the conditional
-- pending-claim BEFORE executor.submit (fact_cards 0034 idiom). Only the
-- claim winner may create the model task; task_id and both terminal flips
-- (done/failed) carry "AND lease_id = ?", so a reclaimed worker's late
-- writes are no-ops. sweep() re-drives a pending row only when it has no
-- lease or the lease is older than the stale TTL (a crashed attempt).
ALTER TABLE mailbox_messages ADD COLUMN lease_id TEXT;
ALTER TABLE mailbox_messages ADD COLUMN leased_at TEXT;
