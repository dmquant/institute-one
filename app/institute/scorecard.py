"""Daily hand scorecard: false-complete/stub detection + hourly hand stats.

Ports the legacy ``hand-scorecard.ts`` idea (ROADMAP Phase 2 "Hand weights +
scorecard"): a ``completed`` task row is not necessarily completed WORK — CLIs
sometimes "succeed" with a refusal, a bare echo of the prompt, or placeholder
scaffolding. ``run_once()`` re-reads the day's completed tasks, judges each
output with CHATTER_PATTERNS-style heuristics, upserts per-task verdicts into
``hand_scorecard``, and recomputes hourly ``hand_stats`` windows over ALL
terminal tasks of the day (migrations/0009_hand_weights.sql).

Verdicts (hand_scorecard.verdict):
- ``false_complete`` — exit 0 but no work product: an AI refusal
  (CHATTER_PATTERNS) or the output is an echo of the prompt.
- ``stub`` — something was produced but it is scaffolding: too short
  (< MIN_OUTPUT_CHARS, after the DONE-line exemption) or TODO/placeholder
  markers (PLACEHOLDER_PATTERNS).
- ``ok`` — everything else. False positives are worse than misses (the
  rate_limit.py philosophy), so heuristics stay conservative.

False-positive guards on the refusal probe (REVIEW-B2 M1):
- the DONE+artifacts exemption is checked FIRST — a real deliverable outranks
  any refusal-looking content in the body (reports legitimately quote refusal
  samples);
- quoted spans (「」『』“” and "double quotes") are stripped before probing —
  quoted refusals are evidence, not refusals;
- the probe only looks at the opening REFUSAL_HEAD_CHARS of the (stripped)
  output — genuine refusals are openers, mid-report mentions are analysis;
- an identity claim ("作为AI/as an AI") alone is NOT a refusal: it must
  co-occur with an inability marker in the same sentence.

Repo-specific exemption: the FILE_DELIVERABLE prompt convention asks analysts
to reply with a single ``DONE: <file>`` line — short output + recorded
artifacts is the HAPPY path here, never a stub.

Scheduling: run_once() never raises (errors come back in the summary dict);
called with no date it settles the PREVIOUS SGT day (designed for a 00:05 SGT
daily job — settling "today" at 23:45 would permanently miss end-of-day tasks,
REVIEW-B2 M2). Job registration lives with the main agent — see
PATCH-NOTES-B2.md; this module deliberately does not import or touch
scheduler.py.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import bus, db
from ..config import get_settings
from .prompts import now_sgt

log = logging.getLogger("institute.scorecard")

TERMINAL_STATUSES = ("completed", "failed", "rate_limited", "cancelled", "expired")

# ---- heuristics ------------------------------------------------------------

MIN_OUTPUT_CHARS = 40          # stripped output shorter than this = stub
ECHO_PREFIX = "[echo] "        # the built-in EchoHand reply shape
ECHO_PROBE_CHARS = 200         # this much contiguous prompt inside the output = echo
ECHO_MIN_PROMPT_CHARS = 120    # prompts shorter than this skip the echo probe
                               # (short prompts legitimately reappear in output)
REFUSAL_HEAD_CHARS = 300       # refusal probes only see the opening of the output:
                               # genuine refusals are openers, later mentions are analysis

# "completed" but the content is a refusal / meta-chatter, not work. Probed
# against the quote-stripped HEAD of the output only (see judge_output).
# An identity claim ("作为AI") alone is NOT a refusal — it must co-occur with
# an inability marker in the same sentence (REVIEW-B2 M1).
_IDENTITY = r"(?:作为(?:一个|一名)?\s*(?:AI|人工智能|语言模型)|as an AI\b|as a language model)"
_SAME_SENTENCE = r"[^。.!！?？\n]{0,60}?"
_INABILITY = (
    r"(?:无法|不能|不便|难以|做不到"
    r"|can(?:no|')t\b|cannot\b|unable to\b|not able to\b|won'?t be able to\b)"
)
_REFUSAL_RE = re.compile(
    # identity + inability in one sentence: 作为AI，我无法… / as an AI I cannot…
    rf"{_IDENTITY}{_SAME_SENTENCE}{_INABILITY}"
    # or a direct first-person inability + action verb, no identity needed
    r"|我(?:暂时)?(?:无法|不能)(?:帮助|协助|完成|执行|提供|访问|回答)"
    r"|I(?:’m| am|'m) (?:unable|not able) to\b"
    r"|I can(?:no|')t (?:help|assist|comply|do that|access)",
    re.IGNORECASE)
CHATTER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("refusal", _REFUSAL_RE),
    ("needs_input", re.compile(
        r"请(?:您)?(?:提供|先提供|补充).{0,12}(?:信息|材料|文件|上下文|细节)"
        r"|could you (?:please )?(?:provide|clarify|share)"
        r"|need (?:more|additional) (?:context|information|details)",
        re.IGNORECASE)),
]

# Quoted spans are evidence being cited, not the hand speaking. Strip them
# before probing for refusals. Covers CJK corner brackets, CJK double quotes,
# curly and straight double quotes (bounded length keeps the regex linear).
_QUOTED_SPAN = re.compile(r"「[^」]{0,400}」|『[^』]{0,400}』|“[^”]{0,400}”|\"[^\"\n]{0,400}\"")

# Work was attempted but the deliverable is placeholder scaffolding.
PLACEHOLDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("todo_marker", re.compile(r"\bTODO\b|\bFIXME\b|\bTBD\b|待补充|待填写|此处填写|内容待定", re.IGNORECASE)),
    ("placeholder", re.compile(
        r"\[(placeholder|占位|待定|插入.{0,8})\]"
        r"|<(placeholder|插入.{0,8})>"
        r"|\{\{[^{}]{1,40}\}\}"
        r"|lorem ipsum",
        re.IGNORECASE)),
]

_DONE_LINE = re.compile(r"^\s*DONE:\s*\S+\s*$", re.IGNORECASE | re.MULTILINE)


def judge_output(prompt: str, output: str, artifacts: list[str] | None = None) -> tuple[str, str]:
    """Judge one completed task's output. Returns (verdict, reason).

    Pure and synchronous so tests can probe the heuristics directly.
    Order matters (REVIEW-B2 M1: a real deliverable outranks refusal-looking
    body text): DONE+artifacts exemption > refusal/needs_input (quote-stripped
    head only) > echo (false_complete) > short output > placeholder (stub) > ok.
    """
    text = (output or "").strip()
    if not text:
        return "false_complete", "empty_output"

    # FILE_DELIVERABLE happy path FIRST: a DONE line + the file actually
    # appeared = delivered, no matter what else the body mentions (reports
    # legitimately quote refusal samples / discuss AI disclaimers).
    if _DONE_LINE.search(text) and artifacts:
        return "ok", "done_with_artifacts"

    # Refusal probes see only the quote-stripped opening: quoted refusals are
    # citations; a refusal buried mid-report is analysis, not a refusal.
    head = _QUOTED_SPAN.sub(" ", text)[:REFUSAL_HEAD_CHARS]
    for reason, pat in CHATTER_PATTERNS:
        if pat.search(head):
            return "false_complete", reason

    if text.startswith(ECHO_PREFIX):
        return "false_complete", "echo_reply"
    probe = (prompt or "").strip()[:ECHO_PROBE_CHARS]
    if len(probe) >= ECHO_MIN_PROMPT_CHARS and probe in text:
        return "false_complete", "echo_reply"

    if len(text) < MIN_OUTPUT_CHARS:
        return "stub", "short_output"

    for reason, pat in PLACEHOLDER_PATTERNS:
        if pat.search(text):
            return "stub", reason

    return "ok", ""


# ---- time plumbing ----------------------------------------------------------

def previous_work_date() -> str:
    """Yesterday's SGT calendar date — the day run_once() settles by default."""
    return (now_sgt() - timedelta(days=1)).strftime("%Y-%m-%d")


def _utc_range_for_work_date(date: str) -> tuple[str, str]:
    """[start, end) UTC ISO bounds of one SGT calendar date.

    Same second-precision '+00:00' shape as bus.now_iso(), so string
    comparison in SQL == time comparison.
    """
    tz = ZoneInfo(get_settings().timezone)
    start_local = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


def _hour_window(ts_iso: str) -> str | None:
    """Truncate a UTC ISO timestamp to its hour window start (None if unparseable)."""
    try:
        dt = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return dt.isoformat(timespec="seconds")


def _duration_ms(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        t0 = datetime.fromisoformat(started_at)
        t1 = datetime.fromisoformat(finished_at)
    except (ValueError, TypeError):
        return None
    return max((t1 - t0).total_seconds(), 0.0) * 1000.0


# ---- the daily job ----------------------------------------------------------

async def _score_completed(date: str, start: str, end: str) -> dict[str, int]:
    """Judge every completed task finished inside the window; upsert verdicts."""
    rows = await db.query(
        "SELECT id, hand, prompt, output, artifacts FROM tasks "
        "WHERE status = 'completed' AND hand IS NOT NULL "
        "AND finished_at >= ? AND finished_at < ?",
        (start, end),
    )
    counts = {"ok": 0, "stub": 0, "false_complete": 0}
    now = bus.now_iso()
    for r in rows:
        try:
            artifacts = json.loads(r["artifacts"] or "[]")
        except (ValueError, TypeError):
            artifacts = []
        try:
            verdict, reason = judge_output(r["prompt"] or "", r["output"] or "", artifacts)
        except Exception:  # noqa: BLE001 - one bad row must not sink the sweep
            log.exception("judge_output crashed for task %s", r["id"])
            continue
        counts[verdict] += 1
        # task_id is UNIQUE: a rerun re-judges instead of duplicating
        await db.execute(
            "INSERT INTO hand_scorecard (hand, work_date, task_id, verdict, reason, created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "hand = excluded.hand, work_date = excluded.work_date, "
            "verdict = excluded.verdict, reason = excluded.reason",
            (r["hand"], date, r["id"], verdict, reason, now),
        )
    return counts


async def _aggregate_stats(start: str, end: str) -> int:
    """Recompute hourly hand_stats windows over the day's terminal tasks.

    Grouping runs in Python (a day is a few hundred rows at most) so odd
    timestamp shapes degrade to a skipped row, not a broken sweep. Rows with
    hand IS NULL ("no hand available" rate-limits) belong to no hand and are
    excluded. Windows are overwritten in place — the sweep is idempotent.
    """
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    rows = await db.query(
        f"SELECT hand, status, started_at, finished_at FROM tasks "
        f"WHERE status IN ({placeholders}) AND hand IS NOT NULL "
        f"AND finished_at >= ? AND finished_at < ?",
        (*TERMINAL_STATUSES, start, end),
    )
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        window = _hour_window(r["finished_at"])
        if window is None:
            continue
        b = buckets.setdefault((r["hand"], window), {
            "total": 0, "ok": 0, "failed": 0, "rate_limited": 0, "durations": [],
        })
        b["total"] += 1
        if r["status"] == "completed":
            b["ok"] += 1
        elif r["status"] in ("failed", "expired"):
            b["failed"] += 1
        elif r["status"] == "rate_limited":
            b["rate_limited"] += 1
        d = _duration_ms(r["started_at"], r["finished_at"])
        if d is not None:
            b["durations"].append(d)

    now = bus.now_iso()
    for (hand, window), b in buckets.items():
        n_samples = len(b["durations"])
        avg = (sum(b["durations"]) / n_samples) if n_samples else None
        await db.execute(
            "INSERT INTO hand_stats (hand, window_start, window_hours, tasks_total, tasks_ok, "
            "tasks_failed, tasks_rate_limited, duration_samples, avg_duration_ms, updated_at) "
            "VALUES (?,?,1,?,?,?,?,?,?,?) "
            "ON CONFLICT(hand, window_start, window_hours) DO UPDATE SET "
            "tasks_total = excluded.tasks_total, tasks_ok = excluded.tasks_ok, "
            "tasks_failed = excluded.tasks_failed, tasks_rate_limited = excluded.tasks_rate_limited, "
            "duration_samples = excluded.duration_samples, "
            "avg_duration_ms = excluded.avg_duration_ms, updated_at = excluded.updated_at",
            (hand, window, b["total"], b["ok"], b["failed"], b["rate_limited"], n_samples, avg, now),
        )
    return len(buckets)


async def run_once(date: str | None = None) -> dict[str, Any]:
    """Score one SGT work date. **No date = the PREVIOUS SGT day.**

    Settlement semantics (REVIEW-B2 M2): the daily job runs at 00:05 SGT and
    settles yesterday, whose task set is closed — settling "today" before
    midnight would permanently miss tasks finishing after the job fired. Pass
    an explicit date to (re)settle any historic day; reruns upsert verdicts
    and overwrite stats windows in place (rows for tasks that later left the
    completed/window set are NOT deleted, and each run emits a fresh
    scorecard.completed event — re-running is safe, not strictly idempotent
    in side effects).

    Never raises: scheduler-facing (the PATCH-NOTES-B2 job wraps this in
    @metered as a second belt). Errors come back as {"date", "error"}.
    """
    d = date or previous_work_date()
    try:
        start, end = _utc_range_for_work_date(d)
        verdicts = await _score_completed(d, start, end)
        windows = await _aggregate_stats(start, end)
        summary: dict[str, Any] = {
            "date": d,
            "scanned": sum(verdicts.values()),
            "verdicts": verdicts,
            "stats_windows": windows,
        }
        await bus.emit("scorecard.completed", "scorecard", d, summary)
        return summary
    except Exception as exc:  # noqa: BLE001 - the scheduler must never see a raise
        log.exception("scorecard run_once failed for %s", d)
        return {"date": d, "error": str(exc)[:500]}
