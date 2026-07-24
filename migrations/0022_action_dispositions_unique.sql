-- DB backstop for the router's propose-once-per-loop claim
-- (REVIEW-C4 P2 / ROUND3-AUDIT-F3 NIT-3, fourth-round unshadow prep).
--
-- route_actions() checks NOT EXISTS then INSERTs, with a model call (up to
-- 300 s) in between; scheduler max_instances=1 narrows but does not close the
-- race between two same-proposed_by callers. This partial unique index makes
-- the database arbitrate: the loser's INSERT raises IntegrityError and
-- route_actions converges (drops its duplicate) like the feeds do on 0018's
-- uq_operator_actions_live_ref.
--
-- Scoped to the two loops only: 'human' dispositions (reserved by 0018's
-- CHECK, no writer yet) stay unconstrained — a human may weigh in repeatedly.
--
-- Numbered 0022: 0019-0021 are reserved by parallel round-4 cards; 0018 is on
-- production and immutable (only-additive migrations rule). Gaps are fine
-- (0009 precedent).

-- Belt and braces before the unique index: if a pre-0022 race ever landed
-- duplicate (action_id, proposed_by) loop rows, keep the EARLIEST (the loop's
-- one valid proposal) so index creation cannot wedge migration/boot.
DELETE FROM action_dispositions
WHERE proposed_by IN ('fast_loop', 'deep_loop')
  AND id NOT IN (
    SELECT MIN(id) FROM action_dispositions GROUP BY action_id, proposed_by
  );

CREATE UNIQUE INDEX IF NOT EXISTS uq_action_dispositions_loop_once
  ON action_dispositions(action_id, proposed_by)
  WHERE proposed_by IN ('fast_loop', 'deep_loop');
