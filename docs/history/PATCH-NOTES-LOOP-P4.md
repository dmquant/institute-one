# LOOP-P4 — chain tick cursor can no longer wedge on a poison event

Date: 2026-07-20
Package: `roadmap/loop-fix-backlog.md` P4（chain 游标卡死，中）
Files: `app/institute/chain.py`, `tests/test_chain.py`

## Overlap determination (vs the R1/R2 chain hardening in PATCH-NOTES-CHAIN-PROPS.md)

The package predates the R1/R2 rounds; most of its surface is already
covered — this round only closed the remaining gap.

Already covered (no work repeated):

- **Application failures never replay the model** — R1 staging: extraction
  output lands in `chain_property_staging` before the cursor advances;
  failed application stays `pending` and retries model-free.
- **Deterministic unknown-entity assertions are bounded** — R2 P1-3:
  `attempts` counter, terminal `skipped` at `STAGING_UNKNOWN_ATTEMPTS`.
- **Deterministic extraction/assembly failures never wedge the cursor** —
  pre-existing R1 semantics: the per-event `try/except` logs and the cursor
  still advances (best-effort enrichment; verified at the tick loop's first
  exception handler).
- **Cursor writes are conditional claims** — R2 P2-1 `_advance_cursor` CAS.

TRUE GAP: a deterministic **persistence** failure (`record_candidates` /
`_stage_properties` raising every time, e.g. schema drift or a poisoned
payload) hit the R1 "halt the batch before the event's cursor" path
UNBOUNDEDLY — the cursor stayed on the same event forever and every hourly
tick re-paid that event's extraction model call. Exactly the backlog's
finding (cited as chain.py:1665-1678; the halt branch now sits in `tick()`'s
persistence `except`).

## Fix

- `_note_persist_failure(event_id)`: one `admin_state` row
  (`chain:extract_persist_failures`) tracks the halting head-of-queue event
  and its consecutive persistence failures; a different event id resets the
  count (only the head event can hold the cursor, so one row suffices). The
  counter is advisory telemetry — the actual skip still gates through the
  `_advance_cursor` conditional claim, so a cross-process lost update costs
  at most one extra retry.
- At `TICK_PERSIST_FAILURE_LIMIT` (3) failures the event is DROPPED: an
  operator card opens first (`operator.open_action` — public API, safe
  outside a transaction, idempotent per live ref `chain-extract:<event_id>`,
  kind `failed_run`), then the cursor advances past the event and the
  counter row is cleared. Card-before-advance means a crash between the two
  re-converges next tick and a drop can never be silent; a card-open failure
  keeps holding the cursor for another bounded retry.
- Below the limit the behavior is unchanged (halt the batch, replay next
  tick), so transient persistence failures keep their durable no-loss
  semantics; the model spend for one poison event is now capped at
  `TICK_PERSIST_FAILURE_LIMIT` extraction calls total.

No new migration: the counter lives in the existing `admin_state` key/value
table.

## Regression test

- `test_tick_poison_persistence_skips_event_after_bounded_failures` —
  deterministic staging failure: limit−1 ticks halt with the cursor held and
  no card; the Nth tick drops the event (cursor lands on it, `failed_run`
  card with `chain-extract:<id>` ref opens); after healing, nothing replays
  and total extraction tasks equal exactly `TICK_PERSIST_FAILURE_LIMIT`.
  (Written first, red against the pre-fix tree.)

## Verification

- `tests/test_chain.py`: `63 passed`
- Combined run: see PATCH-NOTES-LOOP-P11-chain.md (one run covers P4/P8b/P11).

## R3 闭合 (2026-07-20, merge-readiness review P1)

R3 found the model-call bound unsound across a crash window: the failure
count was read/written only AFTER extraction had already run, so a crash
after the Nth counter write (or after the card) but before the cursor CAS
made the NEXT tick re-extract the exhausted event — count N+1, then N+2 …
unbounded calls under repeated crashes. `_note_persist_failure` was also a
read-then-overwrite, losing counts across processes.

Fixes:

- **Pre-extraction exhaustion gate**: `tick()` now reads the durable
  failure state (`_persist_failures`) for every event BEFORE any extraction.
  `count >= TICK_PERSIST_FAILURE_LIMIT` IS the drop-pending marker: the
  event is dropped straight away — idempotent card first
  (`chain-extract:<id>` live ref), then the `_advance_cursor` CAS, then the
  counter clear — with no model call, wherever the previous attempt
  crashed. Total extractions for a poison event are now exactly the limit.
- **Atomic increment**: the counter bump is one in-place
  `UPDATE … json_set(value,'$.count', json_extract(value,'$.count')+1)
  WHERE key=? AND json_valid(value) AND json_extract(value,'$.event_id')=?`
  — statement-atomic, so concurrent owners can only push the count higher
  (the bound still holds); a mismatch/malformed row falls back to resetting
  the counter to this event with count 1. `_clear_persist_failures` is now
  event-id-guarded so it cannot erase a concurrent owner's fresh count.

R3 regression tests (both written first, red against the pre-fix tree with
`calls == limit + 1`; the failing test IS the reviewer's reproduction —
`_stage_properties` pinned to always fail, crash injected at the named
point, recovery asserted at exactly `TICK_PERSIST_FAILURE_LIMIT` calls):

- `test_tick_drop_crash_before_cursor_never_reextracts` — crash after the
  Nth count + card, before the cursor advance.
- `test_tick_drop_crash_after_count_before_card_never_reextracts` — crash
  after the Nth count, before the card.

Verification: `tests/test_chain.py` `67 passed`;
`pytest tests/test_chain.py tests/test_operator.py tests/test_db_migrate.py
-q` → `153 passed`; `compileall` OK. No new migration (counter stays in
`admin_state`).
