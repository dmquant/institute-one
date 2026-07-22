# LOOP-P8b — _auto_cluster pending scan is bounded and ages out

Date: 2026-07-20
Package: `roadmap/loop-fix-backlog.md` P8b（事件循环阻塞 — chain 部分）
Files: `app/institute/chain.py`, `tests/test_chain.py`

## Overlap determination

TRUE GAP — untouched by R1/R2 (those rounds hardened the property store and
the tick cursor, not the clustering pass). `_auto_cluster` (cited as
chain.py:1530-1561; verified in the current tree at the
`# ---- periodic auto-cluster` section) loaded EVERY pending candidate and
nested-matched it against every node's normalized name/alias terms
synchronously on the event loop: unbounded pending×terms work per hourly
tick, growing forever because pending candidates below the promote threshold
never left the pool.

## Fix

- **Scan bound**: the pending query takes `ORDER BY mention_count DESC,
  created_at ASC LIMIT CLUSTER_SCAN_BATCH` (200) — one sweep matches at most
  a batch of the highest-interest candidates; the remainder waits for the
  next hourly sweep instead of blocking the loop.
- **Aging** (`_age_out_candidates`, runs at the head of every cluster pass):
  pending candidates that sat below the promote threshold for
  `CANDIDATE_TTL_DAYS` (30) move to `rejected` in ONE conditional bulk claim
  (`UPDATE … WHERE status='pending' AND mention_count < ? AND created_at <
  ?`; rowcount = aged count), so the pool the scans iterate is bounded by
  live interest, not by history. Candidates at/over the threshold are never
  aged (they promote instead); the cutoff is derived from `bus.now_iso()`
  (no naked `datetime.now()`), and both sides of the comparison share the
  same ISO-UTC string shape.

## Regression tests

(Both written first, red against the pre-fix tree.)

- `test_auto_cluster_scan_is_bounded_per_tick` — three mergeable candidates,
  batch clamped to 2: first sweep merges exactly 2 (highest mention counts),
  the remainder lands on the next sweep.
- `test_auto_cluster_ages_out_stale_low_mention_candidates` — a stale
  below-threshold candidate ages to `rejected`; a fresh below-threshold one
  stays pending; a stale AT-threshold one is untouched by aging and
  auto-promotes in the same tick.

## Verification

- `tests/test_chain.py`: `63 passed`
- Combined run: see PATCH-NOTES-LOOP-P11-chain.md (one run covers P4/P8b/P11).

## R3 闭合 (2026-07-20, merge-readiness review P2)

R3 found the original bound one-sided: `CLUSTER_SCAN_BATCH` capped pending
candidates, but every sweep still loaded ALL `chain_nodes`, expanded ALL
aliases, and ran up to `batch × total surface terms` synchronous matching —
per-tick work still grew unbounded with the graph.

Fix — total comparison budget + persistent rotation (the reviewer's
"minimum correct" option; trade-offs below):

- `CLUSTER_COMPARE_BUDGET` (20 000) bounds the TOTAL surface-term
  comparisons per sweep — candidates × nodes × aliases — regardless of
  graph size. `CLUSTER_SCAN_BATCH` is removed (subsumed).
- Candidates and nodes are walked as a rowid rotation with durable progress
  in `admin_state` `chain:cluster_rotation` (`cand_cursor`, in-flight
  `candidate_id`, `node_cursor`, accumulated `matches` capped at 2): a
  candidate whose node scan outgrows one sweep parks mid-rotation and
  resumes next tick, so every bounded window is eventually visited.
- Correctness against the reviewer's stateless-LIMIT warning: a candidate's
  merge/ambiguity decision is made ONLY after its FULL node rotation
  completes; two accumulated matches short-circuit as ambiguous. A late
  second match in a different window is therefore still seen (regression
  below). The rotation state is advisory scan progress only — the merge
  itself remains the conditional claim inside `_merge_candidate_into_node`.

Trade-offs (documented per the review's instruction):

- One candidate's decision can span multiple ticks on a large graph
  (budget/terms per sweep); with 20 000 comparisons and ~3 terms/node the
  single-sweep decision capacity is ~6 000 nodes — beyond that, decisions
  land at a one-lap-per-few-ticks cadence. The full upgrade path is an
  indexed surface projection table for exact matches (containment scanning
  would still need this budget), deliberately NOT built this round to stay
  within the no-new-migration boundary.
- Candidate selection is now rotation-fair (rowid order) instead of
  mention-count-first; decided-but-unmerged candidates (no match /
  ambiguous) are rescanned on later laps by design — new nodes appear over
  time — with aging (`CANDIDATE_TTL_DAYS`) still bounding the pool.
- Cross-process sweeps share the advisory rotation state (last writer
  wins): worst case is re-scanned windows, never a wrong merge.

R3 regression tests (written first, red against the pre-fix tree):

- `test_auto_cluster_per_tick_comparisons_bounded_as_nodes_grow` — 30
  nodes, budget 12: the decision spreads across exactly three sweeps
  (bounded per-tick work) and the merge still lands after the full lap.
- `test_auto_cluster_ambiguity_across_windows_stays_pending` — matches in
  the first and last windows: never merged, stays pending (the stateless-
  LIMIT failure mode).
- `test_auto_cluster_scan_is_bounded_per_tick` reworked from the
  batch-count bound to the comparison-budget bound (mid-rotation parking +
  next-sweep resume).

Verification: `tests/test_chain.py` `67 passed`;
`pytest tests/test_chain.py tests/test_operator.py tests/test_db_migrate.py
-q` → `153 passed`; `compileall` OK. No new migration (rotation state lives
in `admin_state`; 0038 not needed).

## R4 闭合 (2026-07-21, merge-readiness review P1 + P2 + P2)

R4 found three remaining correctness/liveness defects in the parked
rotation. All state stays in `admin_state`; no migration was added.

### P1 — parked evidence is now bound to graph generation

`node_cursor` was only a SQLite rowid and `matches` survived across ticks.
Because `chain_nodes` is not AUTOINCREMENT, deleting the maximum row and
then promoting a node can reuse a rowid at/below the parked cursor; changing
an already-scanned node's aliases has the same stale-evidence effect. The
old resume could therefore miss a new second match and incorrectly merge
into the remembered sole match.

- `chain:graph_generation` is a monotonic `admin_state` integer. Every
  production chain-node surface write bumps it in the SAME transaction as
  the mutation. Repository-wide `rg` found exactly three production write
  points, all in `chain.py`, and all now covered:
  1. `create_node()` INSERT;
  2. `merge_aliases()` aliases UPDATE (now also a conditional claim checked
     by rowcount);
  3. `promote_candidate()` INSERT when promotion creates a new node.
  There is currently no production node-delete or rename path; any future
  one must call `_bump_graph_generation()` in its mutation transaction.
- Rotation state stores `generation`. A mismatch deletes the parked state
  and restarts candidate/node scanning at zero; this discards both rowid
  cursors and accumulated matches.
- Generation is checked again after scanning and inside
  `_merge_candidate_into_node()`'s final conditional-claim transaction.
  A mutation between scan and merge raises `ClusterGenerationChanged` and
  restarts instead of committing stale evidence.

Regressions:

- `test_auto_cluster_rowid_reuse_invalidates_parked_matches` — parks one
  match at cursor 2, deletes rowid 2, creates an exact-match node which
  reuses rowid 2; generation invalidation rescans and sees ambiguity.
- `test_auto_cluster_alias_change_invalidates_parked_matches` — an alias
  added behind a parked cursor invalidates the old sole-match evidence and
  prevents a false merge.

### P2 — alias-term progress is durable and per-node aliases are capped

When the budget expired inside one node's aliases, the old state did not
store term progress; every tick restarted at alias zero, so a legal long
alias array could livelock and starve the rotation.

- Parked state now carries stable `node_id` + `term_offset`; the next tick
  resumes at the first unchecked normalized term. Term ordering is stable
  for one generation (normalized, de-duplicated, sorted), and a surface
  mutation bumps generation and invalidates the offset.
- `_cluster_terms()` always includes the node name and considers at most
  `CLUSTER_ALIASES_CAP=512` aliases. Thus one malformed/legacy node has a
  hard per-node cap in addition to the global comparison budget.

Regressions:

- `test_auto_cluster_resumes_inside_long_alias_list` — a 30-alias node with
  budget 4 advances offsets `[3, 7, 11, 15]` over four sweeps instead of
  repeating the first terms.
- `test_auto_cluster_caps_alias_terms_per_node` — an alias beyond a
  monkeypatched cap is not considered by auto-cluster.

### P2 — corrupt cursor state is strictly validated and self-heals

`_load_cluster_rotation()` previously coerced arbitrary JSON. A mapping in
`candidate_id` reached SQLite binding and raised forever; a forged high
`node_cursor` plus unrelated `matches` could be trusted into a false merge.

- The state schema is now exact and versioned:
  `version`, `generation`, `cand_cursor`, `candidate_id`, `node_cursor`,
  `node_id`, `term_offset`, `matches`. IDs are null/string only; matches are
  a unique string list of length at most 2; integers reject booleans and
  must be within non-negative SQLite-rowid bounds; field relationships and
  term-offset cap are checked.
- Semantic recovery validates cursor bounds against current table maxima,
  the pending candidate and partial node existence/rowid ordering, and each
  remembered match's existence plus actual current-surface match. Any
  invalidity deletes the key and restarts from a clean state.

Regressions:

- `test_auto_cluster_corrupt_rotation_self_heals_twice` — object-valued
  `candidate_id` no longer reaches SQLite; two consecutive sweeps stay
  healthy.
- `test_auto_cluster_rejects_forged_cursor_and_match_evidence` — impossible
  high node cursor + unrelated existing match is discarded, never merged.

### R4 verification

- Baseline before R4 tests: `tests/test_chain.py` → `67 passed`.
- R4 tests before implementation: `6 failed, 67 deselected`.
- R4 tests after implementation: `6 passed, 67 deselected`.
- Full chain suite: `73 passed`.
- Required combined suite:
  `.venv/bin/python -m pytest tests/test_chain.py tests/test_db_migrate.py -q`
  → `92 passed in 7.83s`.
- `.venv/bin/python -m compileall app -q` → `COMPILE_OK`.

## R5 闭合 (2026-07-21, merge-readiness review P1)

R5 correctly found that R4's `CLUSTER_ALIASES_CAP=512` bounded work by
silently truncating the correctness set. Production `create_node()` and
`merge_aliases()` accept more than 512 aliases, and all other graph
matchers treat every stored alias as a real surface. Auto-cluster could
therefore miss an exact match in alias 513, see a different node's visible
containment match as unique, conditionally merge into the wrong node, then
swallow the target alias collision after the incorrect candidate/backfill
commit.

### Fix

- Removed `CLUSTER_ALIASES_CAP` and every alias slice from the matcher.
  `_cluster_terms()` now normalizes, de-duplicates and stably sorts ALL
  stored aliases plus the node name.
- No product write limit was introduced: every alias accepted by production
  remains visible to auto-cluster, matching `_is_known_entity`,
  `_term_taken_txn`, and mention matching semantics.
- Work remains bounded per tick by the existing
  `CLUSTER_COMPARE_BUDGET`. A node with any number of terms parks at
  `node_id + term_offset` and resumes at the first unchecked term next tick;
  a merge/ambiguity decision still waits for the complete node rotation.
- Rotation schema no longer rejects `term_offset > 513`; it retains the
  non-negative SQLite-integer bound and semantically checks the parked
  offset against the current full term list. Graph-generation changes still
  invalidate the offset and restart from term zero.

This supersedes the R4 defensive-cap paragraph and its former
`test_auto_cluster_caps_alias_terms_per_node` test. The cap was locally
bounded but semantically wrong because the write model had no matching
product limit.

### R5 regressions

All setup uses production graph/candidate entry points.

- `test_auto_cluster_513th_alias_preserves_ambiguity` — node A starts with
  512 unrelated aliases; `merge_aliases(A, candidate)` writes the exact
  match as alias 513; node B's name is a containment match. Before the fix
  auto-cluster returned 1 and merged into B; after the fix it sees both
  nodes and leaves the candidate pending/ambiguous.
- `test_auto_cluster_scans_all_aliases_across_ticks_within_budget` — alias
  521 is eventually found and merged across multiple sweeps with a
  monkeypatched budget of 64; an instrumented matcher asserts every tick
  performs at most 64 comparisons.
- `test_auto_cluster_generation_change_resets_full_alias_progress` — after a
  parked long-alias scan, a production alias mutation bumps generation; the
  next sweep restarts at offset 3 rather than continuing to 7.

### R5 verification

- TDD red: `2 failed, 1 passed, 72 deselected` (the 513-alias wrong merge and
  tail-never-scanned failures reproduced; generation reset already held).
- Targeted green: `3 passed, 72 deselected`.
- Full chain suite: `75 passed`.
- Required combined suite:
  `.venv/bin/python -m pytest tests/test_chain.py tests/test_db_migrate.py -q`
  → `94 passed in 8.97s`.
- `.venv/bin/python -m compileall app -q` → `COMPILE_OK`.
- No migration, backlog, prompt or workflow changes.
