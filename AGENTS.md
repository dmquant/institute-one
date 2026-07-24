# Repository Guidelines

## Project Structure & Module Organization

`app/` contains the FastAPI backend. Key areas are `api/` for REST routers, `institute/` for domain workflows, `hands/` for CLI/model executors, `router/` for task execution, and `vault/` for Obsidian export logic. `frontend/src/` is the React operator UI, and `obsidian-plugin/src/` is the Obsidian plugin. Tests live in `tests/`. Durable schema changes go in `migrations/` — additive numbered files only (gaps are fine), each applied as one transaction, so never put `BEGIN`/`COMMIT`/`ROLLBACK`/`ATTACH`/`VACUUM` inside a migration (a test enforces this); analyst and workflow configuration lives in `catalog/` and `workflows/`. `roadmap/` is the roadmap control plane (design docs plus the `backlog.json` card board). `market-thesis-data/` is a local, intentionally untracked dataset. Screenshots are under `docs/screenshots/`; longer docs are the root-level `README.md` and `ROADMAP.md`.

## Build, Test, and Development Commands

- `./scripts/install.sh`: create the Python environment and install frontend/plugin dependencies.
- `./scripts/start.sh` / `./scripts/stop.sh`: run or stop the local server at `127.0.0.1:8100`.
- `.venv/bin/python -m pytest tests -q`: run the pytest suite using the echo hand.
- `.venv/bin/python -m compileall app -q`: quick backend syntax check.
- `.venv/bin/ruff check app tests scripts`: Python linting (imports, syntax, common mistakes; uses pyproject.toml rules).
- `cd frontend && npm run build`: type-check and build the SPA into `frontend/dist`. This is also the TypeScript type-check command.
- `cd frontend && npm run dev`: start the Vite dev server.
- `cd obsidian-plugin && npm run dev`: rebuild the plugin in watch mode.
- `cd obsidian-plugin && npm run build`: type-check and bundle the plugin. `obsidian-plugin/main.js` is a committed build artifact — rebuild and commit it together with `src/` changes.
- `./scripts/install-hooks.sh` (opt-in): point `core.hooksPath` at the committed `scripts/git-hooks/` pre-commit gate (ruff + compileall on staged Python, `tsc` on staged SPA sources, stale-`main.js` check for plugin changes).

## Coding Style & Naming Conventions

Use Python 3.11+, four-space indentation, async-aware code, and type hints where they clarify API boundaries. Keep model execution behind `app/router/executor.py`; domain code should not spawn CLIs directly. React and plugin code use TypeScript modules, PascalCase components, camelCase functions/hooks, and colocated page files such as `frontend/src/pages/Tasks.tsx`. There is no separate formatter configured, so keep edits consistent with surrounding files.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio`; name files `tests/test_*.py`. `tests/conftest.py` redirects state to a temporary home, disables every real CLI hand (keep its disable loop in sync when adding a hand), and pins `INSTITUTE_DEFAULT_HAND` and `INSTITUTE_RESEARCH_HANDS` to echo, so new tests should avoid consuming external model quota. Its teardown cancels every background-task registry — keep that sweep in sync with `main._drain_background` when adding a new registry. Add focused tests for new loops, API behavior, migrations, and vault-writing rules.

## Commit & Pull Request Guidelines

Git history uses short, descriptive subjects such as `Add agy hand (Google Antigravity CLI)`. Keep commits focused and name the changed subsystem when useful. When landing a notable batch, add a dated entry to `CHANGELOG.md`. Pull requests should include a summary, test/build results, linked issues when applicable, screenshots for UI changes, and notes for migrations or configuration changes.

## Operating Constraints

Never `git push` unless the operator explicitly asks. Keep everything local-only; do not add hosted or cloud infrastructure. Non-trivial changes should map to a card in `roadmap/backlog.json`. The deep-research workflow defaults to the codex+agy hands (`INSTITUTE_RESEARCH_HANDS`).

## Security & Configuration Tips

Keep secrets in `.env` and `INSTITUTE_*` environment variables; do not commit local vault paths, API keys, or `~/.institute-one` data. Before restarting a live server, check `curl -s localhost:8100/api/tasks/queue` so running CLI tasks are not orphaned.
