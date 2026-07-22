# Changelog

Notable changes to institute-one, grouped by push batch (dates are SGT work dates). Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
