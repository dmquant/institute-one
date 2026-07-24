-- Durable idempotency ledger for retry-safe roadmap create mutations (M7-010).
-- Scope includes the route and parent resource, so keys are reusable across
-- unrelated create operations while remaining unique within one mutation scope.

CREATE TABLE IF NOT EXISTS roadmap_idempotency_keys (
  scope             TEXT NOT NULL,
  idempotency_key   TEXT NOT NULL,
  request_hash      TEXT NOT NULL,
  response_json     TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (scope, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_roadmap_idempotency_created
  ON roadmap_idempotency_keys(created_at);
