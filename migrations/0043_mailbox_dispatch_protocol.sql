-- R5 QUEUE AUDIT: make one mailbox dispatch a bounded, recoverable protocol.
--
-- A dispatch now books its executor task in the same transaction as its
-- lease/attempt claim.  mailbox_dispatch_id keeps every task auditable even
-- after the dispatch advances to a later attempt.  A successful reply names
-- its dispatch and is unique for that dispatch; the reply, dispatch terminal
-- state, thread timestamp, and durable events row commit together.
ALTER TABLE mailbox_messages ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mailbox_messages ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mailbox_messages ADD COLUMN dispatch_error TEXT;
ALTER TABLE mailbox_messages ADD COLUMN dispatch_id INTEGER;
ALTER TABLE mailbox_messages ADD COLUMN reply_event_id INTEGER;

ALTER TABLE tasks ADD COLUMN mailbox_dispatch_id INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mailbox_reply_dispatch
  ON mailbox_messages(dispatch_id)
  WHERE kind = 'reply' AND dispatch_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mailbox_dispatch_reply_event
  ON mailbox_messages(reply_event_id)
  WHERE kind = 'dispatch' AND reply_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mailbox_dispatch_recovery
  ON mailbox_messages(status, dispatch_attempts, id)
  WHERE kind = 'dispatch';

CREATE INDEX IF NOT EXISTS idx_tasks_mailbox_dispatch
  ON tasks(mailbox_dispatch_id, created_at)
  WHERE mailbox_dispatch_id IS NOT NULL;
