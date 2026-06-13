"""Vault exporter — projects finished work into the Obsidian vault via bus events.

``register()`` is called once from the app lifespan. Handlers are defensive end
to end: missing runs/boards/files degrade to step summaries, and nothing here
ever raises into the bus (the bus guards too, but belt and braces).
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..institute import claim_audit, evidence
from ..institute.prompts import extract_summary, work_date
from ..institute.quality import evidence_warnings, quality_callout
from .writer import get_writer

log = logging.getLogger("institute.exporter")

_PATH_HOSTILE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')


def _slug(text: str, max_len: int = 80) -> str:
    """Filename-safe slug: keep CJK, replace path-hostile chars with -."""
    s = _PATH_HOSTILE.sub("-", str(text or "").strip())
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s).strip(" -.")
    return s[:max_len].strip(" -.") or "untitled"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _read_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


async def _session_workspace(session_id: Any) -> Path | None:
    if not session_id:
        return None
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (str(session_id),))
    if row and row["workspace_dir"]:
        ws = Path(row["workspace_dir"]).expanduser()
        if ws.is_dir():
            return ws
    return None


async def _get_run(run_id: Any) -> Any | None:
    """workflows.get_run when available, else the raw workflow_runs row."""
    if not run_id:
        return None
    try:
        from ..institute import workflows  # lazy: domain module

        res = workflows.get_run(str(run_id))
        if inspect.isawaitable(res):
            res = await res
        if res is not None:
            return res
    except Exception:
        log.debug("workflows.get_run unavailable; falling back to direct query", exc_info=True)
    return await db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (str(run_id),))


async def _get_board(board_id: str) -> Any | None:
    """whiteboard.get_board when available, else board row + card rows."""
    if not board_id:
        return None
    try:
        from ..institute import whiteboard  # lazy: domain module

        res = whiteboard.get_board(board_id)
        if inspect.isawaitable(res):
            res = await res
        if res is not None:
            return res
    except Exception:
        log.debug("whiteboard.get_board unavailable; falling back to direct query", exc_info=True)
    row = await db.query_one("SELECT * FROM whiteboard_boards WHERE id = ?", (board_id,))
    if row is None:
        return None
    row["cards"] = await db.query(
        "SELECT * FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board_id,)
    )
    return row


def _results_list(run: Any) -> list[dict]:
    raw = _get(run, "results")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _steps_text(run: Any) -> str:
    parts = []
    for r in _results_list(run):
        summary = str(r.get("summary") or "").strip()
        if not summary:
            continue
        title = str(r.get("title") or r.get("step_id") or "步骤")
        parts.append(f"## {title}\n\n{summary}")
    return "\n\n".join(parts)


def _workspace_footer(ws: Path | None) -> str:
    if ws is None:
        return ""
    try:
        names = sorted(p.name for p in ws.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return ""
    if not names:
        return ""
    listing = "\n".join(f"- `{n}`" for n in names)
    return f"## 档案\n\n工作目录：`{ws}`\n\n{listing}"


# ---- research -----------------------------------------------------------

async def _export_research(
    *, topic: str, run_id: str | None, session_id: str | None, summary: str
) -> str | None:
    run = await _get_run(run_id)
    session_id = session_id or _get(run, "session_id")
    ws = await _session_workspace(session_id)
    report = _read_text(ws / "06_深度报告.md") if ws else None
    if report is None:
        report = _steps_text(run)
    summary = (summary or "").strip() or (extract_summary(report) if report else "")
    if not report and not summary:
        log.warning("nothing to export for research run %s (%s)", run_id, topic)
        return None

    parts = []
    if summary:
        parts.append(f"## 核心结论\n\n{summary}")
    if report:
        parts.append(report.strip())
    followups = _read_text(ws / "07_后续跟进.md") if ws else None
    if followups and followups.strip():
        text = followups.strip()
        if not text.startswith(("## 后续跟进", "#")):
            text = "## 后续跟进\n\n" + text
        parts.append(text)
    footer = _workspace_footer(ws)
    if footer:
        parts.append(footer)

    raw_body = "\n\n".join(parts)
    audit = claim_audit.audit_text(raw_body)
    callout = claim_audit.claim_audit_callout(audit)
    body = f"{callout}\n\n{raw_body}" if callout else raw_body
    rel = f"Research/{_slug(topic)}/{work_date()} 深度报告.md"
    frontmatter = {
        "type": "research",
        "topic": topic,
        "run_id": run_id,
        "session": session_id,
        **audit.frontmatter(),
    }
    written = await get_writer().write_note(
        rel, frontmatter, body,
        artifact_kind="research", artifact_id=str(run_id or topic or "research"),
    )
    await _ingest_evidence_safe(
        raw_body,
        artifact_kind="research",
        artifact_id=str(run_id or topic or "research"),
        artifact_path=written or rel,
        topic=topic,
        work_date=work_date(),
    )
    await _audit_claims_safe(
        raw_body,
        artifact_kind="research",
        artifact_id=str(run_id or topic or "research"),
        artifact_path=written or rel,
        topic=topic,
        work_date=work_date(),
    )
    if written:
        log.info("vault export: %s", written)
    return written


async def export_research_queue_item(queue_id: str) -> str | None:
    """Manual re-export of a completed research queue item (API hook).

    Raises LookupError for an unknown id, ValueError when not completed.
    """
    row = await db.query_one("SELECT * FROM research_queue WHERE id = ?", (queue_id,))
    if row is None:
        raise LookupError(f"unknown research queue item: {queue_id}")
    if row["status"] != "completed":
        raise ValueError(f"research item {queue_id} is '{row['status']}', not completed")
    run_id = row["run_id"]
    log_row = None
    if run_id:
        log_row = await db.query_one(
            "SELECT summary FROM research_log WHERE run_id = ? ORDER BY completed_at DESC LIMIT 1",
            (run_id,),
        )
    if log_row is None:
        log_row = await db.query_one(
            "SELECT summary FROM research_log WHERE topic = ? ORDER BY completed_at DESC LIMIT 1",
            (row["topic"],),
        )
    run = await _get_run(run_id)
    return await _export_research(
        topic=row["topic"], run_id=run_id,
        session_id=_get(run, "session_id"), summary=(log_row or {}).get("summary") or "",
    )


async def _on_research(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        await _export_research(
            topic=str(p.get("topic") or "").strip(),
            run_id=str(p.get("run_id") or "") or None,
            session_id=str(p.get("session_id") or "") or None,
            summary=str(p.get("summary") or ""),
        )
    except Exception:
        log.exception("research export failed for %s", event.ref_id)


# ---- briefing / daily -----------------------------------------------------

_COMPILED = {
    "briefing": ("晨会简报.md", "Briefing", "晨会简报"),
    "daily": ("每日日报.md", "Daily", "每日日报"),
}


async def _on_workflow(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        run_id = str(p.get("run_id") or event.ref_id or "") or None
        wf_id = str(p.get("workflow_id") or "")
        run = None
        if not wf_id:
            run = await _get_run(run_id)
            wf_id = str(_get(run, "workflow_id") or "")
        if wf_id not in _COMPILED:
            return
        if run is None:
            run = await _get_run(run_id)
        session_id = p.get("session_id") or _get(run, "session_id")
        ws = await _session_workspace(session_id)
        fname, folder, title = _COMPILED[wf_id]
        text = (_read_text(ws / fname) if ws else None) or _steps_text(run)
        if not text.strip():
            log.warning("nothing to export for %s run %s", wf_id, run_id)
            return
        rel = f"{folder}/{work_date()} {title}.md"
        warnings = evidence_warnings(text)
        audit = claim_audit.audit_text(text)
        callouts = [c for c in (quality_callout(warnings), claim_audit.claim_audit_callout(audit)) if c]
        body = "\n\n".join([*callouts, text])
        written = await get_writer().write_note(
            rel,
            {
                "type": wf_id,
                "run_id": run_id,
                "quality_warnings": warnings,
                **audit.frontmatter(),
            },
            body,
            artifact_kind=wf_id, artifact_id=str(run_id or wf_id),
        )
        await _ingest_evidence_safe(
            text,
            artifact_kind=wf_id,
            artifact_id=str(run_id or wf_id),
            artifact_path=written or rel,
            topic=title,
            work_date=work_date(),
        )
        await _audit_claims_safe(
            text,
            artifact_kind=wf_id,
            artifact_id=str(run_id or wf_id),
            artifact_path=written or rel,
            topic=title,
            work_date=work_date(),
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("workflow export failed for %s", event.ref_id)


# ---- whiteboard ------------------------------------------------------------

async def _on_board(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        board_id = str(p.get("board_id") or event.ref_id or "")
        board = await _get_board(board_id)
        if board is None:
            log.warning("board %s not found; skipping export", board_id)
            return
        cards = _get(board, "cards")
        if cards is None:
            cards = await db.query(
                "SELECT * FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board_id,)
            )
        cards = list(cards or [])
        topic = str(_get(board, "topic") or board_id)
        question = str(_get(board, "question") or "").strip()
        wd = str(_get(board, "work_date") or "") or work_date()
        ws = await _session_workspace(_get(board, "session_id"))

        from ..institute.analysts import get_analyst  # lazy: domain module

        parts = [f"# {topic}"]
        if question:
            parts.append(f"> {question}")
        card_texts: list[str] = []
        card_evidence: list[dict[str, str]] = []
        for n, card in enumerate(sorted(cards, key=lambda c: _get(c, "idx") or 0), start=1):
            analyst_id = str(_get(card, "analyst_id") or "")
            analyst = get_analyst(analyst_id) if analyst_id else None
            who = analyst.name if analyst else (analyst_id or "未知分析师")
            idx = _get(card, "idx")
            idx = idx if isinstance(idx, int) and idx > 0 else n
            output_file = _get(card, "output_file")
            text = _read_text(ws / str(output_file)) if ws and output_file else None
            text = (text or str(_get(card, "summary") or "")).strip() or "（无产出）"
            card_texts.append(text)
            card_evidence.append({
                "id": str(_get(card, "id") or f"{board_id}:{idx}"),
                "idx": str(idx),
                "analyst_id": analyst_id,
                "text": text,
            })
            parts.append(f"## card-{idx:02d} · {who}\n\n{text}")

        from ..institute.whiteboard import closure_block_from_texts  # lazy: avoid exporter/domain cycle

        parts.append(
            closure_block_from_texts(topic, question, card_texts, card_count=len(cards))
        )

        rel = f"Whiteboard/{wd} {_slug(topic)}.md"
        raw_body = "\n\n".join(parts)
        audit = claim_audit.audit_text(raw_body)
        callout = claim_audit.claim_audit_callout(audit)
        body = f"{callout}\n\n{raw_body}" if callout else raw_body
        frontmatter = {
            "type": "whiteboard",
            "board_id": board_id,
            "cards": len(cards),
            **audit.frontmatter(),
        }
        written = await get_writer().write_note(
            rel, frontmatter, body,
            artifact_kind="whiteboard", artifact_id=board_id,
        )
        await _audit_claims_safe(
            raw_body,
            artifact_kind="whiteboard",
            artifact_id=board_id,
            artifact_path=written or rel,
            topic=topic,
            work_date=wd,
        )
        for card_ev in card_evidence:
            await _ingest_evidence_safe(
                card_ev["text"],
                artifact_kind="whiteboard_card",
                artifact_id=card_ev["id"],
                artifact_path=f"{written or rel}#card-{card_ev['idx'].zfill(2)}",
                topic=topic,
                analyst_id=card_ev["analyst_id"],
                work_date=wd,
            )
            await _audit_claims_safe(
                card_ev["text"],
                artifact_kind="whiteboard_card",
                artifact_id=card_ev["id"],
                artifact_path=f"{written or rel}#card-{card_ev['idx'].zfill(2)}",
                topic=topic,
                analyst_id=card_ev["analyst_id"],
                work_date=wd,
            )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("whiteboard export failed for %s", event.ref_id)


# ---- analyst dailies ---------------------------------------------------------

async def _on_analyst_daily(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        analyst_id = str(event.ref_id or "")
        date = str(p.get("date") or work_date())
        ws = await _session_workspace(p.get("session_id"))
        text = _read_text(ws / str(p.get("file") or f"{analyst_id}.md")) if ws else None
        if not text and p.get("task_id"):
            row = await db.query_one("SELECT output FROM tasks WHERE id = ?", (str(p["task_id"]),))
            text = (row or {}).get("output")
        if not text or not text.strip():
            log.warning("nothing to export for analyst daily %s %s", analyst_id, date)
            return

        from ..institute.analysts import get_analyst  # lazy: domain module

        analyst = get_analyst(analyst_id)
        who = analyst.name if analyst else analyst_id
        rel = f"Analysts/{_slug(analyst_id)}/{date} 日报.md"
        quality = p.get("quality") if isinstance(p.get("quality"), dict) else {}
        warnings = list(quality.get("warnings") or []) if isinstance(quality, dict) else []
        audit = claim_audit.audit_text(text)
        callouts = [c for c in (quality_callout(warnings), claim_audit.claim_audit_callout(audit)) if c]
        body = "\n\n".join([*callouts, text.strip()])
        frontmatter = {
            "type": "analyst-daily", "analyst": analyst_id, "analyst_name": who,
            "task": p.get("task_id"),
            "followup_topics": p.get("whiteboard_topics"), "followup_mails": p.get("mailbox_threads"),
            "quality_warnings": warnings,
            **audit.frontmatter(),
        }
        written = await get_writer().write_note(
            rel, frontmatter, body,
            artifact_kind="analyst-daily", artifact_id=f"{analyst_id}:{date}",
        )
        await _ingest_evidence_safe(
            text,
            artifact_kind="analyst-daily",
            artifact_id=f"{analyst_id}:{date}",
            artifact_path=written or rel,
            topic=who,
            analyst_id=analyst_id,
            work_date=date,
        )
        await _audit_claims_safe(
            text,
            artifact_kind="analyst-daily",
            artifact_id=f"{analyst_id}:{date}",
            artifact_path=written or rel,
            topic=who,
            analyst_id=analyst_id,
            work_date=date,
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("analyst daily export failed for %s", event.ref_id)


async def _ingest_evidence_safe(text: str, **kwargs: Any) -> None:
    try:
        n = await evidence.ingest_text(text, **kwargs)
        if n:
            log.info(
                "evidence ingest: %s links for %s %s",
                n,
                kwargs.get("artifact_kind"),
                kwargs.get("artifact_id"),
            )
    except Exception:  # noqa: BLE001 - evidence indexing must not block exports
        log.exception(
            "evidence ingest failed for %s %s",
            kwargs.get("artifact_kind"),
            kwargs.get("artifact_id"),
        )


async def _audit_claims_safe(text: str, **kwargs: Any) -> None:
    try:
        report = await claim_audit.audit_and_store_text(text, **kwargs)
        if report.total:
            log.info(
                "claim audit: %s claims for %s %s (%s)",
                report.total,
                kwargs.get("artifact_kind"),
                kwargs.get("artifact_id"),
                report.counts(),
            )
    except Exception:  # noqa: BLE001 - claim triage must not block exports
        log.exception(
            "claim audit failed for %s %s",
            kwargs.get("artifact_kind"),
            kwargs.get("artifact_id"),
        )


# ---- wiring ------------------------------------------------------------------

def register() -> None:
    """Hook the exporter into the bus. Called once from the app lifespan."""
    bus.on("research.completed", _on_research)
    bus.on("workflow.completed", _on_workflow)
    bus.on("whiteboard.board_completed", _on_board)
    bus.on("analyst_daily.completed", _on_analyst_daily)
    log.info("vault exporter registered (vault_dir=%s)", get_settings().vault_dir or "disabled")
