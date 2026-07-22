# PATCH NOTES — SPA A

Date: 2026-07-20

Scope: Dashboard / Tasks / Research operator-console polish. No shared UI, API client, stylesheet, route, or dependency changes.

## Dashboard

- Added an operator signal strip:
  - open operator actions from `GET /api/operator/triage` → `actions.open`;
  - forecast hit rate, defined explicitly as `hit / (hit + miss)` with `partial` excluded;
  - vector substrate status, current model, reason, and current-model chunk count from `GET /api/vectors/health`.
- Forecast verdicts are not present on `GET /api/forecasts`; the page therefore loads up to the latest 500 settled forecasts with `listForecasts`, then resolves details in batches of 25 with `getForecast`.
- Queue, hands, and recent-task sections now use the shared `Loading`, `Empty`, and `ErrorNote` states consistently.

## Tasks

- Replaced the status select with URL-backed filter chips, including terminal status `overcommitted`.
- Statuses not known by this frontend are appended as raw-value chips instead of being hidden.
- Failed-row error summaries can expand in place without opening the task drawer.
- The task drawer exposes the persisted fallback policy and links a retry generation back to its `lineage_root`.

Backend contract finding: `GET /api/tasks` currently selects neither `lineage_root` nor `fallback_chain`; both fields are available only from `GET /api/tasks/{id}` through the executor `Task` response. Lineage therefore belongs in the detail drawer unless the list projection is expanded later.

## Research

- `GET /api/research/queue` returns `SELECT *` rows, including optional `thesis_id` and `security_id`; both associations now render compactly in queue rows and item details.
- Queue cancellation and manual tick now expose busy/error feedback, and queue/log/report loading and empty states are consistent.
- No research-item requeue button was added: the mounted API has `POST /api/research/queue/{item_id}/cancel` but no research-item retry/requeue route.

## Data-layer follow-ups

Page-local fetches that should move into `frontend/src/api.ts` when that file is available:

- `Dashboard.tsx` → `GET /api/vectors/health`; add a `VectorHealth` type plus `getVectorHealth()` helper, then remove the inline authenticated fetch. It currently mirrors the `institute:token` Bearer-token logic and SPA HTML-fallback guard.

Related type drift to fold into that future API-client pass:

- add `fallback_chain` and `lineage_root` to the full `Task` type;
- add optional `thesis_id` and `security_id` to `ResearchItem`;
- consider a backend forecast-performance aggregate to replace Dashboard's bounded detail fan-out.

## Verification

- `cd frontend && npx tsc -noEmit` — passed.
