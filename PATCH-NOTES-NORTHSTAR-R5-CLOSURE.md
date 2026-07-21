# North Star R5 closure notes

Date: 2026-07-21 (SGT)

## Outcome

The two R5 point-in-time reviews found 15 protocol defects: 10 P1, 4 P2, and 1 P3. All 15 now have implementation fixes and focused regression or fault-injection coverage. The original review reports remain intact as the discovery record; their appended closure addenda supersede the earlier `REQUEST_CHANGES` verdict for the current worktree.

Code-readiness verdict: **ACCEPT**. The M9 and LOOP cards intentionally remain in `review` until operator acceptance; this note does not move the formal roadmap board.

## Factcheck and chain closure

| R5 finding | Closure | Regression evidence |
|---|---|---|
| Completed bound verification task was discarded and the model called again | Recovery now reconciles `fact_cards.verify_task_id` against the durable task result; terminal tasks settle the card without a second model call. Boot recovery runs after executor orphan recovery. | `test_recovery_settles_completed_bound_task_without_model_call`, `test_recovery_terminal_task_failure_converges_without_model_call`, `test_recovery_does_not_reopen_task_with_live_owner` |
| Reset window exposed the old active verdict | Reset invalidates the old active verdict in the same transition, and read/reuse surfaces require the current generation/status pairing. | `test_reset_window_old_verdict_excluded_from_all_read_surfaces`, `test_reverify_after_reset_updates_active_verdict` |
| Dispute outbox crossed verification generations | Outbox identity and delivery validation now bind to the verification generation/task. Reset supersedes an old pending row; a new generation receives a new row; drain refuses stale generations. Migration 0041 supplies the outbox lease columns without mutating already-applied 0034. | `test_old_pending_dispute_outbox_superseded_after_reset`, `test_new_dispute_generation_not_blocked_by_old_delivered`, `test_same_dispute_generation_reuses_outbox_and_delivers_once` |
| Alias 513 could be hidden from clustering correctness | The matcher no longer truncates a legal alias set. It persists `term_offset` and scans all stable terms across bounded ticks; generation changes reset the accumulated evidence. | `test_auto_cluster_513th_alias_preserves_ambiguity`, `test_auto_cluster_scans_all_aliases_across_ticks_within_budget`, `test_auto_cluster_generation_change_resets_full_alias_progress` |

## Revival, mailbox, operator, and vault closure

| R5 finding | Closure | Regression evidence |
|---|---|---|
| Revival marker could outlive an undriven queued child | Migration 0042 adds reciprocal source/child binding. Booking creates the durable prepared task and both bindings atomically; boot/tick recovery drives that same task id. | `test_marker_then_queued_child_survives_restart_and_runs_same_generation`, `test_lifespan_boot_drives_bound_revival_child_instead_of_failing_it` |
| Completed child before marker could trigger a second model call | Reconciliation discovers the bound canonical child and consumes its terminal result instead of spawning another generation. | `test_completed_canonical_is_reconciled_without_second_generation` |
| Born-terminal overcommit consumed the source | Overcommit leaves the source unbound and eligible for a later bounded attempt. | `test_queue_overcommit_defers_without_binding_or_consuming_source` |
| `task.queued` mirror failure stranded a live child | A durable event row is inserted with the booking transaction and then fanned out without reinsertion via `publish_durable`; recovery remains task-driven. | `test_task_queued_emit_failure_never_strands_or_duplicates_child`, `test_publish_durable_fans_out_without_inserting_duplicate` |
| Unrelated `IntegrityError` was treated as a lineage winner | The collision branch proves and validates an actual canonical task before consuming the source; otherwise it releases the lease under the normal bounded failure policy. | `test_unrelated_integrity_error_without_canonical_never_consumes_source` |
| Mailbox marked done before reply/event commit | Task booking is durable and reply insertion, message settlement, thread timestamp, and event outbox insertion commit atomically. A failed reply transaction reuses the already-completed task. | `test_reply_insert_failure_rolls_back_terminal_and_reuses_completed_task`, `test_late_dispatch_worker_cannot_overwrite_task_id_or_reply` |
| Fixed mailbox TTL could reclaim a legitimate long execution | Staleness is derived from the bound task lifecycle/deadline. Recovery keeps the same durable dispatch/task id instead of resubmitting an unproven generation. | `test_boot_recovery_preserves_and_schedules_same_prepared_task` |
| Mailbox dispatch had no retry ceiling | Migration 0043 persists dispatch and reconciliation attempts. Model dispatch is capped at 3 attempts; completed-task settlement is capped at 5 attempts. | `test_dispatch_model_failures_stop_at_attempt_ceiling`, `test_completed_result_settlement_is_itself_bounded` |
| `_inflight` overrode a stale durable lease | SQL/lease CAS is the authority. The process-local set no longer vetoes a database-proven stale dispatch. | `test_sweep_does_not_let_inflight_veto_stale_durable_state` |
| Parameter history could commit without its effect baseline | Parameter history and the effect baseline now commit in one transaction. Legacy missing effects are explicitly backfilled with a marker. | `test_parameter_effect_commits_before_legacy_post_commit_crash`, `test_parameter_effect_insert_failure_rolls_back_change`, `test_legacy_missing_parameter_effect_backfills_with_marker` |
| Vault replace-to-ledger window could open a false conflict card | `VaultWriter.write_note` and the operator's final ledger/file recheck share one coordination lock, closing the replace-to-ledger TOCTOU. | `test_sweep_waits_through_writer_replace_to_ledger_window` |

## Additional stabilization

- Operator event-feed registration now reconciles the bus's actual handler set instead of trusting a stale module boolean. This fixes order-dependent suite failures after handler snapshots are restored (`test_register_is_idempotent_and_repairs_restored_handler_snapshot`).
- Sina response decoding uses `gb18030`, a compatible superset for the upstream Chinese payloads, avoiding the Python 3.14 full-suite codec-order failure.
- Migration 0034 was restored to its originally applied immutable contents; 0041 is the additive bridge for later factcheck outbox lease columns. Fresh installs and the existing live database now take the same schema path.
- The live probe exposed a route-precedence hole: `/api/theses/import-batches` was consumed by `/{thesis_id:path}`. The literal route now precedes the catch-all, reads the real provenance table with stable bounds/order, fails safely on damaged JSON, and redacts local paths plus keyed or inline credential shapes.

## Verification

```text
.venv/bin/python -m pytest tests -q
1159 passed, 2 skipped

.venv/bin/python -m compileall app -q
OK

cd frontend && npm run test
2 files, 16 tests passed

cd frontend && npm run build
OK

cd obsidian-plugin && npm run build
OK
```

The two intentional skips are the opt-in real-network market smoke test (`INSTITUTE_NET_TESTS=1`) and real bge-m3 calibration (`INSTITUTE_CALIBRATION_REAL=1`).

## Post-closure independent review

An independent Claude Code review covered the complete 16k-line working-tree batch and reported no CRITICAL or HIGH defects. The review's two MEDIUM decisions and the actionable low-cost gaps were resolved before the final full-suite run:

| Independent finding | Closure | Regression evidence |
|---|---|---|
| A missing 0028 migration-ledger row could replay the historical `tasks` rebuild and drop columns added by 0039–0043 | `db.migrate()` now proves the already-completed 0028 table contract, preserves later columns/data, replays only idempotent indexes, and fails closed on drift. | `test_replay_lost_0028_ledger_preserves_later_task_columns_and_data` |
| Mixed property period families used lexical ordering (`2026-07 < 2026-Q02`) | Periods are parsed into one chronological end-date key across year/quarter/month/day precision; invalid formats are rejected. | `test_mixed_quarter_and_month_periods_use_chronological_horizon`, `test_period_parser_supports_declared_formats_and_rejects_unknowns` |
| Boot recovery could re-drive durable model work while maintenance was paused | Executor and mailbox recovery expose no-drive modes that preserve/requeue the same durable ids while doing pure cleanup; gated scheduler jobs resume them later. | paused-lifespan recovery/resume coverage in `tests/test_restart_recovery.py` |
| Active prompt overrides were cold after restart | Lifespan pre-warms the override cache before any recovery prompt can be rendered. | boot cache coverage in `tests/test_restart_recovery.py` |
| Multi-agent domain callers could bypass the API cap; partial spawn returned an opaque 500 | The domain enforces unique rosters of at most five, and partial spawn persists/returns a reconnectable failed run with the ids already created. | domain cap and API partial-spawn tests in `tests/test_multi_agent.py` |
| API bodies silently accepted typos and the SPA consumed obsolete multi-agent response fields | Mutation models forbid extras; the SPA discriminates completed `outputs/run_id` from pending `task_ids/run_id`. | API strict-body tests; frontend 16-test suite and production build |
| Python 3.14 could intermittently fail the test-only `gbk` alias lookup | Sina fixtures use canonical `gb18030`, matching production decoding without changing payload bytes. | `tests/test_market_fetchers.py`; 647-test ordered prefix and final full suite |

Remaining review notes are non-blocking performance/fairness debt: staged-property retry fairness, shorter lock-held filesystem work, and empty workspace cleanup after a lost claim. Three reported lows were disproved against the current tree (factcheck scans are bounded, exhausted revival sources are excluded, and forecast calendar boundaries already have direct coverage).

## Live reconciliation

- Pre-restart queue: `running_now=0`.
- Consistent SQLite backup: `/private/tmp/institute-one-pre-r5.qHGejK/institute.db`; backup integrity `ok`, 836 task rows, 34 pre-R5 migration rows.
- Final submission restart backup: `/private/tmp/institute-one-pre-92b06d5.p1qj14/institute.db`; 27M, backup integrity `ok`, 1059 task rows, 43 migration rows through 0043.
- Live database integrity after restart: `ok`; migration ledger is continuous through `0043_mailbox_dispatch_protocol.sql`.
- LaunchAgent `com.institute-one.server` was finally restarted to PID 20512 on `127.0.0.1:8100`.
- `/health`, `/api/meta`, `/api/tasks/queue`, `/api/contract`, `/api/cron/health`, `/api/theses?flat=true`, and `/api/theses/import-batches` returned successfully. The two thesis surfaces returned `[]` for the current empty dataset; the contract's four schema cross-checks are all `ok`; queue remained at `running_now=0`; all 24 scheduler jobs are registered with no latest failure.
- Maintenance was restored to `paused=false`. Two naturally scheduled whiteboard Codex tasks then completed with exit code 0 without touching the repository; the queue drained back to `running_now=0` (959 completed) and the service remained healthy.
- No push was made. Formal M9/LOOP acceptance remains an operator decision.
