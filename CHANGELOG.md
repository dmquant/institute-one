# Changelog

Notable changes to institute-one, grouped by push batch (dates are SGT work dates). Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 2026-07-18 — Parallel-line reconciliation and review closeout

### Added
- Ported the remote line's thesis lifecycle endpoint onto the local implementation: `POST /api/theses/{id}/status` uses optimistic concurrency, appends a version, and emits `thesis.status_changed`; the importer now also has a module CLI entry point.
- The Obsidian roadmap view hydrates live card checklists and exposes an explicit, retryable seed-sync action. Merely opening an empty board no longer writes backend state.

### Changed
- Reconciled the local and remote histories at `3d8dda9` while keeping the more complete local schema/implementation. The incompatible remote `0003_theses.sql` / `0004_securities.sql` migrations remain intentionally excluded.
- Updated the execution map to the current 18-card board: 4 done, 10 in review, 4 inbox.

### Fixed
- Thesis PATCH requests containing lifecycle-only fields now fail loudly instead of returning 200 after silently dropping `status`; no-op status changes also perform a conditional claim.
- Canonical security merges abort rather than deleting one of two colliding operator-owned edges, preserve both rows through rollback, and record a failed import batch for operator resolution.
- Roadmap card PATCH maps explicit nulls on non-null text fields to a domain 400, while keeping `owner` and `blocked_reason` nullable.
- Checklist hydration is independent of coding-session availability, and backend acceptance truth now drives detail, board counts, search, and prompt fallback consistently.
- SSE heartbeats keep one pending subscription read instead of cancelling and closing the async generator every 25 seconds; the stop script now waits for graceful exit and sends Uvicorn a second interrupt when long-lived streams hold shutdown open.

### Verification
- Two local read-only review passes closed all confirmed findings. Full backend suite: 90 passed; backend compile, diff check, shell syntax, and the production Obsidian plugin build are green.

## 2026-07-16 — Roadmap control plane completed (M7-005 … M7-008)

### Added
- **Coding session tracking — card M7-005** (in review): moving a card to `review` now requires a completed coding session with a non-blank summary (override for operators; the gate rides inside the conditional-claim UPDATE so it cannot be raced); terminal sessions (`completed/partial/blocked`) require and keep a summary — post-hoc blanking is rejected, plain-field session writes are conditional claims; `POST /sessions/{id}/commands` gained `attach_as_evidence` (command + `roadmap_evidence` row + audit event commit in ONE transaction; exit code 0 → `pass`, non-zero → `fail`). Plugin card detail gained a Coding Sessions panel: start/complete sessions, record verification commands as evidence.
- **Roadmap CRUD surface — card M7-008** (in review): decisions (`POST/GET/PATCH /api/roadmap/decisions`, resolve-exactly-once), `POST /cards` (transactional, ready-at-birth needs acceptance), `POST /cards/{id}/claim` (atomic inbox/ready → in_progress with the claimed-from status bound into the UPDATE), checklist add/check/rename/remove, dependency add/remove with transactional cycle detection, `GET /export`. Migration `0005_roadmap_dep_source.sql` adds `roadmap_dependencies.source` — import reconciliation owns only its own rows, operator-added dependencies survive re-imports (pair-level `(target, relation)` reconcile).
- **State-faithful export/restore**: `GET /export` emits `dependencies_meta` (relation + ownership), `blocked_reason`, `acceptance_checked`, and `checklists_extra` on top of the seed shape; `import_backlog()` consumes them (checked state merges, never unchecks) and validates the FINAL dependency graph (manual + import edges) is acyclic inside the import transaction.
- **Deterministic agent prompts — card M7-007** (in review): `GET /api/roadmap/cards/{id}/prompt` renders the 06-agent-protocol.md template from live card state (same state → same string; a non-empty `agent_prompt` card field is an operator override). Plugin shows the backend prompt with a copy button, falling back to the bundled-seed template offline.
- **Process views — card M7-006** (in review): plugin process strip (active coding sessions, open decisions with resolve, blocked cards) above the board; release gates use backend data with `evidence_ready` (remaining cards that already carry evidence); per-section fetch failures keep last-known-good data and say so instead of masquerading as "no data".

### Changed
- **Plugin board is backend-first**: statuses come from `GET /api/roadmap/cards` (bundled backlog is the offline seed); API-created cards merge into the board/summary/blocked strip; moves go through `POST /move` with `expected_status` (409 → auto-resync); offline moves fall back to local overrides which are discarded loudly once the backend returns; refresh/move/process fetches are generation-guarded against stale-response overwrites.
- Test suite grew 73 → 86, all green; the M7-005 and M7-006/007/008 deltas each passed a multi-round adversarial codex review (final verdicts: 通过 / APPROVE).

### Fixed
- Validation gaps that surfaced as 500s or silent corruption: NaN/Infinity `sort_order` on create/update/move, checklist rename onto an existing text, `\x1f` (det-id separator) in card ids and dependency relations checked pre-strip, blank-string blockers treated as unblocked consistently.

## 2026-07-15 — Thesis registry, security master, market-thesis-data importer

### Added
- **Thesis registry — cards M1-001/M1-002** (in review): additive migration `0003_thesis_registry.sql` (`theses` lane/thesis tree with lifecycle status, append-only `thesis_versions` view history, `market_thesis_import_*` provenance), `app/institute/theses.py` (tree CRUD; every title/view/direction/status change appends a version row), `app/api/theses.py` under `/api/theses/*` (tree by default, `?flat=true`, create/patch, security link/unlink).
- **Security master — card M2-001** (in review): migration `0004_security_master.sql` (`securities` with canonical `<TICKER>.<MARKET>` ids over CN_A/HK/US/KR/JP, `security_aliases`, `thesis_security_edges` with role/exposure/confidence/rationale), `app/institute/securities.py` (market-thesis-data label normalization such as `A-share ETF → (CN_A, etf)`, alias resolution, edge upserts).
- **market-thesis-data importer — card M1-003** (in review): `app/institute/market_thesis_import.py` reads a `researchos.market_thesis_export` bundle directory, validates structure (manifest schema prefix + count reconciliation as warnings), and imports lanes → theses → stocks → thesis-stock edges with deterministic ids so re-runs are idempotent; dry-run executes the same upserts in a rolled-back transaction for accurate created/updated/unchanged counts; every run lands in `market_thesis_import_batches` (+ per-row items on apply); imported theses arrive as `candidate`/`conflicting` hypotheses and local lifecycle status survives re-import. Routes: `POST /api/theses/import-market-data`, `GET /api/theses/import-batches`.
- **Importer reconciliation semantics** (hardened through a 10-round adversarial codex review): upstream ticker/market corrections recanonicalize securities by bundle source id (children migrate, no stale duplicates, planned rows are never deleted mid-import); edges resolve by natural key with a parking scheme so canonical swaps survive a single apply; in-bundle duplicates are first-source-wins (no order-dependent flip-flops); operator-owned (`manual`) edges are never overwritten, deleted, or parked by an import — an operator edit of an import edge flips ownership to `manual`.
- **Coding session tracking — card M7-005** (in review): roadmap moves to `review` now require a completed coding session with a non-empty summary unless the operator explicitly overrides; session commands can atomically attach pass/fail/info command evidence. The Obsidian roadmap view syncs live card status from SQLite, displays session actor/goal/planned and touched files/summary/command count, starts and completes sessions, records configured verification commands as evidence, and falls back to local seed status only when the backend is unreachable.

### Fixed
- **Test baseline — card M7-009** (done): the suite runs against a tmp copy of `catalog/analysts.json` with per-analyst hand preferences neutralized (new `INSTITUTE_CATALOG_FILE` setting), so roster CRUD tests never rewrite the real catalog and operator hand assignments (codex/claude) no longer dead-end the echo-only test env; roadmap tests track the seeded M7-001 `review` status and use self-seeded temp cards for move-gate checks. 49 → 64 tests, all green.
- Coding-session review gates, override behavior, terminal-summary validation, and command-evidence attachment are covered end to end. The full suite now has 74 passing tests.

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

- **[#1 Add routing, data, and evidence hardening](https://github.com/dmquant/institute-one/pull/1)** (external, opened 2026-06-13, 55 files, +3,952/−182) — pooled hand routing and cheap-tier routing, market-data cache/API (optional IBKR bars, SEC/FMP snapshots) with research data-bundle injection, evidence URL ledger and claim triage, `.env`-aware start/stop and daemonized startup. Not yet reviewed or merged; predates the 2026-07-02 batch, so it overlaps the M0 executor changes and several ROADMAP Phase-0/1b items and will need rebasing.
