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
from ..institute.prompts import extract_summary, work_date
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
    except Exception:  # noqa: BLE001 - optional domain accessor; row fallback below
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
    except Exception:  # noqa: BLE001 - optional domain accessor; row fallback below
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
    *, topic: str, run_id: str | None, session_id: str | None, summary: str,
    queue_id: str | None = None,
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

    if queue_id:
        disputes = await db.query(
            "SELECT c.claim, c.status, vf.evidence FROM fact_cards c "
            "LEFT JOIN verified_facts vf ON vf.fact_card_id = c.id "
            "WHERE c.source_kind = 'research_report' AND c.source_ref = ? "
            "AND c.status IN ('disputed','self_contradicted') "
            "ORDER BY c.created_at", (queue_id,),
        )
        if disputes:
            lines = ["> [!warning] 事实核查：本报告中有论断存疑"]
            for d in disputes:
                lines.append(f"> - {d['claim']}" + (f"（{d['evidence']}）" if d["evidence"] else ""))
            parts.insert(0, "\n".join(lines))

    from ..institute.chain import entity_footer  # lazy: domain module
    ef = await entity_footer("\n\n".join(parts))
    if ef:
        parts.append(ef)

    rel = f"Research/{_slug(topic)}/{work_date()} 深度报告.md"
    frontmatter = {"type": "research", "topic": topic, "run_id": run_id, "session": session_id}
    written = await get_writer().write_note(
        rel, frontmatter, "\n\n".join(parts),
        artifact_kind="research", artifact_id=str(run_id or topic or "research"),
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
        queue_id=queue_id,
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
            queue_id=str(event.ref_id or "") or None,
        )
    except Exception:
        log.exception("research export failed for %s", event.ref_id)


# ---- briefing / daily -----------------------------------------------------

_COMPILED = {
    "briefing": ("晨会简报.md", "Briefing", "晨会简报"),
    "daily": ("每日日报.md", "Daily", "每日日报"),
}


def _run_work_date(payload: dict, run: Any) -> str:
    """The run's frozen WORK_DATE (set at creation), falling back to today.

    A run crossing SGT midnight must not split filename stems between the zh
    export and its bilingual twin (REVIEW-D5 M4): the twin payload carries the
    frozen value, so the zh side reads the same source instead of re-deriving
    the date at completion time.
    """
    for source in (payload.get("variables"), _get(run, "variables")):
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except ValueError:
                continue
        if isinstance(source, dict):
            wd = str(source.get("WORK_DATE") or "").strip()
            if wd:
                return wd
    return work_date()


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
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(text)
        if ef:
            text = f"{text.rstrip()}\n\n{ef}"
        rel = f"{folder}/{_run_work_date(p, run)} {title}.md"
        await get_writer().write_note(
            rel, {"type": wf_id, "run_id": run_id}, text,
            artifact_kind=wf_id, artifact_id=str(run_id or wf_id),
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("workflow export failed for %s", event.ref_id)


# ---- whiteboard ------------------------------------------------------------

async def export_board(board_id: str) -> str | None:
    """Project one whiteboard board to the vault (bus-handler body, also the
    manual re-export hook the dispute callout path uses). Returns the written
    vault-relative path, or None when the board is unknown."""
    board = await _get_board(board_id)
    if board is None:
        log.warning("board %s not found; skipping export", board_id)
        return None
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
    for n, card in enumerate(sorted(cards, key=lambda c: _get(c, "idx") or 0), start=1):
        analyst_id = str(_get(card, "analyst_id") or "")
        analyst = get_analyst(analyst_id) if analyst_id else None
        who = analyst.name if analyst else (analyst_id or "未知分析师")
        idx = _get(card, "idx")
        idx = idx if isinstance(idx, int) and idx > 0 else n
        output_file = _get(card, "output_file")
        text = _read_text(ws / str(output_file)) if ws and output_file else None
        text = (text or str(_get(card, "summary") or "")).strip() or "（无产出）"
        parts.append(f"## card-{idx:02d} · {who}\n\n{text}")

    # source dossier warning callout (same contract as the research branch):
    # disputed claims extracted from this board's cards render as a warning
    # block right under the title — rows are truth, re-export re-reads them
    disputes = await db.query(
        "SELECT c.claim, c.status, vf.evidence FROM fact_cards c "
        "LEFT JOIN verified_facts vf ON vf.fact_card_id = c.id "
        "WHERE c.source_kind = 'whiteboard_card' "
        "AND c.source_ref IN (SELECT id FROM whiteboard_cards WHERE board_id = ?) "
        "AND c.status IN ('disputed','self_contradicted') "
        "ORDER BY c.created_at", (board_id,),
    )
    if disputes:
        lines = ["> [!warning] 事实核查：本白板讨论中有论断存疑"]
        for d in disputes:
            lines.append(f"> - {d['claim']}" + (f"（{d['evidence']}）" if d["evidence"] else ""))
        parts.insert(1, "\n".join(lines))

    from ..institute.chain import entity_footer  # lazy: domain module
    ef = await entity_footer("\n\n".join(parts))
    if ef:
        parts.append(ef)

    rel = f"Whiteboard/{wd} {_slug(topic)}.md"
    frontmatter = {"type": "whiteboard", "board_id": board_id, "cards": len(cards)}
    written = await get_writer().write_note(
        rel, frontmatter, "\n\n".join(parts),
        artifact_kind="whiteboard", artifact_id=board_id,
    )
    if written:
        log.info("vault export: %s", written)
    return written


async def _on_board(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        board_id = str(p.get("board_id") or event.ref_id or "")
        await export_board(board_id)
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
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(text)
        if ef:
            text = f"{text.rstrip()}\n\n{ef}"
        rel = f"Analysts/{_slug(analyst_id)}/{date} 日报.md"
        frontmatter = {
            "type": "analyst-daily", "analyst": analyst_id, "analyst_name": who,
            "task": p.get("task_id"),
            "followup_topics": p.get("whiteboard_topics"), "followup_mails": p.get("mailbox_threads"),
        }
        await get_writer().write_note(
            rel, frontmatter, text.strip(),
            artifact_kind="analyst-daily", artifact_id=f"{analyst_id}:{date}",
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("analyst daily export failed for %s", event.ref_id)


# ---- analyst memory ----------------------------------------------------------

async def _on_memory(event: bus.Event) -> None:
    """One region-mode note per analyst: Analysts/<id>/memory.md.

    The note is rewritten on every compact, so it uses managed-region semantics
    (writer rule 4): the institute owns only the marked region and any human
    annotations outside it survive regeneration.
    """
    if not get_writer().enabled:
        return
    try:
        analyst_id = str(event.ref_id or "")
        from ..institute import memory  # lazy: domain module

        row = await memory.latest(analyst_id)
        if row is None:
            log.warning("no memory row to export for %s", analyst_id)
            return

        from ..institute.analysts import get_analyst  # lazy: domain module

        analyst = get_analyst(analyst_id)
        who = analyst.name if analyst else analyst_id
        body = (
            f"# {who} · 常备记忆（第 {row['version']} 版 · {row['work_date']}）\n\n"
            f"{(row['compact_md'] or '').strip()}"
        )
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(body)
        if ef:
            body = f"{body}\n\n{ef}"
        rel = f"Analysts/{_slug(analyst_id)}/memory.md"
        frontmatter = {"type": "memory", "analyst": analyst_id, "analyst_name": who}
        written = await get_writer().write_note(
            rel, frontmatter, body,
            artifact_kind="memory", artifact_id=analyst_id, region=True,
        )
        if written:
            log.info("vault export: %s", written)
    except Exception:
        log.exception("memory export failed for %s", event.ref_id)


# ---- fact-check disputed claims ---------------------------------------------

async def _on_factcheck_disputed(event: bus.Event) -> None:
    """factcheck.disputed → regenerate the rolling Disputed Claims digest
    (+ re-export the source dossier so its warning callout appears)."""
    if not get_writer().enabled:
        return
    try:
        rows = await db.query(
            "SELECT c.id, c.claim, c.category, c.status, c.analyst_id, "
            "       c.source_kind, c.source_ref, c.created_at, "
            "       vf.evidence, vf.source_urls "
            "FROM fact_cards c "
            "LEFT JOIN verified_facts vf ON vf.fact_card_id = c.id "
            "WHERE c.status IN ('disputed', 'self_contradicted') "
            "ORDER BY c.created_at DESC LIMIT 50"
        )
        if not rows:
            return
        parts = []
        for r in rows:
            try:
                urls = json.loads(r["source_urls"] or "[]")
            except ValueError:
                urls = []
            label = ("已驳斥（DISPUTED）" if r["status"] == "disputed"
                     else "重复已驳斥论断（self_contradicted）")
            lines = [
                f"## {r['claim']}",
                "",
                f"- 判定：{label}",
                f"- 类别：{r['category']}　分析师：{r['analyst_id'] or '（无）'}",
                f"- 来源：{r['source_kind']} `{r['source_ref']}`（{r['created_at']}）",
            ]
            if r["evidence"]:
                lines.append(f"- 证据：{r['evidence']}")
            if urls:
                lines.append("- 链接：" + " ".join(urls))
            parts.append("\n".join(lines))
        body = "\n\n".join(parts)
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(body)
        if ef:
            body = f"{body}\n\n{ef}"
        await get_writer().write_note(
            "Inbox/Disputed Claims.md", {"type": "factcheck"}, body,
            artifact_kind="factcheck", artifact_id="factcheck-disputes",
        )
        # source dossier warning callout: re-project the source note so the
        # callout block lands — rows are truth, the exporter re-reads them
        p = event.payload or {}
        src_kind, src_ref = p.get("source_kind"), p.get("source_ref")
        if src_kind == "research_report" and src_ref:
            try:
                await export_research_queue_item(str(src_ref))
            except (LookupError, ValueError):
                log.warning("dispute source %s not re-exportable", src_ref)
        elif src_kind == "whiteboard_card" and src_ref:
            card = await db.query_one(
                "SELECT board_id FROM whiteboard_cards WHERE id = ?", (str(src_ref),)
            )
            if card:
                await export_board(str(card["board_id"]))
            else:
                log.warning("dispute source card %s unknown; no board dossier", src_ref)
    except Exception:
        log.exception("factcheck disputes export failed for %s", event.ref_id)


# ---- paper book journal ------------------------------------------------------

async def _on_paper_book(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        from ..institute import paper_book  # lazy: domain module

        wd = str((event.payload or {}).get("work_date") or event.ref_id or work_date())
        body = await paper_book.render_journal(wd)
        if not body.strip():
            return
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(body)
        if ef:
            body = f"{body.rstrip()}\n\n{ef}"
        rel = f"Book/journal/{wd}.md"
        await get_writer().write_note(
            rel, {"type": "paper-book-journal", "work_date": wd}, body,
            artifact_kind="paper-book-journal", artifact_id=wd,
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("paper book journal export failed for %s", event.ref_id)


# ---- research tree (BFS explore) --------------------------------------------

async def _on_research_tree_completed(event: bus.Event) -> None:
    """tree.completed → Research/<root_topic>/tree.md（节点树 markdown 投影）。"""
    if not get_writer().enabled:
        return
    try:
        tree_id = str(event.ref_id or "")
        tree = await db.query_one("SELECT * FROM research_trees WHERE id = ?", (tree_id,))
        if tree is None:
            return
        nodes = await db.query(
            "SELECT * FROM research_tree_nodes WHERE tree_id = ? ORDER BY depth, created_at, id",
            (tree_id,),
        )
        by_parent: dict[str | None, list[dict]] = {}
        for n in nodes:
            by_parent.setdefault(n["parent_id"], []).append(n)
        badge = {"completed": "[完成]", "failed": "[失败]", "pruned": "[剪枝]",
                 "pending": "[待研]", "running": "[进行]"}
        lines: list[str] = []

        def _walk(parent_id: str | None, indent: int = 0) -> None:
            for n in by_parent.get(parent_id, ()):
                mark = badge.get(n["status"], f"[{n['status']}]")
                q = f"（{n['question']}）" if n["question"] else ""
                lines.append(f"{'    ' * indent}- {mark} L{n['depth']} {n['topic']}{q}")
                if n["summary"]:
                    lines.append(f"{'    ' * indent}    - 结论：{str(n['summary'])[:300]}")
                _walk(n["id"], indent + 1)

        _walk(None)
        header = (
            f"- 状态：{tree['status']}　节点：{len(nodes)}　"
            f"max_depth={tree['max_depth']}　max_nodes={tree['max_nodes']}\n"
            f"- 创建：{tree['created_at']}　结束：{tree['finished_at'] or '—'}"
        )
        body = f"## 研究树概览\n\n{header}\n\n## 节点树\n\n" + "\n".join(lines)
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(body)
        if ef:
            body = f"{body.rstrip()}\n\n{ef}"
        rel = f"Research/{_slug(tree['root_topic'])}/tree.md"
        await get_writer().write_note(
            rel, {"type": "research_tree", "tree_id": tree_id}, body,
            artifact_kind="research_tree",
            artifact_id=f"research-tree:{_slug(tree['root_topic'])}",
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("research tree export failed for %s", event.ref_id)


# ---- bilingual twins ---------------------------------------------------------

async def _on_twin_ready(event: bus.Event) -> None:
    """bilingual.twin_ready → same stem as the zh export plus ``_en``.

    The payload is BY REFERENCE (REVIEW-D5 M2): the full translation lives
    once in tasks.output, keyed by payload task_id — dereference it here.
    """
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        wf_id = str(p.get("workflow_id") or "")
        task_id = str(p.get("task_id") or "")
        if wf_id not in _COMPILED or not task_id:
            return
        row = await db.query_one("SELECT output FROM tasks WHERE id = ?", (task_id,))
        text = str((row or {}).get("output") or "")
        if not text.strip():
            log.warning("twin task %s has no output; skipping export", task_id)
            return
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(text)
        if ef:
            text = f"{text.rstrip()}\n\n{ef}"
        _fname, folder, title = _COMPILED[wf_id]
        wd = str(p.get("work_date") or "") or work_date()
        rel = f"{folder}/{wd} {title}_en.md"
        run_id = str(p.get("run_id") or event.ref_id or "") or None
        await get_writer().write_note(
            rel, {"type": wf_id, "run_id": run_id, "locale": "en", "task": task_id}, text,
            artifact_kind=wf_id, artifact_id=f"{run_id or wf_id}:en",
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("bilingual twin export failed for %s", event.ref_id)


# ---- committee (weekly deliberation, M8-012) ---------------------------------

_COMMITTEE_WF_ID = "committee"
_COMMITTEE_TERMINAL = ("workflow.completed", "workflow.failed", "workflow.cancelled")


async def _on_committee(event: bus.Event) -> None:
    """workflow.* for the committee workflow → durable run record + vault note.

    Registered on the ``workflow.`` prefix and self-filters: non-committee
    workflows return immediately. Rows first (rows are truth): started opens
    the multi_agent_runs committee record, terminal events settle it — BOTH
    regardless of whether the vault is enabled. Then, for completed runs
    only, the note is projected to ``Committee/<WORK_DATE> 委员会裁决.md``
    with the verdict text AND the input snapshot (the frozen ${WEEK_DISPUTES}
    whiteboard digest the agenda step saw — persisted on the run's variables).
    """
    try:
        p = event.payload or {}
        run_id = str(p.get("run_id") or event.ref_id or "") or None
        wf_id = str(p.get("workflow_id") or "")
        run = None
        if not wf_id and run_id:
            run = await _get_run(run_id)
            wf_id = str(_get(run, "workflow_id") or "")
        if wf_id != _COMMITTEE_WF_ID or not run_id:
            return

        from ..institute import multi_agent  # lazy: domain module

        if event.type == "workflow.started":
            await multi_agent.open_committee_run(run_id)
            return
        if event.type not in _COMMITTEE_TERMINAL:
            return
        try:
            await multi_agent.finalize_committee_run(run_id)
        except Exception:
            log.exception("committee run record settle failed for %s", run_id)

        if event.type != "workflow.completed" or not get_writer().enabled:
            return
        if run is None:
            run = await _get_run(run_id)
        ws = await _session_workspace(p.get("session_id") or _get(run, "session_id"))
        verdict = (_read_text(ws / "委员会裁决.md") if ws else None) or _steps_text(run)
        if not verdict.strip():
            log.warning("nothing to export for committee run %s", run_id)
            return

        variables = p.get("variables") or _get(run, "variables")
        if isinstance(variables, str):
            try:
                variables = json.loads(variables)
            except ValueError:
                variables = {}
        snapshot = str((variables or {}).get("WEEK_DISPUTES") or "").strip()
        parts = [
            verdict.strip(),
            "## 输入快照（当周白板研讨摘要）\n\n"
            + (snapshot or "（本周无已完结的白板研讨记录）"),
        ]
        footer = _workspace_footer(ws)
        if footer:
            parts.append(footer)
        text = "\n\n".join(parts)
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(text)
        if ef:
            text = f"{text}\n\n{ef}"

        wd = _run_work_date(p, run)
        rel = f"Committee/{wd} 委员会裁决.md"
        await get_writer().write_note(
            rel, {"type": "committee", "run_id": run_id, "work_date": wd}, text,
            artifact_kind="committee", artifact_id=str(run_id),
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("committee export failed for %s", event.ref_id)


# ---- wiring ------------------------------------------------------------------

def register() -> None:
    """Hook the exporter into the bus. Called once from the app lifespan."""
    bus.on("research.completed", _on_research)
    bus.on("workflow.completed", _on_workflow)
    bus.on("whiteboard.board_completed", _on_board)
    bus.on("analyst_daily.completed", _on_analyst_daily)
    bus.on("memory.compacted", _on_memory)
    bus.on("factcheck.disputed", _on_factcheck_disputed)
    bus.on("paper_book.marked", _on_paper_book)
    bus.on("tree.completed", _on_research_tree_completed)
    bus.on("bilingual.twin_ready", _on_twin_ready)
    bus.on("workflow.", _on_committee)
    log.info("vault exporter registered (vault_dir=%s)", get_settings().vault_dir or "disabled")
