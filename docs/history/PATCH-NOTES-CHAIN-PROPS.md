# Chain properties + conflicts

Date: 2026-07-20
Roadmap: Phase 4 — Properties + conflicts

## What shipped

- `migrations/0030_chain_properties.sql` adds sourced, temporal entity
  properties with active/superseded/conflicted/retired history, supersede and
  conflict-group audit links, one-active-value enforcement, and an optional
  operator-action link.
- `chain.upsert_property()` implements the hybrid policy transactionally:
  ordinary updates conditionally supersede the active row; different sources
  asserting materially different values for the same `as_of` period move both
  assertions into a conflict group.
- Exact assertion replays are idempotent. Conflict cards use the existing
  public `operator.open_action()` API with kind `other` and a stable
  `chain-property:<group>` ref, so retries and concurrent writers converge on
  one live operator action. Resolution uses `operator.resolve_action()`.
- `get_properties()`, `list_conflicts()`, and
  `resolve_property_conflict()` expose current/history reads, grouped conflict
  reads, and a conditional-claim winner/retire transition.
- The entity extraction prompt accepts optional
  `PROPERTY: entity | key | value | as_of` lines. `tick()` parses entities and
  properties from the same model call, runs promotion/alias clustering, then
  records properties whose entity resolves to a durable chain node before
  advancing the event cursor.
- REST endpoints:
  - `GET /api/chain/nodes/{node_id}/properties`
  - `GET /api/chain/properties/conflicts`
  - `POST /api/chain/properties/conflicts/{conflict_group}/resolve`

## Policy details

- Property keys are whitespace-normalized to lowercase snake-like keys.
- Conflict comparison ignores case and whitespace-only value differences.
- Same-source corrections and different-period updates follow the normal
  supersede path.
- A resolved winner becomes active unless another period is already active for
  that key; in that case the selected historical winner stays superseded, so
  resolving an old dispute cannot displace the current value.
- Unknown extracted entities are skipped rather than staged: the table is
  intentionally foreign-keyed only to durable `chain_nodes`.

## Verification

- Focused property tests: `6 passed`
- Required chain suite: `47 passed`
- Migration discipline/recovery suite: `19 passed`
- `.venv/bin/python -m compileall app -q`: passed

## Remaining risks

- Property keys and values are model-produced text; normalization removes
  formatting-only differences but does not perform unit conversion or semantic
  synonym resolution (`100 GWh` versus `0.1 TWh` can still conflict).
- Operator actions currently use the existing `other` kind because the public
  operator vocabulary has no chain-property-specific kind.
- Properties for entities that remain unpromoted after the processing sweep
  are terminally `skipped` in `chain_property_staging` — a later promotion
  does not replay them (policy unchanged from this round; storage changed
  with the review fixes below).

## Review fixes (2026-07-20)

An independent review flagged three findings against this round; all three
are closed here. Files touched: `app/institute/chain.py`,
`migrations/0030_chain_properties.sql` (safe to extend in-round — verified
the local ledger tops out at `0026`, so 0030 has never been applied
anywhere), `tests/test_chain.py`, and this file.

### Finding 1 (high) — conflict side effects forked outside the transaction

`upsert_property()` committed the property rows first and only then called
`operator.open_action()` and `bus.emit()`: a card failure left committed
`conflicted` rows with no action and no event, and
`resolve_property_conflict()` could likewise fail after its commit.

- The operator card is now created/recovered and linked INSIDE the property
  transaction (`_ensure_conflict_action`, replacing
  `_surface_property_conflict`). `operator.py` has no transaction-aware
  helper and `open_action()` takes the process write lock the transaction
  already holds, so the card is written to `operator_actions` directly with
  `open_action()`'s exact field semantics: `other` kind,
  `chain-property:<group>` ref behind the live-ref check (the 0018 partial
  unique index stays the backstop), `_fold_line(title, 200)`,
  `detail[:2000]`, priority 2. A card failure rolls the whole assertion
  back; the retry recreates rows + card + event together.
- `resolve_property_conflict()` closes the card in the same transaction as
  the winner/retire transitions (`resolve_action`'s conditional claim,
  inlined).
- `chain.property_conflict` / `chain.property_resolved` stay post-commit but
  are best-effort now (the roadmap audit-in-transaction + best-effort mirror
  pattern): an emit failure is logged, never raised, and never unwinds the
  committed rows.

### Finding 2 (high) — late assertions bypassed conflict detection

Same-period comparison only looked at `active`/`conflicted` rows, so
Q1/sourceA → Q2/sourceA → Q1/sourceB (different value) missed the Q1 dispute
entirely — and worse, the third write superseded the newer Q2 current with an
older period ("current" went back in time).

- Conflict detection now compares against every source's CURRENT word about
  the period: its latest non-retired row, INCLUDING `superseded` rows (a row
  displaced by a newer period was never retracted). A source's own older
  corrections and resolution losers stay out of the comparison, so agreeing
  with a source's current value can never conflict against its retracted one.
- On conflict, the other sources' current-word rows (active or superseded)
  join the conflict group. A late-period dispute therefore only touches that
  period's rows; the newer period's active row is never displaced, and the
  existing resolution rule (winner stays `superseded` while another period is
  current) keeps a historical winner historical.
- A LATE non-conflicting assertion — `as_of` strictly below the key's live
  horizon (`MAX(as_of)` over active+conflicted rows; period strings compare
  lexicographically, which is chronological for the prompt's zero-padded
  forms) — lands directly as `superseded` history with no `supersedes_id`.
  Side benefit: one artifact listing periods newest-first no longer ends
  with an older period active.

### Finding 3 (medium-high) — tick cursor lost assertions / replayed model calls

Property-application errors were swallowed while the cursor advanced (the
assertion existed only in memory — lost forever), and the cursor advance sat
AFTER `_auto_cluster`/`_auto_promote`, so a failure there replayed the whole
batch of model extractions and burned quota.

- New durable staging table `chain_property_staging` (added to 0030):
  `tick()` persists each event's parsed PROPERTY assertions there BEFORE
  advancing that event's cursor. `UNIQUE (event_id, entity, prop_key, value,
  as_of)` makes a crash-replayed event's re-staging a no-op; `event_id` is a
  soft reference (the janitor prunes `events` rows).
- The cursor advances per event, immediately after that event's extraction
  output is durably persisted (candidate sightings + property staging).
  Assembly/extraction failures still advance it (unchanged wedge
  protection), but a PERSISTENCE failure halts the batch before the failed
  event's cursor — only that event replays next tick.
- `_auto_cluster`/`_auto_promote` failures can no longer replay the batch:
  by then every consumed event's cursor is already committed.
- `_apply_staged_properties` (replacing `_record_extracted_properties`) runs
  after the promotion sweep as before: `pending` → `applied` on success or
  exact replay; `pending` → `skipped` terminally for entities still unknown
  after the sweep (pre-staging semantics preserved) and for deterministic
  refusals (`ChainError`/`LookupError`); any other failure keeps the row
  `pending`, so the next tick retries it WITHOUT a new model call. Batched
  at 200 rows per tick.

### Regression tests (tests/test_chain.py)

- `test_property_conflict_action_failure_rolls_back_assertion` — injected
  card failure rolls the conflict transition back; the retry lands rows +
  card + event.
- `test_property_bus_mirror_failure_never_unwinds_commit` — emit failures on
  both paths log instead of raising; rows and card stay committed.
- `test_late_assertion_conflicts_with_superseded_same_period` — the review's
  Q1/A → Q2/A → Q1/B sequence: Q1 disputes against the superseded row, Q2
  stays active, the resolved historical winner stays `superseded`.
- `test_late_assertion_records_history_without_displacing_active` — late
  same-source and late agreeing-source assertions land as history only.
- `test_tick_apply_failure_keeps_cursor_and_never_replays_model` —
  application failure: cursor advances, staging stays `pending`, the retry
  applies with no new model call (`tasks` count pinned at 1 throughout).
- `test_tick_staging_persistence_failure_halts_batch_before_cursor` — a
  staging write failure holds the cursor back; only the unpersisted event
  replays (one extra model call, no lost assertion).
- `test_staged_property_unknown_entity_terminally_skipped` — unknown
  entities park as `skipped`, never retried.

### Verification (review fixes)

- `.venv/bin/python -m pytest tests/test_chain.py tests/test_operator.py -q`:
  `112 passed` (54 chain + 58 operator)
- Full suite `.venv/bin/python -m pytest tests -q`: `992 passed, 4 skipped`
- Migration discipline/recovery suite: `19 passed`
- `.venv/bin/python -m compileall app -q`: passed

## R2 adversarial review fixes (2026-07-20)

A second adversarial review (R2) returned REQUEST_CHANGES with four findings
against the fixes above; each attack was first reproduced against the working
tree with a throwaway script (`/tmp/r2_repro.py` — all four confirmed, then
re-run to confirm all fixed). Files touched: `app/institute/chain.py`,
`migrations/0030_chain_properties.sql` (additive column on the still-unapplied
staging table), `tests/test_chain.py`, this file.

### R2 P1-1 — `as_of` periods now stored in zero-padded canonical form

Raw string comparison ranked `2026-2` above `2026-10`, so a February
assertion displaced the October current through the live-horizon check, and
`2026-2` / `2026-02` counted as two different periods in the same-period
comparison.

- `_normalize_period()` runs inside `_property_inputs()`: every maximal
  single-digit run gains a leading zero (`2026-2` → `2026-02`, `2026-Q2` →
  `2026-Q02`, `2026-W7` → `2026-W07`); multi-digit runs pass through, and
  the transform is idempotent. Periods are therefore stored AND compared
  only in canonical form — lexicographic comparison is chronological within
  a format family, and spelling variants collapse to one period identity
  (the padded spelling of an existing assertion is an exact replay).
- Compatibility with pre-normalization rows: none exist — `0030` has never
  been applied outside throwaway test DBs (the live ledger tops out at
  `0026`). If a DB somehow carried raw rows, they would keep their stored
  spelling and only compare against new assertions under that spelling; the
  canonical form applies to every write from this change forward.
- Mixed format families for one key (e.g. quarters vs months) remain
  documented garbage-in, unchanged from R1.

### R2 P1-2 — same-second corrections resolve by rowid, not random uuid

`ORDER BY created_at DESC, id DESC` tie-broke same-second rows by the random
uuid `id`, so the "latest non-retired per source" pick could select a
RETRACTED value and manufacture a conflict against a source that agreed with
the real current word.

- Every semantic ordering now tie-breaks on SQLite's implicit monotonic
  `rowid` (insertion order): the per-source-latest pick and the
  supersedes-target pick in `upsert_property()`, plus the group-row
  orderings in `_ensure_conflict_action()` and
  `resolve_property_conflict()` (deterministic card/detail order). No
  schema change needed — `chain_properties` is a rowid table.

### R2 P1-3 — promotion racing the skip check no longer swallows assertions

`_apply_staged_properties()` read the entity resolution and then terminally
marked the row `skipped` in a separate write: a promotion committing between
the two permanently discarded a legitimate assertion (TOCTOU).

- The staging table gains `attempts INTEGER NOT NULL DEFAULT 0` (additive on
  the 0030 table introduced this round). An unresolved entity now costs the
  row ONE attempt via a single atomic UPDATE (`attempts = attempts + 1,
  status = CASE WHEN attempts + 1 >= N THEN 'skipped' ELSE 'pending' END` —
  decision and counter move together, so a racing promotion merely wastes
  one attempt and the next sweep applies the row).
- `STAGING_UNKNOWN_ATTEMPTS = 24` (one hourly-tick day of grace) bounds the
  retries before the terminal `skipped`; this also softens the R1 "unknown
  entities are one-shot" limitation — an entity promoted within a day now
  rescues its staged assertions. Deterministic refusals
  (`ChainError`/`LookupError`) still skip outright; transient application
  failures still stay `pending` without spending attempts.

### R2 P2-1 — the tick cursor is a conditional claim (CAS)

`_set_cursor()` wrote unconditionally, so a second tick owner in another
process could regress the cursor with its stale batch position and replay
the winner's model extractions.

- Replaced by `_advance_cursor(prev, event_id)`: seeds the `admin_state` row
  once, then `UPDATE … WHERE key = ? AND CAST(value AS INTEGER) = prev`
  (CAST mirrors `_get_cursor`'s tolerant decode). The swap only lands while
  this owner still holds the cursor — regression is impossible.
- On a lost claim, `tick()` abandons the remainder of its batch (the
  already-done work is idempotent; the events belong to the winner), so at
  most the single in-flight event's extraction is double-spent, never the
  cascade. In-process overlap remains excluded by `_tick_lock` as before.

### Regression tests (tests/test_chain.py)

- `test_period_normalization_blocks_short_month_displacing_current` — the
  `2026-2` vs `2026-10` attack lands as late history; the padded spelling
  replays onto the same row. (Existing fixtures updated for the canonical
  form: `2026-Q2` → stored `2026-Q02`.)
- `test_same_second_correction_beats_uuid_order` — frozen clock + forced
  descending uuids: the correction stays the source's current word; an
  agreeing source no longer manufactures a conflict.
- `test_staged_property_survives_promotion_racing_the_skip_check` — a
  promotion landing right after the stale resolve read costs one attempt
  (`pending`, not `skipped`); the next sweep applies the assertion.
- `test_staged_property_unknown_entity_skipped_after_bounded_attempts` —
  (reworked from the R1 one-shot test) misses count attempts and the
  terminal skip fires exactly at the bound.
- `test_tick_cursor_conditional_claim_never_regresses` — CAS unit checks
  (stale owner refused, cursor never regresses) plus a mid-batch rival
  integration: the loser abandons its batch without extracting the next
  event, and the next tick resumes from the winner's cursor.
- `test_cursor_crash_replay_does_not_double_count` updated to stub the CAS
  (`_advance_cursor`) instead of the removed `_set_cursor`.

### Verification (R2 fixes)

- `.venv/bin/python -m pytest tests/test_chain.py tests/test_operator.py
  tests/test_db_migrate.py -q`: `135 passed` (58 chain + 58 operator + 19
  migrations). Note: mid-verification a parallel workstream added four
  test-first `test_router_failure_placeholder*` cases to
  `tests/test_operator.py` whose `operator.py` implementation has not landed
  yet; they are unrelated to chain (no chain imports, red with or without
  this change) and were deselected in the final re-run — every pre-existing
  operator test stays green.
- `.venv/bin/python -m compileall app -q`: passed
- `/tmp/r2_repro.py` re-run: P1-1 late history + canonical storage, P1-2 no
  false conflict, P2-1 stale claim refused with no cursor regression and no
  extra model task.
