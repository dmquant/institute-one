# 08 - Claude Coding Handover

## Purpose

This document is the takeover brief for Claude or any coding agent continuing the implementation work. The repo is mid-transition from design planning into coding. Preserve the current local-only architecture, keep the roadmap process inside Obsidian first, and do not push to git unless the operator explicitly asks.

## Current Operator Intent

The system goal is a continuously evolving thesis-alpha research institute:

- research focuses on investable theses / 赛道, especially AI infrastructure and adjacent lanes such as GPU, storage, power grid, software, energy, robotics, healthcare, industrial supply chains, and macro transmission;
- China A-share stocks are the primary universe, with US and Hong Kong coverage required;
- research should loop indefinitely across theses, evidence, stocks, forecasts, and alpha performance;
- no Cloudflare or hosted infrastructure should be introduced; everything runs locally;
- deep research should use only `codex` and `agy` hands by default;
- `market-thesis-data/` is the current bootstrap dataset from the full AI Institute output;
- `design/` is intentionally ignored by git;
- do not `git push`.

## Repository State At Handover

Current relevant dirty/untracked files include:

```text
M .gitignore
M app/config.py
M app/hands/registry.py
M app/institute/workflows.py
M app/router/executor.py
M obsidian-plugin/README.md
M obsidian-plugin/main.js
M obsidian-plugin/src/dashboard.ts
M obsidian-plugin/src/main.ts
M obsidian-plugin/tsconfig.json
M tests/conftest.py
M tests/test_workflows.py
?? AGENTS.md
?? market-thesis-data/
?? obsidian-plugin/src/roadmap.ts
?? roadmap/
```

Do not revert these. They are intentional local work.

## Work Already Completed

### Contributor And Process Docs

- `AGENTS.md` was created as a contributor guide.
- `roadmap/` was created as the global coding process package.
- `roadmap/backlog.json` is the seed backlog.
- `roadmap/README.md` now says the roadmap control plane starts inside the Obsidian plugin and later syncs to local SQLite.
- `roadmap/01-portal-design.md` was updated from a React-first `/roadmap` page to an Obsidian plugin view.
- `roadmap/04-automation.md` and `roadmap/05-global-coding-process.md` now make `cd obsidian-plugin && npm run build` the primary UI verification for roadmap work.

### Design Docs

The richer local design docs live under ignored `design/local-thesis-alpha/`. They are not in git because `.gitignore` includes:

```text
design/
```

Important local design docs include:

- `design/local-thesis-alpha/01-local-architecture.md`
- `design/local-thesis-alpha/02-thesis-stock-model.md`
- `design/local-thesis-alpha/03-infinite-research-loop.md`
- `design/local-thesis-alpha/04-alpha-portfolio-loop.md`
- `design/local-thesis-alpha/05-implementation-roadmap.md`
- `design/local-thesis-alpha/06-market-data-pit.md`
- `design/local-thesis-alpha/10-market-thesis-data-bootstrap.md`

These docs are local working design context. Do not assume they are tracked.

### Research Hand Policy

Research hand routing was constrained to the configured research chain:

- `app/config.py` adds `research_hands: str = "codex,agy"` and `research_hand_names`.
- `app/hands/registry.py` adds `resolve_chain`.
- `app/router/executor.py` supports explicit `fallback_chain`.
- `app/institute/workflows.py` uses the configured research hands for research workflow steps.
- tests override research hands to `echo`.
- `tests/test_workflows.py` adds a focused test proving research uses configured hands only.

Verification already run:

```text
.venv/bin/python -m pytest tests -q
# 39 passed

.venv/bin/python -m compileall app -q
```

### Market Thesis Data Bootstrap

`market-thesis-data/` exists and is untracked. It is the intended starting dataset for thesis registry and stock map implementation.

Observed dataset shape:

```text
files:
  README.md
  manifest.json
  bundle.json
  theses.json
  lanes.json
  stocks.json
  edges.json
  theses.csv
  lanes.csv
  stocks.csv
  thesis_stock_edges.csv

counts:
  theses: 74
  lanes: 55
  stocks/ETFs: 236
  edges: 1888
  thesis-stock edges: 1020

market distribution:
  US: 96
  A-share: 75
  HK: 26
  US ETF: 21
  US ADR: 8
  A-share ETF: 5
  Korea: 2
  HK ETF: 2
  Japan: 1

actionCode distribution:
  pair_or_probe: 30
  deep_research_candidate: 21
  trim_or_hedge: 16
  probe: 4
  radar: 3
```

JSON validation already passed:

```text
python3 -m json.tool market-thesis-data/manifest.json
python3 -m json.tool market-thesis-data/bundle.json
```

The import contract is documented in:

- `design/local-thesis-alpha/10-market-thesis-data-bootstrap.md`
- `roadmap/07-market-thesis-data-kickoff.md`

### Obsidian Roadmap Console

The roadmap console was implemented inside the existing Obsidian plugin, not the React SPA.

Key files:

- `obsidian-plugin/src/roadmap.ts`
- `obsidian-plugin/src/main.ts`
- `obsidian-plugin/src/dashboard.ts`
- `obsidian-plugin/README.md`
- `obsidian-plugin/tsconfig.json`
- generated bundle: `obsidian-plugin/main.js`

Implemented behavior:

- registers an `institute-roadmap` `ItemView`;
- adds a ribbon icon and command `Institute: 打开路线图`;
- adds a dashboard shortcut button `路线图`;
- imports `roadmap/backlog.json` directly into the plugin bundle;
- renders Kanban columns: `inbox`, `ready`, `in_progress`, `review`, `verify`, `done`, `parked`;
- supports filters by search, phase, status, priority, and type;
- supports drag/drop card moves;
- persists local status overrides in plugin settings under `roadmapStatusOverrides`;
- prevents moving a card to `done` when dependencies are incomplete;
- shows card details: summary, acceptance, verification commands, expected files, design links, dependencies, and deterministic agent prompt;
- shows release gate progress for Release A–F (M0–M10);
- exports a markdown-backed Kanban note to `Institute/Roadmap/Implementation Kanban.md` through `plugin.subPath(...)`.

External reference used: existing Obsidian Kanban plugins were treated as UX/format inspiration only. Do not copy GPL code from community plugins.

Verification already run:

```text
cd obsidian-plugin && npm run build
python3 -m json.tool roadmap/backlog.json
git diff --check
```

All passed.

## Important Existing Repo Rules

Read `CLAUDE.md` before coding. Key constraints:

- model calls must go through `app/router/executor.py`;
- use conditional-claim state transitions;
- migrations are additive only;
- scheduler jobs never raise;
- prompts are product surface, do not casually rewrite them;
- vault notes are projections and only `app/vault/writer.py` writes under the vault;
- timestamps use `bus.now_iso()` for storage and SGT work dates where user-facing;
- plugin HTTP must use Obsidian `requestUrl`, not browser `fetch`;
- before restarting the backend, check the task queue to avoid orphaning running CLI tasks.

Also preserve these operator constraints:

- no Cloudflare;
- no hosted PM tool;
- no arbitrary command runner from imported JSON;
- no destructive git commands;
- no git push;
- do not revert user or prior-agent changes.

## Recommended Next Coding Sequence

### Step 1 - Make Roadmap State Durable (`M7-001`)

Implement the local SQLite roadmap backend first. This makes the roadmap console the real global coding process, not just a bundled seed view.

Expected files:

```text
migrations/*.sql
app/institute/roadmap.py
app/api/roadmap.py
app/main.py
tests/test_roadmap.py
```

Minimum schema:

- `roadmap_cards`
- `roadmap_dependencies`
- `roadmap_checklists`
- `roadmap_evidence`
- `roadmap_sessions`
- `roadmap_session_commands`
- `roadmap_decisions`
- `roadmap_events` if needed

Use `roadmap/02-data-model.md` as the source contract. Import `roadmap/backlog.json` idempotently by `card.id`. Preserve the JSON fields: `id`, `title`, `type`, `phase`, `status`, `priority`, `risk`, `summary`, `design_links`, `expected_files`, `dependencies`, `acceptance`, and `verification`.

Target API surface:

```text
GET    /api/roadmap/cards
GET    /api/roadmap/cards/{id}
POST   /api/roadmap/import
PATCH  /api/roadmap/cards/{id}
POST   /api/roadmap/cards/{id}/move
POST   /api/roadmap/cards/{id}/sessions
PATCH  /api/roadmap/sessions/{id}
POST   /api/roadmap/sessions/{id}/commands
GET    /api/roadmap/sessions
GET    /api/roadmap/release-gates
```

Acceptance:

- seed import upserts cards by id;
- dependencies validate known ids;
- moving to `done` fails when dependencies are not complete unless an explicit override field is provided;
- status/type/priority values are validated;
- tests cover idempotent import, dependency validation, move rules, and basic session creation.

Verification:

```text
.venv/bin/python -m pytest tests/test_roadmap.py -q
.venv/bin/python -m compileall app -q
```

### Step 2 - Wire Obsidian Roadmap View To API (`M7-003` continuation)

Once `M7-001` exists, update `obsidian-plugin/src/roadmap.ts` so it prefers the backend API and falls back to bundled `roadmap/backlog.json` only when the backend is unavailable.

Expected changes:

- add roadmap methods to `obsidian-plugin/src/api.ts`;
- load cards from `/api/roadmap/cards`;
- move cards through `/api/roadmap/cards/{id}/move`;
- save sessions/evidence once APIs exist;
- keep Kanban markdown export;
- keep local overrides only as fallback/offline state.

Verification:

```text
cd obsidian-plugin && npm run build
```

### Step 3 - Build Thesis Registry And Security Master (`M1-001`, `M2-001`)

After roadmap durability, start the actual thesis-alpha data model.

Implement additive migrations for:

- thesis lanes;
- theses;
- thesis versions;
- import batches/items;
- securities;
- security aliases;
- thesis-security edges.

Use `design/local-thesis-alpha/02-thesis-stock-model.md` and `design/local-thesis-alpha/10-market-thesis-data-bootstrap.md`.

Critical requirements:

- support China A-share suffixes (`.SH`, `.SZ`, `.BJ`) and market normalization;
- support HK and US symbols;
- keep aliases for Chinese names and unsuffixed tickers;
- preserve import provenance from `market-thesis-data/`;
- keep schema additive and local SQLite only.

Verification should include:

```text
.venv/bin/python -m pytest tests/test_theses.py tests/test_securities.py tests/test_market_thesis_import.py -q
.venv/bin/python -m compileall app -q
```

### Step 4 - Import `market-thesis-data/` (`M1-003`)

Implement a dry-run and apply path for `market-thesis-data/bundle.json`.

Dry-run should report:

- count of lanes, theses, stocks, and edges;
- unknown market labels;
- duplicate symbols;
- edges referencing missing theses or securities;
- action-code distribution;
- China A-share coverage count.

Apply should be idempotent.

## Suggested First Claude Prompt

Use this as the first takeover command:

```text
Read CLAUDE.md, AGENTS.md, roadmap/README.md, roadmap/02-data-model.md, roadmap/05-global-coding-process.md, roadmap/08-claude-handover.md, and git status.

Do not push, do not revert existing changes, do not introduce hosted infrastructure.

Start with roadmap card M7-001: implement the local SQLite roadmap schema, idempotent seed import from roadmap/backlog.json, minimal roadmap API, and tests. Keep migrations additive. After implementation, run:

.venv/bin/python -m pytest tests/test_roadmap.py -q
.venv/bin/python -m compileall app -q
cd obsidian-plugin && npm run build

Summarize changed files, verification results, and remaining follow-up cards.
```

## Final Safety Checklist Before Any Handoff Commit

Before committing or handing back:

1. run `git status --short`;
2. confirm `design/` remains ignored;
3. confirm `market-thesis-data/` is intentionally untracked unless the operator says otherwise;
4. run targeted tests for touched backend files;
5. run `cd obsidian-plugin && npm run build` if plugin files changed;
6. run `git diff --check`;
7. do not push.
