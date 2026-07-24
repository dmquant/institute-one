-- Operator self-improvement chain (ROADMAP Phase 6 L item, full loop — M8-008).
--
-- 0023 shipped the minimal recipe reuse loop (approved disposition → recipe →
-- zero-model-call suggestions). This migration adds the remaining chain:
--
--   operator_observations  durable daily metric snapshots of operator
--                          behaviour (which action kinds recur, per-recipe hit
--                          rates, router suggestion quality). One snapshot per
--                          (kind, subject, SGT work date) — the unique index
--                          is the race backstop for the sweep's check-then-
--                          insert (the 0018 feeds idiom). recipe_id links a
--                          snapshot to the recipe it observes.
--   operator_proposals     system-generated improvement proposals derived
--                          from observations (promote a disposition to a
--                          recipe / retire a bad recipe / tighten a whitelisted
--                          parameter). Every proposal also opens an
--                          operator_actions inbox card (ref 'proposal:<id>').
--                          A proposal APPLIES ONLY through the explicit web-UI
--                          human approve endpoint (POST /api/operator/
--                          proposals/{id}/approve) — never via vault
--                          frontmatter, never via MCP (proposal §8.2
--                          invariant). Status transitions are conditional
--                          claims; the partial unique index on dedupe_ref
--                          stops duplicate LIVE proposals for the same change.
--   operator_effects       before/after effect measurement. A row opens when
--                          a recipe is promoted/retired or a parameter
--                          changes: baseline metrics freeze at that moment;
--                          outcome stays NULL until the measure sweep fills it
--                          (UPDATE … WHERE outcome IS NULL — the conditional
--                          claim). One effects row per applied proposal
--                          (partial unique index).
--   parameter_history      whitelisted-parameter change history (operator.py
--                          PARAMETER_KEYS — only operator:confidence_floor
--                          today). old/new values are JSON; NULL means the key
--                          was/became unset (built-in default). Rollback is a
--                          conditional claim on rolled_back_at plus a byte-CAS
--                          against admin_state, recorded as a NEW history row
--                          (changed_by 'rollback:<id>', rollback_of set).
--
-- kind / subject_kind vocabularies carry NO CHECK on purpose (open sets,
-- code-enforced in app/institute/operator.py — the recipes-status precedent
-- from 0023; 0018's CHECKed action kinds forced proposals to ride as 'other').
-- Closed sets (proposal status, applied) keep CHECKs, 0018-style.
-- Times are bus.now_iso() (UTC ISO); work_date is prompts.work_date() (SGT).

-- ============ observations (daily metric snapshots) ============
CREATE TABLE IF NOT EXISTS operator_observations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,             -- operator.OBSERVATION_KINDS (code-enforced)
  subject     TEXT NOT NULL,             -- action kind | 'recipe:<id>' | 'router'
  recipe_id   INTEGER,                   -- set when the observation is about a recipe
  work_date   TEXT NOT NULL,             -- SGT work date of the sweep (dedupe key)
  window_days INTEGER NOT NULL,
  metrics     TEXT NOT NULL DEFAULT '{}',-- JSON numbers, self-describing
  created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_observations_day
  ON operator_observations(kind, subject, work_date);
CREATE INDEX IF NOT EXISTS idx_operator_observations_recipe
  ON operator_observations(recipe_id) WHERE recipe_id IS NOT NULL;

-- ============ proposals (the operator inbox items) ============
CREATE TABLE IF NOT EXISTS operator_proposals (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL,          -- operator.PROPOSAL_KINDS (code-enforced)
  title          TEXT NOT NULL,
  rationale      TEXT NOT NULL DEFAULT '',
  params         TEXT NOT NULL DEFAULT '{}',  -- JSON: what to change (ids / key+value)
  dedupe_ref     TEXT NOT NULL DEFAULT '',    -- e.g. promote_recipe:<disp> | retire_recipe:<id> | set_parameter:<key>
  observation_id INTEGER,                -- provenance: the observation that triggered it
  recipe_id      INTEGER,                -- linked recipe when applicable
  action_id      INTEGER,                -- the kanban inbox card surfacing this proposal
  status         TEXT NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed','approved','rejected')),
  decided_at     TEXT,
  decided_note   TEXT,
  applied        INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0,1)),
  created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_proposals_live_ref
  ON operator_proposals(dedupe_ref) WHERE status = 'proposed' AND dedupe_ref <> '';
CREATE INDEX IF NOT EXISTS idx_operator_proposals_status
  ON operator_proposals(status, kind);

-- ============ effect measurement (before/after windows) ============
CREATE TABLE IF NOT EXISTS operator_effects (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_kind TEXT NOT NULL,            -- operator.EFFECT_SUBJECT_KINDS (code-enforced)
  subject_ref  TEXT NOT NULL,            -- 'recipe:<id>' | 'param:<key>'
  recipe_id    INTEGER,
  proposal_id  INTEGER,
  window_days  INTEGER NOT NULL,
  baseline     TEXT NOT NULL DEFAULT '{}',  -- metrics frozen when the change landed
  outcome      TEXT,                     -- NULL until measured (conditional-claim target)
  baseline_at  TEXT NOT NULL,
  measured_at  TEXT,
  created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_effects_proposal
  ON operator_effects(proposal_id) WHERE proposal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_operator_effects_subject
  ON operator_effects(subject_kind, subject_ref);

-- ============ parameter history (rollback-able) ============
CREATE TABLE IF NOT EXISTS parameter_history (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  key            TEXT NOT NULL,          -- admin_state key (operator.PARAMETER_KEYS whitelist)
  old_value      TEXT,                   -- JSON; NULL = key was unset (built-in default)
  new_value      TEXT,                   -- JSON; NULL = change unset the key (rollback to default)
  changed_by     TEXT NOT NULL,          -- 'api' | 'proposal:<id>' | 'rollback:<history_id>'
  proposal_id    INTEGER,
  rollback_of    INTEGER,                -- history row this change reverts
  rolled_back_at TEXT,                   -- set on the ORIGINAL row once reverted (conditional-claim target)
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parameter_history_key ON parameter_history(key, id);
