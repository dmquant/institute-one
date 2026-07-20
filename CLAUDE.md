# institute-one — agent guide

Single-node AI research institute: FastAPI + asyncio + SQLite, one process. AI analysts run scheduled workflows (briefing/daily/research), collaborate on whiteboards, answer mailbox threads, and export everything to an Obsidian vault. Full docs: `README.md`; design rationale: `../proposal/PROPOSAL.md`. Contributor conventions (code style, commit/PR format, secrets hygiene): `AGENTS.md` — keep it in sync when commands or conventions change. Taking over ongoing work? Read `roadmap/08-claude-handover.md` first.

## Commands

```bash
./scripts/install.sh                         # bootstrap: venv (pip install -e ".[dev]") + npm deps
.venv/bin/python -m pytest tests -q          # test suite (echo hand, no quota; asyncio_mode=auto — no marks needed)
.venv/bin/python -m compileall app -q        # syntax check
./scripts/start.sh | ./scripts/stop.sh       # server on 127.0.0.1:8100 (log: ~/.institute-one/logs/server.log)
.venv/bin/python -m app.cli start|stop|status|doctor  # operator CLI; doctor = offline read-only health report (the `institute` console script appears after a re-`pip install -e`)
./scripts/install-service.sh [--activate]    # render the launchd plist (com.institute-one.server); --activate also bootstraps+enables
./scripts/uninstall-service.sh               # bootout the launchd job and remove the plist (plist kept if unload fails)
cd frontend && npm run build                 # SPA → frontend/dist (server restart picks it up); npm run dev = Vite dev server
cd obsidian-plugin && npm run build          # plugin → main.js
./scripts/install-plugin.sh /path/to/Vault   # deploy plugin
```

**Before restarting the server**: check `curl -s localhost:8100/api/tasks/queue` — a restart orphans running CLI tasks. Restart only when queued+running is 0, or accept the orphan recovery.

## Map

| Path | What |
|---|---|
| `app/config.py` | ONE settings object (`INSTITUTE_*` env / `.env`). Derived paths under `~/.institute-one`. |
| `app/db.py` | aiosqlite helpers: `query/query_one/execute/insert/transaction`. `execute` returns rowcount (used by conditional claims). |
| `app/bus.py` | `emit()` → events table + SSE subscribers + registered handlers (`on(prefix, fn)`). Handlers must never raise. |
| `app/hands/` | Hand ABC (`base.py`), per-CLI hands, `rate_limit.py` signature parsers, `registry.py` (cooldowns in `rate_limits.json`, fallback chains, breaker). `build_hands()` in `__init__.py`. |
| `app/router/executor.py` | THE execution path: `submit()` (await) / `spawn()` (fire-and-forget). Every model call = one `tasks` row. Global semaphore (3) + per-hand mutex. Optional `fallback_chain` confines resolution + rate-limit retry to an explicit hand list (`registry.resolve_chain`). |
| `app/institute/` | Domain: `analysts` (roster CRUD over `catalog/analysts.json`), `prompts` (date anchor, persona sandwich, `extract_summary`), `sessions`, `workflows` (JSON step engine), `scheduler` (APScheduler, SGT, `metered()`), `daily`, `analyst_daily`, `whiteboard`, `mailbox`, `research` (+ `parse_followups`), `archive` (FTS5). |
| `app/institute/memory.py` | Analyst standing memory: versioned compacts (23:30 SGT job); `memory_block()` is injected at the four workflow prompt-assembly points (dailies/whiteboard/mailbox/workflow steps; ad-hoc asks not yet); vault note uses managed regions. |
| `app/institute/scorecard.py` | Task QA over terminal `tasks` rows: `judge_output()` heuristics + `run_once()` (00:05 SGT, settles the PREVIOUS day) → `hand_scorecard` verdicts + hourly `hand_stats`. |
| `app/institute/digests.py` | Curl-back markdown digests (`GET /api/institute/*.md`) — Step-0 context for CLI hands (the prompt-side `curl` block is wired into the workflow prompts — round-5 prompt card); read-only, 8KB clamp, placeholders instead of errors. |
| `app/institute/market_data.py` | Local PIT store (calendar, bars, benchmarks, suspensions): corrections append new versions, never overwrite; `get_bars_pit(sec, as_of)` answers "what did we know at T". |
| `app/institute/market_fetchers.py` | FMP → Stooq → Sina fetcher ladder with per-market symbol dialects; confidence-gated PIT ingest; `${DATA_BUNDLE}` research injection. |
| `app/institute/forecasts.py` | Forecast ledger: falsifiable calls with deterministic `settlement_rule`s, settled ONLY from the PIT store (+ `forecast_extract.py`, the zero-quota regex extractor). |
| `app/institute/paper_book.py` | Virtual positions opened from forecasts (size 1.0 — measures call quality, not capital): opener + settle jobs, nightly journal export. |
| `app/institute/chain.py` | Chain graph (entities/edges/mentions): the vault IS the graph — `Chain/<entity>.md` managed regions, Dataview inline relations, wikilink footers. |
| `app/institute/operator.py` | Operator loop first slice: action feeds + SHADOW-mode router (logged suggestions only — the human gate stays human; approve re-checks the live confidence floor at consume time). `recipes` is a schema-only placeholder (0018) — the self-improvement chain is unbuilt. |
| `app/institute/factcheck.py` | Fact-check v2: claim extraction → tiered reuse gate → verification verdicts (`fact_cards` / `verified_facts`). |
| `app/institute/research_tree.py` | BFS Explore mode (Phase 7): one tree = one root topic drilled breadth-first under per-tree caps + an SGT daily tree budget; children insert inside the parent's completion transaction; 5-min gated tick; single-fire `tree.completed` only after the tree drains. |
| `app/institute/projects.py` | Research projects (Phase 7): named long-running containers over research items/boards/threads; link/enqueue arbitration is a single `INSERT … SELECT … WHERE status='active'` so archived projects refuse new attachments. |
| `app/institute/bilingual.py` | Bilingual twins (Phase 7): translates briefing/daily export text to EN, emits by-reference `bilingual.twin_ready` (full text lives once, in the `tasks` row); corrupt maintenance state fails CLOSED (treated as paused). |
| `app/cli.py` | `institute` console script (pyproject `[project.scripts]`): start/stop delegate to the scripts; `status`/`doctor` work without the server — doctor is strictly read-only (`file:…?mode=ro`). |
| `app/api/contract.py` | `GET /api/contract`: versioned status enums / field caps / ref grammar, IMPORTED from owning modules and cross-checked against live schema CHECKs; `GET /api/artifacts?ref=` dereferences `task:\|note:\|fact_card:`. |
| `app/vault/` | `writer.py` (five rules: atomic, ownership marker, hash-ledger never-clobber, skip-if-unchanged, rebuildable via `doctor()`) + `exporter.py` (bus handlers → vault notes). |
| `app/api/` | One router module per area; `router = APIRouter(...)` at module level; mounted in `app/main.py`. |
| `app/mcp.py` | MCP endpoint: hand-rolled JSON-RPC 2.0 at `POST /api/mcp` (no SDK) — lives outside `app/api/`. |
| `migrations/*.sql` | Additive only — add a new numbered file, never edit old ones (number gaps are fine). Each file runs as ONE transaction, statement by statement: no `BEGIN`/`COMMIT`/`ROLLBACK`/`ATTACH`/`VACUUM` inside migrations (enforced by `tests/test_db_migrate.py`); avoid PRAGMA too. |
| `workflows/*.json` | Workflow definitions, reconciled into DB at boot (`reconcile_from_disk`). Steps: `{id, title, analyst_id, prompt, output_file, timeout_s, hand?}` — `analyst` is a legacy alias folded into `analyst_id` at reconcile time (unknown ids warn loudly); missing analyst → chief-strategist; `${VAR}` substitution. |
| `catalog/analysts.json` | The roster (source of truth; CRUD API writes it back). |
| `roadmap/` | Roadmap control plane: process docs + `backlog.json` seed board. The Obsidian plugin bundles `backlog.json` at build time — don't move/rename it. Takeover brief: `08-claude-handover.md`. |
| `market-thesis-data/` | Bootstrap dataset (74 theses / 55 lanes / 236 stocks, JSON+CSV) for the thesis-registry import. Commercial data — NEVER commit: lives at an external path pointed to by `INSTITUTE_THESIS_BUNDLE` (in-repo dir is a legacy fallback, still untracked read-only). |
| `tests/` | pytest-asyncio (auto mode); `conftest.py` points `INSTITUTE_HOME` at tmp, disables real CLI hands (keep the disable loop in sync when adding hands), pins default + research hands to echo; its teardown cancels every background-task registry (keep in sync with `main._drain_background`). |

## Hard rules

1. **One execution path.** Model calls go through `executor.submit/spawn` — never spawn a CLI directly from domain code.
2. **Conditional-claim idiom** for every state transition: `UPDATE … SET status='running' WHERE id=? AND status='queued'`, check rowcount. This is what makes loops re-entrant and restart-safe.
3. **Scheduler jobs never raise.** Wrap with `@metered(name, gated=…)`; `gated=True` for anything that submits NEW model calls (respects the maintenance pause — toggle via `POST /api/admin/maintenance {"paused": bool}`); no-quota jobs (janitor / hand-scorecard / market-refresh) stay ungated.
4. **Prompts are the product.** Never paraphrase existing prompt strings in `prompts.py` / `workflows/*.json` during refactors. The sandwich is: date anchor → persona → context blocks → task → CITATION_MANDATE → file deliverable.
5. **Don't churn the battle-tested**: `rate_limits.json` persistence (never-shorten, 60s floor), `get_cli_env()` login-shell capture, per-CLI rate-limit signatures (no generic backstops — false positives are worse than misses), the VaultWriter five rules.
6. **Rows are truth; vault notes are projections.** Only `vault/writer.py` writes under the vault. New exports = new bus handler in `exporter.py`, frontmatter must include `managed: institute`.
7. **Timestamps**: `bus.now_iso()` (UTC ISO) for storage; `prompts.work_date()` (SGT date) for any "today" logic. Never `datetime.now()` raw.
8. **Follow-ups recursion is bounded** — keep it that way: per-source caps (research 3+2, dailies 2+1), self-mail dropped, replies/cards never generate further follow-ups, max 2 active boards.
9. **Operator constraints** (from `roadmap/08-claude-handover.md`): never `git push` unless explicitly asked; local-only — no hosted/cloud infrastructure; don't revert the intentionally-dirty working tree; non-trivial changes map to a roadmap card in `roadmap/backlog.json`.
10. **Research stays on codex+agy.** The research workflow ignores `analyst.hand`: `_workflow_hand_policy` (in `workflows.py`) round-robins `settings.research_hand_names` (`INSTITUTE_RESEARCH_HANDS`, default `codex,agy`) and confines fallback to that chain. Tests pin it to `echo`.

## Recipes

- **New hand**: subclass `Hand` in `app/hands/<name>_hand.py` (copy `claude_hand.py`); add signatures to `rate_limit.py` if the CLI has quota walls; register in `build_hands()`; add to `DEFAULT_FALLBACK_CHAINS` in `registry.py`; enable flag in `config.py`; add the flag to the disable loop in `tests/conftest.py`. Test with a fake hand against the registry.
- **New workflow**: JSON in `workflows/` (analyst ids from the catalog); it reconciles at boot or via `reconcile_from_disk()`. Schedule it: add a config time + a `metered` job in `scheduler.py`. Vault export: handler in `vault/exporter.py` keyed on `workflow.completed` payload `workflow_id`.
- **New analyst**: POST `/api/analysts` or edit `catalog/analysts.json` (`reload()` happens on CRUD; manual edits are picked up on the next read — the cache is mtime-checked). Non-ops analysts automatically join dailies, whiteboard rotation, follow-up catalogs.
- **New domain loop**: durable pending rows in a table + a `tick()` that conditional-claims + a `metered` scheduler job + bus events on completion + (optional) vault export handler + an echo-hand test.
- **New SPA page**: `frontend/src/pages/X.tsx` + route/nav in `App.tsx` + client fns in `api.ts`; `npm run build`; server restart serves it (SPA fallback handles deep links).
- **Frontend/plugin HTTP**: plugin must use Obsidian `requestUrl` (CORS); SPA is same-origin.

## Gotchas

- One CLI = one concurrent task (per-hand mutex): an `/api/ask` queues behind a running workflow step on the same hand. Spread work across hands.
- The roster cache is mtime-checked — manual edits to `catalog/analysts.json` are picked up on the next read; CRUD still calls `reload()` explicitly.
- `tasks.output` is capped (200KB); deliverables are FILES in the session workspace.
- Echo hand writes files only via the `WRITE_FILE: <name>` prompt convention — tests rely on this.
- Daily-cap / cooldown date comparisons mix UTC timestamps with SGT work dates (documented, ±8h at boundaries).
- `analyst_daily` guard is one `admin_state` row per analyst per day (`analyst_daily:<date>:<analyst_id>`; a legacy per-day blob is still merged on read, never written); force rerun via the per-analyst endpoint.
- Hand weights are opt-in (`INSTITUTE_ENABLE_HAND_WEIGHTS`, default false = pre-weights behaviour): the registry cache must be pre-warmed at boot (`refresh_weights_cache()` in lifespan) or it runs neutral 1.0 with one WARNING until a `GET/PUT /api/hands/weights` heals it. Explicit `analyst.hand` / step hands always beat weights; research weights only reorder inside `research_hand_names` (rule 10).
- PIT reads are two-legged: `get_bars_pit(sec, as_of=T)` picks, per bar date, the version with the greatest `as_known_at <= T` — corrections append versions, never overwrite; `as_of=None` = latest known; a bare-date `as_of` means 00:00 UTC that day.
- Managed regions (`write_note(..., region=True)`): only the text between `%% institute:begin %%`/`%% institute:end %%` markers is owned and replaced; hand-written text outside survives regeneration. Any marker damage/edit falls back to a conflict sibling (never clobber).
- `obsidian-plugin/main.js` is a committed build artifact — after editing plugin src, `npm run build` and commit `main.js` with the `.ts` changes. Roadmap card moves persist only in plugin-local settings (`roadmapStatusOverrides`) until the backend roadmap API (card M7-001) lands.
- `design/` exists locally but is deliberately gitignored — roadmap cards link into it; read it for context, never commit it.
- No linter/formatter is configured (Python or TS) — match surrounding style; the npm builds double as the TS type check.
