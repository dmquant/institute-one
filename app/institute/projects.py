"""Research projects — named long-running containers (ROADMAP Phase 7).

A project groups research queue items, whiteboard boards and mailbox threads
(and, once the BFS Explore mode lands, research trees) so a multi-week line
of inquiry has one page and one digest. Tables from migrations/0021:

- ``projects``       the container (status: active -> archived, conditional
                     claim — archived projects keep history, refuse new links).
- ``project_links``  (project, kind, ref) attachments, idempotent through
                     UNIQUE + INSERT OR IGNORE (the database is the arbiter).

Research items reach a project on two rails, merged at read time:
explicit ``project_links`` rows (kind='research') and the direct
``research_queue.project_id`` column written by ``research.enqueue(...,
project_id=)``. ``get()``/``digest_md()`` union them so both entry points
land on the same project page without research.py depending on this module
for anything beyond existence validation.

``digest_md()`` renders the project summary as bounded markdown (<= 8KB,
``digests.clamp_md``) for the curl-back digest surface.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from typing import Any

from .. import bus, db
from .digests import clamp_md
from .prompts import work_date

log = logging.getLogger("institute.projects")

# projects.status / project_links.kind enums — canonical code constants
# mirroring the CHECKs in migrations/0021_projects.sql (QUEUE_STATUSES idiom).
PROJECT_STATUSES = ("active", "archived")
LINK_KINDS = ("research", "board", "thread", "tree")

MAX_NAME_LEN = 200
MAX_DESCRIPTION_LEN = 4000

# Referential validation per kind: link() checks the ref exists before
# attaching. research_trees (0020) is probed dynamically — the table exists on
# the integrated checkout, but a standalone cherry-pick of this card may lack
# it, in which case tree refs are accepted unvalidated (REVIEW-D5 M1).
_REF_TABLES: dict[str, tuple[str, str]] = {
    "research": ("research_queue", "id"),
    "board": ("whiteboard_boards", "id"),
    "thread": ("mailbox_threads", "id"),
    "tree": ("research_trees", "id"),
}


async def create(name: str, description: str = "") -> dict[str, Any]:
    """Create an active project. Name is the human key: non-empty, unique.

    The name is collapsed to ONE plain line (inner newlines/whitespace runs
    become single spaces): it is structural metadata rendered into markdown
    headings and filenames, so it must never carry line breaks (REVIEW-D5 M3).
    Description stays verbatim — markdown there is a product choice.
    """
    name = " ".join((name or "").split())
    description = (description or "").strip()
    if not name:
        raise ValueError("project name must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"project name exceeds {MAX_NAME_LEN} chars ({len(name)})")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError(f"description exceeds {MAX_DESCRIPTION_LEN} chars ({len(description)})")
    project_id = uuid.uuid4().hex[:12]
    try:
        await db.execute(
            "INSERT INTO projects (id, name, description, status, created_at) "
            "VALUES (?,?,?,'active',?)",
            (project_id, name, description, bus.now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(name) — the INSERT is the arbiter (concurrent same-name racers too)
        raise ValueError(f"project name {name!r} already exists") from exc
    return await db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))  # type: ignore[return-value]


async def list_projects(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Projects with ``n_links`` = TOTAL attachment count: explicit
    project_links plus direct research_queue.project_id rows not also linked
    explicitly (REVIEW-D5 L2 — the count matches what get() expands)."""
    sql = (
        "SELECT p.*, "
        "  (SELECT COUNT(*) FROM project_links l WHERE l.project_id = p.id) "
        "  + (SELECT COUNT(*) FROM research_queue q WHERE q.project_id = p.id "
        "     AND q.id NOT IN (SELECT l2.ref_id FROM project_links l2 "
        "                      WHERE l2.project_id = p.id AND l2.kind = 'research')) "
        "  AS n_links "
        "FROM projects p"
    )
    params: list[Any] = []
    if status:
        if status not in PROJECT_STATUSES:
            raise ValueError(f"unknown status {status!r} (one of {', '.join(PROJECT_STATUSES)})")
        sql += " WHERE p.status = ?"
        params.append(status)
    sql += " ORDER BY p.created_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return await db.query(sql, params)


async def archive(project_id: str) -> dict[str, Any] | None:
    """Archive a project (conditional claim; idempotent — re-archiving is a
    no-op). Returns the fresh row, or None for an unknown id. Links and the
    research_queue.project_id column stay: archived means frozen, not erased."""
    row = await db.query_one("SELECT id FROM projects WHERE id = ?", (project_id,))
    if row is None:
        return None
    await db.execute(
        "UPDATE projects SET status='archived' WHERE id=? AND status='active'", (project_id,)
    )
    return await db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))


async def link(project_id: str, kind: str, ref_id: str) -> dict[str, Any]:
    """Attach one artifact to a project. Idempotent: re-linking the same
    (kind, ref) returns ``linked=False`` (INSERT OR IGNORE + rowcount is the
    one authoritative signal — topic_pool idiom).

    The archived freeze is ATOMIC (REVIEW-D5 H1): the INSERT selects its row
    FROM projects WHERE status='active', so the active check and the write are
    one statement — a concurrent archive can never interleave between a
    pre-read and the insert. rowcount 0 is then disambiguated: an existing
    link row means idempotent replay (allowed even on archived projects —
    same stance as unlink: history curation is not a new attachment); no row
    means the project is missing or archived.

    The ref must exist for research/board/thread; 'tree' validates against
    research_trees (0020) when the table exists and degrades to unvalidated
    on a standalone cherry-pick (see _REF_TABLES)."""
    kind = (kind or "").strip()
    ref_id = (ref_id or "").strip()
    if kind not in LINK_KINDS:
        raise ValueError(f"unknown link kind {kind!r} (one of {', '.join(LINK_KINDS)})")
    if not ref_id:
        raise ValueError("ref_id must not be empty")
    table, col = _REF_TABLES[kind]
    try:
        found = await db.query_one(f"SELECT {col} FROM {table} WHERE {col} = ?", (ref_id,))
    except sqlite3.OperationalError:
        if kind != "tree":
            raise  # research/board/thread tables are 0001 schema — never missing
        found = True  # research_trees not migrated (standalone cherry-pick): accept
        log.info("research_trees table missing; accepting tree ref %r unvalidated", ref_id)
    if not found:
        raise ValueError(f"{kind} {ref_id!r} not found")
    n = await db.execute(
        "INSERT OR IGNORE INTO project_links (project_id, kind, ref_id, created_at) "
        "SELECT ?,?,?,? FROM projects WHERE id = ? AND status = 'active'",
        (project_id, kind, ref_id, bus.now_iso(), project_id),
    )
    row = await db.query_one(
        "SELECT * FROM project_links WHERE project_id=? AND kind=? AND ref_id=?",
        (project_id, kind, ref_id),
    )
    if n:
        return {**(row or {}), "linked": True}
    if row is not None:  # already linked before — idempotent replay
        return {**row, "linked": False}
    proj = await db.query_one("SELECT status FROM projects WHERE id = ?", (project_id,))
    if proj is None:
        raise ValueError(f"project {project_id!r} not found")
    raise ValueError(f"project {project_id!r} is archived")


async def unlink(project_id: str, kind: str, ref_id: str) -> bool:
    """Detach one artifact. True if a row was removed. Works on archived
    projects too (curation of history is not a new attachment)."""
    n = await db.execute(
        "DELETE FROM project_links WHERE project_id=? AND kind=? AND ref_id=?",
        (project_id, kind, ref_id),
    )
    return n > 0


# ---- read side ---------------------------------------------------------------

async def _research_items(project_id: str) -> list[dict[str, Any]]:
    """Union of the two research rails (explicit links + direct project_id),
    deduplicated by queue id, oldest first."""
    rows = await db.query(
        "SELECT q.id AS ref_id, q.topic, q.status, q.run_id, q.created_at "
        "FROM project_links l JOIN research_queue q ON q.id = l.ref_id "
        "WHERE l.project_id = ? AND l.kind = 'research' "
        "UNION "
        "SELECT id AS ref_id, topic, status, run_id, created_at "
        "FROM research_queue WHERE project_id = ? "
        "ORDER BY created_at",
        (project_id, project_id),
    )
    return rows


async def _linked_rows(project_id: str, kind: str) -> list[dict[str, Any]]:
    if kind == "board":
        return await db.query(
            "SELECT l.ref_id, b.topic, b.status, b.work_date, l.created_at "
            "FROM project_links l LEFT JOIN whiteboard_boards b ON b.id = l.ref_id "
            "WHERE l.project_id = ? AND l.kind = 'board' ORDER BY l.created_at",
            (project_id,),
        )
    if kind == "thread":
        return await db.query(
            "SELECT l.ref_id, t.subject, t.analyst_id, t.status, l.created_at "
            "FROM project_links l LEFT JOIN mailbox_threads t ON t.id = l.ref_id "
            "WHERE l.project_id = ? AND l.kind = 'thread' ORDER BY l.created_at",
            (project_id,),
        )
    # tree: enriched from research_trees (0020) when the table exists; bare
    # refs on a standalone cherry-pick that lacks it (REVIEW-D5 M1)
    try:
        return await db.query(
            "SELECT l.ref_id, t.root_topic, t.status, l.created_at "
            "FROM project_links l LEFT JOIN research_trees t ON t.id = l.ref_id "
            "WHERE l.project_id = ? AND l.kind = 'tree' ORDER BY l.created_at",
            (project_id,),
        )
    except sqlite3.OperationalError:
        return await db.query(
            "SELECT ref_id, created_at FROM project_links "
            "WHERE project_id = ? AND kind = 'tree' ORDER BY created_at",
            (project_id,),
        )


async def get(project_id: str) -> dict[str, Any] | None:
    """Project row + all attachments expanded per kind (None for unknown id)."""
    project = await db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return None
    project["links"] = {
        "research": await _research_items(project_id),
        "board": await _linked_rows(project_id, "board"),
        "thread": await _linked_rows(project_id, "thread"),
        "tree": await _linked_rows(project_id, "tree"),
    }
    return project


# ---- digest -------------------------------------------------------------------

_MAX_DIGEST_ROWS = 20  # per section, before the byte clamp (digests idiom)

# Markdown structure characters escaped when structural metadata (the project
# NAME) is rendered into headings — links/images/HTML/backticks in a name must
# read as text, never as markup (REVIEW-D5 M3). Descriptions and linked-item
# titles stay verbatim: markdown there is content, a product choice.
_MD_STRUCTURE = re.compile(r"([\\`*_{}\[\]()<>#!|~])")


def _md_inline(text: str | None) -> str:
    """One plain line with markdown structure characters escaped."""
    return _MD_STRUCTURE.sub(r"\\\1", " ".join((text or "").split()))


def _one_line(text: str | None, cap: int = 120) -> str:
    """Collapse a title to one plain line so list items stay list items
    (digests._one_line semantics, kept local — that helper is module-private)."""
    flat = " ".join((text or "").split()).lstrip("#->*• ").strip()
    return flat[:cap].rstrip() + "…" if len(flat) > cap else flat


async def digest_md(project_id: str) -> str | None:
    """Project summary as bounded markdown (<= 8KB, digests contract: clean
    markdown, verbatim titles, placeholders instead of errors for empty
    sections). None for an unknown project — the API layer turns that into
    a 404 (a project digest is an addressed resource, not a Step-0 curl)."""
    project = await get(project_id)
    if project is None:
        return None
    links = project["links"]
    lines = [
        f"# 项目：{_md_inline(project['name'])}",
        "",
        f"_状态 {project['status']} · 创建 {project['created_at']} · 生成 {work_date()}（SGT）_",
        "",
    ]
    if (project["description"] or "").strip():
        lines += [project["description"].strip(), ""]

    def section(title: str, rows: list[dict[str, Any]], render) -> None:
        lines.append(f"## {title}（{len(rows)}）")
        lines.append("")
        if rows:
            lines.extend(render(r) for r in rows[:_MAX_DIGEST_ROWS])
            if len(rows) > _MAX_DIGEST_ROWS:
                lines.append(f"- …（其余 {len(rows) - _MAX_DIGEST_ROWS} 项从略）")
        else:
            lines.append("_（无）_")
        lines.append("")

    section(
        "深度研究", links["research"],
        lambda r: f"- {_one_line(r['topic'])} — {r['status']}"
                  + (f"（run `{r['run_id']}`）" if r.get("run_id") else ""),
    )
    section(
        "白板研讨", links["board"],
        lambda r: f"- {_one_line(r.get('topic') or r['ref_id'])}"
                  + (f"（{r['work_date']}）" if r.get("work_date") else "")
                  + f" — {r.get('status') or '未知'}",
    )
    section(
        "邮件线程", links["thread"],
        lambda r: f"- {_one_line(r.get('subject') or r['ref_id'])} — {r.get('status') or '未知'}"
                  + (f"（{r['analyst_id']}）" if r.get("analyst_id") else ""),
    )
    section(
        "研究树", links["tree"],
        lambda r: f"- `{r['ref_id']}`"
                  + (f" {_one_line(r['root_topic'])}" if r.get("root_topic") else "")
                  + (f" — {r['status']}" if r.get("status") else ""),
    )
    return clamp_md("\n".join(lines).rstrip() + "\n")
