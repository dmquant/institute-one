# PATCH-NOTES-NORTHSTAR-R1 — ROADMAP partial gaps

## Scope

This patch closes the remaining client configuration surfaces for bilingual
locale and optional bearer auth, then adds the executor's per-hand queue-depth
guard where the persisted task-status contract permits.

## Initial verification

- SPA locale: the backend already exposes `GET/PUT /api/bilingual/preference`;
  the SPA had no client functions or Settings control.
- Auth: the pure-ASGI middleware already protects every `/api/*` route when
  `INSTITUTE_TOKEN` is set. The SPA and Obsidian plugin had no token storage or
  `Authorization` header support.
- MCP: `api_mcp.router` is included on the same FastAPI app after
  `install_auth(app)`, and its routes are under `/api/mcp`; no MCP backend
  change is needed.
- Executor depth: no cap existed. `tasks.status` still had the original SQLite
  `CHECK`, which excludes `overcommitted`; a durable new terminal status
  therefore required a migration, initially outside this task's file allowlist
  and later authorized as `migrations/0028_task_overcommitted.sql` (round 2
  below).

## Decisions and deviations

- Keep locale scope to Settings preference read/write only; twin rendering is
  unchanged.
- Read client tokens at request time so saving or clearing a token takes effect
  without reconstructing the API client.
- SPA JSON/text requests now read `institute:token` from `localStorage`;
  Settings provides a password input and reconnects after save. All four
  `fetch` paths in `api.ts`, including project digest and streaming ask, attach
  the bearer header. The Obsidian plugin persists `token` in plugin settings
  and adds the header to both JSON and text requests.
- Do not fake `overcommitted` as `failed` or as an in-memory-only task: either
  would violate the task-row audit contract. Await permission for the required
  status migration.

## Round 2 — per-hand queue-depth cap (`overcommitted`, migration 0028)

- `migrations/0028_task_overcommitted.sql`: SQLite cannot alter a CHECK, so
  the file is the standard table rebuild inside `db.migrate()`'s single
  transaction — create `tasks_rebuild_0028` with the widened enum, copy all
  rows with an explicit column list (verified against live
  `PRAGMA table_info(tasks)`: the nineteen 0001 columns plus 0024's
  `fallback_chain`/`lineage_root`; nothing references `tasks` by FK and no
  triggers/views exist), drop, rename, recreate all six indexes (0001
  `idx_tasks_*` x4, 0009 `idx_tasks_status_finished`, 0024
  `uq_tasks_lineage_active`). Rehearsed against a `.backup` copy of the
  741-row production DB: integrity_check ok, row count preserved, indexes
  present, `overcommitted` insert accepted, bogus status still rejected.
  No ADD COLUMN, so the M8-001 `_skip_add_column` crash guard is never in
  play; `IF NOT EXISTS` keeps a manual replay harmless. 0026/0027 were taken
  by parallel partitions (neither touches `tasks`); 0028 numbering confirmed
  free.
- `app/config.py`: `hand_queue_depth: int = 8` (`INSTITUTE_HAND_QUEUE_DEPTH`);
  `<=0` disables.
- `app/router/executor.py`: `TERMINAL` gains `overcommitted`; `submit()` and
  `spawn()` run an admission check before row creation — a hand whose queued
  backlog (counted by `requested_hand`, since `hand` is NULL until the running
  claim) STRICTLY EXCEEDS the cap sheds the task as a born-terminal
  `overcommitted` row (hand NULL, `finished_at == created_at`, single
  `task.overcommitted` event, no queued/running events, no asyncio task).
  Strictly-greater deliberately admits a backlog of exactly the cap: the
  analyst-daily sweep legitimately parks up to roster-size (currently 9)
  queued rows on one hand via `asyncio.gather`, and the default cap of 8 with
  a `>=` check would have shed the tail of every normal sweep. The check is
  best-effort (count and INSERT are separate statements) — concurrent submits
  can overshoot by a few rows, acceptable for load shedding.
- `app/api/tasks.py`: unchanged — cancel's 409 guard uses `executor.TERMINAL`
  (auto-covers the new status) and retry stays failed-only; an overcommitted
  retry endpoint is a possible follow-up, not in scope.
- `/api/contract` needs no code change: enums import from `executor`, and the
  live-schema cross-check parses the rebuilt CHECK.
- `frontend/src/api.ts`: `TaskStatus` union gains `"overcommitted"` (type
  only; pages untouched per partition, so the Tasks filter dropdown and the
  `StatusBadge` zh map don't know the new value yet — the badge renders the
  raw string).
- `tests/test_executor.py` +4: submit fast-fail beyond cap (born-terminal row,
  event shape, cancel refuses), spawn fast-fail, at/under-cap admission with
  per-hand isolation, and `recover_orphans()` leaving `overcommitted` alone
  while sweeping the queued backlog.

Out-of-partition follow-ups (not done here): `scorecard.TERMINAL_STATUSES`
deliberately keeps excluding `overcommitted` (shed rows never ran on a hand,
so they carry no execution-quality signal — but if shed-rate should show up in
hand stats later, that constant is the place); SPA pages/`ui.tsx` could add a
filter option and a zh label for the new badge.

## Open risks

- `frontend/src/useSSE.ts` has a direct streaming `fetch` outside `api.ts`, so
  it cannot attach the configured token under this task's file allowlist.
  Authenticated deployments still receive durable events through the
  token-aware `listEvents` polling fallback, but low-latency stream wake-ups
  remain disconnected until that file is allowed to use the shared auth
  headers.

## Verification

- `cd frontend && npx tsc -noEmit` — passed.
- `cd obsidian-plugin && npx tsc -noEmit -skipLibCheck` — passed.
- `.venv/bin/python -m pytest tests/test_executor.py tests/test_auth.py -q` —
  14 passed (round-1 baseline, before the depth work).

Round 2:

- `.venv/bin/python -m pytest tests/test_executor.py tests/test_db_migrate.py
  tests/test_restart_recovery.py -q` — 36 passed.
- `.venv/bin/python -m compileall app -q` — clean; both tsc checks re-passed.
- Migration rehearsal on a production-DB backup copy (see round-2 notes).
- Full-suite context (`pytest tests -q`): 926 passed, 7 failed — all 7
  verified NOT from this partition: 5 vector/similarity failures reproduce
  with this diff stashed and pass with `INSTITUTE_ENABLE_VECTORS=false`
  (the repo `.env` now sets it true, leaking past conftest's env pinning);
  `test_mcp_roundtrip` expects 21 scheduler jobs but a parallel partition's
  `scheduler.py` change registers 22; `test_contract` parses the enum from
  `0001_init.sql` text and needs its parser taught about the 0028 rebuild —
  that test file belongs to another partition, so the fix is left to the
  integrator (the live-schema `schema_cross_check` itself reports "ok").
