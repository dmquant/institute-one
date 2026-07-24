-- M8-013: durable delivery intent for disputed fact-check claims.
--
-- A DISPUTED verdict (or self_contradicted card) and its notification intent
-- are written in one transaction. The drain later materializes that intent as
-- a mailbox thread; retries are bounded and visible through the fact-check API.
CREATE TABLE IF NOT EXISTS factcheck_dispute_outbox (
  id           TEXT PRIMARY KEY,
  dispute_id   TEXT NOT NULL,
  fact_card_id TEXT NOT NULL REFERENCES fact_cards(id) ON DELETE CASCADE,
  recipient_id TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','delivered','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error   TEXT,
  created_at   TEXT NOT NULL,
  delivered_at TEXT,
  UNIQUE (dispute_id, recipient_id)
);

CREATE INDEX IF NOT EXISTS idx_factcheck_dispute_outbox_status
  ON factcheck_dispute_outbox(status, attempts, created_at);
