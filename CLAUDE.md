# institute-one — agent guide

Single-node AI research institute: FastAPI + asyncio + SQLite, one process. AI analysts run scheduled workflows (briefing/daily/research), collaborate on whiteboards, answer mailbox threads, and export everything to an Obsidian vault. Full docs: `README.md`; design rationale: `../proposal/PROPOSAL.md`.

## Commands

```bash
.venv/bin/python -m pytest tests -q          # test suite (echo hand, no quota)
.venv/bin/python -m compileall app -q        # syntax check
./scripts/start.sh | ./scripts/stop.sh       # server on 127.0.0.1:8100
cd frontend && npm run build                 # SPA → frontend/dist (server restart picks it up)
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
| `app/router/executor.py` | THE execution path: `submit()` (await) / `spawn()` (fire-and-forget). Every model call = one `tasks` row. Global semaphore (3) + per-hand mutex. |
| `app/institute/` | Domain: `analysts` (roster CRUD over `catalog/analysts.json`), `prompts` (date anchor, persona sandwich, `extract_summary`), `sessions`, `workflows` (JSON step engine), `scheduler` (APScheduler, SGT, `metered()`), `daily`, `analyst_daily`, `whiteboard`, `mailbox`, `research` (+ `parse_followups`), `archive` (FTS5). |
| `app/vault/` | `writer.py` (five rules: atomic, ownership marker, hash ledger, never-clobber, rebuildable) + `exporter.py` (bus handlers → vault notes). |
| `app/api/` | One router module per area; `router = APIRouter(...)` at module level; mounted in `app/main.py`. |
| `migrations/*.sql` | Additive only — add a new numbered file, never edit old ones. |
| `workflows/*.json` | Workflow definitions, reconciled into DB at boot (`reconcile_from_disk`). Steps: `{id, title, analyst, prompt, output_file, timeout_s}`; `${VAR}` substitution. |
| `catalog/analysts.json` | The roster (source of truth; CRUD API writes it back). |
| `tests/` | pytest-asyncio; `conftest.py` points `INSTITUTE_HOME` at tmp, enables only the echo hand. |

## Hard rules

1. **One execution path.** Model calls go through `executor.submit/spawn` — never spawn a CLI directly from domain code.
2. **Conditional-claim idiom** for every state transition: `UPDATE … SET status='running' WHERE id=? AND status='queued'`, check rowcount. This is what makes loops re-entrant and restart-safe.
3. **Scheduler jobs never raise.** Wrap with `@metered(name, gated=…)`; `gated=True` for anything that starts new work (respects the maintenance pause).
4. **Prompts are the product.** Never paraphrase existing prompt strings in `prompts.py` / `workflows/*.json` during refactors. The sandwich is: date anchor → persona → context blocks → task → CITATION_MANDATE → file deliverable.
5. **Don't churn the battle-tested**: `rate_limits.json` persistence (never-shorten, 60s floor), `get_cli_env()` login-shell capture, per-CLI rate-limit signatures (no generic backstops — false positives are worse than misses), the VaultWriter five rules.
6. **Rows are truth; vault notes are projections.** Only `vault/writer.py` writes under the vault. New exports = new bus handler in `exporter.py`, frontmatter must include `managed: institute`.
7. **Timestamps**: `bus.now_iso()` (UTC ISO) for storage; `prompts.work_date()` (SGT date) for any "today" logic. Never `datetime.now()` raw.
8. **Follow-ups recursion is bounded** — keep it that way: per-source caps (research 3+2, dailies 2+1), self-mail dropped, replies/cards never generate further follow-ups, max 2 active boards.

## Recipes

- **New hand**: subclass `Hand` in `app/hands/<name>_hand.py` (copy `claude_hand.py`); add signatures to `rate_limit.py` if the CLI has quota walls; register in `build_hands()`; add to `DEFAULT_FALLBACK_CHAINS` in `registry.py`; enable flag in `config.py`. Test with a fake hand against the registry.
- **New workflow**: JSON in `workflows/` (analyst ids from the catalog); it reconciles at boot or via `reconcile_from_disk()`. Schedule it: add a config time + a `metered` job in `scheduler.py`. Vault export: handler in `vault/exporter.py` keyed on `workflow.completed` payload `workflow_id`.
- **New analyst**: POST `/api/analysts` or edit `catalog/analysts.json` (`reload()` happens on CRUD; a manual edit needs restart or `analysts.reload()`). Non-ops analysts automatically join dailies, whiteboard rotation, follow-up catalogs.
- **New domain loop**: durable pending rows in a table + a `tick()` that conditional-claims + a `metered` scheduler job + bus events on completion + (optional) vault export handler + an echo-hand test.
- **New SPA page**: `frontend/src/pages/X.tsx` + route/nav in `App.tsx` + client fns in `api.ts`; `npm run build`; server restart serves it (SPA fallback handles deep links).
- **Frontend/plugin HTTP**: plugin must use Obsidian `requestUrl` (CORS); SPA is same-origin.

## Gotchas

- One CLI = one concurrent task (per-hand mutex): an `/api/ask` queues behind a running workflow step on the same hand. Spread work across hands.
- The roster is `lru_cache`d — CRUD reloads it, manual JSON edits don't.
- `tasks.output` is capped (200KB); deliverables are FILES in the session workspace.
- Echo hand writes files only via the `WRITE_FILE: <name>` prompt convention — tests rely on this.
- Daily-cap / cooldown date comparisons mix UTC timestamps with SGT work dates (documented, ±8h at boundaries).
- `analyst_daily` guard lives in `admin_state` key `analyst_daily:<date>`; force rerun via the per-analyst endpoint.
