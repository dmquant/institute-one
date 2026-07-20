-- Research hardening (ROADMAP Phase 0): give research_log an SGT work_date.
--
-- Problem: research_log.completed_at is a UTC ISO timestamp (bus.now_iso()),
-- but the research daily cap counts "today" as the SGT calendar date
-- (prompts.work_date()). Comparing substr(completed_at, 1, 10) to the SGT
-- date miscounts within ±8h of midnight (a run completed 16:00–24:00 UTC
-- belongs to the NEXT SGT day). New rows write work_date explicitly at insert
-- time; the cap compares on this column only.
--
-- Legacy-row compatibility (accepted semantics): rows created before this
-- migration keep work_date NULL — deliberately NOT backfilled. The cap query
-- is a plain `work_date = ?` equality, so NULL rows never count toward any
-- day's cap. One-time consequence: on the deployment day the counter starts
-- from the rows written after the restart, so the institute may run up to a
-- full research_daily_cap that day regardless of pre-restart completions.
-- That single-day over-run is preferred over backfilling from completed_at,
-- which would mislabel late-UTC completions and silently shift cap counting
-- for historical rows.
ALTER TABLE research_log ADD COLUMN work_date TEXT;

CREATE INDEX IF NOT EXISTS idx_research_log_work_date ON research_log(work_date);
