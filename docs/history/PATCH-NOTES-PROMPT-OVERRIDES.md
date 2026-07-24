# PATCH NOTES — Prompt overrides (ROADMAP Phase 2 ☐ "Prompt-overrides")

`prompt_overrides` table (shadow → active → retired, per scope) layered over
the prompt constants, with an operations API — prompt iteration becomes data
instead of code, relaxing CLAUDE.md rule 4 **safely**: with no active override
every prompt renders byte-identically to the code constants (pinned by tests).

## Files

| File | Change |
|---|---|
| `migrations/0029_prompt_overrides.sql` | NEW — `prompt_overrides` table + partial unique index (≤1 active per scope) |
| `app/institute/prompt_overrides.py` | NEW — scope registry, `resolve`/`render` + process cache, CRUD, conditional-claim lifecycle |
| `app/institute/prompts.py` | four mount points now render via `prompt_overrides.render()`; templates extracted verbatim (`DATE_ANCHOR_TEMPLATE`, `PERSONA_TEMPLATE`; `CITATION_MANDATE`/`FILE_DELIVERABLE` unchanged) |
| `app/api/prompt_overrides.py` | NEW — operations API (CRUD + transitions + scope query + diff preview). **Not mounted** (see below) |
| `tests/test_prompt_overrides.py` | NEW — 15 tests |

## Mount lines for app/main.py (main-controller action required)

The router is deliberately not wired (per task boundary, `app/main.py`
untouched). Two edits inside `create_app()` / `lifespan()`:

1. In `create_app()`, add to the `from .api import (…)` block:

```python
        prompt_overrides as api_prompt_overrides,
```

   and add to the `for r in (…)` router tuple:

```python
        api_prompt_overrides.router,
```

2. **Recommended** boot pre-warm in `lifespan()` (right after the
   `refresh_weights_cache()` call — same idiom, same reason: `resolve()` is
   sync and never reads the DB itself):

```python
    from .institute.prompt_overrides import refresh_cache as refresh_prompt_overrides
    await refresh_prompt_overrides()
```

Degradation without the pre-warm: prompts render from the **code defaults**
(byte-identical pre-override behaviour) with ONE warning logged; any
`GET /api/prompt-overrides` lazy-heals the cache (the hand-weights idiom).
Nothing breaks — active overrides just don't apply until the first heal.

Note: mounting the router adds routes to the `tests/test_api_routes.py`
auto-enumerated smoke. They classify cleanly under its existing rules (GETs
don't 5xx; mutating routes have required bodies → 422, or path params → 404),
so no table entries are needed.

## Design

- **Schema** (`0029`): `id, scope, content, status CHECK ('shadow','active','retired'), note, created_at, activated_at, retired_at`; `CREATE UNIQUE INDEX … ON prompt_overrides(scope) WHERE status='active'` is the DB backstop for "at most one active per scope". `scope` carries no CHECK (open set, code-enforced against the registry — the 0023 recipes precedent).
- **Lifecycle** (hard rule 2): `activate()` runs in ONE transaction — retire the scope's previous active + conditional-claim the shadow (`… WHERE id=? AND status='shadow'`, rowcount checked); `retire()` is a one-shot conditional claim. Lost claims raise `OverrideConflict` (409 at the API). Retired rows are immutable history; re-activating old content = new shadow row.
- **Read path**: `resolve(scope, default)` / `render(scope, default, **fields)` are sync (prompt assembly is sync) over a process-local cache pushed by `refresh_cache()` (invalidation hook: `invalidate_cache()`; every lifecycle write refreshes). Cold cache = code defaults + one warning. `render` falls back to the default rendering if an active override fails to format (e.g. manual DB edit) — the prompt path can never break on data.
- **Validation**: scope must be registered; templated scopes may only use the registry's placeholder fields (`string.Formatter().parse`); zero-field scopes are literal blocks (braces stay literal). Content ≤16000 chars, note ≤2000.
- **Shadow semantics**: a shadow row is a recorded draft — it never touches prompts; the row itself is the record (list/diff it via the API), no logging hook needed on the hot path.

## Mount points (4 — all in `app/institute/prompts.py`, the one domain file in scope)

These four blocks enter EVERY analyst prompt across dailies / whiteboard /
mailbox / workflow steps / ask / MCP — the highest-leverage cut, per the task
boundary (daily-task / research-step templates live in `analyst_daily.py` /
`workflows/research.json`, out of scope; the registry design lets a later
card add e.g. `analyst_daily.task` scopes by extending `SCOPES` + one render
call at the site).

| scope | default | fields |
|---|---|---|
| `prompts.date_anchor` | `DATE_ANCHOR_TEMPLATE` | `{datetime}` |
| `prompts.persona_block` | `PERSONA_TEMPLATE` | `{name} {name_en} {focus} {persona}` |
| `prompts.citation_mandate` | `CITATION_MANDATE` | — (literal) |
| `prompts.file_deliverable` | `FILE_DELIVERABLE` | `{filename}` |

## API (prefix `/api/prompt-overrides`)

- `GET ""` — list (filters: `scope`, `status`, `limit`); opportunistically refreshes the resolve cache
- `GET /scopes` — every registered scope: description, fields, code default, active id, per-status counts (+ stray unregistered DB scopes, marked inert)
- `POST ""` — create shadow draft (201)
- `GET /{id}` / `PUT /{id}` (drafts only) / `DELETE /{id}` (drafts only, 204)
- `GET /{id}/diff` — unified diff override vs code default
- `POST /{id}/activate` — shadow → active (atomically retires old active)
- `POST /{id}/retire` — active → retired

## Verification

```
.venv/bin/python -m pytest tests/test_prompt_overrides.py tests/test_analyst_daily.py tests/test_workflows.py -q
→ 42 passed  (15 new + 27 regression: default behaviour byte-unchanged)

.venv/bin/python -m pytest tests/test_db_migrate.py tests/test_memory.py tests/test_whiteboard_similarity.py tests/test_digests.py -q
→ 86 passed  (migration hygiene incl. 0029; the existing prompt byte-identity witnesses)

.venv/bin/python -m compileall app -q → OK
```

Not committed (per instructions). Roadmap: this lands the Phase 2
`☐ Prompt-overrides (M)` item — flip it to ☑ in `ROADMAP.md` / backlog when
the main controller wires the mount.
