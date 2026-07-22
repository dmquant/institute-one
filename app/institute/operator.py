"""Operator loop — ROADMAP Phase 6 first slice: action feeds + shadow router.

The institute starts managing itself; THE HUMAN GATE STAYS HUMAN. Iron rules
(ROADMAP Phase 6 / proposal §8.2 invariant), enforced here and locked by
tests/test_operator.py:

1. **Shadow mode first.** Every disposition this module records carries
   ``shadow=1``: it is a logged SUGGESTION. ``route_actions()`` never mutates
   the action row it routed, never touches prompts, schedules, workflows,
   weights, feature switches or vault content, and there is no code path in
   this module that writes ``shadow=0``.
2. **prompt/schedule territory never auto-acts.** Suggestions for kinds whose
   remediation lives in prompt/schedule space (``HUMAN_PINNED_KINDS``), and
   suggestions whose proposed disposition IS a prompt/schedule change
   (``HUMAN_PINNED_DISPOSITIONS``), are flagged ``human_pinned`` — a hard pin
   that must survive the eventual end of shadow mode.
3. **Proposals are approved by a human in the web UI only** — the explicit
   ``POST /api/operator/dispositions/{id}/approve`` endpoint (app/api/
   operator.py). Never via vault frontmatter, never via MCP. Approval itself
   is a bookkeeping act (it resolves the action row); it does not execute any
   system change either.
4. **The 0.7 confidence floor is a consumption gate, not a label**
   (REVIEW-C4 M1). Two layers (F3 P3-1): the ``low_confidence`` flag is a
   PROPOSAL-TIME cache (telemetry/UI only, frozen at routing time), while the
   gate itself lives in the approve endpoint, which re-checks the stored
   confidence against the LIVE floor at consumption time — raising the floor
   retroactively blocks older unflagged proposals; missing confidence never
   passes. The human path for below-floor suggestions is a manual PATCH on
   the action. The floor is live-tunable: admin_state key
   ``operator:confidence_floor`` (``get_confidence_floor()``;
   missing/corrupt row -> 0.7).

Feeds (bus handlers, registered by ``register()`` during app startup):
- ``factcheck.disputed``  -> disputed_fact   (payload shape is treated as
  untrusted — see ``FACTCHECK_DISPUTED_EVENT``)
- ``task.failed``         -> failed_run      (skips the router's own
  classification tasks, or a failing hand would breed actions forever)
- ``workflow.failed``     -> failed_run
- ``scorecard.completed`` -> scorecard_anomaly when the false_complete rate
  crosses ``FALSE_COMPLETE_RATE_THRESHOLD`` over >= ``SCORECARD_MIN_SCANNED``
  judged tasks (B2's event, app/institute/scorecard.py)
plus ``sweep_vault_conflicts()``: a periodic sweep (not a bus feed) that turns
vault ledger conflict/drift into vault_conflict actions.

Idempotency: one live (open/in_progress) action per ``ref`` — feeds
check-then-insert and migrations/0018's partial unique index closes the await
race. Handlers never raise (the bus guards too; belt and braces). The
router's propose-once-per-loop claim is likewise DB-backed: migrations/0022's
partial unique index on ``(action_id, proposed_by)`` arbitrates concurrent
same-loop calls (REVIEW-C4 P2 / F3 NIT-3).

``scheduler.py`` mounts the 15-minute fast tick and hourly deep tick with
``gated=True``; this module deliberately does not import the scheduler.

Recipes (Phase 6 L item, minimal reuse loop): a human-
APPROVED disposition can be promoted into a recipe
(``promote_disposition_to_recipe``); ``route_actions`` consults active recipes
before calling a model — a match records the suggestion directly (marked by
``recipe_id``, confidence inherited, ZERO model calls) and it remains
``shadow=1`` behind the same live confidence floor and human approval gate as
every model suggestion. Iron rules 1–4 are unchanged.

Self-improvement chain (M8-008, migrations/0026): observations (windowed
metric snapshots over existing rows) feed deterministic, zero-model-call
proposals (promote/retire a recipe, tune a whitelisted parameter) that surface
as kanban inbox cards; a proposal APPLIES only through the explicit web-UI
human endpoint (``POST /api/operator/proposals/{id}/approve`` — never vault
frontmatter, never MCP, proposal §8.2); every applied change freezes a
before-window effect baseline that ``measure_effects()`` later completes, and
whitelisted parameter changes append ``parameter_history`` rows that can be
rolled back (a rollback is itself a new history row). See the section marker
below.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..vault.writer import (
    _extract_region,
    _has_ownership,
    _read_exact,
    _sha_file,
    _sha_text,
    get_writer,
)
from . import prompts

log = logging.getLogger("institute.operator")

# ---- vocabulary -------------------------------------------------------------

ACTION_KINDS = (
    "vault_conflict", "disputed_fact", "scorecard_anomaly",
    "failed_run", "cron_failure", "other",
)
ACTION_STATUSES = ("open", "in_progress", "done", "dismissed")
LIVE_STATUSES = ("open", "in_progress")

# What the router may propose. Anything else a model replies is recorded as
# 'unparsed' (never trusted downstream).
DISPOSITION_VOCAB = (
    "retry", "dismiss", "escalate", "investigate",
    "rebuild_note", "adjust_prompt", "adjust_schedule",
)

# ROADMAP Phase 6 "0.7 confidence floor" — a CONSUMPTION GATE, not a label
# (REVIEW-C4 M1): below-floor suggestions are stored as shadow telemetry
# (flagged low_confidence) but the approve endpoint refuses to consume them.
# The live value is the admin_state row; this is only the fallback default.
CONFIDENCE_FLOOR_DEFAULT = 0.7
CONFIDENCE_FLOOR_KEY = "operator:confidence_floor"

# Iron rule 2: prompt/schedule-change territory. scorecard_anomaly remediation
# is prompt space (the hand "completed" garbage — you fix the prompt, not the
# row); cron_failure remediation is schedule space. Both are pinned at the
# KIND level: even a high-confidence suggestion stays human_pinned.
HUMAN_PINNED_KINDS = frozenset({"scorecard_anomaly", "cron_failure"})
# ...and pinned at the DISPOSITION level regardless of kind.
HUMAN_PINNED_DISPOSITIONS = frozenset({"adjust_prompt", "adjust_schedule"})

# scorecard anomaly thresholds (module constants, not Settings — B1 §1 idiom)
FALSE_COMPLETE_RATE_THRESHOLD = 0.2
SCORECARD_MIN_SCANNED = 5      # below this a bad rate is noise, not an anomaly

DEFAULT_PRIORITY = {
    "scorecard_anomaly": 3, "cron_failure": 3,
    "vault_conflict": 2, "disputed_fact": 2,
    "failed_run": 1, "other": 1,
}

ROUTER_SOURCE = "operator-router"  # tasks.source for classification calls
ROUTER_TIMEOUT_S = 300

# C1 (fact-check) is being built in parallel; this is the agreed event name.
# If C1's PATCH-NOTES lands a different one, this constant is the one-line fix.
# Defensive by construction: if the event never fires, the handler never runs.
FACTCHECK_DISPUTED_EVENT = "factcheck.disputed"


# ---- actions ----------------------------------------------------------------

# Injection surface (REVIEW-C4 M3 / F3 P2-1): titles and refs come from
# untrusted event payloads and end up inside the router prompt, where the
# parser's protocol regexes are line-anchored. Folding to ONE printable line
# makes such text inert (it cannot contain a line break at all).
_CONTROL_OR_SEP = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _fold_line(text: str, cap: int) -> str:
    """One printable line: control/separator chars become spaces, whitespace
    runs collapse, length capped."""
    return " ".join(_CONTROL_OR_SEP.sub(" ", text).split())[:cap]


async def open_action(
    kind: str, ref: str, title: str, detail: str = "", priority: int | None = None,
) -> dict[str, Any]:
    """Open a kanban action; idempotent per ref: a live (open/in_progress)
    action with the same non-empty ref is returned instead of duplicated.

    Titles are folded to one line on the way in; refs are ``kind:<id>``
    one-liners, so a newline/control character in a ref is an injection
    attempt (F3 P2-1), not a legitimate id — refused outright (feeds swallow
    the ValueError through their own belts).

    Returns ``{"id": ..., "created": bool}``.
    """
    if kind not in ACTION_KINDS:
        raise ValueError(f"unknown action kind {kind!r}")
    if ref and _CONTROL_OR_SEP.search(ref):
        raise ValueError("control characters in action ref")
    if ref:
        live = await db.query_one(
            "SELECT id FROM operator_actions WHERE ref = ? AND status IN (?, ?)",
            (ref, *LIVE_STATUSES),
        )
        if live is not None:
            return {"id": live["id"], "created": False}
    now = bus.now_iso()
    try:
        action_id = await db.insert(
            "INSERT INTO operator_actions (kind, ref, title, detail, status, priority, created_at, updated_at) "
            "VALUES (?,?,?,?,'open',?,?,?)",
            (kind, ref, _fold_line(title, 200), detail[:2000],
             priority if priority is not None else DEFAULT_PRIORITY.get(kind, 1),
             now, now),
        )
    except sqlite3.IntegrityError:
        # lost the check-then-insert race to a concurrent feed — the partial
        # unique index (0018) guarantees a live row for this ref now exists
        live = await db.query_one(
            "SELECT id FROM operator_actions WHERE ref = ? AND status IN (?, ?)",
            (ref, *LIVE_STATUSES),
        )
        if live is None:  # pathological (e.g. CHECK violation) — surface it
            raise
        return {"id": live["id"], "created": False}
    return {"id": action_id, "created": True}


async def resolve_action(action_id: int, resolution: str) -> bool:
    """Human disposition: mark done. Conditional claim — only a live action
    can be resolved, so double-disposal loses cleanly (rowcount 0)."""
    now = bus.now_iso()
    n = await db.execute(
        "UPDATE operator_actions SET status='done', resolution=?, resolved_at=?, updated_at=? "
        "WHERE id=? AND status IN (?, ?)",
        (resolution, now, now, action_id, *LIVE_STATUSES),
    )
    return n > 0


async def dismiss_action(action_id: int, reason: str = "") -> bool:
    """Human disposition: dismiss (won't fix / noise). Conditional claim."""
    now = bus.now_iso()
    n = await db.execute(
        "UPDATE operator_actions SET status='dismissed', resolution=?, resolved_at=?, updated_at=? "
        "WHERE id=? AND status IN (?, ?)",
        (reason or "dismissed", now, now, action_id, *LIVE_STATUSES),
    )
    return n > 0


# ---- feeds (bus handlers — must never raise) --------------------------------

def _payload_str(payload: dict[str, Any], *keys: str, cap: int = 120) -> str:
    """First non-empty string under *keys, folded to one line (F3 P2-1: a
    newline inside an untrusted claim must not survive into a title)."""
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return _fold_line(v, cap)
    return ""


async def _on_factcheck_disputed(event: bus.Event) -> None:
    """C1's disputed-claim event -> disputed_fact action. Payload untrusted."""
    try:
        payload = event.payload if isinstance(event.payload, dict) else {}
        fact_ref = str(event.ref_id or payload.get("fact_id") or payload.get("claim_id") or "").strip()
        claim = _payload_str(payload, "claim", "statement", "summary", "text")
        analyst = _payload_str(payload, "analyst_id", "analyst", cap=40)
        title = f"Disputed fact: {claim}" if claim else f"Disputed fact {fact_ref or '(unknown ref)'}"
        detail_parts = [p for p in (
            f"claim: {claim}" if claim else "",
            f"analyst: {analyst}" if analyst else "",
            f"event: {event.type} #{event.id}",
        ) if p]
        await open_action(
            "disputed_fact",
            f"fact:{fact_ref}" if fact_ref else "",
            title, "\n".join(detail_parts),
        )
    except Exception:  # noqa: BLE001 - feeds must never break the emitter
        log.exception("factcheck.disputed feed failed for event %s", event.id)


async def _on_task_failed(event: bus.Event) -> None:
    """task.failed -> failed_run action (one live action per task id)."""
    try:
        task_id = str(event.ref_id or "").strip()
        if not task_id:
            return
        row = await db.query_one(
            "SELECT hand, requested_hand, source, error FROM tasks WHERE id = ?", (task_id,)
        )
        source = (row or {}).get("source") or ""
        if source == ROUTER_SOURCE:
            # never file actions about our own classification calls: a failing
            # hand would otherwise breed one action per routing attempt
            return
        hand = (row or {}).get("hand") or (row or {}).get("requested_hand") or "?"
        await open_action(
            "failed_run", f"task:{task_id}",
            f"Task failed: {source or 'unknown'}/{hand} ({task_id})",
            ((row or {}).get("error") or "")[:1000],
        )
    except Exception:  # noqa: BLE001
        log.exception("task.failed feed failed for event %s", event.id)


async def _on_workflow_failed(event: bus.Event) -> None:
    """workflow.failed -> failed_run action (one live action per run id)."""
    try:
        run_id = str(event.ref_id or "").strip()
        if not run_id:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        workflow_id = _payload_str(payload, "workflow_id", cap=60) or "unknown"
        await open_action(
            "failed_run", f"workflow:{run_id}",
            f"Workflow failed: {workflow_id} (run {run_id})",
            f"workflow_id: {workflow_id}\nsession_id: {payload.get('session_id') or ''}",
        )
    except Exception:  # noqa: BLE001
        log.exception("workflow.failed feed failed for event %s", event.id)


async def _on_scorecard_completed(event: bus.Event) -> None:
    """scorecard.completed -> scorecard_anomaly when the false_complete rate
    crosses the threshold over a non-noise sample (B2 payload shape:
    {"date", "scanned", "verdicts": {"ok","stub","false_complete"}, ...})."""
    try:
        payload = event.payload if isinstance(event.payload, dict) else {}
        verdicts = payload.get("verdicts")
        if not isinstance(verdicts, dict):
            return
        try:
            scanned = int(payload.get("scanned") or 0)
            false_complete = int(verdicts.get("false_complete") or 0)
        except (TypeError, ValueError):
            return
        if scanned < SCORECARD_MIN_SCANNED:
            return
        rate = false_complete / scanned
        if rate <= FALSE_COMPLETE_RATE_THRESHOLD:
            return
        date = str(payload.get("date") or event.ref_id or "").strip() or "unknown"
        await open_action(
            "scorecard_anomaly", f"scorecard:{date}",
            f"Scorecard anomaly {date}: false_complete rate {rate:.0%}",
            f"scanned: {scanned}\nverdicts: {verdicts}\n"
            f"threshold: {FALSE_COMPLETE_RATE_THRESHOLD:.0%} over >= {SCORECARD_MIN_SCANNED} tasks",
        )
    except Exception:  # noqa: BLE001
        log.exception("scorecard.completed feed failed for event %s", event.id)


def register() -> None:
    """Hook the feeds onto the bus and repair a restored handler snapshot.

    Tests and embedded app lifecycles may temporarily snapshot/restore the
    process-global bus handler list.  A separate boolean cannot describe that
    real registration state: after a restore it can stay true while every
    operator feed is absent.  Reconcile the exact handler tuples instead, so
    repeated calls stay idempotent and a later call repairs missing hooks.
    """
    registrations = (
        (FACTCHECK_DISPUTED_EVENT, _on_factcheck_disputed),
        ("task.failed", _on_task_failed),
        ("workflow.failed", _on_workflow_failed),
        ("scorecard.completed", _on_scorecard_completed),
    )
    added = 0
    for registration in registrations:
        if registration not in bus._handlers:
            bus.on(*registration)
            added += 1
    if added:
        log.info("operator feeds registered (%d repaired/added)", added)


# ---- vault-conflict sweep ----------------------------------------------------

# Flood guard (loop-fix P10a): a mass drift event — a human reorganising the
# vault by hand touches hundreds of notes at once — must not bury the kanban
# under one card per path in a single sweep. New cards per run are capped; the
# surplus is reported (``deferred``) and later sweeps drain it, because live
# cards from earlier runs converge (created=False) without consuming the cap.
SWEEP_MAX_NEW_ACTIONS = 20

# Freshness grace (R3-P2): VaultWriter replaces the file FIRST, then upserts
# the ledger — a drift verdict on bytes that changed within this window is
# plausibly a write in flight, not a human edit, so it defers one sweep. The
# final ledger+disk recheck also holds VaultWriter.coordination_lock (R5 P3),
# the same lock spanning writer os.replace→ledger upsert; even a writer paused
# longer than this grace can no longer create a false conflict card.
SWEEP_FRESH_GRACE_S = 120

# Freshness is a BOUNDED window (R4-P2): an mtime in the future beyond this
# skew is a clock anomaly (drifted clock, restored backup, sync tool), not
# freshness — without the bound, a negative age stays "fresh" until the wall
# clock catches up and a real human edit is deferred for months.
SWEEP_MAX_CLOCK_SKEW_S = 300

# Fairness cursor (R3-P2): the last path the card loop attempted, persisted in
# admin_state so the next sweep starts AFTER it (round-robin over the sorted
# candidate list). Without it, re-closing the head cards each round re-opened
# the same head paths every sweep and starved the tail behind the cap forever.
SWEEP_CURSOR_KEY = "operator:vault_sweep_cursor"

# Attempt bound per sweep: each attempt re-verifies with ONE small on-loop
# file read, so a mass drift where nothing cards (all fresh/converged) must
# not degenerate into an unbounded on-loop scan — the very thing P8a moved
# off the loop. The cursor carries the remainder to later sweeps.
SWEEP_MAX_ATTEMPTS = 100


def _classify_vault_row(root: Path, r: dict[str, Any]) -> str:
    """doctor()'s verdict for ONE ledger row: clean|conflict|missing|drifted.

    Mirrors writer.doctor()'s classification exactly (same check order) using
    the writer's own helpers, because doctor() returns counts without refs.
    Follow-up card unchanged: a doctor(detail=True) in writer.py would remove
    this mirror entirely (noted in PATCH-NOTES-C4.md). Synchronous: the batch
    caller runs under asyncio.to_thread; the card loop re-verifies single
    rows on the loop (one small file read — the writer's own on-loop cost)."""
    path = root / r["path"]
    if not path.exists():
        return "missing"
    if r["state"] == "conflict":
        return "conflict"
    if r["mode"] == "region":
        text = _read_exact(path)
        if text is None:
            return "missing" if not path.exists() else "drifted"
        region = _extract_region(text)
        if region is None or _sha_text(region) != r["sha256"] or not _has_ownership(text):
            return "drifted"
        return "clean"
    if _sha_file(path) != r["sha256"]:
        return "drifted"
    return "clean"


def _classify_vault_rows(
    root: Path, rows: list[dict[str, Any]],
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """ONE synchronous ledger-vs-disk pass: doctor()-shaped counts PLUS the
    per-path non-clean states ([(path, conflict|drifted|missing)]) that
    actions need. The sweep used to call doctor() AND a per-path mirror —
    reading + hashing every vault file twice; the merge halves the IO
    (loop-fix P8a). Runs under asyncio.to_thread so the full-table file read
    + SHA never blocks the event loop."""
    counts = {"total": len(rows), "clean": 0, "conflict": 0, "missing": 0, "drifted": 0}
    nonclean: list[tuple[str, str]] = []
    for r in rows:
        state = _classify_vault_row(root, r)
        counts[state] += 1
        if state != "clean":
            nonclean.append((r["path"], state))
    return counts, nonclean


def _changed_within_grace(root: Path, rel: str, now_ts: float) -> bool:
    """True when the file's bytes changed within the BOUNDED freshness window
    ``-SWEEP_MAX_CLOCK_SKEW_S <= age < SWEEP_FRESH_GRACE_S`` (or the file
    vanished mid-check — the world is moving; let the next sweep judge).

    An mtime further in the future than the allowed skew is a clock anomaly
    (R4-P2): logged and treated as NOT fresh, so a real drift cards now
    instead of deferring until the wall clock catches up."""
    try:
        age = now_ts - (root / rel).stat().st_mtime
    except OSError:
        return True
    if age < -SWEEP_MAX_CLOCK_SKEW_S:
        log.warning("vault sweep: %s mtime is %.0fs in the FUTURE (clock anomaly); "
                    "treating as stale, not fresh", rel, -age)
        return False
    return age < SWEEP_FRESH_GRACE_S


async def sweep_vault_conflicts() -> dict[str, Any]:
    """Turn vault ledger conflict/drift into vault_conflict actions.

    One classification pass over the ledger — run in a worker thread, because
    the full-table file read + SHA is synchronous IO that must not block the
    event loop (loop-fix P8a) — yields both the doctor()-shaped counts and
    the per-path candidates. The card loop then, per candidate:

    - re-verifies against the FRESH ledger row (R3-P2: the thread scan judged
      a snapshot; a VaultWriter write completing mid-scan — disk os.replace
      before ledger upsert — must not card as drift once its upsert lands);
    - defers drift verdicts whose file changed within SWEEP_FRESH_GRACE_S
      (the same race caught DURING the window: plausibly a write in flight;
      a genuine human edit is carded one sweep later);
    - opens at most ``SWEEP_MAX_NEW_ACTIONS`` NEW cards per run (loop-fix
      P10a; the surplus rides back as ``deferred``), starting AFTER the
      persisted round-robin cursor (R3-P2) so head cards being re-closed
      every round can never starve the tail;
    - stays idempotent per ref (a live action for the same path never
      duplicates).

    Missing notes are counted but NOT actioned: rows are truth and the note
    is rebuildable, deletion is a human prerogative, not a conflict. Never
    raises (sweep-facing)."""
    try:
        writer = get_writer()
        if writer.root is None:
            return {"skipped": "vault_disabled"}
        rows = await db.query(
            "SELECT path, sha256, state, mode FROM vault_index ORDER BY path")
        counts, nonclean = await asyncio.to_thread(_classify_vault_rows, writer.root, rows)
        candidates = [(rel, s) for rel, s in nonclean if s in ("conflict", "drifted")]
        cur_row = await db.query_one(
            "SELECT value FROM admin_state WHERE key = ?", (SWEEP_CURSOR_KEY,))
        cursor = _loads(cur_row["value"], None) if cur_row else None
        if isinstance(cursor, str) and candidates:
            start = next((i for i, (rel, _s) in enumerate(candidates) if rel > cursor), 0)
            candidates = candidates[start:] + candidates[:start]
        opened = 0
        deferred = 0
        errors = 0
        attempts = 0
        attempted: str | None = None
        now_ts = datetime.fromisoformat(bus.now_iso()).timestamp()
        try:
            for rel, _scan_state in candidates:
                if opened >= SWEEP_MAX_NEW_ACTIONS or attempts >= SWEEP_MAX_ATTEMPTS:
                    deferred += 1
                    continue
                attempts += 1
                attempted = rel  # the cursor advances past every attempt,
                #                  whatever its outcome — that IS the fairness
                try:
                    # R5 P3: one writer owns this lock from BEFORE disk
                    # replacement through its ledger upsert. Holding the same
                    # lock across our final fresh read/classification/action
                    # closes the last check→card TOCTOU window.
                    async with writer.coordination_lock:
                        fresh = await db.query_one(
                            "SELECT path, sha256, state, mode FROM vault_index WHERE path = ?",
                            (rel,))
                        if fresh is None:
                            continue  # row deleted since the snapshot
                        # This fresh read can hash a whole file/region just like
                        # the initial scan. Keep it off the event loop while the
                        # coordination lock closes the writer TOCTOU window.
                        state = await asyncio.to_thread(
                            _classify_vault_row, writer.root, fresh,
                        )
                        if state not in ("conflict", "drifted"):
                            continue  # the writer caught up; scan verdict is history
                        if state == "drifted" and _changed_within_grace(
                            writer.root, rel, now_ts,
                        ):
                            log.info("vault sweep deferring fresh change on %s (grace)", rel)
                            continue
                        res = await open_action(
                            "vault_conflict", f"vault:{rel}",
                            f"Vault {state}: {rel}",
                            f"state: {state}\npath: {rel}\n"
                            "resolve in Obsidian (merge/delete the sibling or restore the region), "
                            "then re-run the sweep",
                        )
                    opened += 1 if res["created"] else 0
                except Exception:  # noqa: BLE001 - R4-P2: one poison path (e.g. a
                    # control char the writer accepts but the action-ref grammar
                    # refuses) must not sink the round or freeze the cursor;
                    # writer-entry validation is the follow-up root fix
                    errors += 1
                    log.exception("vault sweep failed on %r; quarantined this round", rel)
        finally:
            if attempted is not None:
                # persisted even when a later step blows up (R4-P2): the next
                # round must start AFTER whatever we attempted, or a repeating
                # failure would pin the rotation to the same head forever.
                # Plain upsert on purpose: sweeps are scheduler-serialized
                # (max_instances=1); a raced manual run merely shifts fairness
                await db.execute(
                    "INSERT INTO admin_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (SWEEP_CURSOR_KEY, json.dumps(attempted)))
        if deferred:
            log.warning("vault sweep capped at %s new actions; %s conflict/drift "
                        "paths deferred to later sweeps", SWEEP_MAX_NEW_ACTIONS, deferred)
        return {"doctor": counts, "opened": opened, "deferred": deferred, "errors": errors}
    except Exception as exc:  # noqa: BLE001 - scheduler-facing, never raise
        log.exception("vault conflict sweep failed")
        return {"error": str(exc)[:500]}


# ---- recipes (Phase 6 minimal reuse loop) ------------------------------------
# A recipe = human-approved knowledge made reusable: kind + title keywords →
# disposition. route_actions consults recipes BEFORE the model; a hit records
# the suggestion directly (recipe_id set, confidence inherited, zero model
# calls) — still shadow=1, still behind the live floor + human approval gate.

RECIPE_STATUSES = ("active", "retired")  # code-enforced (0023 adds no CHECK)
RECIPE_MAX_KEYWORDS = 6

# word-ish tokens only: single ASCII letters and bare numbers (instance ids
# like "t-123", dates, counts) never become pattern keywords.
_KEYWORD_TOKEN = re.compile(r"[a-z][a-z0-9]+|[\u4e00-\u9fff]{2,}")


def _title_keywords(title: str, cap: int = RECIPE_MAX_KEYWORDS) -> list[str]:
    """Deduped pattern keywords from an action title, in order, capped."""
    out: list[str] = []
    for tok in _KEYWORD_TOKEN.findall(_fold_line(title, 200).casefold()):
        if tok not in out:
            out.append(tok)
        if len(out) >= cap:
            break
    return out


async def promote_disposition_to_recipe(
    disposition_id: int, *, proposal_id: int | None = None,
) -> dict[str, Any]:
    """Distill a HUMAN-APPROVED disposition into a recipe row.

    The human gate extends to recipe knowledge: only dispositions carrying the
    ``approved`` flag (set exclusively by the web-UI approve endpoint) are
    promotable, and only vocabulary dispositions ('unparsed' never becomes
    reusable knowledge). Pattern = the source action's kind + title keywords;
    confidence is inherited, so recipe-proposed suggestions face the same live
    consumption floor as model ones. Idempotent per source disposition (0023's
    partial unique index): re-promoting returns the existing recipe.

    Every activation freezes a before-window effect baseline (M8-008);
    ``proposal_id`` links it when the promotion applies an approved proposal.

    Returns the recipe row plus ``{"created": bool}``; raises ValueError on
    unknown/unapproved/unpromotable dispositions.
    """
    d = await db.query_one(
        "SELECT * FROM action_dispositions WHERE id = ?", (disposition_id,)
    )
    if d is None:
        raise ValueError(f"unknown disposition {disposition_id}")
    if "approved" not in (d["flags"] or "").split(","):
        raise ValueError(
            f"disposition {disposition_id} is not human-approved; only approved "
            "suggestions can become recipes"
        )
    if d["disposition"] not in DISPOSITION_VOCAB:
        raise ValueError(f"disposition {d['disposition']!r} is not promotable")
    action = await db.query_one(
        "SELECT kind, title FROM operator_actions WHERE id = ?", (d["action_id"],)
    )
    if action is None:  # FK'd; belt and braces
        raise ValueError(f"action {d['action_id']} for disposition {disposition_id} is gone")
    keywords = _title_keywords(action["title"] or "")
    if not keywords:
        # fail closed: with ALL-keywords-match semantics an empty keyword set
        # would match every action of the kind — too broad to be knowledge
        raise ValueError("no usable keywords in the action title; recipe would over-match")
    joined = " ".join(keywords)
    try:
        recipe_id = await db.insert(
            "INSERT INTO recipes "
            "(pattern, disposition, kind, keywords, confidence, source_disposition_id, status, created_at) "
            "VALUES (?,?,?,?,?,?,'active',?)",
            (f"{action['kind']}: {joined}", d["disposition"], action["kind"],
             joined, d["confidence"], disposition_id, bus.now_iso()),
        )
    except sqlite3.IntegrityError:
        existing = await db.query_one(
            "SELECT * FROM recipes WHERE source_disposition_id = ?", (disposition_id,)
        )
        if existing is None:  # pathological — surface it
            raise
        if proposal_id is not None:
            # an approved proposal landing on an already-promoted disposition
            # still gets its effect row (unique per proposal — convergence)
            await _open_effect("recipe", f"recipe:{existing['id']}",
                               recipe_id=existing["id"], proposal_id=proposal_id)
        return {**existing, "created": False}
    row = await db.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    log.info("promoted disposition %s to recipe %s (%s: %s → %s)",
             disposition_id, recipe_id, action["kind"], joined, d["disposition"])
    await _open_effect("recipe", f"recipe:{recipe_id}",
                       recipe_id=recipe_id, proposal_id=proposal_id)
    return {**(row or {}), "created": True}


async def list_recipes(status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return await db.query(
            "SELECT * FROM recipes WHERE status = ? ORDER BY id DESC", (status,)
        )
    return await db.query("SELECT * FROM recipes ORDER BY id DESC")


async def retire_recipe(recipe_id: int, *, proposal_id: int | None = None) -> bool:
    """Conditional claim: only an active recipe retires; repeats lose (False).

    A successful retirement (or one applying an approved proposal) freezes a
    before-window effect baseline so the retirement's impact is measurable."""
    n = await db.execute(
        "UPDATE recipes SET status='retired', retired_at=? WHERE id=? AND status='active'",
        (bus.now_iso(), recipe_id),
    )
    if n > 0 or proposal_id is not None:
        await _open_effect("recipe", f"recipe:{recipe_id}",
                           recipe_id=recipe_id, proposal_id=proposal_id)
    return n > 0


async def _match_recipe(action: dict[str, Any]) -> dict[str, Any] | None:
    """Best active recipe for an action: same kind AND every keyword substring-
    matches the folded casefold title. Ties break to highest confidence
    (missing sorts lowest), then newest."""
    rows = await db.query(
        "SELECT * FROM recipes WHERE status = 'active' AND kind = ?", (action["kind"],)
    )
    if not rows:
        return None
    title = _fold_line(action["title"] or "", 200).casefold()
    best: tuple[tuple[float, int], dict[str, Any]] | None = None
    for r in rows:
        keywords = (r["keywords"] or "").split()
        if not keywords:
            continue  # same over-match guard as promotion (legacy/manual rows)
        if all(k in title for k in keywords):
            key = (r["confidence"] if r["confidence"] is not None else -1.0, r["id"])
            if best is None or key > best[0]:
                best = (key, r)
    return best[1] if best else None


# ---- action router (SHADOW MODE) ---------------------------------------------

# The classification prompt. The reply format uses <angle-bracket> placeholders
# on purpose: the echo hand reflects the whole prompt back, and the parser's
# ^DISPOSITION: ([a-z_]+)$ line regex must never match the template itself.
ROUTER_PROMPT = """\
你是研究所的运维分诊路由器（operator triage router）。当前处于 SHADOW MODE：
你的建议只会被记录下来供人工复核，绝不会被自动执行。

请针对下面这一条 operator action 给出处置建议：

【Action】
- kind: {kind}
- ref: {ref}
- priority: {priority}
- title: {title}
- detail:
{detail}

【可选处置】retry（重试原任务）| dismiss（噪音，关掉）| escalate（升级给人工，紧急）|
investigate（需要深入调查）| rebuild_note（重建 vault 笔记）|
adjust_prompt（需要改提示词——只会记录，人工批准前绝不执行）|
adjust_schedule（需要改调度——只会记录，人工批准前绝不执行）

只回复两行，除此之外不要输出任何内容：
DISPOSITION: <从可选处置中选一个词>
CONFIDENCE: <0 到 1 之间的数字>
"""

_DISPOSITION_LINE = re.compile(r"^DISPOSITION:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)
_CONFIDENCE_LINE = re.compile(
    r"^CONFIDENCE:\s*(1(?:\.0+)?|0(?:\.\d+)?|\.\d+)\s*$", re.IGNORECASE | re.MULTILINE
)


_PROTOCOL_LINE = re.compile(r"^(\s*)(DISPOSITION|CONFIDENCE)(\s*:)", re.MULTILINE | re.IGNORECASE)


def _quote_detail(detail: str) -> str:
    """Neutralize protocol lines inside untrusted action detail (REVIEW-C4 M3).

    Hands that reflect the prompt (echo; some CLIs quote it back) would make a
    'DISPOSITION: …' line inside detail the parser's last match. Prefixing
    every line with '> ' breaks the line-anchored protocol regex for reflected
    content while keeping the detail readable for the model.
    """
    quoted = "\n".join(f"> {line}" for line in detail.splitlines())
    # belt and braces: a protocol line that somehow stays line-anchored gets
    # its colon spaced out so the parser regex cannot match it
    return _PROTOCOL_LINE.sub(r"\1> \2 \3", quoted)


def build_router_prompt(action: dict[str, Any]) -> str:
    """Every untrusted field is neutralized before interpolation (REVIEW-C4
    M3 + F3 P2-1): detail is quoted line by line, title/ref are folded to one
    line here EVEN THOUGH open_action already folds/refuses them — rows that
    predate that hygiene (or arrive by other writers) must not steer the
    parser either. kind is vocabulary-checked, priority is an int."""
    return ROUTER_PROMPT.format(
        kind=action["kind"], ref=_fold_line(action["ref"] or "", 200) or "(none)",
        priority=action["priority"], title=_fold_line(action["title"] or "", 200),
        detail=_quote_detail((action["detail"] or "(no detail)")[:1500]),
    )


def parse_disposition(output: str) -> tuple[str, float | None]:
    """(disposition, confidence) from a router reply; last match wins (real
    replies end with the two lines). Out-of-vocabulary or missing disposition
    degrades to 'unparsed'; out-of-range confidence fails the regex -> None.
    Pure and synchronous so tests can probe it directly."""
    text = output or ""
    d_matches = _DISPOSITION_LINE.findall(text)
    disposition = d_matches[-1].lower() if d_matches else ""
    if disposition not in DISPOSITION_VOCAB:
        disposition = "unparsed"
    c_matches = _CONFIDENCE_LINE.findall(text)
    confidence: float | None = None
    if c_matches:
        try:
            confidence = min(max(float(c_matches[-1]), 0.0), 1.0)
        except ValueError:  # regex-guarded; belt and braces
            confidence = None
    return disposition, confidence


def _floor_from_raw(raw: str | None) -> float:
    """Effective confidence floor for a raw admin_state value; missing /
    corrupt / out-of-range degrade to the built-in default. ONE parser for
    both the consumption gate and set_parameter's raise-only judgment — the
    two must never disagree about what the current floor is."""
    if raw is None:
        return CONFIDENCE_FLOOR_DEFAULT
    try:
        v = float(json.loads(raw))
        return v if 0.0 <= v <= 1.0 else CONFIDENCE_FLOOR_DEFAULT
    except (ValueError, TypeError):
        return CONFIDENCE_FLOOR_DEFAULT


async def get_confidence_floor() -> float:
    """Live floor from admin_state (operator:confidence_floor); 0.7 fallback."""
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (CONFIDENCE_FLOOR_KEY,)
    )
    return _floor_from_raw(row["value"] if row else None)


def disposition_flags(
    kind: str, disposition: str, confidence: float | None,
    floor: float = CONFIDENCE_FLOOR_DEFAULT,
) -> str:
    """Comma-joined marker set for a suggestion (see migrations/0018)."""
    flags: list[str] = []
    if confidence is None or confidence < floor:
        flags.append("low_confidence")
    if kind in HUMAN_PINNED_KINDS or disposition in HUMAN_PINNED_DISPOSITIONS:
        flags.append("human_pinned")
    return ",".join(flags)


ROUTE_ERROR_FLAG = "route_error"  # marks a failure placeholder, not model output


async def _record_route_failure(
    action: dict[str, Any], proposed_by: str, floor: float,
) -> None:
    """Spend the propose-once slot on a FAILED route attempt (loop-fix P2).

    A poison action (router task keeps ending non-completed, or the attempt
    raises in flight) used to leave NO disposition row, so the candidate
    query's NOT EXISTS guard never engaged: the same high-priority row was
    re-selected every tick, burning model quota forever and hogging the cap.

    The placeholder is telemetry, not a suggestion: ``'unparsed'`` with NULL
    confidence can never pass the live consumption floor nor become a recipe,
    it stays ``shadow=1`` (iron rule 1), keeps the ``human_pinned`` marker for
    pinned territory (iron rule 2), and the ``route_error`` flag keeps it
    distinguishable from model garbage. Losing the 0022 propose-once race is
    convergence; anything else logs and swallows (this runs on the failure
    path — a second fault must not sink the tick)."""
    flags = ",".join(filter(None, (
        disposition_flags(action["kind"], "unparsed", None, floor=floor),
        ROUTE_ERROR_FLAG,
    )))
    try:
        await db.insert(
            "INSERT INTO action_dispositions "
            "(action_id, proposed_by, disposition, confidence, shadow, flags, recipe_id, created_at) "
            "VALUES (?,?,'unparsed',NULL,1,?,NULL,?)",  # shadow=1 ALWAYS — iron rule 1
            (action["id"], proposed_by, flags, bus.now_iso()),
        )
    except sqlite3.IntegrityError:
        log.info("action %s already proposed by %s; dropping failure placeholder",
                 action["id"], proposed_by)
    except Exception:  # noqa: BLE001 - the failure path must stay bounded
        log.exception("recording route-failure placeholder for action %s failed", action["id"])


async def route_actions(
    cap: int = 5, *, proposed_by: str = "fast_loop", hand: str | None = None,
) -> dict[str, Any]:
    """Classify up to ``cap`` open actions and record shadow dispositions.

    SHADOW MODE IRON RULE (rule 1 in the module docstring): this function's
    ONLY writes are ``action_dispositions`` rows with ``shadow=1``. It never
    updates ``operator_actions`` (the rows it routes stay byte-identical), and
    it executes no system change of any kind. The single side effect besides
    the disposition rows is the model call itself (one ``tasks`` row per
    action, through executor.submit — the one execution path).

    Recipe short-circuit (Phase 6 minimal loop): each action is first checked
    against active recipes — a match records the suggestion directly
    (``recipe_id`` set, disposition + confidence inherited from the approved
    source, NO model call, no tasks row) and counts as this loop's proposal.
    Only recipe misses go to the model.

    Each loop (fast_loop/deep_loop) proposes at most once per action: already-
    proposed actions are skipped, so a 15-min tick does not re-burn quota on a
    stagnant kanban. Never raises (scheduler-facing; errors in the summary).
    """
    if proposed_by not in ("fast_loop", "deep_loop"):
        raise ValueError("route_actions proposes as fast_loop or deep_loop only")
    try:
        hand_name = hand or get_settings().default_hand
        cap = max(1, min(int(cap), 50))
        # proposal inbox cards (ref 'proposal:<id>', M8-008) are decided by the
        # human proposal endpoints, never classified by the router — routing
        # them would burn quota on the operator's own paperwork (the sibling
        # of the ROUTER_SOURCE guard in the task.failed feed)
        rows = await db.query(
            "SELECT id, kind, ref, title, detail, priority FROM operator_actions a "
            "WHERE a.status = 'open' AND a.ref NOT LIKE 'proposal:%' AND NOT EXISTS ("
            "  SELECT 1 FROM action_dispositions d "
            "  WHERE d.action_id = a.id AND d.proposed_by = ?) "
            "ORDER BY a.priority DESC, a.created_at ASC, a.id ASC LIMIT ?",
            (proposed_by, cap),
        )
        proposed: list[dict[str, Any]] = []
        errors = 0
        recipe_hits = 0
        floor = await get_confidence_floor()
        for a in rows:
            try:
                recipe = await _match_recipe(a)
                recipe_id = None
                if recipe is not None:
                    recipe_id = recipe["id"]
                    disposition, confidence = recipe["disposition"], recipe["confidence"]
                else:
                    task = await executor.submit(
                        hand_name, build_router_prompt(a),
                        source=ROUTER_SOURCE, timeout_s=ROUTER_TIMEOUT_S,
                    )
                    if task.status != "completed":
                        errors += 1
                        log.warning("router task %s for action %s ended %s", task.id, a["id"], task.status)
                        await _record_route_failure(a, proposed_by, floor)
                        continue
                    disposition, confidence = parse_disposition(task.output)
                flags = disposition_flags(a["kind"], disposition, confidence, floor=floor)
                try:
                    disp_id = await db.insert(
                        "INSERT INTO action_dispositions "
                        "(action_id, proposed_by, disposition, confidence, shadow, flags, recipe_id, created_at) "
                        "VALUES (?,?,?,?,1,?,?,?)",  # shadow=1 ALWAYS — iron rule 1
                        (a["id"], proposed_by, disposition, confidence, flags, recipe_id, bus.now_iso()),
                    )
                except sqlite3.IntegrityError:
                    # lost the propose-once race to a concurrent same-loop
                    # call inside our model-call window — 0022's partial
                    # unique index arbitrates; the winner's row stands and
                    # this is convergence, not an error (the feeds' idiom)
                    log.info("action %s already proposed by %s; dropping duplicate", a["id"], proposed_by)
                    continue
                if recipe_id is not None:
                    recipe_hits += 1
                    log.info("action %s routed by recipe %s (%s, zero model calls)",
                             a["id"], recipe_id, disposition)
                proposed.append({
                    "id": disp_id, "action_id": a["id"], "disposition": disposition,
                    "confidence": confidence, "flags": flags, "recipe_id": recipe_id,
                })
            except Exception:  # noqa: BLE001 - one bad action must not sink the tick
                errors += 1
                log.exception("routing action %s failed", a["id"])
                await _record_route_failure(a, proposed_by, floor)
        return {
            "proposed_by": proposed_by, "hand": hand_name, "shadow": True,
            "routed": len(rows), "proposed": proposed, "errors": errors,
            "recipe_hits": recipe_hits,
        }
    except Exception as exc:  # noqa: BLE001 - scheduler-facing, never raise
        log.exception("route_actions failed")
        return {"proposed_by": proposed_by, "error": str(exc)[:500]}


# ---- self-improvement chain (M8-008: observations → proposals → effects) -----
# The full Phase 6 L loop over migrations/0026. Everything below is
# DETERMINISTIC SQL over rows this module already writes — zero model calls,
# zero new execution paths. The human gate stays human: a proposal APPLIES
# only through the explicit web-UI approve endpoint (app/api/operator.py),
# never via vault frontmatter, never via MCP (proposal §8.2 invariant), and
# what it applies goes through the same primitives a human would use
# (promote / retire / set_parameter). Every applied change freezes a
# before-window baseline in operator_effects; measure_effects() completes it
# once the after-window has elapsed. Whitelisted parameter changes append
# parameter_history rows that can be rolled back (a rollback is itself a new
# history row — history is append-only).

OBSERVATION_KINDS = ("action_recurrence", "recipe_performance", "router_quality")
PROPOSAL_KINDS = ("promote_recipe", "retire_recipe", "set_parameter")
PROPOSAL_STATUSES = ("proposed", "approved", "rejected")  # code-enforced + 0026 CHECK
EFFECT_SUBJECT_KINDS = ("recipe", "parameter")

OBSERVE_WINDOW_DAYS = 7
EFFECT_WINDOW_DAYS = 7
PROPOSAL_ACTION_PRIORITY = 2   # inbox cards sit above failed_run noise
# freshness horizon for proposal generation (loop-fix P10b): a snapshot older
# than this describes a window that ended long ago — the daily observe sweep
# missing a whole week means the chain is unhealthy, not that month-old facts
# should keep proposing changes
OBSERVATION_MAX_AGE_DAYS = 7

# proposal-generation thresholds (module constants, not Settings — B1 §1 idiom)
RECURRENCE_MIN_OPENED = 3        # a kind is "recurring" at >= this many opens/window
RECIPE_PROPOSE_MIN_APPROVED = 2  # >= this many same-signature approved dispositions
RECIPE_RETIRE_MIN_HITS = 5       # below this an adoption rate is noise
RECIPE_RETIRE_MAX_ADOPTION = 0.2 # hit adoption <= this proposes retirement
FLOOR_TUNE_MIN_DECIDED = 5       # confident suggestions with a human verdict
FLOOR_TUNE_MIN_AGREEMENT = 0.3   # approval share below this proposes a floor raise
FLOOR_TUNE_STEP = 0.05           # raise-only: tightening the gate is the safe direction
FLOOR_TUNE_CAP = 0.95


def _flags_has(flags: str | None, marker: str) -> bool:
    return marker in (flags or "").split(",")


def _loads(text: str | None, fallback: Any) -> Any:
    """JSON-decode our own stored blobs; corrupt/empty rows degrade to the
    fallback instead of raising (sweep-facing callers must not die on one
    bad row)."""
    try:
        v = json.loads(text) if text else None
    except (ValueError, TypeError):
        return fallback
    return v if v is not None else fallback


def _window_bounds(days: int) -> tuple[str, str]:
    """[since, until) ISO bounds for a trailing window ending now (UTC, the
    bus.now_iso() scale that created_at columns store)."""
    until = bus.now_iso()
    since = (datetime.fromisoformat(until) - timedelta(days=days)).isoformat(timespec="seconds")
    return since, until


# ---- observations -------------------------------------------------------------

async def _router_window_metrics(since: str, until: str) -> dict[str, Any]:
    """Router-suggestion quality facts over [since, until] (bounds INCLUSIVE:
    timestamps are second-resolution, so a half-open upper bound would drop
    rows written in the same second as the sweep; a row landing exactly on a
    baseline boundary may count in both windows — a documented ±1s quirk, the
    daily-cap idiom): volume, recipe share, parse failures, human verdicts,
    and how confident suggestions fared. Same shape at observation, baseline
    and outcome time so effect deltas compare key by key."""
    rows = await db.query(
        "SELECT d.confidence, d.flags, d.recipe_id, d.disposition, a.status AS action_status "
        "FROM action_dispositions d JOIN operator_actions a ON a.id = d.action_id "
        "WHERE d.proposed_by IN ('fast_loop','deep_loop') "
        "AND d.created_at >= ? AND d.created_at <= ?",
        (since, until),
    )
    floor = await get_confidence_floor()
    m: dict[str, Any] = {
        "suggestions": len(rows), "recipe_hits": 0, "model_calls": 0,
        "unparsed": 0, "approved": 0, "confident": 0,
        "confident_approved": 0, "confident_dismissed": 0, "floor": floor,
    }
    for r in rows:
        m["recipe_hits" if r["recipe_id"] is not None else "model_calls"] += 1
        if r["disposition"] == "unparsed":
            m["unparsed"] += 1
        approved = _flags_has(r["flags"], "approved")
        if approved:
            m["approved"] += 1
        if r["confidence"] is not None and r["confidence"] >= floor:
            m["confident"] += 1
            if approved:
                m["confident_approved"] += 1
            if r["action_status"] == "dismissed":
                m["confident_dismissed"] += 1
    return m


async def observe_operator(window_days: int = OBSERVE_WINDOW_DAYS) -> dict[str, Any]:
    """Durable metric snapshots of operator behaviour (M8-008 step 1).

    Three observation kinds over the trailing window: which action kinds keep
    recurring (``action_recurrence``, one row per kind), how each recipe
    performs (``recipe_performance``, hits/adoption, linked via recipe_id),
    and overall router quality (``router_quality``). One row per (kind,
    subject, SGT work date) — re-running the sweep the same day refreshes the
    snapshot in place (0026's unique index arbitrates). Read-only over the
    rows it observes; never raises (sweep-facing)."""
    try:
        window_days = max(1, min(int(window_days), 90))
        since, until = _window_bounds(window_days)
        wd = prompts.work_date()
        now = bus.now_iso()
        written = 0

        async def _snapshot(kind: str, subject: str, metrics: dict[str, Any],
                            recipe_id: int | None = None) -> None:
            nonlocal written
            await db.execute(
                "INSERT INTO operator_observations "
                "(kind, subject, recipe_id, work_date, window_days, metrics, created_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(kind, subject, work_date) DO UPDATE SET "
                "metrics = excluded.metrics, window_days = excluded.window_days, "
                "recipe_id = excluded.recipe_id",
                (kind, subject, recipe_id, wd, window_days,
                 json.dumps(metrics, ensure_ascii=False), now),
            )
            written += 1

        # 1) which action kinds keep recurring
        opened = {r["kind"]: r["n"] for r in await db.query(
            "SELECT kind, COUNT(*) AS n FROM operator_actions "
            "WHERE created_at >= ? GROUP BY kind", (since,))}
        resolved = {r["kind"]: r["n"] for r in await db.query(
            "SELECT kind, COUNT(*) AS n FROM operator_actions "
            "WHERE status = 'done' AND resolved_at >= ? GROUP BY kind", (since,))}
        dismissed = {r["kind"]: r["n"] for r in await db.query(
            "SELECT kind, COUNT(*) AS n FROM operator_actions "
            "WHERE status = 'dismissed' AND resolved_at >= ? GROUP BY kind", (since,))}
        open_now = {r["kind"]: r["n"] for r in await db.query(
            "SELECT kind, COUNT(*) AS n FROM operator_actions "
            "WHERE status = 'open' GROUP BY kind")}
        for kind in sorted(set(opened) | set(resolved) | set(dismissed) | set(open_now)):
            await _snapshot("action_recurrence", kind, {
                "opened": opened.get(kind, 0), "resolved": resolved.get(kind, 0),
                "dismissed": dismissed.get(kind, 0), "open_now": open_now.get(kind, 0),
            })

        # 2) per-recipe hit rate + adoption (retired recipes only while they
        #    still show window activity)
        for r in await db.query("SELECT id, kind, status FROM recipes"):
            hits = await db.query(
                "SELECT flags FROM action_dispositions "
                "WHERE recipe_id = ? AND created_at >= ?", (r["id"], since))
            if not hits and r["status"] != "active":
                continue
            approved = sum(1 for h in hits if _flags_has(h["flags"], "approved"))
            await _snapshot("recipe_performance", f"recipe:{r['id']}", {
                "status": r["status"], "hits": len(hits), "hits_approved": approved,
                "adoption_rate": (approved / len(hits)) if hits else None,
                "kind_actions_opened": opened.get(r["kind"], 0),
            }, recipe_id=r["id"])

        # 3) overall router quality
        await _snapshot("router_quality", "router", await _router_window_metrics(since, until))
        return {"work_date": wd, "window_days": window_days, "observations": written}
    except Exception as exc:  # noqa: BLE001 - sweep-facing, never raise
        log.exception("observe_operator failed")
        return {"error": str(exc)[:500]}


async def list_observations(
    kind: str | None = None, subject: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    where, params = [], []
    for col, val in (("kind", kind), ("subject", subject)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT * FROM operator_observations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = await db.query(sql, [*params, limit])
    for r in rows:
        r["metrics"] = _loads(r["metrics"], {})
    return rows


async def _latest_observations(
    kind: str, max_age_days: int = OBSERVATION_MAX_AGE_DAYS,
) -> list[dict[str, Any]]:
    """Newest snapshot per subject for one observation kind (upserts keep a
    day's id stable, so MAX(id) per subject is the latest work date) — but
    only while that newest snapshot is FRESH (loop-fix P10b): a subject the
    observe sweep stopped covering must not keep feeding proposal generation
    from its frozen last snapshot forever (rejection frees the dedupe ref, so
    one stale observation would re-propose the same change every sweep). A
    subject whose latest snapshot is older than ``max_age_days`` SGT work
    dates drops out entirely."""
    cutoff = (
        datetime.fromisoformat(prompts.work_date()) - timedelta(days=max_age_days)
    ).date().isoformat()
    return await db.query(
        "SELECT * FROM operator_observations o WHERE o.kind = ? AND o.work_date >= ? "
        "AND o.id = ("
        "  SELECT MAX(id) FROM operator_observations "
        "  WHERE kind = o.kind AND subject = o.subject)",
        (kind, cutoff),
    )


# ---- proposals ------------------------------------------------------------------

async def _file_proposal(
    kind: str, title: str, rationale: str, params: dict[str, Any], dedupe_ref: str,
    *, observation_id: int | None = None, recipe_id: int | None = None,
) -> int | None:
    """One proposal row + its kanban inbox card (ref ``proposal:<id>``),
    atomically. 0026's partial unique index keeps ONE live proposal per
    dedupe_ref: losing that race — or re-proposing an undecided change — is
    convergence (None), not an error. Decided proposals free the ref, so a
    rejected change may be re-proposed later (the actions-live-ref idiom)."""
    now = bus.now_iso()
    try:
        async with db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO operator_proposals "
                "(kind, title, rationale, params, dedupe_ref, observation_id, recipe_id, "
                " status, applied, created_at) "
                "VALUES (?,?,?,?,?,?,?,'proposed',0,?)",
                (kind, _fold_line(title, 200), rationale[:2000],
                 json.dumps(params, ensure_ascii=False), dedupe_ref,
                 observation_id, recipe_id, now),
            )
            pid = cur.lastrowid
            cur = await conn.execute(
                "INSERT INTO operator_actions "
                "(kind, ref, title, detail, status, priority, created_at, updated_at) "
                "VALUES ('other',?,?,?,'open',?,?,?)",
                (f"proposal:{pid}", _fold_line(f"Proposal #{pid}: {title}", 200),
                 rationale[:2000], PROPOSAL_ACTION_PRIORITY, now, now),
            )
            await conn.execute(
                "UPDATE operator_proposals SET action_id = ? WHERE id = ?",
                (cur.lastrowid, pid),
            )
        log.info("filed %s proposal %s (%s)", kind, pid, dedupe_ref)
        return pid
    except sqlite3.IntegrityError:
        log.info("%s proposal already pending (dedupe %s); converging", kind, dedupe_ref)
        return None


async def generate_proposals(window_days: int = OBSERVE_WINDOW_DAYS) -> dict[str, Any]:
    """Deterministic improvement proposals from the LATEST observations
    (M8-008 step 2) — zero model calls, and NOTHING applies here: every
    proposal waits in the inbox for the explicit human approve endpoint.

    Three rules:
    - promote_recipe: a recurring action kind (>= RECURRENCE_MIN_OPENED opens
      in the observed window) with >= RECIPE_PROPOSE_MIN_APPROVED unanimously-
      approved same-signature model dispositions and no covering active recipe;
    - retire_recipe: an active recipe whose latest performance snapshot shows
      >= RECIPE_RETIRE_MIN_HITS hits at adoption <= RECIPE_RETIRE_MAX_ADOPTION;
    - set_parameter: confident suggestions keep being rejected by humans
      (< FLOOR_TUNE_MIN_AGREEMENT approval over >= FLOOR_TUNE_MIN_DECIDED
      verdicts) → raise the confidence floor one step (raise-only: tightening
      the human gate is the safe direction to automate).

    Never raises (sweep-facing)."""
    try:
        window_days = max(1, min(int(window_days), 90))
        since, _until = _window_bounds(window_days)
        created: list[int] = []

        # 1) recurring approved fixes → promote_recipe
        recurrence = {o["subject"]: o for o in await _latest_observations("action_recurrence")}
        recurring = {
            k for k, o in recurrence.items()
            if _loads(o["metrics"], {}).get("opened", 0) >= RECURRENCE_MIN_OPENED
        }
        if recurring:
            rows = await db.query(
                "SELECT d.id AS disp_id, d.disposition, d.flags, "
                "a.kind, a.title, a.id AS action_id "
                "FROM action_dispositions d JOIN operator_actions a ON a.id = d.action_id "
                "WHERE d.created_at >= ? AND d.recipe_id IS NULL "
                "AND d.proposed_by IN ('fast_loop','deep_loop') "
                "AND NOT EXISTS (SELECT 1 FROM recipes r WHERE r.source_disposition_id = d.id)",
                (since,),
            )
            groups: dict[tuple[str, str], dict[str, Any]] = {}
            for r in rows:
                if r["kind"] not in recurring or not _flags_has(r["flags"], "approved"):
                    continue
                if r["disposition"] not in DISPOSITION_VOCAB:
                    continue
                sig = " ".join(_title_keywords(r["title"] or ""))
                if not sig:  # promotion would fail closed on empty keywords anyway
                    continue
                g = groups.setdefault(
                    (r["kind"], sig), {"actions": set(), "dispositions": set(), "newest": 0})
                g["actions"].add(r["action_id"])
                g["dispositions"].add(r["disposition"])
                g["newest"] = max(g["newest"], r["disp_id"])
            for (kind, sig), g in sorted(groups.items()):
                if len(g["actions"]) < RECIPE_PROPOSE_MIN_APPROVED:
                    continue
                if len(g["dispositions"]) != 1:
                    continue  # the humans disagreed — that is not knowledge yet
                if await db.query_one(
                    "SELECT 1 AS x FROM recipes WHERE status = 'active' "
                    "AND kind = ? AND keywords = ?", (kind, sig),
                ):
                    continue
                obs = recurrence.get(kind)
                disposition = next(iter(g["dispositions"]))
                pid = await _file_proposal(
                    "promote_recipe",
                    f"Promote recipe: {kind} → {disposition} ({sig})",
                    f"{len(g['actions'])} 个 {kind} action（签名 '{sig}'）在近 {window_days} 天"
                    f"被人工一致批准为 {disposition}；提炼成 recipe 后同型 action 可零模型调用直接建议。",
                    {"disposition_id": g["newest"]},
                    f"promote_recipe:{kind}:{sig}",
                    observation_id=obs["id"] if obs else None,
                )
                if pid:
                    created.append(pid)

        # 2) recipes with hits nobody approves → retire_recipe
        for o in await _latest_observations("recipe_performance"):
            m = _loads(o["metrics"], {})
            hits, rate = m.get("hits", 0), m.get("adoption_rate")
            if hits < RECIPE_RETIRE_MIN_HITS or rate is None or rate > RECIPE_RETIRE_MAX_ADOPTION:
                continue
            recipe = await db.query_one(
                "SELECT * FROM recipes WHERE id = ? AND status = 'active'", (o["recipe_id"],))
            if recipe is None:
                continue
            pid = await _file_proposal(
                "retire_recipe",
                f"Retire recipe #{recipe['id']}: {recipe['pattern']}",
                f"该 recipe 近窗命中 {hits} 次但只有 {m.get('hits_approved', 0)} 次被批准"
                f"（采纳率 {rate:.0%} <= {RECIPE_RETIRE_MAX_ADOPTION:.0%}）——它在产出没人接受的建议。",
                {"recipe_id": recipe["id"]},
                f"retire_recipe:{recipe['id']}",
                observation_id=o["id"], recipe_id=recipe["id"],
            )
            if pid:
                created.append(pid)

        # 3) confident suggestions humans keep rejecting → raise the floor
        rq = await _latest_observations("router_quality")
        if rq:
            m = _loads(rq[0]["metrics"], {})
            decided = m.get("confident_approved", 0) + m.get("confident_dismissed", 0)
            if decided >= FLOOR_TUNE_MIN_DECIDED and \
                    (m.get("confident_approved", 0) / decided) < FLOOR_TUNE_MIN_AGREEMENT:
                floor = await get_confidence_floor()
                new_floor = round(min(FLOOR_TUNE_CAP, floor + FLOOR_TUNE_STEP), 2)
                if new_floor > floor:
                    pid = await _file_proposal(
                        "set_parameter",
                        f"Raise confidence floor {floor:g} → {new_floor:g}",
                        f"高于置信度门槛的建议近窗只有 {m.get('confident_approved', 0)}/{decided} 被批准"
                        f"（< {FLOOR_TUNE_MIN_AGREEMENT:.0%}）；提高消费门槛是收紧人工门的安全方向。",
                        {"key": CONFIDENCE_FLOOR_KEY, "value": new_floor},
                        f"set_parameter:{CONFIDENCE_FLOOR_KEY}",
                        observation_id=rq[0]["id"],
                    )
                    if pid:
                        created.append(pid)
        return {"created": created, "count": len(created)}
    except Exception as exc:  # noqa: BLE001 - sweep-facing, never raise
        log.exception("generate_proposals failed")
        return {"error": str(exc)[:500]}


async def list_proposals(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if status:
        rows = await db.query(
            "SELECT * FROM operator_proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit))
    else:
        rows = await db.query(
            "SELECT * FROM operator_proposals ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["params"] = _loads(r["params"], {})
    return rows


def _int_param(params: dict[str, Any], name: str, proposal_id: int) -> int:
    try:
        return int(params[name])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"proposal {proposal_id} params are missing a usable {name!r}")


async def _check_floor_raise_only(key: str, value: Any, proposal_id: int) -> None:
    """Loop-fix P6b: the floor-tune generation rule is raise-only, but a
    proposal can rot in the inbox while a human moves the live floor —
    approving a stale one must not quietly LOWER the consumption gate.
    Re-checked against the LIVE floor at approve time (equal refuses too:
    not a raise). Guards the PROPOSAL path only; the direct human parameters
    API keeps its deliberate move-either-way semantics.

    FAST 409 ONLY, not the arbiter (R3-P1): this runs before the decide claim,
    so a stale proposal refuses without burning it — but a human PUT can land
    between this check and the write. The BINDING judgment lives inside
    set_parameter(raise_only=True), bound to the byte-CAS reference, so the
    interleaved case fails the CAS instead of lowering the human's value.

    Skipped when a parameter_history row already carries this proposal_id:
    then the change itself already landed and the caller is a bookkeeping-only
    replay (P6a) that must not re-judge — or re-apply — the move."""
    if key != CONFIDENCE_FLOOR_KEY:
        return
    if await db.query_one(
        "SELECT 1 AS x FROM parameter_history WHERE proposal_id = ?", (proposal_id,)
    ):
        return
    floor = await get_confidence_floor()
    if float(value) <= floor:
        raise ValueError(
            f"proposal {proposal_id} would set the confidence floor to "
            f"{float(value):g} but the live floor is already {floor:g}; floor "
            "proposals are raise-only — lower it deliberately via the "
            "parameters API instead")


async def approve_proposal(proposal_id: int, note: str = "") -> dict[str, Any]:
    """THE human gate for proposals (iron rule 3's sibling — web UI only,
    never vault frontmatter, never MCP).

    Conditional-claims proposed→approved (a rival approve/reject loses
    cleanly with rowcount 0), then applies the change through the SAME
    primitives a human would use — promote_disposition_to_recipe (idempotent),
    retire_recipe (conditional claim), set_parameter (byte-CAS + history) —
    freezes the effect baseline (one per proposal, 0026's unique index) and
    resolves the inbox card (conditional; a human who already closed it wins).
    Params are validated BEFORE the claim so a malformed proposal cannot burn
    it. If an apply step fails after the claim, the proposal stays queryable
    as approved-with-applied=0 — and this endpoint may be called again to
    REPLAY the apply (loop-fix P6a): every apply primitive is idempotent or
    a conditional claim, so at-least-once is safe, and ``applied=1`` (itself
    a conditional claim) closes the replay window. Stale ``set_parameter``
    floor proposals additionally re-check direction against the LIVE floor
    (loop-fix P6b): floor proposals are raise-only, so one that would lower
    (or merely equal) the current gate refuses BEFORE the claim.

    Raises ValueError on unknown/already-decided proposals or invalid params.
    """
    p = await db.query_one("SELECT * FROM operator_proposals WHERE id = ?", (proposal_id,))
    if p is None:
        raise ValueError(f"unknown proposal {proposal_id}")
    params = _loads(p["params"], None)
    if not isinstance(params, dict):
        raise ValueError(f"proposal {proposal_id} params are corrupt")
    if p["kind"] == "promote_recipe":
        disposition_id = _int_param(params, "disposition_id", proposal_id)
    elif p["kind"] == "retire_recipe":
        recipe_id = _int_param(params, "recipe_id", proposal_id)
    elif p["kind"] == "set_parameter":
        key = str(params.get("key") or "")
        value = _validate_parameter(key, params.get("value"))
        await _check_floor_raise_only(key, value, proposal_id)
    else:
        raise ValueError(f"proposal {proposal_id} kind {p['kind']!r} is not applicable")

    now = bus.now_iso()
    n = await db.execute(
        "UPDATE operator_proposals SET status='approved', decided_at=?, decided_note=? "
        "WHERE id=? AND status='proposed'",
        (now, note[:1000], proposal_id),
    )
    if n == 0:
        # loop-fix P6a: a lost claim is terminal UNLESS the proposal is the
        # stuck approved-with-applied=0 shape (a prior apply failed after its
        # claim) — that one replays the apply below instead of being refused
        # forever. Re-read: p predates the claim attempt, so a rival decider
        # may have moved the row since.
        fresh = await db.query_one(
            "SELECT status, applied FROM operator_proposals WHERE id = ?", (proposal_id,))
        if (
            fresh is not None
            and fresh["status"] == "approved"
            and fresh["applied"] == 1
            and p["kind"] == "set_parameter"
        ):
            # R5 legacy repair: old code could commit parameter history, lose
            # the post-commit effect, then still mark applied=1. A repeated
            # human approve normally loses, but this one broken invariant gets
            # exactly one repair attempt. set_parameter's prior path creates a
            # clearly-marked late_backfill; once present, repeats are 409 again.
            hist = await db.query_one(
                "SELECT * FROM parameter_history WHERE proposal_id = ?",
                (proposal_id,))
            effect = await db.query_one(
                "SELECT 1 AS x FROM operator_effects WHERE proposal_id = ?",
                (proposal_id,))
            if hist is not None and effect is None:
                hist = await set_parameter(
                    key, value, changed_by=f"proposal:{proposal_id}",
                    proposal_id=proposal_id, raise_only=True)
                log.warning(
                    "repaired legacy applied proposal %s missing its effect",
                    proposal_id,
                )
                row = await db.query_one(
                    "SELECT * FROM operator_proposals WHERE id = ?", (proposal_id,))
                return {
                    **(row or {}),
                    "applied_info": {"parameter_history_id": hist["id"]},
                }
        if fresh is None or fresh["status"] != "approved" or fresh["applied"] != 0:
            status = fresh["status"] if fresh else p["status"]
            raise ValueError(f"proposal {proposal_id} is already decided ({status!r})")
        log.info("proposal %s is approved but unapplied; replaying apply", proposal_id)

    applied: dict[str, Any]
    if p["kind"] == "promote_recipe":
        recipe = await promote_disposition_to_recipe(disposition_id, proposal_id=proposal_id)
        applied = {"recipe_id": recipe["id"], "recipe_created": recipe["created"]}
        await db.execute(
            "UPDATE operator_proposals SET recipe_id=? WHERE id=?", (recipe["id"], proposal_id))
    elif p["kind"] == "retire_recipe":
        applied = {"retired": await retire_recipe(recipe_id, proposal_id=proposal_id)}
    else:
        # set_parameter owns BOTH replay invariants (R5): it may only return
        # after the canonical history and its effect both exist.
        hist = await set_parameter(
            key, value, changed_by=f"proposal:{proposal_id}", proposal_id=proposal_id,
            raise_only=True)
        applied = {"parameter_history_id": hist["id"]}
    n = await db.execute(
        "UPDATE operator_proposals SET applied=1 WHERE id=? AND applied=0", (proposal_id,))
    if n == 0:
        # a rival replay finished the bookkeeping first (R3-P2): the apply
        # legs above are idempotent/convergent, so this is convergence
        log.info("proposal %s applied flag already set by a rival replay", proposal_id)
    if p["action_id"] is not None:
        await db.execute(
            "UPDATE operator_actions SET status='done', resolution=?, resolved_at=?, updated_at=? "
            "WHERE id=? AND status IN (?, ?)",
            (f"proposal #{proposal_id} approved" + (f" — {note}" if note else ""),
             now, now, p["action_id"], *LIVE_STATUSES),
        )
    log.info("proposal %s approved and applied: %s", proposal_id, applied)
    row = await db.query_one("SELECT * FROM operator_proposals WHERE id = ?", (proposal_id,))
    return {**(row or {}), "applied_info": applied}


async def reject_proposal(proposal_id: int, note: str = "") -> dict[str, Any]:
    """Human rejection: conditional claim proposed→rejected, NOTHING applies,
    the inbox card is dismissed (conditionally). The dedupe_ref frees up, so
    the same change may be re-proposed by a later sweep — rejection is a
    verdict on this instance, not a permanent ban (the actions-ref idiom)."""
    p = await db.query_one("SELECT * FROM operator_proposals WHERE id = ?", (proposal_id,))
    if p is None:
        raise ValueError(f"unknown proposal {proposal_id}")
    now = bus.now_iso()
    n = await db.execute(
        "UPDATE operator_proposals SET status='rejected', decided_at=?, decided_note=? "
        "WHERE id=? AND status='proposed'",
        (now, note[:1000], proposal_id),
    )
    if n == 0:
        raise ValueError(f"proposal {proposal_id} is already decided ({p['status']!r})")
    if p["action_id"] is not None:
        await db.execute(
            "UPDATE operator_actions SET status='dismissed', resolution=?, resolved_at=?, updated_at=? "
            "WHERE id=? AND status IN (?, ?)",
            (f"proposal #{proposal_id} rejected" + (f" — {note}" if note else ""),
             now, now, p["action_id"], *LIVE_STATUSES),
        )
    return await db.query_one(
        "SELECT * FROM operator_proposals WHERE id = ?", (proposal_id,)) or {}


# ---- whitelisted parameters + history -------------------------------------------

def _validate_confidence_floor(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence floor must be a number")
    v = float(value)
    if not 0.0 <= v <= 1.0:
        raise ValueError("confidence floor must be between 0 and 1")
    return v


# The tunable surface. Deliberately a whitelist: the self-improvement chain
# may only touch parameters listed here — prompt/schedule territory stays
# fully human (iron rule 2) and is NOT reachable as a "parameter".
PARAMETER_VALIDATORS: dict[str, Any] = {
    CONFIDENCE_FLOOR_KEY: _validate_confidence_floor,
}
PARAMETER_DEFAULTS: dict[str, Any] = {
    CONFIDENCE_FLOOR_KEY: CONFIDENCE_FLOOR_DEFAULT,
}


def _validate_parameter(key: str, value: Any) -> Any:
    validator = PARAMETER_VALIDATORS.get(key)
    if validator is None:
        raise ValueError(
            f"unknown parameter {key!r} (tunable: {sorted(PARAMETER_VALIDATORS)})")
    return validator(value)


async def get_parameters() -> dict[str, Any]:
    """Current whitelisted-parameter state for the operator UI."""
    out: dict[str, Any] = {}
    for key in sorted(PARAMETER_VALIDATORS):
        row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
        out[key] = {
            "stored": _loads(row["value"], None) if row else None,
            "default": PARAMETER_DEFAULTS.get(key),
            "set": row is not None,
        }
    return out


async def _capture_parameter_effect(
    key: str, *, proposal_id: int | None,
    mode: str = "application", application_at: str | None = None,
) -> dict[str, Any]:
    """Capture the pre-change parameter baseline and its logical timestamp.

    Normal application captures BEFORE the parameter write and the returned
    row is inserted in the SAME transaction as admin_state + history (R5):
    ``baseline_at`` is also the history ``created_at``, so the measurement
    audit has one durable application clock and cannot be lost after commit.

    ``mode='late_backfill'`` is only for legacy history rows committed by old
    code without an effect. It captures CURRENT telemetry, not the historical
    application-time baseline, so the baseline JSON carries an explicit,
    durable marker and baseline_at stays the honest capture time. It must
    never masquerade as the original measurement."""
    since, captured_at = _window_bounds(EFFECT_WINDOW_DAYS)
    baseline = await _router_window_metrics(since, captured_at)
    if mode == "late_backfill":
        baseline["_baseline_capture"] = {
            "mode": "late_backfill",
            "application_at": application_at,
            "captured_at": captured_at,
        }
    return {
        "subject_kind": "parameter",
        "subject_ref": f"param:{key}",
        "recipe_id": None,
        "proposal_id": proposal_id,
        "window_days": EFFECT_WINDOW_DAYS,
        "baseline": json.dumps(baseline, ensure_ascii=False),
        "baseline_at": captured_at,
        "created_at": captured_at,
    }


async def _insert_parameter_effect(conn: Any, effect: dict[str, Any]) -> int:
    """Insert one prepared effect through *conn*; errors MUST propagate.

    Unlike _open_effect() (best-effort bookkeeping for older recipe paths),
    this is a required leg of the parameter application protocol. The caller
    invokes it inside the admin_state + parameter_history transaction, so an
    insertion failure rolls the parameter change back whole."""
    cur = await conn.execute(
        "INSERT INTO operator_effects "
        "(subject_kind, subject_ref, recipe_id, proposal_id, window_days, "
        " baseline, outcome, baseline_at, created_at) "
        "VALUES (?,?,?,?,?,?,NULL,?,?)",
        (
            effect["subject_kind"], effect["subject_ref"], effect["recipe_id"],
            effect["proposal_id"], effect["window_days"], effect["baseline"],
            effect["baseline_at"], effect["created_at"],
        ),
    )
    return cur.lastrowid


def _validate_prior_parameter_history(
    history: dict[str, Any], key: str, new_raw: str, proposal_id: int,
) -> None:
    """A proposal id may only converge on the exact change being requested."""
    if history["key"] != key or history["new_value"] != new_raw:
        raise ValueError(
            f"proposal {proposal_id} already recorded a different parameter "
            f"change ({history['key']!r} -> {history['new_value']!r})")


def _validate_parameter_effect_match(
    effect: dict[str, Any], history: dict[str, Any],
) -> None:
    """The proposal's unique effect must measure THIS parameter change."""
    expected_ref = f"param:{history['key']}"
    if (
        effect["subject_kind"] != "parameter"
        or effect["subject_ref"] != expected_ref
    ):
        raise ValueError(
            f"parameter proposal {history['proposal_id']} has a mismatched "
            f"effect ({effect['subject_kind']!r}, {effect['subject_ref']!r}); "
            f"expected ('parameter', {expected_ref!r})")


async def _ensure_parameter_effect(history: dict[str, Any]) -> dict[str, Any]:
    """Return the proposal's effect, repairing only a LEGACY missing row.

    New writes cannot need this repair: history + effect commit atomically.
    Old rows may predate R5. Their original baseline is unrecoverable, so the
    repair is explicitly marked ``late_backfill`` and starts measurement at
    the honest current capture time. Failure propagates; approve_proposal may
    not mark/return an application as complete until both invariants exist."""
    proposal_id = history.get("proposal_id")
    if proposal_id is None:
        raise ValueError(
            f"parameter history {history.get('id')} has no proposal id; "
            "cannot identify its required effect")
    existing = await db.query_one(
        "SELECT * FROM operator_effects WHERE proposal_id = ?", (proposal_id,))
    if existing is not None:
        _validate_parameter_effect_match(existing, history)
        return existing

    effect = await _capture_parameter_effect(
        history["key"], proposal_id=proposal_id, mode="late_backfill",
        application_at=history["created_at"],
    )
    async with db.transaction() as conn:
        # A rival repair may have landed while the baseline was captured.
        cur = await conn.execute(
            "SELECT * FROM operator_effects WHERE proposal_id = ?", (proposal_id,))
        winner = await cur.fetchone()
        await cur.close()
        if winner is not None:
            winner = dict(winner)
            _validate_parameter_effect_match(winner, history)
            return winner
        await _insert_parameter_effect(conn, effect)
    repaired = await db.query_one(
        "SELECT * FROM operator_effects WHERE proposal_id = ?", (proposal_id,))
    if repaired is None:  # pathological — never certify a half-invariant
        raise RuntimeError(
            f"parameter proposal {proposal_id} effect backfill did not persist")
    _validate_parameter_effect_match(repaired, history)
    log.warning(
        "backfilled missing parameter effect for legacy proposal %s; "
        "baseline captured late at %s (application was %s)",
        proposal_id, effect["baseline_at"], history["created_at"],
    )
    return repaired


async def set_parameter(
    key: str, value: Any, *, changed_by: str = "api", proposal_id: int | None = None,
    raise_only: bool = False,
) -> dict[str, Any]:
    """Change a WHITELISTED admin_state parameter, appending the
    parameter_history row (old/new raw JSON) in the same transaction.

    The admin_state write is a byte-level compare-and-swap against the value
    we read (the feature-switches idiom): a concurrent change loses with
    ValueError instead of silently interleaving history. Every change freezes
    a before-window effect baseline (M8-008 step 3). Returns the history row.

    ``raise_only=True`` (R3-P1 loop-fix, the PROPOSAL apply path) makes the
    direction judgment BINDING: the new floor must exceed the exact value the
    CAS references — since the write lands only WHERE value == that reference,
    "judged against X" and "written over X" are the same X, and a human PUT
    interleaving anywhere loses the CAS instead of being silently lowered.
    (approve_proposal's pre-claim check is only a fast 409; THIS is the
    arbiter.) The direct human API keeps move-either-way semantics
    (raise_only=False). Today the whitelist holds only the confidence floor;
    a future tunable needs its own direction policy before proposals may
    touch it.

    R5 protocol: the pre-change effect baseline is captured before the write,
    then operator_effects + parameter_history + admin_state commit in ONE
    transaction. Every replay (fast prior, in-lock prior, unique-index loser)
    validates both history AND effect before returning; only a legacy missing
    effect is repaired, with a durable late_backfill marker."""
    norm = _validate_parameter(key, value)
    new_raw = json.dumps(norm)
    if proposal_id is not None:
        prior = await db.query_one(
            "SELECT * FROM parameter_history WHERE proposal_id = ?", (proposal_id,))
        if prior is not None:
            _validate_prior_parameter_history(prior, key, new_raw, proposal_id)
            await _ensure_parameter_effect(prior)
            return prior

    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (key,))
    old_raw = row["value"] if row else None
    effect = await _capture_parameter_effect(key, proposal_id=proposal_id)
    now = effect["baseline_at"]
    prior_without_effect: dict[str, Any] | None = None
    try:
        async with db.transaction() as conn:
            if proposal_id is not None:
                # per-proposal idempotency INSIDE the write transaction
                # (R3-P2): a raced replay converges on the winner's history
                # row instead of appending a no-op duplicate (rollback rows
                # never carry proposal_id). db.transaction() holds the
                # process-wide write lock, so this read is serialized against
                # rival writers; 0037's partial unique index is the DB-level
                # backstop for any writer outside that lock.
                cur = await conn.execute(
                    "SELECT * FROM parameter_history WHERE proposal_id = ?",
                    (proposal_id,))
                prior = await cur.fetchone()
                await cur.close()
                if prior is not None:
                    prior = dict(prior)
                    _validate_prior_parameter_history(
                        prior, key, new_raw, proposal_id)
                    cur = await conn.execute(
                        "SELECT * FROM operator_effects WHERE proposal_id = ?",
                        (proposal_id,))
                    has_effect = await cur.fetchone()
                    await cur.close()
                    if has_effect is not None:
                        _validate_parameter_effect_match(
                            dict(has_effect), prior)
                        log.info(
                            "parameter change for proposal %s already recorded "
                            "with effect (history %s); converging",
                            proposal_id, prior["id"],
                        )
                        return prior
                    prior_without_effect = prior

            if prior_without_effect is None:
                if raise_only and key == CONFIDENCE_FLOOR_KEY:
                    # judged against the SAME old_raw the CAS below references —
                    # a rival change between the judgment and the write fails the
                    # CAS instead of landing a stale (lower) value (R3-P1)
                    current = _floor_from_raw(old_raw)
                    if float(norm) <= current:
                        raise ValueError(
                            f"confidence floor is raise-only on the proposal path: "
                            f"{float(norm):g} <= current {current:g} — the floor moved "
                            "since this proposal was judged; lower it deliberately via "
                            "the parameters API instead")
                if row is None:
                    cur = await conn.execute(
                        "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, ?)",
                        (key, new_raw))
                else:
                    cur = await conn.execute(
                        "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
                        (new_raw, key, old_raw))
                if cur.rowcount == 0:
                    raise ValueError(
                        f"parameter {key!r} changed concurrently — reload and retry")
                cur = await conn.execute(
                    "INSERT INTO parameter_history "
                    "(key, old_value, new_value, changed_by, proposal_id, rollback_of, created_at) "
                    "VALUES (?,?,?,?,?,NULL,?)",
                    (key, old_raw, new_raw, changed_by, proposal_id, now))
                history_id = cur.lastrowid
                # Required protocol leg: any error rolls back admin + history.
                await _insert_parameter_effect(conn, effect)
    except sqlite3.IntegrityError:
        # 0037's partial unique index fired: a rival writer recorded this
        # proposal's change between our in-transaction lookup and our insert
        # (unreachable through db.transaction()'s write lock, but the DB is
        # the arbiter of last resort) — the transaction rolled back whole;
        # converge on the winner's row (the feeds' idiom).
        if proposal_id is None:
            raise
        winner = await db.query_one(
            "SELECT * FROM parameter_history WHERE proposal_id = ?", (proposal_id,))
        if winner is None:  # pathological — surface it
            raise
        _validate_prior_parameter_history(winner, key, new_raw, proposal_id)
        await _ensure_parameter_effect(winner)
        log.info("parameter change for proposal %s raced; converging on history %s",
                 proposal_id, winner["id"])
        return winner

    if prior_without_effect is not None:
        await _ensure_parameter_effect(prior_without_effect)
        return prior_without_effect

    log.info("parameter %s: %s -> %s (%s)", key, old_raw, new_raw, changed_by)
    return await db.query_one(
        "SELECT * FROM parameter_history WHERE id = ?", (history_id,)) or {}


async def rollback_parameter(history_id: int) -> dict[str, Any]:
    """Revert one parameter change (M8-008 step 4).

    Two conditional claims land in ONE transaction: ``rolled_back_at`` on the
    original row (a change reverts exactly once) and a byte-CAS on admin_state
    requiring the CURRENT value to still be this change's new_value — if a
    later change moved the key, roll THAT one back first. The revert itself is
    appended as a NEW history row (``changed_by='rollback:<id>'``,
    ``rollback_of`` set), so history stays append-only. Returns the new row;
    raises ValueError (unknown / already rolled back / superseded)."""
    h = await db.query_one("SELECT * FROM parameter_history WHERE id = ?", (history_id,))
    if h is None:
        raise ValueError(f"unknown parameter history {history_id}")
    if h["key"] not in PARAMETER_VALIDATORS:
        raise ValueError(f"parameter {h['key']!r} is not tunable")
    effect = await _capture_parameter_effect(h["key"], proposal_id=None)
    now = effect["baseline_at"]
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE parameter_history SET rolled_back_at = ? "
            "WHERE id = ? AND rolled_back_at IS NULL",
            (now, history_id))
        if cur.rowcount == 0:
            raise ValueError(f"parameter history {history_id} is already rolled back")
        if h["new_value"] is None:  # the change unset the key; revert = restore old
            cur = await conn.execute(
                "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, ?)",
                (h["key"], h["old_value"]))
        elif h["old_value"] is None:  # first-ever set; revert = unset (built-in default)
            cur = await conn.execute(
                "DELETE FROM admin_state WHERE key = ? AND value = ?",
                (h["key"], h["new_value"]))
        else:
            cur = await conn.execute(
                "UPDATE admin_state SET value = ? WHERE key = ? AND value = ?",
                (h["old_value"], h["key"], h["new_value"]))
        if cur.rowcount == 0:
            # raising rolls the rolled_back_at claim back too — all or nothing
            raise ValueError(
                f"parameter {h['key']!r} has changed since history {history_id}; "
                "roll back the newer change first")
        cur = await conn.execute(
            "INSERT INTO parameter_history "
            "(key, old_value, new_value, changed_by, proposal_id, rollback_of, created_at) "
            "VALUES (?,?,?,?,NULL,?,?)",
            (h["key"], h["new_value"], h["old_value"], f"rollback:{history_id}",
             history_id, now))
        new_id = cur.lastrowid
        # Same R5 protocol as set_parameter: a rollback history row and its
        # pre-change effect baseline are one atomic unit.
        await _insert_parameter_effect(conn, effect)
    log.info("parameter %s rolled back to %s (history %s)", h["key"], h["old_value"], history_id)
    return await db.query_one("SELECT * FROM parameter_history WHERE id = ?", (new_id,)) or {}


async def list_parameter_history(key: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if key:
        return await db.query(
            "SELECT * FROM parameter_history WHERE key = ? ORDER BY id DESC LIMIT ?",
            (key, limit))
    return await db.query("SELECT * FROM parameter_history ORDER BY id DESC LIMIT ?", (limit,))


# ---- effect measurement -----------------------------------------------------------

async def _recipe_effect_metrics(recipe_id: int, since: str, until: str) -> dict[str, Any]:
    """Window facts for one recipe: how often its action kind occurred, how
    often the recipe answered (hits vs model suggestions), how its suggestions
    were received. Same shape at baseline and outcome time. Bounds INCLUSIVE
    for the same second-resolution reason as _router_window_metrics."""
    r = await db.query_one("SELECT kind FROM recipes WHERE id = ?", (recipe_id,))
    kind = (r or {}).get("kind") or ""
    opened = (await db.query_one(
        "SELECT COUNT(*) AS n FROM operator_actions "
        "WHERE kind = ? AND created_at >= ? AND created_at <= ?",
        (kind, since, until)))["n"]
    hits = await db.query(
        "SELECT flags FROM action_dispositions "
        "WHERE recipe_id = ? AND created_at >= ? AND created_at <= ?",
        (recipe_id, since, until))
    model = (await db.query_one(
        "SELECT COUNT(*) AS n FROM action_dispositions d "
        "JOIN operator_actions a ON a.id = d.action_id "
        "WHERE a.kind = ? AND d.recipe_id IS NULL "
        "AND d.proposed_by IN ('fast_loop','deep_loop') "
        "AND d.created_at >= ? AND d.created_at <= ?",
        (kind, since, until)))["n"]
    approved = sum(1 for h in hits if _flags_has(h["flags"], "approved"))
    return {
        "actions_opened": opened, "recipe_hits": len(hits),
        "hits_approved": approved, "model_suggestions": model,
    }


async def _open_effect(
    subject_kind: str, subject_ref: str, *,
    recipe_id: int | None = None, proposal_id: int | None = None,
    window_days: int = EFFECT_WINDOW_DAYS,
) -> None:
    """Freeze the BEFORE window for a just-applied change. Bookkeeping must
    never break the act it measures: an existing per-proposal row is
    convergence (0026's unique index), anything else logs and swallows."""
    try:
        since, until = _window_bounds(window_days)
        if subject_kind == "recipe":
            baseline = await _recipe_effect_metrics(int(recipe_id), since, until)
        else:
            baseline = await _router_window_metrics(since, until)
        await db.insert(
            "INSERT INTO operator_effects "
            "(subject_kind, subject_ref, recipe_id, proposal_id, window_days, "
            " baseline, outcome, baseline_at, created_at) "
            "VALUES (?,?,?,?,?,?,NULL,?,?)",
            (subject_kind, subject_ref, recipe_id, proposal_id, window_days,
             json.dumps(baseline, ensure_ascii=False), until, until),
        )
    except sqlite3.IntegrityError:
        log.info("effect row for proposal %s already exists; converging", proposal_id)
    except Exception:  # noqa: BLE001 - measurement must not break the change
        log.exception("opening effect baseline for %s failed", subject_ref)


async def measure_effects() -> dict[str, Any]:
    """Complete due effect rows (M8-008 step 3): outcome = the AFTER window
    ([baseline_at, baseline_at + window_days)) in the same metric shape, plus
    numeric deltas (e.g. a negative ``model_suggestions`` delta = model calls
    avoided after a recipe landed). The UPDATE is a conditional claim on
    ``outcome IS NULL`` — each row is measured exactly once, repeats lose.
    Never raises (sweep-facing)."""
    try:
        now_dt = datetime.fromisoformat(bus.now_iso())
        measured, pending = 0, 0
        for e in await db.query(
            "SELECT * FROM operator_effects WHERE outcome IS NULL ORDER BY id"
        ):
            due = datetime.fromisoformat(e["baseline_at"]) + timedelta(days=e["window_days"])
            if now_dt < due:
                pending += 1
                continue
            since, until = e["baseline_at"], due.isoformat(timespec="seconds")
            if e["subject_kind"] == "recipe":
                after = await _recipe_effect_metrics(e["recipe_id"], since, until)
            else:
                after = await _router_window_metrics(since, until)
            baseline = _loads(e["baseline"], {})
            deltas = {
                k: round(after[k] - baseline[k], 6)
                for k, v in after.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and isinstance(baseline.get(k), (int, float))
                and not isinstance(baseline.get(k), bool)
            }
            measured += await db.execute(
                "UPDATE operator_effects SET outcome=?, measured_at=? "
                "WHERE id=? AND outcome IS NULL",
                (json.dumps({**after, "deltas": deltas}, ensure_ascii=False),
                 bus.now_iso(), e["id"]),
            )
        return {"measured": measured, "pending": pending}
    except Exception as exc:  # noqa: BLE001 - sweep-facing, never raise
        log.exception("measure_effects failed")
        return {"error": str(exc)[:500]}


async def list_effects(subject_kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if subject_kind:
        rows = await db.query(
            "SELECT * FROM operator_effects WHERE subject_kind = ? ORDER BY id DESC LIMIT ?",
            (subject_kind, limit))
    else:
        rows = await db.query("SELECT * FROM operator_effects ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["baseline"] = _loads(r["baseline"], {})
        r["outcome"] = _loads(r["outcome"], None) if r["outcome"] else None
    return rows
