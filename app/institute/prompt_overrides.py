"""Prompt overrides — prompt iteration as data (ROADMAP Phase 2).

A ``prompt_overrides`` row can replace one registered prompt block (a
"scope") at render time, layered OVER the code constants in ``prompts.py``:

- **shadow**: a recorded draft; never affects prompts (the row itself is the
  record — ``resolve`` only ever sees actives).
- **active**: at most one per scope (partial unique index is the DB backstop);
  ``resolve(scope, default)`` returns its content instead of the code default.
- **retired**: immutable history — what ran when. Re-activating old content
  means creating a NEW shadow row, never editing history.

Lifecycle transitions are conditional claims inside one transaction
(hard rule 2): activating a shadow atomically retires the scope's previous
active; a lost claim raises ``OverrideConflict`` and rolls everything back.

The read path mirrors the hand-weights registry cache: ``resolve``/``render``
are synchronous (prompt assembly is sync code) and read a process-local cache
that async code pushes via ``refresh_cache()`` — every write path here calls
it, the API GETs refresh opportunistically, and boot SHOULD pre-warm it (see
PATCH-NOTES-PROMPT-OVERRIDES.md). A never-loaded cache serves the code
defaults byte-identically and logs ONE warning, so a missed pre-warm degrades
to exactly the pre-override behaviour instead of breaking prompts.

Safety invariant (the "relax CLAUDE.md rule 4 safely" contract): with no
active override the mount points in ``prompts.py`` render byte-identically to
the previous inline strings, and ``render`` falls back to the code default on
any broken override content — the prompt path can never break on data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from string import Formatter
from typing import Any

from .. import bus, db

log = logging.getLogger("institute.prompt_overrides")

# prompt_overrides.status enum — canonical code constant mirroring the CHECK
# in migrations/0029_prompt_overrides.sql.
STATUSES = ("shadow", "active", "retired")

MAX_CONTENT_LEN = 16000     # chars; a prompt block, not a document
MAX_NOTE_LEN = 2000


class OverrideConflict(RuntimeError):
    """A lifecycle transition lost its conditional claim (wrong current status)."""


# ---- scope registry ---------------------------------------------------------
# The closed set of mount points (open vocabulary in the TABLE, code-enforced
# here — the 0023 recipes-status precedent). ``default_attr`` names the code
# default on institute.prompts (imported lazily: prompts.py imports this
# module for its mount points); ``fields`` is the exact set of ``str.format``
# placeholders render() supplies — override content may use a subset, nothing
# else. Zero-field scopes are literal blocks: render never formats them, so
# braces in their content stay literal text.

@dataclass(frozen=True)
class ScopeSpec:
    default_attr: str
    fields: tuple[str, ...]
    description: str


SCOPES: dict[str, ScopeSpec] = {
    "prompts.date_anchor": ScopeSpec(
        "DATE_ANCHOR_TEMPLATE", ("datetime",),
        "时间锚点行 — 每个分析师 prompt 的第一块（{datetime} = SGT 当前时间）",
    ),
    "prompts.persona_block": ScopeSpec(
        "PERSONA_TEMPLATE", ("name", "name_en", "focus", "persona"),
        "persona 三明治的人设块模板（字段取自名册 catalog/analysts.json）",
    ),
    "prompts.citation_mandate": ScopeSpec(
        "CITATION_MANDATE", (),
        "【引用规范】块 — 进入每一个分析师 prompt（日报/白板/信箱/工作流步骤/ask）",
    ),
    "prompts.file_deliverable": ScopeSpec(
        "FILE_DELIVERABLE", ("filename",),
        "【交付规范】块 — 所有带 output_file 的分析师 prompt",
    ),
}


def default_text(scope: str) -> str:
    """The code default for a scope (lazy import: prompts.py imports us)."""
    validate_scope(scope)
    from . import prompts

    return getattr(prompts, SCOPES[scope].default_attr)


# ---- validation ---------------------------------------------------------------

def validate_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError(
            f"unknown scope {scope!r} (known: {', '.join(sorted(SCOPES))})"
        )


def validate_content(scope: str, content: str) -> None:
    """Reject content that could not render safely at the scope's mount point."""
    validate_scope(scope)
    if not str(content).strip():
        raise ValueError("content must not be empty")
    if len(content) > MAX_CONTENT_LEN:
        raise ValueError(f"content exceeds {MAX_CONTENT_LEN} chars")
    spec = SCOPES[scope]
    if not spec.fields:
        return  # literal block: render never formats it, braces stay literal
    try:
        found = {f for _, f, _, _ in Formatter().parse(content) if f is not None}
    except ValueError as exc:  # unbalanced/malformed braces
        raise ValueError(f"invalid format template: {exc}") from exc
    unknown = found - set(spec.fields)
    if unknown:
        raise ValueError(
            f"unknown placeholders {sorted(unknown)} (allowed: {sorted(spec.fields)}; "
            "literal braces must be doubled: {{ }})"
        )


# ---- resolve cache (sync read path, async refresh — the hand-weights idiom) ----

# None = never loaded in this process (serve code defaults, warn once);
# {} = loaded and no active overrides. Pushed by refresh_cache() only.
_cache: dict[str, str] | None = None
_warned = False


def resolve(scope: str, default: str) -> str:
    """Active override content for ``scope``, else ``default`` (byte-identical)."""
    global _warned
    if _cache is None:
        if not _warned:
            _warned = True
            log.warning(
                "prompt_overrides cache never loaded in this process — serving "
                "code defaults; pre-warm via refresh_cache() at boot or hit "
                "GET /api/prompt-overrides"
            )
        return default
    return _cache.get(scope, default)


def render(scope: str, default: str, **fields: str) -> str:
    """resolve() + placeholder substitution, falling back to the code default.

    An active override that fails to format (validation bypassed via manual DB
    edits, or a registry change since activation) must degrade to the exact
    default rendering instead of breaking every prompt in the institute.
    """
    template = resolve(scope, default)
    if not fields:
        return template
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        if template is not default:
            log.exception(
                "active prompt override for %s failed to format; falling back "
                "to the code default", scope,
            )
        return default.format(**fields)


async def refresh_cache() -> dict[str, str]:
    """Reload active overrides into the process cache: {scope: content}."""
    global _cache, _warned
    rows = await db.query(
        "SELECT scope, content FROM prompt_overrides WHERE status = 'active'"
    )
    _cache = {r["scope"]: r["content"] for r in rows}
    _warned = False
    return dict(_cache)


def invalidate_cache() -> None:
    """Drop the cache: resolve serves code defaults until the next refresh."""
    global _cache
    _cache = None


# ---- CRUD ----------------------------------------------------------------------

async def get(override_id: int) -> dict[str, Any] | None:
    return await db.query_one(
        "SELECT * FROM prompt_overrides WHERE id = ?", (override_id,)
    )


async def list_overrides(
    scope: str | None = None, status: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r} (expected one of {STATUSES})")
    where, params = [], []
    if scope:
        where.append("scope = ?")
        params.append(scope)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM prompt_overrides"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return await db.query(sql, params)


async def create(scope: str, content: str, note: str = "") -> dict[str, Any]:
    """Record a new shadow draft (never affects prompts until activated)."""
    validate_content(scope, content)
    if len(note) > MAX_NOTE_LEN:
        raise ValueError(f"note exceeds {MAX_NOTE_LEN} chars")
    oid = await db.insert(
        "INSERT INTO prompt_overrides (scope, content, status, note, created_at) "
        "VALUES (?,?,'shadow',?,?)",
        (scope, content, note, bus.now_iso()),
    )
    row = await get(oid)
    assert row is not None
    return row


async def update_draft(
    override_id: int, *, content: str | None = None, note: str | None = None,
) -> dict[str, Any]:
    """Edit a shadow draft. Active/retired rows are immutable (audit history)."""
    row = await get(override_id)
    if row is None:
        raise LookupError(f"unknown prompt override {override_id}")
    if content is None and note is None:
        return row
    if content is not None:
        validate_content(row["scope"], content)
    if note is not None and len(note) > MAX_NOTE_LEN:
        raise ValueError(f"note exceeds {MAX_NOTE_LEN} chars")
    claimed = await db.execute(
        "UPDATE prompt_overrides SET content = COALESCE(?, content), "
        "note = COALESCE(?, note) WHERE id = ? AND status = 'shadow'",
        (content, note, override_id),
    )
    if not claimed:
        raise OverrideConflict(
            f"prompt override {override_id} is '{row['status']}', not shadow — "
            "activated content is immutable; create a new draft instead"
        )
    row = await get(override_id)
    assert row is not None
    return row


async def delete_draft(override_id: int) -> None:
    """Discard a shadow draft. Active/retired rows are history and stay."""
    claimed = await db.execute(
        "DELETE FROM prompt_overrides WHERE id = ? AND status = 'shadow'",
        (override_id,),
    )
    if claimed:
        return
    row = await get(override_id)
    if row is None:
        raise LookupError(f"unknown prompt override {override_id}")
    raise OverrideConflict(
        f"prompt override {override_id} is '{row['status']}', not shadow — "
        "history rows cannot be deleted (retire an active instead)"
    )


# ---- lifecycle (conditional claims) ---------------------------------------------

async def activate(override_id: int) -> dict[str, Any]:
    """shadow → active, atomically retiring the scope's previous active.

    One transaction: retire-old + claim-new commit (or roll back) together,
    so a lost claim can never leave the scope with zero or two actives. The
    conditional claim (``AND status = 'shadow'``) is the arbiter; the partial
    unique index is the cross-writer backstop.
    """
    now = bus.now_iso()
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT * FROM prompt_overrides WHERE id = ?", (override_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise LookupError(f"unknown prompt override {override_id}")
        row = dict(row)
        if row["status"] != "shadow":
            raise OverrideConflict(
                f"prompt override {override_id} is '{row['status']}', not shadow"
            )
        # belt: the registry may have tightened since the draft was recorded
        validate_content(row["scope"], row["content"])
        await conn.execute(
            "UPDATE prompt_overrides SET status = 'retired', retired_at = ? "
            "WHERE scope = ? AND status = 'active'",
            (now, row["scope"]),
        )
        cur = await conn.execute(
            "UPDATE prompt_overrides SET status = 'active', activated_at = ? "
            "WHERE id = ? AND status = 'shadow'",
            (now, override_id),
        )
        claimed = cur.rowcount
        await cur.close()
        if not claimed:  # unreachable in-process (write lock), kept for the idiom
            raise OverrideConflict(
                f"prompt override {override_id} changed concurrently; retry"
            )
    await refresh_cache()
    log.info("prompt override %s activated for scope %s", override_id, row["scope"])
    row = await get(override_id)
    assert row is not None
    return row


async def retire(override_id: int) -> dict[str, Any]:
    """active → retired: the scope falls back to its code default."""
    claimed = await db.execute(
        "UPDATE prompt_overrides SET status = 'retired', retired_at = ? "
        "WHERE id = ? AND status = 'active'",
        (bus.now_iso(), override_id),
    )
    if not claimed:
        row = await get(override_id)
        if row is None:
            raise LookupError(f"unknown prompt override {override_id}")
        raise OverrideConflict(
            f"prompt override {override_id} is '{row['status']}', not active"
        )
    await refresh_cache()
    row = await get(override_id)
    assert row is not None
    log.info("prompt override %s retired for scope %s", override_id, row["scope"])
    return row


# ---- operator overview -----------------------------------------------------------

async def scopes_overview() -> list[dict[str, Any]]:
    """Every registered scope with its code default and live override state
    (plus any stray DB scopes no longer in the registry — visible, inert)."""
    counts = await db.query(
        "SELECT scope, status, COUNT(*) AS n FROM prompt_overrides GROUP BY scope, status"
    )
    actives = {
        r["scope"]: r for r in await db.query(
            "SELECT id, scope, activated_at FROM prompt_overrides WHERE status = 'active'"
        )
    }
    by_scope: dict[str, dict[str, int]] = {}
    for c in counts:
        by_scope.setdefault(c["scope"], {})[c["status"]] = c["n"]

    out: list[dict[str, Any]] = []
    for scope in sorted(SCOPES):
        spec = SCOPES[scope]
        active = actives.get(scope)
        out.append({
            "scope": scope,
            "registered": True,
            "description": spec.description,
            "fields": list(spec.fields),
            "default": default_text(scope),
            "active_id": active["id"] if active else None,
            "activated_at": active["activated_at"] if active else None,
            "counts": {s: by_scope.get(scope, {}).get(s, 0) for s in STATUSES},
        })
    for scope in sorted(set(by_scope) - set(SCOPES)):
        active = actives.get(scope)
        out.append({
            "scope": scope,
            "registered": False,
            "description": "（未注册的 scope — 没有挂载点，不会生效）",
            "fields": [],
            "default": None,
            "active_id": active["id"] if active else None,
            "activated_at": active["activated_at"] if active else None,
            "counts": {s: by_scope.get(scope, {}).get(s, 0) for s in STATUSES},
        })
    return out
