"""Curl-back digests — read-only markdown renderings of institute state.

These back the ``GET /api/institute/*.md`` endpoints (ROADMAP Phase 2,
proposal §6.1: "the cleanest, most debuggable context mechanism"): a CLI hand
fetches them via a Step-0 ``curl 127.0.0.1:8100/api/institute/....md`` block
at the top of its prompt, so the exact context a model saw is reproducible
by running the same curl by hand.

Contract for every renderer in this module:

- **Read-only.** SELECTs only — never writes, never migrates. A digest must
  be safe to hit at any moment, including mid-workflow.
- **Clean markdown.** No JSON wrapping, no envelopes; body text (titles,
  summaries, memory compacts) is quoted verbatim, never paraphrased.
- **Bounded.** Each digest is clamped to ``DIGEST_CAP_BYTES`` (8KB) with an
  explicit truncation marker — a digest is a context block, not an archive.
- **Degrade, never 500.** Tables owned by other phases may be missing on an
  older checkout (``analyst_memory`` from the memory card; ``fact_cards`` /
  ``verified_facts`` from Phase 3; ``operator_actions`` is Phase 6). Missing
  table/column ⇒ a stable placeholder document, so prompts can already embed
  the curl today.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from .prompts import work_date

DIGEST_CAP_BYTES = 8192
TRUNCATION_MARK = "\n\n> [digest truncated at 8KB]"

# hard row caps before the byte clamp: keep queries cheap even on a big DB
_MAX_ROWS_PER_SECTION = 40


def clamp_md(text: str, cap_bytes: int = DIGEST_CAP_BYTES) -> str:
    """Byte-aware cap that cuts on UTF-8 boundaries and appends a marker."""
    raw = text.encode("utf-8")
    if len(raw) <= cap_bytes:
        return text
    keep = max(cap_bytes - len(TRUNCATION_MARK.encode("utf-8")), 0)
    # errors="ignore" drops the tail bytes of a code point split by the cut
    return raw[:keep].decode("utf-8", errors="ignore") + TRUNCATION_MARK


def _one_line(text: str | None, cap: int = 200) -> str:
    """Collapse a summary to a single plain line so list items stay list items."""
    flat = " ".join((text or "").split())
    flat = flat.lstrip("#->*• ").strip()
    if len(flat) > cap:
        flat = flat[:cap].rstrip() + "…"
    return flat


def _utc_threshold(days: int) -> str:
    """Cutoff in bus.now_iso() shape so string comparison == time comparison."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ---- recent reports ---------------------------------------------------------

def _run_line(row: dict[str, Any]) -> str:
    variables = json.loads(row["variables"] or "{}")
    date = variables.get("WORK_DATE") or (row["started_at"] or "")[:10]
    title = row["session_title"] or row["workflow_name"] or row["workflow_id"]
    results = json.loads(row["results"] or "[]")
    summary = _one_line(results[-1].get("summary")) if results else ""
    line = f"- {date} · {title}"
    return f"{line} — {summary}" if summary else line


def _research_line(row: dict[str, Any]) -> str:
    date = row["work_date"] or (row["completed_at"] or "")[:10]
    summary = _one_line(row["summary"])
    line = f"- {date} · {row['topic']}"
    return f"{line} — {summary}" if summary else line


async def recent_reports_md(days: int = 7) -> str:
    """Completed workflow reports (briefing/daily/…) + deep-research log, newest first."""
    days = min(max(days, 1), 90)
    threshold = _utc_threshold(days)
    runs = await db.query(
        """SELECT r.id, r.workflow_id, r.variables, r.results, r.started_at,
                  s.title AS session_title, w.name AS workflow_name
           FROM workflow_runs r
           LEFT JOIN sessions s ON s.id = r.session_id
           LEFT JOIN workflows w ON w.id = r.workflow_id
           WHERE r.status = 'completed' AND r.started_at >= ?
           ORDER BY r.started_at DESC LIMIT ?""",
        (threshold, _MAX_ROWS_PER_SECTION),
    )
    research = await db.query(
        """SELECT topic, summary, completed_at, work_date FROM research_log
           WHERE completed_at >= ?
           ORDER BY completed_at DESC, id DESC LIMIT ?""",
        (threshold, _MAX_ROWS_PER_SECTION),
    )

    lines = [
        f"# Recent reports (last {days} days)",
        "",
        f"_generated {work_date()} (SGT) · workflow runs: {len(runs)} · research: {len(research)}_",
        "",
        "## Workflow reports",
        "",
    ]
    lines += [_run_line(r) for r in runs] or [f"_no completed workflow runs in the last {days} days_"]
    lines += ["", "## Research", ""]
    lines += [_research_line(r) for r in research] or [f"_no research completed in the last {days} days_"]
    return clamp_md("\n".join(lines) + "\n")


# ---- analyst memory ---------------------------------------------------------

async def analyst_memory_md(analyst_id: str) -> str:
    """Latest ``analyst_memory`` compact for one analyst, verbatim.

    The table belongs to the Phase 2 analyst-memory card (may not be migrated
    yet on this checkout): missing table/column or no rows ⇒ the stable
    placeholder ``# no memory yet`` instead of a 500.
    """
    try:
        row = await db.query_one(
            "SELECT version, work_date, compact_md FROM analyst_memory "
            "WHERE analyst_id = ? ORDER BY version DESC LIMIT 1",
            (analyst_id,),
        )
    except sqlite3.OperationalError:  # no such table / no such column
        row = None
    if row is None or not (row["compact_md"] or "").strip():
        return f"# no memory yet\n\n_analyst: {analyst_id}_\n"
    head = f"# Analyst memory — {analyst_id} (v{row['version']}, {row['work_date']})"
    return clamp_md(f"{head}\n\n{row['compact_md'].strip()}\n")


# ---- fact-check disputes ------------------------------------------------------

_DISPUTE_LABELS = {
    "disputed": "已驳斥（DISPUTED）",
    "self_contradicted": "重复已驳斥论断（self_contradicted）",
}


async def analyst_disputes_md(analyst_id: str) -> str:
    """Disputed / self-contradicted claims charged to one analyst, newest first.

    Backed by the Phase 3 fact-check tables (``fact_cards`` joined to
    ``verified_facts`` for evidence/urls). A checkout without migration 0015
    degrades to the stable ``# no disputes recorded`` placeholder — same
    document an analyst with a clean record gets.
    """
    try:
        rows = await db.query(
            "SELECT c.claim, c.category, c.status, c.source_kind, c.source_ref, "
            "       c.created_at, vf.evidence, vf.source_urls "
            "FROM fact_cards c "
            "LEFT JOIN verified_facts vf ON vf.fact_card_id = c.id "
            "WHERE c.analyst_id = ? AND c.status IN ('disputed','self_contradicted') "
            "ORDER BY c.created_at DESC LIMIT 20",
            (analyst_id,),
        )
    except sqlite3.OperationalError:  # no such table / no such column
        rows = []
    if not rows:
        return f"# no disputes recorded\n\n_analyst: {analyst_id}_\n"

    lines = [
        f"# Disputed claims — {analyst_id}",
        "",
        f"_generated {work_date()} (SGT) · {len(rows)} claim(s), newest first_",
    ]
    for r in rows:
        try:
            urls = json.loads(r["source_urls"] or "[]")
        except ValueError:
            urls = []
        lines += ["", f"## {_one_line(r['claim'], cap=300)}", ""]
        lines.append(f"- 判定：{_DISPUTE_LABELS.get(r['status'], r['status'])}")
        lines.append(
            f"- 类别：{r['category']}　来源：{r['source_kind']} `{r['source_ref']}`"
            f"（{(r['created_at'] or '')[:10]}）"
        )
        if r["evidence"]:
            lines.append(f"- 证据：{_one_line(r['evidence'], cap=300)}")
        if urls:
            lines.append("- 链接：" + " ".join(str(u) for u in urls[:5]))
    return clamp_md("\n".join(lines) + "\n")


# ---- placeholders for later phases ------------------------------------------


async def operator_actions_md() -> str:
    """Operator-actions digest — **placeholder until the operator console**.

    The ``operator_actions`` table arrives in ROADMAP Phase 6; the URL shape
    is stable now so prompts can embed the curl before the data exists (the
    path :func:`analyst_disputes_md` already went down).
    """
    return "# no operator actions recorded\n\n_operator console lands in ROADMAP Phase 6_\n"
