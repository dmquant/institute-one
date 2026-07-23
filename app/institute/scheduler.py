"""APScheduler wiring — every periodic loop in one place.

No persistent jobstore: jobs are pure kickers over durable DB state, so a
restart loses nothing. Domain modules are imported lazily inside each job so a
broken/missing module degrades that one job instead of breaking boot.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .. import bus, db
from ..config import get_settings
from .prompts import now_sgt, work_date

log = logging.getLogger("institute.scheduler")

_scheduler: AsyncIOScheduler | None = None
# Publicly tracked scheduler executions. The metered wrapper owns this set, so
# shutdown never needs to inspect APScheduler's private executor/future fields.
_inflight_job_tasks: set[asyncio.Task[Any]] = set()

RATE_LIMIT_REVIVAL_LIMIT = 3
# Candidate-scan bound (P11g): never pull the whole rate_limited backlog into
# memory. The window is not anchored to the head: a persisted keyset cursor
# (R3 P1) advances it across firings and wraps at the tail, so rows the Python
# side still has to skip (cooling hand, unresolvable hand) can never starve
# eligible rows sorted behind them.
RATE_LIMIT_REVIVAL_SCAN_LIMIT = 50
RATE_LIMIT_REVIVAL_CURSOR_KEY = "rate_limit_revival_cursor"
RATE_LIMIT_REVIVAL_MARKER = "[rate-limit-revival:claimed]"
# R5: 0042's immutable source.revival_task_id <-> child.revived_from_task_id
# binding is the arbitration authority. 0039 lease fields remain compatible
# schema only; the marker is display/legacy metadata written after completion.
RATE_LIMIT_REVIVAL_MAX_ATTEMPTS = 5
JANITOR_DELETE_LIMIT = 5000
RESEARCH_TREE_BOOKED_PREFIX = "research_tree_booked:"


# ---- maintenance switch ----------------------------------------------------
#
# admin_state is the operator-config surface. Two keys matter to metered():
# 'maintenance' (global pause of every gated job) and 'feature_switches'
# (per-job kill switches, key convention 'job:<name>', consumed below).
#
# Both rows are re-read on EVERY job firing, so they sit behind a short
# process-local TTL cache: a firing burst (24 jobs, several per minute) would
# otherwise pay two serialized SQLite reads each time. Every write path —
# set_maintenance() below and the feature-switches CAS PUT in
# app/api/operator.py — invalidates explicitly, so a committed flip is
# visible immediately; the TTL only bounds staleness for out-of-band edits
# (manual sqlite3), where a few seconds is harmless.
_ADMIN_STATE_CACHE_TTL_S = 5.0
_admin_state_cache: dict[str, tuple[float, str | None]] = {}


async def _admin_state_value(key: str) -> str | None:
    """Raw admin_state value for ``key``, cached for ~5s. The raw string is
    cached (not the parsed payload) so the fail-open JSON handling in the
    callers below stays exactly where it was."""
    now = time.monotonic()
    hit = _admin_state_cache.get(key)
    if hit is not None and now - hit[0] < _ADMIN_STATE_CACHE_TTL_S:
        return hit[1]
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    value = row["value"] if row is not None else None
    _admin_state_cache[key] = (now, value)
    return value


def invalidate_admin_state_cache(key: str | None = None) -> None:
    """Drop cached admin_state rows so a committed write is seen at once
    (``None`` = every cached row)."""
    if key is None:
        _admin_state_cache.clear()
    else:
        _admin_state_cache.pop(key, None)


async def get_maintenance() -> bool:
    value = await _admin_state_value("maintenance")
    if value is None:
        return False
    try:
        return bool(json.loads(value).get("paused", False))
    except Exception:  # noqa: BLE001 - corrupt state means not paused
        return False


async def set_maintenance(paused: bool) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES ('maintenance', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"paused": paused}),),
    )
    invalidate_admin_state_cache("maintenance")
    log.info("maintenance %s", "paused" if paused else "resumed")


async def job_switch_enabled(name: str) -> bool:
    """Per-job feature switch (M8-006 enforcement): admin_state key
    'feature_switches', switch name convention ``job:<name>``.

    A missing row, missing key, or corrupt value all mean ENABLED — switches
    are explicit opt-out, so the default is open and pre-switch deployments
    keep running unchanged (same fail-open posture as get_maintenance).
    Both value shapes are accepted: the versioned envelope the CAS PUT writes
    ({"version": N, "switches": {...}}) and the legacy flat {name: bool} map.
    """
    value = await _admin_state_value("feature_switches")
    if value is None:
        return True
    try:
        raw = json.loads(value)
    except Exception:  # noqa: BLE001 - corrupt state means enabled
        return True
    if not isinstance(raw, dict):
        return True
    switches = raw.get("switches") if isinstance(raw.get("switches"), dict) else raw
    return bool(switches.get(f"job:{name}", True))


async def _record_metric(
    name: str, fired_at: str, *, duration_ms: int = 0, ok: bool = True,
    error: str | None = None, skipped: bool = False,
) -> None:
    """One cron_metrics row per firing (GET /api/cron/health aggregates them).

    Metrics are observability, never control flow: a failed write is logged
    and swallowed so it can't break the job or the never-raise guarantee.

    skipped=True reuses the 0008 skipped_by_maintenance column for BOTH skip
    causes (maintenance pause and feature switch): every consumer (cron_health,
    doctor, SPA) already treats that flag as "did not execute — neither ok nor
    failed", which is exactly the semantics a switch skip needs; a new column
    would force the same rule into every aggregation for zero behavior gain.
    The cause is disambiguated per-row via ``error`` (switch skips carry a
    'skipped by feature switch' marker; ok=1 keeps them out of last_error).
    """
    try:
        await db.execute(
            "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, error, skipped_by_maintenance) "
            "VALUES (?,?,?,?,?,?)",
            (name, fired_at, duration_ms, int(ok), error, int(skipped)),
        )
    except Exception:  # noqa: BLE001 - metrics must never break a job
        log.exception("cron metric write failed for job %s", name)


def _error_summary(exc: BaseException, cap: int = 500) -> str:
    return f"{type(exc).__name__}: {exc}"[:cap]


def metered(name: str, *, gated: bool = False) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[None]]]:
    """Wrap a scheduler job: logs duration, never raises; gated jobs skip under
    maintenance; EVERY job (gated or not) skips while its feature switch
    ``job:<name>`` is off — a missing switch means enabled. Every firing
    writes one cron_metrics row (skips included)."""
    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[None]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> None:
            task = asyncio.current_task()
            if task is not None:
                _inflight_job_tasks.add(task)
            fired_at = bus.now_iso()
            try:
                if gated and await get_maintenance():
                    log.debug("job %s skipped (maintenance paused)", name)
                    await _record_metric(name, fired_at, skipped=True)
                    return
                if not await job_switch_enabled(name):
                    log.debug("job %s skipped (feature switch job:%s off)", name, name)
                    await _record_metric(
                        name, fired_at, skipped=True,
                        error=f"skipped by feature switch job:{name}=false",
                    )
                    return
                t0 = time.monotonic()
                try:
                    await fn(*args, **kwargs)
                except Exception as exc:
                    await _record_metric(
                        name, fired_at, duration_ms=int((time.monotonic() - t0) * 1000),
                        ok=False, error=_error_summary(exc),
                    )
                    raise
                dt = time.monotonic() - t0
                log.log(logging.INFO if dt >= 1.0 else logging.DEBUG, "job %s finished in %.1fs", name, dt)
                await _record_metric(name, fired_at, duration_ms=int(dt * 1000), ok=True)
            except Exception:  # noqa: BLE001 - scheduler jobs must never raise
                log.exception("job %s failed", name)
            finally:
                if task is not None:
                    _inflight_job_tasks.discard(task)
        wrapper.job_name = name  # type: ignore[attr-defined] - introspection for tests/ops
        wrapper.gated = gated  # type: ignore[attr-defined]
        return wrapper
    return deco


# ---- jobs (domain modules imported lazily inside) --------------------------
#
# Gating semantics: gated=True for every job that submits NEW model calls —
# briefing/daily/analyst-dailies open the day's runs, whiteboard-kickoff opens
# boards, whiteboard-tick claims the next card (+ handoff), mailbox-sweep
# re-drives orphaned dispatches, research-tick claims queue items,
# research-tree-tick claims BFS explore nodes (executor.submit),
# factcheck-tick runs extraction/verification tasks, chain-tick runs entity
# extraction, operator-fast-route/operator-deep-route classify actions through
# executor.submit, committee opens the weekly debate run, rate-limit-revival
# respawns terminal calls. In-flight work still drains under maintenance:
# card/dispatch/workflow drivers are plain asyncio tasks outside the scheduler.
# Ungated jobs — janitor, hand-scorecard,
# market-refresh, operator-vault-sweep, paper-opener, paper-mtm — never spend
# model quota (cleanup, task QA over terminal rows, market data fetching,
# vault conflict sweeps, paper-book ledger DB reads/writes) and keep running
# while paused.

@metered("briefing", gated=True)
async def _briefing_job() -> None:
    from . import daily
    await daily.run_briefing()


@metered("daily-report", gated=True)
async def _daily_job() -> None:
    from . import daily
    await daily.run_daily()


@metered("analyst-dailies", gated=True)
async def _analyst_dailies_job() -> None:
    from . import analyst_daily
    await analyst_daily.run_all()


@metered("whiteboard-kickoff", gated=True)
async def _whiteboard_kickoff_job() -> None:
    from . import whiteboard
    await whiteboard.kickoff()


@metered("whiteboard-tick", gated=True)
async def _whiteboard_tick_job() -> None:
    from . import whiteboard
    await whiteboard.tick()


@metered("mailbox-sweep", gated=True)
async def _mailbox_sweep_job() -> None:
    from . import mailbox
    await mailbox.sweep()


@metered("research-tick", gated=True)
async def _research_tick_job() -> None:
    from . import research
    await research.tick()


@metered("research-tree-tick", gated=True)
async def _research_tree_tick_job() -> None:
    from . import research_tree
    await research_tree.tick()


@metered("factcheck-tick", gated=True)
async def _factcheck_tick_job() -> None:
    from . import factcheck
    await factcheck.tick()


@metered("factcheck-outbox")
async def _factcheck_outbox_job() -> None:
    # metered like every other job: a bare string-ref registration kept it
    # out of the registry, so /api/cron/health under-reported by one job
    from . import factcheck
    await factcheck.drain_dispute_outbox()


@metered("chain-tick", gated=True)
async def _chain_tick_job() -> None:
    from . import chain
    await chain.tick()


@metered("memory-compact", gated=True)
async def _memory_compact_job() -> None:
    from . import memory
    await memory.compact_all()


@metered("committee", gated=True)
async def _committee_job() -> None:
    from . import workflows
    await workflows.run_committee_once(source="scheduler")


@metered("operator-fast-route", gated=True)
async def _operator_fast_route_job() -> None:
    from . import operator
    await operator.route_actions(cap=5, proposed_by="fast_loop")  # 便宜 hand = settings.default_hand


@metered("operator-deep-route", gated=True)
async def _operator_deep_route_job() -> None:
    from . import operator
    # 强 hand 的旋钮：hand="claude"（或主代理属意的强 hand）；不传则同 default_hand
    await operator.route_actions(cap=10, proposed_by="deep_loop")


@metered("operator-selfimprove")
async def _operator_selfimprove_job() -> None:
    # M8-008 daily self-improvement sweep: observe -> propose -> measure.
    # Zero model calls (deterministic derivation), so never gated; proposals
    # it opens still pass through the human approve gate before applying.
    from . import operator
    await operator.observe_operator()
    await operator.generate_proposals()
    await operator.measure_effects()


@metered("operator-vault-sweep")
async def _operator_vault_sweep_job() -> None:
    from . import operator
    await operator.sweep_vault_conflicts()


@metered("paper-opener")
async def _paper_opener_job() -> None:
    from . import paper_book
    await paper_book.opener_tick()


@metered("paper-mtm")
async def _paper_mtm_job() -> None:
    from . import paper_book
    await paper_book.mark_to_market()


@metered("portfolio-proposer")
async def _portfolio_proposer_job() -> None:
    # pure DB reads + PIT marks -> proposals; zero model calls, never gated
    from . import portfolios
    await portfolios.sunday_proposer_job()


@metered("hand-scorecard")
async def _scorecard_job() -> None:
    from . import scorecard
    await scorecard.run_once()   # no-arg = settle the previous SGT day (closed set)


@metered("market-refresh")
async def _market_refresh_job() -> None:
    from . import market_fetchers
    await market_fetchers.refresh_all(limit=get_settings().market_refresh_limit)


def _revival_error(error: str | None) -> str:
    base = (error or "").rstrip()
    return f"{base}\n{RATE_LIMIT_REVIVAL_MARKER}" if base else RATE_LIMIT_REVIVAL_MARKER


async def _revival_cursor() -> tuple[str, str] | None:
    """Persisted keyset position of the candidate scan (None = head of order).

    One admin_state row (no migration); missing/corrupt state degrades to a
    head scan — the same fail-open posture as get_maintenance().
    """
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (RATE_LIMIT_REVIVAL_CURSOR_KEY,)
    )
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
        ts, task_id = data["ts"], data["id"]
        if isinstance(ts, str) and isinstance(task_id, str):
            return ts, task_id
    except Exception:  # noqa: BLE001 - corrupt cursor means scan from the head
        pass
    return None


async def _set_revival_cursor(cursor: tuple[str, str] | None) -> None:
    if cursor is None:
        await db.execute(
            "DELETE FROM admin_state WHERE key = ?", (RATE_LIMIT_REVIVAL_CURSOR_KEY,)
        )
        return
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (RATE_LIMIT_REVIVAL_CURSOR_KEY, json.dumps({"ts": cursor[0], "id": cursor[1]})),
    )


def _revival_key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["finished_at"] or row["created_at"], row["id"])


async def _mark_revival_consumed(
    source: dict[str, Any],
    child: dict[str, Any],
) -> bool:
    """Consume a source only when its reciprocal canonical child completed.

    The marker is compatibility/display metadata, never arbitration. The
    immutable 0042 binding + child status are re-checked in SQL so a stale
    reconciliation read cannot consume the wrong source.
    """
    n = await db.execute(
        "UPDATE tasks SET "
        "error = CASE WHEN instr(COALESCE(error, ''), ?) = 0 THEN ? ELSE error END, "
        "revival_lease_id = NULL, revival_leased_at = NULL "
        "WHERE id = ? AND status = 'rate_limited' AND revival_task_id = ? "
        "AND EXISTS (SELECT 1 FROM tasks canonical "
        "            WHERE canonical.id = ? "
        "            AND canonical.revived_from_task_id = tasks.id "
        "            AND canonical.status = 'completed')",
        (
            RATE_LIMIT_REVIVAL_MARKER,
            _revival_error(source["error"]),
            source["id"],
            child["id"],
            child["id"],
        ),
    )
    return n > 0


async def _reconcile_bound_revival(
    source_id: str,
    executor: Any,
    registry: Any,
) -> bool:
    """Reconcile/drive one immutable canonical child.

    Returns True only when this firing attached a model driver, so the
    per-firing revival cap continues to count potential model work rather than
    cheap completion reconciliation.
    """
    pair = await executor.get_canonical_respawn(source_id)
    if pair is None:
        log.error("revival source %s has an invalid canonical binding", source_id)
        return False
    source, child = pair
    status = child["status"]
    if status == "completed":
        await _mark_revival_consumed(source, child)
        return False
    if status in ("queued", "running"):
        hand = child["requested_hand"]
        if hand and registry.cooling_until(hand) is not None:
            return False
        return await executor.drive_prepared(child["id"])
    if status in ("failed", "rate_limited", "expired", "overcommitted"):
        requeued = await executor.requeue_prepared(
            source["id"],
            child["id"],
            max_attempts=RATE_LIMIT_REVIVAL_MAX_ATTEMPTS,
        )
        if requeued:
            return await executor.drive_prepared(child["id"])
        return False
    # Cancellation is an explicit operator terminal: retain the binding and
    # do not call the model again. The source remains visibly unconsumed.
    return False


@metered("rate-limit-revival", gated=True)
async def _rate_limit_revival_job() -> None:
    """Respawn cooled-down terminal tasks, at most three per firing.

    Durable protocol (R5, migration 0042): executor.prepare_respawn_from_row()
    conditionally binds the source and inserts one born-queued canonical child
    in the SAME SQLite transaction. The reciprocal binding remains unique
    after the child becomes terminal, so every crash window reconciles the
    same id: queued/running is driven, completed consumes the source, and
    retryable failures requeue that id under the bounded attempt counter.
    Marker/0039 leases are compatibility metadata, not arbitration.

    The candidate scan is one bounded keyset window per firing (R3 P1):
    the position persists across firings and wraps at the tail, so a head full
    of cooling/unresolvable rows cannot starve eligible rows behind it.
    """
    from ..hands.registry import get_registry
    from ..router import executor

    registry = get_registry()
    revived = 0
    cursor = await _revival_cursor()
    sql = (
        "SELECT t.* FROM tasks t WHERE t.status = 'rate_limited' "
        "AND t.revived_from_task_id IS NULL "
        "AND ("
        "  (t.revival_task_id IS NOT NULL "
        "   AND (instr(COALESCE(t.error, ''), ?) = 0 "
        "        OR NOT EXISTS (SELECT 1 FROM tasks done "
        "                       WHERE done.id = t.revival_task_id "
        "                       AND done.revived_from_task_id = t.id "
        "                       AND done.status = 'completed'))) "
        "  OR "
        "  (t.revival_task_id IS NULL "
        "   AND instr(COALESCE(t.error, ''), ?) = 0 "
        "   AND t.revival_attempts < ? "
        "   AND NOT EXISTS (SELECT 1 FROM tasks live "
        "                   WHERE live.lineage_root = COALESCE(t.lineage_root, t.id) "
        "                   AND live.status IN ('queued','running')))"
        ")"
    )
    params: list[Any] = [
        RATE_LIMIT_REVIVAL_MARKER,
        RATE_LIMIT_REVIVAL_MARKER,
        RATE_LIMIT_REVIVAL_MAX_ATTEMPTS,
    ]
    if cursor is not None:
        sql += " AND (COALESCE(t.finished_at, t.created_at), t.id) > (?, ?)"
        params.extend(cursor)
    sql += " ORDER BY COALESCE(t.finished_at, t.created_at) ASC, t.id ASC LIMIT ?"
    params.append(RATE_LIMIT_REVIVAL_SCAN_LIMIT)
    rows = await db.query(sql, params)

    # Default advance: past the full window, or wrap when the tail was reached
    # (short window). A cap-break overrides with the last row actually
    # processed, so the unscanned remainder of this window comes up next.
    next_cursor = _revival_key(rows[-1]) if len(rows) == RATE_LIMIT_REVIVAL_SCAN_LIMIT else None
    for i, row in enumerate(rows):
        if revived >= RATE_LIMIT_REVIVAL_LIMIT:
            next_cursor = _revival_key(rows[i - 1])
            break
        if row["revival_task_id"]:
            try:
                revived += int(await _reconcile_bound_revival(row["id"], executor, registry))
            except Exception:  # noqa: BLE001 - one corrupt binding cannot block the batch
                log.exception("rate-limit revival reconcile failed for task %s", row["id"])
            continue
        hand = row["hand"] or row["requested_hand"]
        if not hand or registry.cooling_until(hand) is not None:
            continue

        try:
            async with db.transaction() as conn:
                prepared = await executor.prepare_respawn_from_row(
                    conn,
                    row,
                    max_attempts=RATE_LIMIT_REVIVAL_MAX_ATTEMPTS,
                )
        except sqlite3.IntegrityError:
            # Do not guess which constraint fired. Only a real reciprocal
            # canonical winner with the exact expected policy may converge.
            winner = await executor.get_canonical_respawn(row["id"])
            if winner is None:
                log.exception(
                    "rate-limit revival prepare hit IntegrityError with no "
                    "matching canonical winner for task %s",
                    row["id"],
                )
                continue
            try:
                revived += int(
                    await _reconcile_bound_revival(row["id"], executor, registry)
                )
            except Exception:  # noqa: BLE001
                log.exception("rate-limit revival winner reconcile failed for %s", row["id"])
        except Exception:  # noqa: BLE001 - one corrupt row must not block the batch
            log.exception("rate-limit revival prepare failed for task %s", row["id"])
            continue
        if prepared is None:  # queue depth over cap: defer, consume nothing
            continue
        try:
            revived += int(await executor.drive_prepared(prepared.task_id))
        except Exception:  # noqa: BLE001 - durable child is retried next tick/boot
            log.exception("could not drive prepared revival %s", prepared.task_id)
    await _set_revival_cursor(next_cursor)
    if revived:
        log.info("rate-limit revival spawned %d task(s)", revived)


async def _nightly_backup() -> None:
    """Consistent nightly DB snapshot during the 03:00-05:00 SGT window, once
    per date.

    ``VACUUM INTO`` (runtime SQL — never inside a migration file) snapshots
    through SQLite's own transaction machinery, so a concurrent auto-checkpoint
    can no longer corrupt the copy the way it could mid-``shutil.copy2``; the
    old pre-copy ``wal_checkpoint`` is redundant with it. The snapshot lands on
    a temp name and is renamed into place only on success, so a crashed attempt
    never leaves a half-written file that the ``target.exists()`` once-per-date
    guard would mistake for a finished backup.
    """
    if not (3 <= now_sgt().hour < 5):
        return
    settings = get_settings()
    target = settings.backups_dir / f"institute-{work_date()}.db"
    if target.exists():
        return
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / (target.name + ".tmp")
    tmp.unlink(missing_ok=True)  # crashed-attempt residue: VACUUM INTO refuses existing files
    try:
        await db.execute("VACUUM INTO ?", (str(tmp),))
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info("janitor wrote backup %s", target.name)


@metered("janitor")
async def _janitor() -> None:
    settings = get_settings()
    # hard rule 7: the UTC clock is bus.now_iso(), never a raw local clock read
    now_utc = datetime.fromisoformat(bus.now_iso())

    # 1) workflow runs stuck 'running' >6h with no live task under them
    stuck_cutoff = (now_utc - timedelta(hours=6)).isoformat(timespec="seconds")
    for run in await db.query(
        "SELECT id FROM workflow_runs WHERE status = 'running' AND started_at < ?", (stuck_cutoff,)
    ):
        live = await db.query_one(
            "SELECT id FROM tasks WHERE parent_run_id = ? AND status IN ('queued','running') LIMIT 1",
            (run["id"],),
        )
        if live is None:
            n = await db.execute(
                "UPDATE workflow_runs SET status = 'failed', "
                "error = 'expired by janitor: stuck running >6h with no live task', finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (bus.now_iso(), run["id"]),
            )
            if n:
                log.warning("janitor expired stuck workflow run %s", run["id"])

    # 2) stale topic pool entries (>14 days pending)
    pool_cutoff = (now_utc - timedelta(days=14)).isoformat(timespec="seconds")
    n = await db.execute(
        "UPDATE topic_pool SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
        (pool_cutoff,),
    )
    if n:
        log.info("janitor expired %d stale pool topics", n)

    # 3) adhoc workspaces older than 7 days
    def _sweep_adhoc() -> int:
        adhoc = settings.workspaces_dir / "adhoc"
        if not adhoc.is_dir():
            return 0
        horizon = time.time() - 7 * 86400
        removed = 0
        for d in adhoc.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < horizon:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        return removed

    removed = await asyncio.to_thread(_sweep_adhoc)
    if removed:
        log.info("janitor removed %d old adhoc workspaces", removed)

    # 4) cron metrics older than 30 days (the table IS the health window)
    metrics_cutoff = (now_utc - timedelta(days=30)).isoformat(timespec="seconds")
    n = await db.execute(
        "DELETE FROM cron_metrics WHERE id IN "
        "(SELECT id FROM cron_metrics WHERE fired_at < ? ORDER BY id ASC LIMIT ?)",
        (metrics_cutoff, JANITOR_DELETE_LIMIT),
    )
    if n:
        log.info("janitor removed %d old cron metrics", n)

    # 5) events retention. events.id is AUTOINCREMENT and every replay path
    # asks for id > cursor, so deleting an old prefix only creates harmless
    # gaps: ids are never reused and cursors remain monotonic. Vault exporter
    # handlers run synchronously during bus.emit and project authoritative
    # domain rows; they do not rely on historical event replay.
    events_cutoff = (
        now_utc - timedelta(days=max(1, int(settings.events_retention_days)))
    ).isoformat(timespec="seconds")
    n = await db.execute(
        "DELETE FROM events WHERE id IN "
        "(SELECT id FROM events WHERE created_at < ? ORDER BY id ASC LIMIT ?)",
        (events_cutoff, JANITOR_DELETE_LIMIT),
    )
    if n:
        log.info("janitor removed %d expired events", n)

    # 6) SGT-dated research-tree booked counters older than 30 days. Exactly
    # 30 days remains in-window; malformed/admin keys are left untouched.
    counter_cutoff = (now_sgt().date() - timedelta(days=30)).isoformat()
    suffix_pos = len(RESEARCH_TREE_BOOKED_PREFIX) + 1
    n = await db.execute(
        "DELETE FROM admin_state WHERE key IN "
        "(SELECT key FROM admin_state "
        " WHERE substr(key, 1, ?) = ? AND length(key) = ? "
        " AND substr(key, ?, 10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        " AND substr(key, ?, 10) < ? ORDER BY key ASC LIMIT ?)",
        (len(RESEARCH_TREE_BOOKED_PREFIX), RESEARCH_TREE_BOOKED_PREFIX,
         len(RESEARCH_TREE_BOOKED_PREFIX) + 10, suffix_pos, suffix_pos,
         counter_cutoff, JANITOR_DELETE_LIMIT),
    )
    if n:
        log.info("janitor removed %d expired research-tree counters", n)

    # 7) nightly DB backup — isolated so a backup failure can never poison the
    # janitor's other steps or flip its cron metric to failed (P9)
    try:
        await _nightly_backup()
    except Exception:  # noqa: BLE001 - backup is best-effort, the rest must run
        log.exception("janitor backup failed")


# ---- lifecycle --------------------------------------------------------------

def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    sched = AsyncIOScheduler(timezone=settings.timezone)

    def cron(job: Callable, name: str, hhmm: str, day_of_week: str | None = None) -> None:
        hhmm = (hhmm or "").strip()
        if not hhmm:
            log.info("job %s disabled (empty time)", name)
            return
        try:
            h, m = hhmm.split(":")
            trigger = CronTrigger(day_of_week=day_of_week, hour=int(h), minute=int(m),
                                  timezone=settings.timezone)
        except (ValueError, TypeError):
            log.error("job %s: cannot parse time %r; disabled", name, hhmm)
            return
        sched.add_job(job, trigger, id=name, max_instances=1, coalesce=True, misfire_grace_time=3600)

    def every(job: Callable, name: str, *, minutes: int = 0, seconds: int = 0) -> None:
        if minutes <= 0 and seconds <= 0:
            log.info("job %s disabled (non-positive interval)", name)
            return
        sched.add_job(
            job, IntervalTrigger(minutes=minutes, seconds=seconds, timezone=settings.timezone),
            id=name, max_instances=1, coalesce=True, misfire_grace_time=60,
        )

    cron(_briefing_job, "briefing", settings.briefing_time)
    cron(_daily_job, "daily-report", settings.daily_time)
    cron(_analyst_dailies_job, "analyst-dailies", settings.analyst_daily_time)
    cron(_memory_compact_job, "memory-compact", settings.memory_compact_time)
    cron(_scorecard_job, "hand-scorecard", settings.scorecard_time)
    cron(_operator_selfimprove_job, "operator-selfimprove", "00:15")  # after scorecard, before market open
    cron(_committee_job, "committee", settings.committee_time, day_of_week="fri")
    cron(_paper_mtm_job, "paper-mtm", "00:00")   # 00:00 SGT，ROADMAP 原文
    cron(_portfolio_proposer_job, "portfolio-proposer", "22:00", day_of_week="sun")  # ROADMAP Phase 5
    every(_whiteboard_kickoff_job, "whiteboard-kickoff", minutes=settings.whiteboard_kickoff_minutes)
    every(_whiteboard_tick_job, "whiteboard-tick", seconds=settings.whiteboard_tick_seconds)
    every(_mailbox_sweep_job, "mailbox-sweep", seconds=settings.mailbox_sweep_seconds)
    every(_research_tick_job, "research-tick", minutes=settings.research_tick_minutes)
    every(_research_tree_tick_job, "research-tree-tick", minutes=5)
    every(_factcheck_tick_job, "factcheck-tick", minutes=settings.factcheck_tick_minutes)
    every(_factcheck_outbox_job, "factcheck-outbox", minutes=1)
    every(_chain_tick_job, "chain-tick", minutes=60)
    every(_operator_fast_route_job, "operator-fast-route", minutes=15)
    every(_operator_deep_route_job, "operator-deep-route", minutes=60)
    every(_operator_vault_sweep_job, "operator-vault-sweep", minutes=60)
    every(_paper_opener_job, "paper-opener", minutes=5)
    every(_market_refresh_job, "market-refresh", minutes=settings.market_refresh_minutes)
    every(_rate_limit_revival_job, "rate-limit-revival", minutes=5)
    every(_janitor, "janitor", minutes=settings.janitor_minutes)

    sched.start()
    _scheduler = sched
    log.info("scheduler started: %d jobs (tz=%s)", len(sched.get_jobs()), settings.timezone)


def job_registry() -> list[dict[str, Any]]:
    """Public snapshot of every @metered job, sorted by name.

    The definition surface (name, gated) comes from this module's @metered
    functions, so it is complete even with the scheduler stopped (tests,
    doctor). The live surface (registered, trigger, next_run_time) comes from
    APScheduler's get_jobs(): registered=False either means the scheduler is
    not running or the job was disabled by configuration (empty time /
    non-positive interval), and trigger/next_run_time are None then.
    """
    live: dict[str, Any] = {}
    if _scheduler is not None:
        live = {j.id: j for j in _scheduler.get_jobs()}
    metered_fns = {
        fn.job_name: fn
        for fn in globals().values()
        if callable(fn) and hasattr(fn, "job_name")
    }
    snapshot: list[dict[str, Any]] = []
    for name in sorted(metered_fns):
        job = live.get(name)
        next_run = getattr(job, "next_run_time", None)
        snapshot.append({
            "name": name,
            "gated": bool(metered_fns[name].gated),
            "registered": job is not None,
            "trigger": str(job.trigger) if job is not None else None,
            "next_run_time": next_run.isoformat() if next_run is not None else None,
        })
    return snapshot


def inflight_jobs() -> set[asyncio.Task]:
    """Snapshot live metered job tasks without APScheduler internals.

    Call before ``shutdown()`` so the lifespan drain can await cancellation
    before closing SQLite. A copy keeps callers from mutating the registry.
    """
    return {task for task in _inflight_job_tasks if not task.done()}


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")
