# Changelog

Notable changes to institute-one, grouped by push batch (dates are SGT work dates). Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 2026-07-03 — Thesis schema (M1-001) and the PR #1 review

### Added
- **Thesis schema migration — card M1-001** (in review): additive `migrations/0003_theses.sql` — `theses` (lifecycle CHECK `candidate|active|watch|dormant|retired`; lanes as `kind='lane'` rows per the bootstrap contract), `thesis_versions` (per-thesis version counter, `supersedes_id` linkage, history preserved across updates), and `market_thesis_imports`/`market_thesis_import_items` provenance (manifest fields, `bundle_sha256`, dry-run/apply modes, idempotency enforced only for completed applies so failed imports never brick a retry). 16 new schema-level tests; suite 49 → 65.

### Changed
- External **PR #1 reviewed** (four-subsystem adversarial review): verdict is *selective adoption* — roughly half the PR independently implements planned roadmap work (Phase 1b market data, Phase 2 quality detection, Phase 3 evidence/claims) and is worth re-implementing against current main in tranches; wholesale merge is not viable (predates the M0 research-hand policy, migration-number collisions, prompt-surface and rate-limit-signature rule violations). See the "Open pull requests" section below.

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
