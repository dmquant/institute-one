-- FACTCHECK-INTEGRITY: verification lease token + outbox intent kind.
--
-- fact_cards.lease_id: random token written by the pending→verifying
-- conditional claim (factcheck._claim_card). Every later transition of that
-- verification attempt (settle / release / crash-fail) carries
-- "AND lease_id = ?", and the stale-verifying sweep clears the lease when it
-- re-opens a crashed card — so a stale worker whose card was re-opened (and
-- possibly re-claimed under a fresh lease) can never land its late write.
ALTER TABLE fact_cards ADD COLUMN lease_id TEXT;

-- factcheck_dispute_outbox.intent: the 0025 outbox now carries TWO intent
-- kinds, both written inside the dispute transaction:
--   'mailbox' — materialize the analyst notification thread (the original
--               0025 semantics, hence the backfill default);
--   'event'   — emit the durable ``factcheck.disputed`` bus event (the old
--               post-commit best-effort emit could be lost to a crash).
-- Event rows use recipient_id = '' so the existing
-- UNIQUE(dispute_id, recipient_id) yields exactly one event intent per
-- dispute. Additive only: 0025 stays untouched per the migration rules.
ALTER TABLE factcheck_dispute_outbox ADD COLUMN intent TEXT NOT NULL DEFAULT 'mailbox'
  CHECK (intent IN ('mailbox','event'));
