-- Prompt overrides (ROADMAP Phase 2 M item — makes prompt iteration data
-- instead of code, relaxing CLAUDE.md rule 4 SAFELY: with no active override
-- every prompt renders byte-identically to the code constants).
--
--   prompt_overrides   one row per staged/live/former override of a prompt
--                      mount point. scope names the mount point (e.g.
--                      'prompts.citation_mandate'); it carries NO CHECK on
--                      purpose (open set, code-enforced against the scope
--                      registry in app/institute/prompt_overrides.py — the
--                      recipes-status precedent from 0023). Lifecycle is the
--                      closed set shadow → active → retired (CHECKed,
--                      0018-style): shadow rows are recorded drafts that
--                      never affect prompts; the partial unique index is the
--                      race backstop guaranteeing at most ONE active row per
--                      scope (transitions in code are conditional claims and
--                      retire the old active inside the same transaction);
--                      retired rows are immutable history — what ran when —
--                      the thesis_versions idiom. Re-activating old content
--                      means a NEW shadow row, never editing history (the
--                      parameter_history rollback-as-new-row idiom).
-- Times are bus.now_iso() (UTC ISO).

CREATE TABLE IF NOT EXISTS prompt_overrides (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  scope        TEXT NOT NULL,             -- mount point (registry code-enforced)
  content      TEXT NOT NULL,             -- override text (placeholders validated in code)
  status       TEXT NOT NULL DEFAULT 'shadow'
                 CHECK (status IN ('shadow','active','retired')),
  note         TEXT NOT NULL DEFAULT '',  -- operator rationale
  created_at   TEXT NOT NULL,
  activated_at TEXT,                      -- set on shadow → active
  retired_at   TEXT                       -- set on active → retired
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_overrides_active_scope
  ON prompt_overrides(scope) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_prompt_overrides_scope
  ON prompt_overrides(scope, status);
