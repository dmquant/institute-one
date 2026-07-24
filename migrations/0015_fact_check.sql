-- Phase 3: Fact-check v2 — claim extraction, tier-1 reuse gate, verification,
-- disputed surfacing (ROADMAP Phase 3; proposal §6.2 row 3).
-- Numbered 0015: pre-allocated by the main agent for the C1 partition;
-- db.migrate() applies files in sorted order, gaps are fine.
--
-- Times: created_at/verified_at/expires_at/started_at/finished_at/
-- verify_started_at are bus.now_iso() (UTC ISO seconds — lexicographic order
-- == time order); work_date is the SGT calendar date (drives the daily
-- verification attempt cap, same convention as research_log.work_date).

-- ============ extraction queue ============
-- Durable "extract claims from this source" work, fed by bus.on hooks
-- (whiteboard.card_completed / research.completed) and drained by the
-- factcheck tick under the maintenance gate — extraction burns model quota,
-- so it must NOT run inside a bus handler (handlers must stay fast and never
-- raise; gated scheduler jobs are where new model work is allowed to start).
-- UNIQUE(source_kind, source_ref): one extraction per source, ever — hooks
-- INSERT OR IGNORE so replayed/duplicate completion events are no-ops.
CREATE TABLE IF NOT EXISTS fact_extract_queue (
  id          TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('whiteboard_card','research_report','daily')),
  source_ref  TEXT NOT NULL,             -- whiteboard_cards.id | research_queue.id | daily ref
  analyst_id  TEXT,                      -- claiming analyst when the source has one (cards do, reports don't)
  status      TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')) DEFAULT 'pending',
  error       TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,                      -- claim time; a stale 'running' (crash) is re-opened by tick()
  finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_extract_source ON fact_extract_queue(source_kind, source_ref);
CREATE INDEX IF NOT EXISTS idx_fact_extract_status ON fact_extract_queue(status, created_at);

-- ============ fact cards ============
-- One row per extracted checkable claim. status lifecycle:
--   pending           → awaiting verification
--   verifying         → durable pre-call claim (REVIEW-C1 P1-1): verify_pending
--                       conditional-claims pending→verifying BEFORE the model
--                       call, so two processes can never double-verify one
--                       card; a crash leaves 'verifying' + verify_started_at,
--                       re-opened by the tick's stale sweep
--   verified/disputed/unverifiable → terminal verdicts (verified_facts row exists)
--   reused            → tier-1 gate hit a live VERIFIED neighbor: no re-verification,
--                       related_fact_id points at the reused verified_facts row
--   self_contradicted → tier-1 gate hit a DISPUTED neighbor: the claim repeats an
--                       already-refuted fact; surfaced like a dispute, never verified
-- related_fact_id is a soft reference to verified_facts.id (reused /
-- self_contradicted provenance) — no FK on purpose: pruning old facts must
-- not cascade into historical cards.
-- content_hash = sha256(source_kind|source_ref|claim)[:16]: the database-level
-- idempotency arbiter for extraction (INSERT OR IGNORE), so re-running an
-- extraction for the same source can never duplicate cards.
--
-- The daily verification budget is NOT here: attempts (successes AND failures)
-- are counted in an admin_state row 'factcheck_attempts:<work_date>' whose
-- conditional UPDATE (value < cap) is the atomic arbiter — see
-- factcheck._reserve_attempt().
CREATE TABLE IF NOT EXISTS fact_cards (
  id                TEXT PRIMARY KEY,
  source_kind       TEXT NOT NULL CHECK (source_kind IN ('whiteboard_card','research_report','daily')),
  source_ref        TEXT NOT NULL,
  analyst_id        TEXT,
  claim             TEXT NOT NULL,
  category          TEXT NOT NULL CHECK (category IN ('numerical','financial','event','policy','other')),
  status            TEXT NOT NULL CHECK (status IN ('pending','verifying','verified','disputed','unverifiable','reused','self_contradicted')) DEFAULT 'pending',
  related_fact_id   TEXT,
  content_hash      TEXT UNIQUE,
  created_at        TEXT NOT NULL,
  verify_started_at TEXT                 -- set on the pending→verifying claim; cleared on release
);
CREATE INDEX IF NOT EXISTS idx_fact_cards_status ON fact_cards(status, created_at);
CREATE INDEX IF NOT EXISTS idx_fact_cards_source ON fact_cards(source_kind, source_ref);

-- ============ verified facts ============
-- One verdict per card (UNIQUE fact_card_id). Written in the same transaction
-- as the fact_cards.status conditional claim, so a card can never be terminal
-- without its verdict row (or vice versa). expires_at = verified_at +
-- per-category TTL (from the factcheck_reuse_policy row below), FROZEN at
-- verification time — a later policy change affects new verdicts only.
-- source_urls is a JSON list of URLs pulled from the verifier's SOURCES line.
CREATE TABLE IF NOT EXISTS verified_facts (
  id           TEXT PRIMARY KEY,
  fact_card_id TEXT NOT NULL UNIQUE REFERENCES fact_cards(id) ON DELETE CASCADE,
  verdict      TEXT NOT NULL CHECK (verdict IN ('VERIFIED','DISPUTED','UNVERIFIABLE')),
  evidence     TEXT,
  source_urls  TEXT,
  work_date    TEXT NOT NULL,
  verified_at  TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verified_facts_date ON verified_facts(work_date);
CREATE INDEX IF NOT EXISTS idx_verified_facts_expiry ON verified_facts(verdict, expires_at);

-- ============ claim vectors ============
-- One embedding per fact card, written at extraction time when the vector
-- layer is live. Deliberately NOT vector_chunks (A8): that table's
-- index/search semantics are keyed to archive_files' current sha
-- (rebuild-by-source), which would silently drop non-archive sources like a
-- claim string. And deliberately NOT the vec_search vec0 virtual table: the
-- reuse gate and claim_check compare cosine in Python over a small, bounded
-- set (a few rows per day, capped by the daily verification cap), so this
-- works without the sqlite-vec extension — the whiteboard_topic_vectors
-- precedent (0011/B4). The model column inherits A8's semantics: queries
-- filter on the CURRENT embed model, so a model switch hides old vectors
-- instead of comparing across spaces (degrade-open: old claims simply stop
-- gating; a missing vector means the gate answers "fresh").
CREATE TABLE IF NOT EXISTS fact_claim_vectors (
  fact_card_id TEXT PRIMARY KEY REFERENCES fact_cards(id) ON DELETE CASCADE,
  model        TEXT NOT NULL,
  dim          INTEGER NOT NULL,
  embedding    BLOB NOT NULL,                     -- little-endian float32 array
  created_at   TEXT NOT NULL
);

-- ============ reuse policy (config row) ============
-- Per-category cosine threshold + TTL for the tier-1 reuse gate, as ONE JSON
-- admin_state row (the 0011 whiteboard_similarity idiom: admin_state is the
-- existing operator-config surface, visible via GET /api/admin/state; the
-- code keeps built-in defaults, so deleting this row degrades gracefully).
-- Numbers/finance move fast (tight threshold, short TTL); events/policy are
-- stable once true (looser threshold, long TTL).
INSERT OR IGNORE INTO admin_state (key, value) VALUES (
  'factcheck_reuse_policy',
  '{"numerical": {"threshold": 0.92, "ttl_days": 7}, "financial": {"threshold": 0.92, "ttl_days": 7}, "event": {"threshold": 0.88, "ttl_days": 30}, "policy": {"threshold": 0.88, "ttl_days": 30}, "other": {"threshold": 0.90, "ttl_days": 14}}'
);
