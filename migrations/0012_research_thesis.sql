-- Thesis-aware research queue (card M3-001).
-- Contract: roadmap/backlog.json M3-001 acceptance (the design docs
-- design/local-thesis-alpha/03-infinite-research-loop.md and
-- 10-market-thesis-data-bootstrap.md are gitignored and absent in this clone).
-- Numbered 0012: 0008-0011 are reserved by parallel second-round cards;
-- db.migrate() applies files in sorted order, gaps are fine.
--
-- Every new column is NULLABLE and defaults to NULL, so pre-migration rows and
-- topic-only enqueues are untouched: the old behavior (topic dedup, cooldown by
-- research_log.topic, tick/claim/cap semantics) does not change for them.
--
-- Dual-rail dedup/cooldown design:
--   topic rail (old)   rows with dedup_key NULL. Pending dedup matches on
--                      research_queue.topic, the 30-day cooldown on
--                      research_log.topic — both restricted to dedup_key IS
--                      NULL rows. Every pre-0012 row has dedup_key NULL, so
--                      for all pre-migration state the queries are
--                      semantically equivalent to the old code path.
--   structured rail    rows carrying thesis/security context. dedup_key =
--                      sha256(thesis_id, security_id, normalized question) is
--                      computed at insert time (research.structured_dedup_key;
--                      normalization = NFKC + casefold + whitespace collapse).
--                      Pending dedup matches research_queue.dedup_key; the
--                      cooldown matches research_log.dedup_key, which is
--                      written when a structured item completes. Old
--                      research_log rows keep dedup_key NULL and can never
--                      collide with a structured cooldown probe.
-- The rails are independent BY DESIGN: a structured item never dedups against
-- a topic-only item with the same topic string (same thesis+security with a
-- differently-worded question is a different task), and — deliberately unlike
-- the pre-0012 rule — a topic-only enqueue matches only topic-rail rows, so a
-- narrow structured pending item never swallows a broader topic request.
--
-- FKs use ON DELETE SET NULL: the queue is a durable work log; deleting a
-- thesis/security must not erase research history, and the stored dedup_key
-- keeps dedup/cooldown stable even after the reference is nulled.

ALTER TABLE research_queue ADD COLUMN thesis_id TEXT REFERENCES theses(id) ON DELETE SET NULL;
ALTER TABLE research_queue ADD COLUMN security_id TEXT REFERENCES securities(id) ON DELETE SET NULL;
ALTER TABLE research_queue ADD COLUMN question TEXT;
ALTER TABLE research_queue ADD COLUMN output_type TEXT;      -- open set (e.g. deep_report); domain-validated, no CHECK (additive migrations cannot widen one)
ALTER TABLE research_queue ADD COLUMN priority_reason TEXT;  -- free text, e.g. practical.actionCode=deep_research_candidate
ALTER TABLE research_queue ADD COLUMN dedup_key TEXT;        -- structured rail only; NULL = topic rail

ALTER TABLE research_log ADD COLUMN dedup_key TEXT;          -- structured cooldown key; NULL on topic-rail and legacy rows

CREATE INDEX IF NOT EXISTS idx_research_queue_dedup ON research_queue(dedup_key, status);
CREATE INDEX IF NOT EXISTS idx_research_log_dedup ON research_log(dedup_key, completed_at);

-- Concurrency backstop for the structured rail: at most ONE active
-- (pending/running) item per triple. enqueue()'s check-then-insert is not
-- atomic under concurrent callers (two seeds racing, double-clicked API);
-- this partial unique index makes the INSERT itself the arbiter — the loser
-- hits the constraint and re-reads the winner (A2 rowcount/conditional-claim
-- spirit: the database decides, not the pre-read). Completed/failed/cancelled
-- rows leave the index, so a triple can be re-researched later; the topic
-- rail (dedup_key NULL) is untouched — partial indexes never index NULL keys,
-- preserving the old rail's permissive historical behavior.
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_queue_dedup_active
  ON research_queue(dedup_key) WHERE dedup_key IS NOT NULL AND status IN ('pending','running');
