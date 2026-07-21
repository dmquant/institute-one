-- Event-outbox drainer lease, split from 0034 after the live upgrade exposed
-- why an applied migration must remain immutable.
--
-- mailbox intents are claimed and materialized in one transaction. Event
-- intents must call bus.emit outside that transaction, so an attempts-value
-- CAS alone lets a second drainer re-select the row between claim and emit.
-- lease_id is the claim->emit->delivered mutex; leased_at lets the stale sweep
-- recover a drainer that died in that window.
--
-- The live database had already recorded 0034 before these columns were
-- designed. Keeping them only in this new additive file both upgrades that
-- database and restores byte-stable 0034 semantics for every fresh install.
ALTER TABLE factcheck_dispute_outbox ADD COLUMN lease_id TEXT;
ALTER TABLE factcheck_dispute_outbox ADD COLUMN leased_at TEXT;
