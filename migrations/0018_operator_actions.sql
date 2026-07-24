-- Operator loop & triage (ROADMAP Phase 6, first slice).
-- Numbered 0018: 0015-0017 are reserved by parallel round-3 cards (C1-C3);
-- db.migrate() applies files in sorted order, gaps are fine (0009 precedent).
--
-- Three tables:
--   operator_actions    the actions kanban: one row per thing a human (or,
--                       later, an approved recipe) must look at. Fed by bus
--                       events + the vault-conflict sweep (app/institute/
--                       operator.py); status transitions are conditional
--                       claims (WHERE status IN ...), done/dismissed terminal.
--   action_dispositions model-proposed (or human) dispositions for an action.
--                       SHADOW MODE IRON RULE: shadow=1 rows are logged
--                       suggestions only and are NEVER executed automatically;
--                       this round writes shadow=1 unconditionally. Proposals
--                       become real only through the explicit human approval
--                       endpoint (POST /api/operator/dispositions/{id}/approve)
--                       -- never via vault frontmatter, never via MCP.
--   recipes             placeholder for the Phase 6 L item (recurring fixes
--                       become recipes). Schema reserved now so 0018 stays
--                       final; NO code reads or writes it this round.
-- Times: created_at/updated_at/resolved_at are bus.now_iso() (UTC ISO).

-- ============ operator actions (the kanban) ============
CREATE TABLE IF NOT EXISTS operator_actions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN
                ('vault_conflict','disputed_fact','scorecard_anomaly',
                 'failed_run','cron_failure','other')),
  ref         TEXT NOT NULL DEFAULT '',   -- e.g. task:<id> | workflow:<run> | vault:<path> | fact:<id> | scorecard:<date>
  title       TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                ('open','in_progress','done','dismissed')),
  priority    INTEGER NOT NULL DEFAULT 1, -- higher = more urgent
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  resolved_at TEXT,
  resolution  TEXT
);
CREATE INDEX IF NOT EXISTS idx_operator_actions_status ON operator_actions(status, kind);

-- Feed idempotency backstop: at most ONE live (open/in_progress) action per
-- ref. Feeds check-then-insert; this partial unique index closes the await
-- race between the check and the insert. Empty refs (manual/other actions)
-- are exempt. A resolved/dismissed ref can be re-opened as a NEW action.
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_actions_live_ref
  ON operator_actions(ref) WHERE status IN ('open','in_progress') AND ref <> '';

-- ============ action dispositions (shadow suggestions) ============
-- flags is a comma-joined marker set (not in the original shorthand spec; the
-- low_confidence / human_pinned markers need a queryable home):
--   low_confidence  confidence missing or < 0.7 (the ROADMAP confidence floor)
--   human_pinned    prompt/schedule-change territory -- may NEVER auto-act,
--                   even after shadow mode ends (ROADMAP Phase 6 hard pin)
--   approved        a human approved this suggestion via the web UI endpoint
CREATE TABLE IF NOT EXISTS action_dispositions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id   INTEGER NOT NULL REFERENCES operator_actions(id) ON DELETE CASCADE,
  proposed_by TEXT NOT NULL CHECK (proposed_by IN ('fast_loop','deep_loop','human')),
  disposition TEXT NOT NULL,              -- router vocabulary or 'unparsed'
  confidence  REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  shadow      INTEGER NOT NULL DEFAULT 1 CHECK (shadow IN (0,1)),
  flags       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_dispositions_action ON action_dispositions(action_id, proposed_by);

-- ============ recipes (Phase 6 L placeholder -- schema only) ============
CREATE TABLE IF NOT EXISTS recipes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern     TEXT NOT NULL,              -- what recurring situation this matches
  disposition TEXT NOT NULL,              -- the fix that worked
  created_at  TEXT NOT NULL
);
