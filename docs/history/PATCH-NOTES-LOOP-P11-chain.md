# LOOP-P11 (chain items a/b/c) — low-risk bounded-autonomy patches

Date: 2026-07-20
Package: `roadmap/loop-fix-backlog.md` P11a/b/c（chain 部分；d–h 属 paper_book/
scheduler/mailbox，归其他执行者）
Files: `app/institute/chain.py`, `tests/test_chain.py`

## Per-item determination

### P11a — staged 断言加 attempts，N 次转 skipped：已由 R2 P1-3 覆盖，本轮零改动

Verified in the current tree, not re-implemented:

- `migrations/0030_chain_properties.sql` already carries
  `chain_property_staging.attempts INTEGER NOT NULL DEFAULT 0` (added by the
  R2 round — the table itself is new in 0030 and 0030 has never been applied
  anywhere, so no separate migration exists or is needed).
- `_apply_staged_properties` already spends one attempt per unknown-entity
  miss via a single atomic UPDATE and flips to terminal `skipped` at
  `STAGING_UNKNOWN_ATTEMPTS` (24); deterministic refusals skip outright;
  transient errors stay `pending` without spending attempts.
- Existing regressions:
  `test_staged_property_unknown_entity_skipped_after_bounded_attempts`,
  `test_staged_property_survives_promotion_racing_the_skip_check`
  (see PATCH-NOTES-CHAIN-PROPS.md, "R2 P1-3").

### P11b — _auto_promote 每 tick 上限：真缺口，已修

The promote query (cited as chain.py:1568-1571) selected EVERY pending
candidate at/over the threshold; each promotion is a transaction plus
vault-export fan-out, so a large backlog monopolized one tick. The query now
takes `LIMIT AUTO_PROMOTE_BATCH` (20, `ORDER BY mention_count DESC`); the
remainder drains across sweeps. Regression (written first, red):
`test_auto_promote_bounded_per_tick` — cap forced to 1, two eligible
candidates promote one per tick across two ticks.

### P11c — artifact 读取先钳制（512KB）：真缺口，已修

`_read_text` (serving `_artifact_from_event`, cited as chain.py:1066-1107)
read session-workspace report files unbounded; a runaway artifact flooded
the INSTR backstop scan and the extraction text assembly (the prompt's
6000-char cap applied only later). It now reads at most `ARTIFACT_READ_CAP`
(512 × 1024) bytes — binary read + UTF-8 decode with `errors="replace"`, so
a clamp-boundary-split character degrades to U+FFFD, which no matcher
depends on. Whiteboard text comes from bounded DB rows and the
analyst-daily task fallback from `tasks.output` (executor-capped at 200KB),
so the file path was the only unbounded source. Regression (written first,
red): `test_artifact_read_clamped_to_cap` — bytes beyond the clamp never
enter the assembled artifact text.

## Boundary compliance

- No new migration (P11a's `attempts` already in 0030; P4's counter uses the
  existing `admin_state` table).
- Status transitions remain conditional claims; timestamps via
  `bus.now_iso()`; no prompt strings touched; no new dependencies; only
  `app/institute/chain.py` and `tests/test_chain.py` modified across
  P4/P8b/P11.

## Verification (one run covers P4 / P8b / P11-chain)

- `tests/test_chain.py`: `63 passed` (58 pre-existing + 5 new loop-fix
  regressions)
- `.venv/bin/python -m pytest tests/test_chain.py tests/test_operator.py
  tests/test_db_migrate.py -q`: `140 passed, 4 deselected` — the four
  deselected `test_router_failure_placeholder*` cases are another
  executor's in-flight test-first work for operator P2 (red with or without
  this change, no chain imports); every pre-existing operator test passes.
- `.venv/bin/python -m compileall app -q`: passed
