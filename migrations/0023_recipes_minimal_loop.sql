-- Recipes minimal self-improvement loop (ROADMAP Phase 6 L item, first slice).
--
-- 0018 shipped `recipes` as a schema placeholder (pattern, disposition,
-- created_at; no code touched it). This migration turns it into the minimal
-- reuse loop: a human-approved disposition can be promoted into a recipe;
-- route_actions() consults active recipes BEFORE calling a model — a match
-- produces the suggestion directly (still shadow=1, zero model calls).
-- observations/proposals/effect measurement stay future work (PATCH-NOTES-E7).
--
-- Columns are ADDed (0018 is on production and immutable). New columns avoid
-- CHECK/REFERENCES on purpose: the ADD COLUMN crash-recovery guard in
-- app/db.py compares type/NOT NULL/DEFAULT only (S4-P0-01), so constraint-free
-- declarations keep the recovery path provable. Enum/reference integrity is
-- enforced in code (app/institute/operator.py).
--
--   kind        which operator_actions.kind this recipe applies to
--   keywords    space-joined casefold tokens extracted from the source
--               action's title; ALL must substring-match a candidate title
--   confidence  inherited from the promoted disposition; recipe-proposed
--               dispositions carry it and face the SAME live consumption gate
--   source_disposition_id  provenance (the approved disposition it came from)
--   status      'active' | 'retired' (code-enforced vocabulary)
--
-- action_dispositions.recipe_id: non-NULL marks a suggestion produced by a
-- recipe match instead of a model call. proposed_by stays fast_loop/deep_loop
-- (0018's CHECK is immutable), so recipe suggestions still occupy the
-- propose-once-per-loop slot (0022's partial unique index) — a recipe hit and
-- a model proposal can never duplicate for the same (action, loop).

ALTER TABLE recipes ADD COLUMN kind TEXT NOT NULL DEFAULT 'other';
ALTER TABLE recipes ADD COLUMN keywords TEXT NOT NULL DEFAULT '';
ALTER TABLE recipes ADD COLUMN confidence REAL;
ALTER TABLE recipes ADD COLUMN source_disposition_id INTEGER;
ALTER TABLE recipes ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE recipes ADD COLUMN retired_at TEXT;

ALTER TABLE action_dispositions ADD COLUMN recipe_id INTEGER;

-- promote-once backstop (the feeds' 0018/0022 idiom): one recipe per source
-- disposition — a concurrent double-promote loses on the index and converges.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recipes_source_disposition
  ON recipes(source_disposition_id) WHERE source_disposition_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_recipes_status_kind ON recipes(status, kind);
