-- Cron observability (ROADMAP Phase 2): one row per scheduler-job firing.
--
-- Written by scheduler.metered() on EVERY invocation — successes, failures
-- (jobs never raise; the error summary lands here), and maintenance skips
-- (skipped_by_maintenance = 1, so pause windows are visible instead of
-- looking like a dead scheduler). GET /api/cron/health aggregates per job;
-- the janitor deletes rows older than 30 days, so the table IS the window.
--
-- fired_at is UTC ISO (bus.now_iso()).
CREATE TABLE IF NOT EXISTS cron_metrics (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  job                    TEXT NOT NULL,           -- metered() name, e.g. 'briefing', 'janitor'
  fired_at               TEXT NOT NULL,
  duration_ms            INTEGER NOT NULL DEFAULT 0,
  ok                     INTEGER NOT NULL DEFAULT 1 CHECK (ok IN (0, 1)),
  error                  TEXT,                    -- compact summary when ok = 0
  skipped_by_maintenance INTEGER NOT NULL DEFAULT 0 CHECK (skipped_by_maintenance IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_cron_metrics_job ON cron_metrics(job, fired_at);
CREATE INDEX IF NOT EXISTS idx_cron_metrics_fired ON cron_metrics(fired_at);
