# Changelog

Notable changes to institute-one, grouped by push batch (dates are SGT work dates). Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 2026-07-23 — Frontend render performance batch

### Changed
- Dashboard event-driven refetches throttled to a 5s minimum gap (same pattern as the Topbar): busy-bus periods no longer fire two requests per event; the 15s/30s polls and the live `EventFeed` are unaffected. The per-second `useNow` tick moved out of the page into a small `HandCooldown` badge component — the 299-line page no longer re-renders every second for two countdown badges.
- Route-level code splitting: 20 of 21 pages are `React.lazy` behind a `Suspense` boundary (Dashboard stays eager for first paint). Main bundle 310.55 → 197.75 kB (−36%), gzip 95.84 → 65.69 kB (−31%); pages load on demand.
- Insights chart aggregations (`EventStackChart`, `TaskSuccessChart`, `ResearchTrendChart`) and the Forecasts `favoriteIds` set are memoized against their data inputs.

## 2026-07-23 — Audit follow-through batch 3 (functional bugs, consistency, hygiene)

### Added
- Plugin ask-stream now sends the bearer token when configured — previously the streaming view simply 401'd in the one secured deployment mode; 401 surfaces an explicit token hint, and the stream has an overall timeout. `runAsk` falls back to creating an Ask note when the captured editor went stale (a finished 15-minute answer can no longer be lost and misreported as failure). Settings validate the base URL (`http(s)` only).
- MCP read-tool bounds: `mailbox_get_thread` takes a latest-window `limit` (default 50) and every pre-Phase-8 read tool (`research_log_recent`, `fact_cards_list`, `events_recent`) is clamped to the 8KB `_READ_OUTPUT_CAP`. New `resolve_ask` shared by HTTP and MCP ask paths — MCP gains the idle-hand preference plus `model`/`timeout_s` pass-through, retiring the drifted parallel implementation.
- Config validation: `max_concurrent >= 1` (the error points at the maintenance switch instead of silently starving the semaphore), `default_timeout_s > 0`, `output_cap_bytes > 0`, and `timezone` pre-checked via zoneinfo at settings load. `research_daily_cap <= 0` is documented as "disabled" and logs once per process instead of running a healthy-looking no-op job.
- `HandRegistry.pick_weighted()`: the four drifted weighted-hand selection copies (workflows / mailbox / whiteboard / analyst_daily) converge on one implementation, pool semantics preserved per call site.
- Workflow variable validation: a declared, non-lazy variable that is missing or blank (e.g. research `TOPIC` from a manual UI run) is rejected with 400 before any run row exists — no more literal `${TOPIC}` burning seven model calls; `ANALYST_CATALOG` is lazy-computed like `WEEK_DISPUTES`.
- The forecasts list endpoint inlines each row's settlement via one batch query, so the SPA Forecasts page and plugin verdict badges actually render (they consumed a field the list never returned).
- useSSE multiplexing: all unfiltered hook instances share one module-level `/api/events/stream` with per-subscriber cursors and fan-out wakeups; filtered hooks keep typed streams. Public hook API and catch-up semantics unchanged.

### Changed
- `topic_pool.added` is emitted from the domain layer (`whiteboard.add_topic` on real insert) — HTTP-originated topics now appear in the event feed too; the MCP adapter's own emit is gone.
- Mailbox dispatch lease TTL follows `default_timeout_s` (45-minute floor), so raising the executor timeout can no longer cause duplicate dispatches of a live task.
- Scheduler maintenance / feature-switch reads sit behind a 5s TTL cache with explicit invalidation on every write path; `/api/cron/health` aggregates are limited to their stated 30-day window; `sessions.list_messages` caps at the latest 500; scorecard scoring paginates by id keyset (200/batch); SQLite runs `PRAGMA synchronous=NORMAL` under WAL.
- CLI: `institute doctor` reuses `operator._classify_vault_rows` (the second live copy of the classifier deleted); auth probes distinguish a renamed/removed status subcommand from "not logged in" (WARN instead of FAIL); wildcard/IPv6 bind hosts map to loopback for health probes, fixing false "NOT RUNNING".
- Plugin: dashboard skips refreshes while hidden and defers collapsed sections until first opened; status bar and dashboard share a 5s meta/dailyStatus cache invalidated on mutations; the injected `<style>` moved to `styles.css` (Obsidian loads/unloads it); prompt-hydration failures cache a marker with a retry button instead of refiring doomed requests; `obsidian` pinned to `^1`; `install-plugin.sh` rebuilds when any `src/*.ts` or `roadmap/backlog.json` is newer than `main.js`.
- Frontend: `EventFeed` filtering/previews memoized; `loadVectorHealth` moved into `api.ts` (auth + SPA-fallback + timeout for free); `Research.tsx` widening casts removed (fields declared on the API types); Workflows run form no longer renders lazy variables as inputs and treats blank strings as unset; vitest downgraded to `^3.2` (peer-compatible with vite 5).
- `workflows/research.json`: five `curl -s` calls became `curl -sf` (a 401 body is no longer fed to the model as research content), and steps 01–06 pin `timeout_s: 1800` explicitly like the other workflows.
- `roadmap/backlog.json`: M8 cards' `design_links` now point at `docs/history/` (20 bare filenames fixed — those cards are executable again), dead `expected_files` entries repaired, and `card-template.md` realigned to the actual 13-field card schema.
- Three real-delay test sleeps replaced with fake clocks / event gates (`test_operator.py`'s 0.4s blocking-read stays — it is the payload under test, not a timing guess).

## 2026-07-23 — Project-review optimization batch (hygiene, hot paths, test gaps)

### Added
- `GET /api/forecasts/stats`: settled hit/miss/partial counts aggregated server-side over the performance scope (backfill excluded) — the SPA dashboard used to page up to 500 settled forecasts and fetch each settlement row client-side to derive the same numbers; it now makes one request.
- Opt-in local git hooks: `scripts/install-hooks.sh` points `core.hooksPath` at the committed `scripts/git-hooks/pre-commit` (ruff + compileall on staged Python, `tsc` on staged SPA sources, and a rebuilt-`main.js` check when `obsidian-plugin/src` is staged).
- Test coverage: `tests/test_claims.py` locks the shared conditional-claim algorithm directly (one winner, CAS takeover/release, lease staleness rules, heartbeat renew/lose); SPA smoke tests for the Mailbox (unanswered badge, thread creation) and Workflows (run start, run-now skip note) pages; `test_forecasts.py` covers the new stats aggregate.

### Changed
- `observe_operator` collapses its N+1 queries: the four per-kind recurrence COUNTs are one conditional-SUM scan, and per-recipe window hits are pre-aggregated in a single GROUP BY instead of one query per recipe.
- Bounded fan-out for the two unbounded gathers: the analyst-dailies sweep and the research-tree tick now run under a serialized-turns time budget — a wedged driver can no longer hang the sweep forever behind a live heartbeat (dailies) or block every future tick (trees; the timed-out batch requeues its running nodes).
- The unhandled-exception handler no longer echoes `str(exc)` to clients (raw exception text can carry local paths / SQL fragments); the traceback stays in the server log and the `error`/`path`/`transient` fields are unchanged.
- `roadmap.import_backlog` reads the seed file off-loop (`asyncio.to_thread`), closing the last sync-read straggler from the batch-2 offload sweep.
- `.qoder/` (IDE workspace cache) is gitignored.

## 2026-07-22 — Audit follow-through batch 2 (shared helpers, event-loop offload, hardening)

### Added
- `app/institute/claims.py`: one shared admin_state lease helper (`claim_admin_state` / `release_admin_state` / `heartbeat_admin_state` + `lease_stale_checker`) replacing four drifted copies in analyst_daily, memory, whiteboard, and the committee workflow — the future-`claimed_at` guard now lives in exactly one place; per-site token shapes and staleness predicates are preserved as callbacks.
- `executor.book_prepared` / `executor.submit_prepared`: the pre-booked queued-task pattern now has one canonical 17-column INSERT and a public drive API; factcheck and mailbox no longer reach into `executor._execute` / `executor._running` (all `# noqa: SLF001` escape hatches deleted).
- `app/util.py`: shared `new_id()` (9 copied helpers + 34 inline `uuid4().hex[:12]` sites migrated), clamped `read_text()` (the `ARTIFACT_READ_CAP` protection now covers factcheck and vault-exporter reads, not just chain), and `session_workspace()`.
- Origin guard for the no-token posture: non-GET `/api/*` requests with a foreign `Origin` get 403 (loopback aliases, the Vite dev server, and Obsidian's `app://` scheme pass; missing `Origin` — curl/launchd — stays allowed). Token-configured deployments are unchanged.
- Pre-migration backup: when `migrate()` finds pending files against a live database, it first snapshots to `backups/pre-migrate-<timestamp>.db` (same `VACUUM INTO` → tmp → rename pattern as the nightly backup; a fresh database skips it). Backup failure aborts the boot rather than migrating unprotected.
- Frontend `req`/`reqText` now time out after 15s (caller signals merged; timeout surfaces as `ApiError(408)`). The long-lived `askStream` NDJSON path is deliberately exempt.

### Changed
- Event-loop offload on the artifact-completion hot path: `POST /api/vault/doctor` now runs its full-vault SHA scan via `asyncio.to_thread` and reuses the operator sweep's `_classify_vault_rows` as the single classifier (the mirrored loop in `VaultWriter.doctor` is gone); vault writer disk I/O, exporter workspace reads, forecast-extract file reads, and chain backstop report reads all run off-loop while emit keeps its await semantics.
- Chain `_match_hits` caps scanned text at `MATCH_TEXT_CAP` (20KB) for both the footer and backstop paths.
- SPA: the bus-driven meta strip moved into a dedicated `Topbar` component — SSE events no longer re-render the whole `<Routes>` tree, and event-driven meta refetches are throttled to 5s (30s polling kept).
- The five WIP page smoke tests (Tasks, Analysts, Insights, MultiAgent, Forecasts) now assert against element-scoped queries instead of whole-page `textContent`; the full vitest suite is green (11 files / 35 tests).

### Fixed
- Dead-code cleanup: removed `chain.reject_candidate` (zero callers — no route, MCP tool, UI call, or test) and the phantom `factcheck_extract_hand` / `factcheck_verify_hand` hooks (the settings fields never existed, so `extra="ignore"` silently dropped the env vars; extraction/verification now use `default_hand` directly). Vestigial defensive `getattr` reads became direct attribute access (`enable_vectors`, `embed_model`, `token`, `factcheck_daily_cap`).
- Stale docs: `CLAUDE.md` no longer claims no linter is configured (ruff landed earlier today); the `config.py` factcheck comment now matches how `factcheck_tick_minutes` is actually read.

## 2026-07-22 — Post-audit optimization sweep (worktree close-out, hygiene, calibration)

### Added
- Roadmap operator acceptance protocol (M7-011): audited seed reconciliation with dry-run preview, idempotent create mutations (migration 0044 `roadmap_idempotency_keys`), the offline `scripts/apply-roadmap-acceptance.py` batch tool with backup/maintenance/empty-queue preconditions, release-gate projections through M10, and SPA controls for operator self-improvement proposals (approve/reject/parameter PUT with 409 recovery).
- Structured fail-closed majority ballots for multi-agent `majority_vote` and the committee workflow: only an exact final `VERDICT:` line counts, invalid ballots are explicit and count against the quorum.
- Ruff lint configuration in `pyproject.toml` (correctness-focused select, dev extra) — `ruff check app tests scripts` is clean; a Dashboard SPA smoke test joins the vitest suite (4 files / 19 tests).
- Chain `REPROJECT_KINDS` now covers the footer-bearing factcheck / paper-book-journal / research_tree / committee notes, closing the historical-backfill gap left by the vault-projection extension.

### Changed
- Real bge-m3 calibration executed against local Ollama (`INSTITUTE_CALIBRATION_REAL=1`): 50+ known-pair corpus, tier separation and the classifier matrix all passed — the last ◔ item in ROADMAP Phase 1a; formal M8-004 acceptance stays with the operator.
- Service scripts hardened: settings come from `scripts/runtime-config.py` (same pydantic-settings path as the app), `start.sh` gained bounded log rotation and a health-checked startup, scheduler in-flight jobs are tracked through a public metered registry instead of APScheduler internals, and the scorecard time is configurable (`INSTITUTE_SCORECARD_TIME`).
- Repository hygiene: 105 historical `PATCH-NOTES-*` / `REVIEW-*` / `ROUND*-AUDIT-*` reports moved to `docs/history/`; `.kiro/`, `.claude/`, `.ruff_cache/` and the per-task `implementation-notes.md` are gitignored.

### Fixed
- Echo hand refuses `WRITE_FILE` paths that escape the workspace (absolute, `..`, resolved-symlink escapes), with an all-or-nothing preflight.
- The operator vault-conflict sweep's final fresh recheck now runs off the event loop while holding the writer coordination lock.
- Board audit: the live SQLite roadmap board and `roadmap/backlog.json` verified drift-free (55 seed cards; live adds two parked M7 cards only).

## 2026-07-21 — North Star and bounded-autonomy closure (local acceptance batch)

### Added
- M9 operator surfaces and integrity loops: prompt overrides, graph properties, portfolios, favorites/insights, forecast evidence, and hardened factcheck workflows, with corresponding FastAPI, SPA, MCP, plugin, migration, and echo-hand test coverage.
- M10 bounded-autonomy controls across executor, operator, factcheck, chain, research tree, scheduler, mailbox, paper book, vault projection, and backups. Poison work, retries, scans, queue claims, and model dispatches now have durable progress plus explicit ceilings.
- Additive migrations 0026–0043, including durable factcheck/outbox leases, generation-aware verification binding, reciprocal revival task binding, and the mailbox dispatch protocol.

### Changed
- Boot recovery now reconciles executor orphans before task-aware factcheck, revival, and mailbox recovery, then starts the scheduler.
- Event delivery can fan out an event row already committed with its domain transaction, preserving atomic durability without duplicate event inserts.
- The live local database was backed up, reconciled through migration 0043, integrity-checked, and restarted only after confirming no running tasks. Formal M9/LOOP roadmap cards remain in review for operator acceptance.

### Fixed
- Closed all 15 R5 protocol findings (10 P1, 4 P2, 1 P3): stale verification reuse, cross-generation dispute delivery, alias-set truncation, revival crash windows/overcommit loss, mailbox reply loss/duplicate model calls/unbounded reclaim, stale `_inflight` veto, missing parameter effects, and the vault replace-to-ledger TOCTOU.
- Restored migration 0034 to its applied immutable form and moved the later outbox lease additions into migration 0041, so fresh and upgraded databases follow the same additive path.
- Made operator feed registration reconcile actual bus handlers and switched Sina Chinese payload decoding to GB18030, eliminating order-dependent Python 3.14 full-suite failures.
- Closed the post-R5 independent-review findings: safe lost-ledger recovery for the historical 0028 `tasks` rebuild, structured cross-family property-period ordering, strict no-spend boot recovery while maintenance is paused, prompt-override cache pre-warm, and domain-level multi-agent roster/spawn failure guards.
- Aligned the multi-agent SPA with the durable 200/202 API response shapes and made mutation bodies reject unknown fields.
- Restored the literal `/api/theses/import-batches` read surface ahead of the path-like thesis catch-all, with stable pagination bounds, damaged-JSON fallback, and provenance credential/path redaction; the final submission candidate verifies at 1159 passed / 2 intentional skips.

## 2026-07-03 — Thesis registry, security master, live Kanban, PR #1 review

### Added
- **Thesis schema migration — card M1-001** (done): additive `migrations/0003_theses.sql` — `theses` (lifecycle CHECK `candidate|active|watch|dormant|retired`; lanes as `kind='lane'` rows per the bootstrap contract), `thesis_versions` (per-thesis version counter, `supersedes_id` linkage, history preserved across updates), and `market_thesis_imports`/`market_thesis_import_items` provenance (manifest fields, `bundle_sha256`, dry-run/apply modes, idempotency enforced only for completed applies so failed imports never brick a retry). 16 new schema-level tests.
- **Thesis domain module and API — card M1-002** (in review): `app/institute/theses.py` + `app/api/theses.py` — create/update/tree/list with every content revision appending a `thesis_versions` row (supersedes chain), lifecycle transitions via conditional claim with optional `expected_status` → 409, duplicate slugs and concurrent version races mapped to 400/409 instead of raw 500s, path-like thesis ids (`ai/gpu`) supported. `GET/POST/PATCH /api/theses*`.
- **Security master schema — card M2-001** (in review): additive `migrations/0004_securities.sql` — `securities` (canonical `.SH/.SZ/.BJ`/`.HK`/US ids, market + instrument-type normalization covering every value in the bootstrap bundle), `security_aliases` (Chinese names, unsuffixed tickers), `thesis_security_edges` (role, exposure, confidence, rationale, provenance), with a documented importer warning for cross-listed duplicate Chinese names (中芯国际, 中远海控).
- **Plugin Kanban wired to the backend — card M7-003** (done): the Obsidian roadmap view now prefers the live roadmap API (server-side card moves with dependency-block Notices and override retry, auto-seed of an empty backend, release gates from `GET /api/roadmap/release-gates`), falling back to the bundled seed + local overrides offline.
- **Bundle importer — card M1-003** (in review): `app/institute/market_thesis_import.py` — `import_bundle(path, mode='dry_run'|'apply')` plus a module CLI. Dry-run validates and reports counts/warnings without domain writes; apply loads the full universe (55 lanes, 74 theses with seeded v1 versions, 236 securities with zh-name/ticker aliases and warn-and-skip on cross-listing collisions, 1,020 thesis-security edges) in one transaction with per-record provenance items — completed ⇔ imported, mid-apply failure rolls back to zero writes. Verified against the real bundle: every one of the 1,888 edges is imported, skipped-with-reason, or warned — none dropped silently.
- **Coding session tracking — card M7-005** (in review): move-to-review gate (a card needs a session with a non-empty summary, `override` is the single escape hatch), null-field 400s instead of 500s, and an API-mode sessions panel in the plugin card detail (start/finish modals, per-card lazy hydration). Suite 49 → 101.

### Changed
- External **PR #1 reviewed** (four-subsystem adversarial review): verdict is *selective adoption* — roughly half the PR independently implements planned roadmap work (Phase 1b market data, Phase 2 quality detection, Phase 3 evidence/claims) and is worth re-implementing against current main in tranches; wholesale merge is not viable (predates the M0 research-hand policy, migration-number collisions, prompt-surface and rate-limit-signature rule violations). See the "Open pull requests" section below.
- Roadmap board: M7-001 and M1-001 approved to done; M1-002, M2-001, M7-003 in review; board diagrams refreshed (5 done · 3 in review · 2 ready · 6 inbox).

### Fixed
- `tests/test_roadmap.py` no longer hardcodes seed card statuses (two tests broke when M7-001 moved to `review` on the board); assertions now derive from `backlog.json`.

## 2026-07-02 — Roadmap control plane, research-hand policy, bilingual docs

### Added
- **Roadmap control plane** (`roadmap/`): process docs (01–08 + card template) and a 16-card `backlog.json` seed board across phases M0–M7. Every non-trivial change flows design → card → coding session → diff → verification → review → release gate → done. Agent takeover brief: `roadmap/08-claude-handover.md`.
- **Durable roadmap backend — card M7-001** (in review): additive migration `0002_roadmap.sql` (9 tables), `app/institute/roadmap.py` (atomic idempotent backlog import, card moves gated by dependencies *and* evidence behind a conditional claim, coding sessions with command logs, computed release gates), `app/api/roadmap.py` (12 routes under `/api/roadmap/*`). Card M7-008 tracks the deliberately deferred surface (decisions, claim, export, checklist/dependency CRUD).
- **Obsidian plugin roadmap Kanban** (command *Institute: 打开路线图*): bundles `backlog.json` at build time, drag-and-drop with dependency blocking, per-card detail pane (acceptance, verification commands, generated agent prompt), release-gate progress bars, Markdown board export to `Institute/Roadmap/Implementation Kanban.md`; dashboard shortcut button.
- **Research-hand policy — card M0** (done): the research workflow round-robins `INSTITUTE_RESEARCH_HANDS` (default `codex,agy`) via `_workflow_hand_policy`; new `fallback_chain` parameter on `executor.submit/spawn` plus `registry.resolve_chain` confine quota fallback to the configured chain.
- **`AGENTS.md`** contributor guide: structure, commands, style, testing, and operating constraints.

### Changed
- READMEs (English + 简体中文) refreshed in structural parity: agy hand in the architecture diagram, research-hand policy, roadmap control plane section with M0–M7 execution-map diagrams, corrected VaultWriter rules and workflow step shape.
- `CLAUDE.md` agent guide: new map rows (`roadmap/`, `market-thesis-data/`, `app/mcp.py`), operator-constraint hard rules (no unrequested push, local-only, cards for non-trivial work), refreshed commands and gotchas. `ROADMAP.md` now points to `roadmap/` for execution-level tracking.
- Test suite grew 39 → 49, all green (research-hand policy tests + roadmap backend tests).

### Fixed
- `tests/conftest.py`: `AGY` added to the hand-disable loop — fallback-chain tests could previously invoke the real Antigravity CLI and burn quota; research hands pinned to `echo` in tests.
- `.gitignore` now enforces the intentionally-untracked status of `design/` and `market-thesis-data/`.

## 2026-06-11 — v0.1 and the agy hand

### Added
- **agy hand**: Google Antigravity CLI subprocess hand (serial lock, flag-order rules, brain/scratch artifact capture), gemini-family rate-limit signatures, chained `gemini ↔ agy` fallback.
- **v0.1 initial release**: single-process AI research institute (FastAPI + asyncio + SQLite) — executor task spine with global semaphore and per-hand mutex; claude/codex/gemini/opencode CLI hands plus ollama and direct-API fallbacks with persistent never-shorten cooldowns and a circuit breaker; five scheduled SGT loops (morning briefing, analyst dailies, daily report, whiteboard, deep research) with bounded follow-up recursion; VaultWriter + bus-driven exporter projecting all products into an Obsidian vault; React operator SPA; Obsidian plugin; hand-rolled MCP endpoint at `/api/mcp`; echo-hand test suite.

## Open pull requests

- **[#1 Add routing, data, and evidence hardening](https://github.com/dmquant/institute-one/pull/1)** (external, opened 2026-06-13, 55 files, +3,952/−182) — pooled hand routing and cheap-tier routing, market-data cache/API (optional IBKR bars, SEC/FMP snapshots) with research data-bundle injection, evidence URL ledger and claim triage, `.env`-aware start/stop and daemonized startup. Reviewed 2026-07-03: **selective adoption** — will not merge as-is (conflicts with the M0 research-hand policy, migration-number collisions with `0002_roadmap.sql`+, prompt-surface and rate-limit-signature hard-rule violations); ~10 roadmap-aligned components queued for re-implementation against current main in tranches (market data → M4 cards; quality/evidence/claims → new M8 cards; ops daemonization → new M9 cards). Awaiting operator sign-off on the tranche plan and the PR reply.
