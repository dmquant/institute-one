"""SQLite access layer.

One aiosqlite connection for the process (aiosqlite serializes statements on a
worker thread).  Helpers commit per call; multi-statement work uses
``transaction()`` which holds the write lock.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .config import get_settings

log = logging.getLogger("institute.db")

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


async def init() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    settings = get_settings()
    settings.ensure_dirs()
    c = await aiosqlite.connect(settings.db_path, isolation_level=None)
    try:
        c.row_factory = aiosqlite.Row
        await c.execute("PRAGMA journal_mode=WAL")
        await c.execute("PRAGMA busy_timeout=5000")
        await c.execute("PRAGMA foreign_keys=ON")
        await migrate(c)
    except BaseException:
        # a half-initialized connection must not leak into the module global:
        # a same-process retry of init() would short-circuit on `_conn is not
        # None` and hand out a connection that skipped (part of) migrate()
        try:
            await c.close()
        except Exception:  # noqa: BLE001 - don't mask the original failure
            log.exception("closing connection after failed init also failed")
        raise
    _conn = c
    return _conn


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db.init() has not been called")
    return _conn


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements.

    ``executescript`` issues an implicit COMMIT before running, which breaks
    the per-file "script + ledger row in ONE transaction" guarantee — so we
    execute statement by statement inside an explicit transaction instead.
    Accumulate lines until ``sqlite3.complete_statement`` says the buffer is a
    full statement (it understands quoted strings and comments, so a ';'
    inside either never splits early). Migration files contain no BEGIN/COMMIT
    of their own (asserted in tests).
    """
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            buf = ""
            if stmt and stmt != ";":
                statements.append(stmt)
    tail = buf.strip()
    if tail:  # unterminated final statement (no trailing ';')
        statements.append(tail)
    return statements


def _strip_leading_comments(stmt: str) -> str:
    """Drop leading whitespace / -- line comments / block comments.

    ``_split_statements`` keeps a statement's preceding comment block attached
    (harmless to execute), but statement-kind detection must look at the first
    real SQL token.
    """
    prev = None
    while prev != stmt:
        prev = stmt
        stmt = stmt.lstrip()
        if stmt.startswith("--"):
            nl = stmt.find("\n")
            stmt = "" if nl < 0 else stmt[nl + 1:]
        elif stmt.startswith("/*"):
            end = stmt.find("*/")
            stmt = "" if end < 0 else stmt[end + 2:]
    return stmt


# one identifier, quotes required to be BALANCED: "x", 'x', `x`, [x], or bare.
# Never a lone opening quote — an unbalanced-quote statement must fall through
# to SQLite and raise its own syntax error instead of being "recovered".
_IDENT = r"(?:\"([^\"]*)\"|'([^']*)'|`([^`]*)`|\[([^\]]*)\]|([A-Za-z_][A-Za-z0-9_$]*))"
_ADD_COLUMN_RE = re.compile(
    rf"^ALTER\s+TABLE\s+{_IDENT}\s+ADD\s+(?:COLUMN\s+)?{_IDENT}\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_CONSTRAINT_KEYWORD_RE = re.compile(
    r"\b(CONSTRAINT|NOT\s+NULL|NULL|DEFAULT|CHECK|REFERENCES|UNIQUE|PRIMARY|COLLATE|GENERATED|AS)\b",
    re.IGNORECASE,
)
_DEFAULT_VALUE_RE = re.compile(
    r"\bDEFAULT\s+(\((?:[^()]|\([^()]*\))*\)|'(?:[^']|'')*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
# column constraints PRAGMA table_info cannot verify — proving them needs the
# stored CREATE TABLE text (S4-P0-01); NOT NULL/NULL/DEFAULT are pragma-visible
_UNPROVABLE_CONSTRAINT_RE = re.compile(
    r"\b(CONSTRAINT|CHECK|REFERENCES|UNIQUE|PRIMARY|COLLATE|GENERATED|AS)\b",
    re.IGNORECASE,
)
# first bare token of a table-level (not column) definition entry
_TABLE_CONSTRAINT_STARTERS = frozenset({"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"})


class MigrationRecoveryError(RuntimeError):
    """An ADD COLUMN replay found the column with an INCOMPATIBLE definition."""


def _statement_body(stmt: str) -> str:
    """Trim anything after the terminating ';' (e.g. a trailing line comment).

    ``_split_statements`` keeps "ALTER ...;  -- note" as one chunk because
    complete_statement() is only True once the comment line ends. Splitting on
    ';' and re-testing completeness finds the real terminator without being
    fooled by semicolons inside string literals.
    """
    acc = ""
    for part in stmt.split(";")[:-1]:
        acc += part + ";"
        if sqlite3.complete_statement(acc):
            return acc
    return stmt


def _first_ident(groups: tuple[str | None, ...], offset: int) -> str | None:
    for i in range(offset, offset + 5):
        if groups[i] is not None:
            return groups[i]
    return None


def _norm_type(type_text: str) -> str:
    # whitespace-insensitive, case-insensitive: VARCHAR (30) == varchar(30)
    return "".join(type_text.split()).casefold()


def _decl_parts(decl: str) -> tuple[str, bool, str | None]:
    """(normalized type, notnull, DEFAULT text or None) from a declaration tail."""
    decl = decl.strip().rstrip(";").strip()
    m = _CONSTRAINT_KEYWORD_RE.search(decl)
    type_part = decl[: m.start()] if m else decl
    notnull = re.search(r"\bNOT\s+NULL\b", decl, re.IGNORECASE) is not None
    dm = _DEFAULT_VALUE_RE.search(decl)
    return _norm_type(type_part), notnull, dm.group(1) if dm else None


def _default_equal(declared: str | None, pragma_value: str | None) -> bool:
    if declared is None or pragma_value is None:
        return declared is None and pragma_value is None
    a, b = declared.strip(), pragma_value.strip()
    if a == b:
        return True
    # PRAGMA table_info reports parenthesized defaults without the outer parens
    if a.startswith("(") and a.endswith(")") and a[1:-1].strip() == b:
        return True
    if a.startswith("'") or b.startswith("'"):
        return False  # string literals compare exactly
    return a.casefold() == b.casefold()  # keywords/numbers: CURRENT_TIMESTAMP etc.


def _sql_span(text: str, i: int) -> tuple[str, int]:
    """Classify the span starting at ``text[i]`` -> (kind, end index).

    kind: 'quote' for ``'…'`` / ``"…"`` / `` `…` `` / ``[…]`` (doubled-quote
    escapes honored), 'comment' for ``--`` line and ``/* */`` block comments,
    'char' for one plain character. Unterminated spans run to end of text.
    """
    ch = text[i]
    if ch in ("'", '"', "`"):
        j = i + 1
        while j < len(text):
            if text[j] != ch:
                j += 1
            elif j + 1 < len(text) and text[j + 1] == ch:  # doubled -> escaped
                j += 2
            else:
                return "quote", j + 1
        return "quote", len(text)
    if ch == "[":
        end = text.find("]", i + 1)
        return "quote", len(text) if end < 0 else end + 1
    if text.startswith("--", i):
        end = text.find("\n", i)
        return "comment", len(text) if end < 0 else end + 1
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return "comment", len(text) if end < 0 else end + 2
    return "char", i + 1


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _norm_def(text: str) -> str:
    """Canonical form of a column definition for equality comparison:
    comments dropped, whitespace collapsed (kept as ONE space only where it
    separates two word characters), plain text casefolded; quoted spans
    (string literals / quoted identifiers) preserved verbatim."""
    out: list[str] = []
    gap = False
    i = 0
    while i < len(text):
        kind, j = _sql_span(text, i)
        if kind == "comment":
            gap = True
        elif kind == "quote":
            out.append(text[i:j])
            gap = False
        elif text[i].isspace():
            gap = True
        else:
            if gap and out and _is_word_char(out[-1][-1]) and _is_word_char(text[i]):
                out.append(" ")
            out.append(text[i].casefold())
            gap = False
        i = j
    return "".join(out)


def _table_column_defs(create_sql: str) -> dict[str, str] | None:
    """casefolded column name -> definition tail, parsed from a stored
    CREATE TABLE statement (``sqlite_master.sql``). Table-level constraints
    (PRIMARY KEY/UNIQUE/CHECK/FOREIGN KEY/CONSTRAINT entries) are ignored.
    Returns None when the text cannot be parsed (virtual tables, exotic
    syntax) — the caller must then treat column constraints as UNPROVEN."""
    i, body_start = 0, None
    while i < len(create_sql):
        kind, j = _sql_span(create_sql, i)
        if kind == "char" and create_sql[i] == "(":
            body_start = j
            break
        i = j
    if body_start is None:
        return None
    parts: list[str] = []
    depth, start, end = 0, body_start, None
    i = body_start
    while i < len(create_sql):
        kind, j = _sql_span(create_sql, i)
        if kind == "char":
            ch = create_sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(create_sql[start:i])
                start = j
        i = j
    if end is None:
        return None
    parts.append(create_sql[start:end])
    defs: dict[str, str] = {}
    for part in parts:
        head = _strip_leading_comments(part)
        m = re.match(_IDENT, head)
        if m is None:
            return None
        if m.group(5) is not None and m.group(5).upper() in _TABLE_CONSTRAINT_STARTERS:
            continue
        name = _first_ident(m.groups(), 0)
        if name is None:
            return None
        defs[name.casefold()] = head[m.end():]
    return defs


async def _skip_add_column(c: aiosqlite.Connection, stmt: str) -> bool:
    """True if ``stmt`` is an ADD COLUMN whose column already exists with a
    matching definition. Raises MigrationRecoveryError on a mismatch.

    Recovery path for ledgers written before migrations became atomic: a crash
    between the old ``executescript`` (auto-committed) and the ledger INSERT
    left the schema changed but the file unrecorded. Replaying is safe for the
    idempotent CREATE ... IF NOT EXISTS statements, but ALTER ADD COLUMN would
    abort with "duplicate column" and wedge boot — skip it ONLY when the
    existing column provably matches the declaration: type / NOT NULL /
    DEFAULT via PRAGMA table_info, plus the FULL declaration text (CHECK,
    REFERENCES, COLLATE, ... — invisible to that PRAGMA) against the stored
    CREATE TABLE in sqlite_master. SQLite extends the stored statement with
    an ADD COLUMN's declaration verbatim, so a genuine pre-atomic crash
    replay always compares equal after whitespace/case/comment normalization
    (S4-P0-01). A same-name column with a different or unprovable definition
    means schema drift, not a replay: fail loudly instead of silently
    recording the file as applied. Statements this parser can't understand
    (e.g. unbalanced quotes) fall through to SQLite for its own error.
    """
    m = _ADD_COLUMN_RE.match(_statement_body(_strip_leading_comments(stmt)))
    if m is None:
        return False
    groups = m.groups()
    table, column = _first_ident(groups, 0), _first_ident(groups, 5)
    decl = groups[10] or ""
    quoted_table = '"' + table.replace('"', '""') + '"'
    cur = await c.execute(f"PRAGMA table_info({quoted_table})")
    rows = await cur.fetchall()
    await cur.close()
    # SQLite identifiers are case-insensitive; Python comparison is not
    existing = next((r for r in rows if str(r[1]).casefold() == column.casefold()), None)
    if existing is None:
        return False

    want_type, want_notnull, want_default = _decl_parts(decl)
    have_type = _norm_type(str(existing[2] or ""))
    have_notnull = bool(existing[3])
    have_default = existing[4]
    if (
        want_type != have_type
        or want_notnull != have_notnull
        or not _default_equal(want_default, have_default)
    ):
        raise MigrationRecoveryError(
            f"column {table}.{column} already exists but does not match the migration: "
            f"existing (type={existing[2]!r}, notnull={have_notnull}, default={have_default!r}) "
            f"vs declared (type={want_type!r}, notnull={want_notnull}, default={want_default!r}). "
            "This is schema drift, not a crash replay — reconcile the table manually "
            "before recording the migration."
        )

    # PRAGMA table_info cannot see CHECK/REFERENCES/COLLATE/...: prove the
    # whole declaration against the stored CREATE TABLE before certifying
    decl_body = decl.strip().rstrip(";").strip()
    cur = await c.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ? COLLATE NOCASE",
        (table,),
    )
    master = await cur.fetchone()
    await cur.close()
    defs = _table_column_defs(str(master[0])) if master is not None and master[0] else None
    stored = defs.get(column.casefold()) if defs is not None else None
    if stored is not None:
        if _norm_def(stored) == _norm_def(decl_body):
            return True
        raise MigrationRecoveryError(
            f"column {table}.{column} already exists but its stored definition does "
            f"not match the migration: existing {stored.strip()!r} vs declared "
            f"{decl_body!r} (constraint text is compared against sqlite_master — "
            "PRAGMA table_info cannot see CHECK/REFERENCES). This is schema drift, "
            "not a crash replay — reconcile the table manually before recording "
            "the migration."
        )
    if _UNPROVABLE_CONSTRAINT_RE.search(decl_body):
        raise MigrationRecoveryError(
            f"column {table}.{column} already exists and its declaration carries "
            f"constraints PRAGMA table_info cannot verify ({decl_body!r}), but the "
            f"stored CREATE TABLE for {table!r} could not be parsed to prove them. "
            "Refusing to certify the replay: verify the column's constraints "
            "manually, then record the file with "
            "INSERT INTO schema_migrations (name) VALUES ('<file>.sql')."
        )
    return True


async def _recover_completed_tasks_rebuild(
    c: aiosqlite.Connection,
    migration_name: str,
    statements: list[str],
) -> bool:
    """Recover a lost ledger row for the historical 0028 table rebuild.

    0028 is the one intentionally non-additive migration in the chain: SQLite
    required rebuilding ``tasks`` to widen its status CHECK. Re-running that
    rebuild against a later schema would silently discard columns added by
    0039--0043 (and their data). When the current table already proves that
    0028's complete base definition is present, replay only its idempotent
    indexes and let ``migrate`` restore the ledger row. A partial/drifted
    overcommitted schema fails closed instead of falling through to DROP.
    """
    if migration_name != "0028_task_overcommitted.sql":
        return False

    expected_defs: dict[str, str] | None = None
    for stmt in statements:
        body = _strip_leading_comments(stmt)
        if re.match(
            r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?tasks_rebuild_0028\b",
            body,
            re.IGNORECASE,
        ):
            expected_defs = _table_column_defs(body)
            break
    if expected_defs is None:
        raise MigrationRecoveryError(
            "0028_task_overcommitted.sql no longer contains its expected rebuild table"
        )

    cur = await c.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None or not row[0]:
        return False
    current_defs = _table_column_defs(str(row[0]))
    if current_defs is None:
        return False

    expected_status = expected_defs.get("status")
    current_status = current_defs.get("status")
    if expected_status is None or current_status is None:
        return False
    expected_values = set(re.findall(r"'((?:[^']|'')*)'", expected_status))
    current_values = set(re.findall(r"'((?:[^']|'')*)'", current_status))
    if not expected_values.issubset(current_values):
        if "overcommitted" in current_values:
            raise MigrationRecoveryError(
                "tasks.status contains overcommitted but does not preserve the full "
                "0028 status contract; refusing the destructive rebuild replay"
            )
        return False  # normal first application: 0001's CHECK lacks overcommitted
    if _decl_parts(expected_status) != _decl_parts(current_status):
        raise MigrationRecoveryError(
            "tasks.status type/null/default drifted from 0028; refusing the "
            "destructive rebuild replay"
        )

    mismatched = [
        name
        for name, expected in expected_defs.items()
        if name != "status"
        and (
            name not in current_defs
            or _norm_def(current_defs[name]) != _norm_def(expected)
        )
    ]
    if mismatched:
        raise MigrationRecoveryError(
            "tasks already carries the 0028 status contract but its base columns "
            f"drifted ({', '.join(sorted(mismatched))}); refusing the destructive "
            "rebuild replay"
        )

    # A pre-atomic crash could have landed the table rename but missed one of
    # the trailing indexes. Re-run only those naturally idempotent statements.
    for stmt in statements:
        body = _strip_leading_comments(stmt)
        if re.match(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\b", body, re.IGNORECASE):
            await c.execute(stmt)
    log.warning(
        "migration %s: tasks already proves the completed rebuild; preserving "
        "later columns and restoring only indexes + ledger",
        migration_name,
    )
    return True


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


async def migrate(c: aiosqlite.Connection) -> None:
    await c.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {r["name"] for r in await (await c.execute("SELECT name FROM schema_migrations")).fetchall()}
    for path in sorted(_migrations_dir().glob("*.sql")):
        if path.name in applied:
            continue
        # one transaction per file: schema changes + the ledger row commit (or
        # roll back) together, so a crash can never leave a half-recorded file.
        # COMMIT sits INSIDE the protected block: a commit-stage failure
        # (SQLITE_BUSY, disk full, I/O error) must also roll back, or the
        # connection is left inside an open transaction and every retry dies
        # on "cannot start a transaction within a transaction" (REVIEW-B1 H1)
        await c.execute("BEGIN")
        try:
            statements = _split_statements(path.read_text(encoding="utf-8"))
            recovered_rebuild = await _recover_completed_tasks_rebuild(
                c, path.name, statements,
            )
            for stmt in [] if recovered_rebuild else statements:
                if await _skip_add_column(c, stmt):
                    log.warning(
                        "migration %s: column already exists, skipping %r "
                        "(pre-atomic crash recovery)", path.name, stmt.splitlines()[0],
                    )
                    continue
                await c.execute(stmt)
            await c.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))
            await c.execute("COMMIT")
        except BaseException:
            try:
                await c.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - some failures auto-roll-back and
                # leave no active transaction; never mask the original cause
                log.debug("rollback after failed migration %s was a no-op", path.name)
            log.error(
                "migration %s failed and was rolled back; fix the script or, if the "
                "schema objects already exist from a pre-atomic partial apply, record "
                "it manually: INSERT INTO schema_migrations (name) VALUES ('%s')",
                path.name, path.name,
            )
            raise


# ---- helpers -----------------------------------------------------------

async def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    cur = await conn().execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def query_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = await query(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: tuple | list = ()) -> int:
    """Run a write statement. Returns rowcount (useful for conditional claims)."""
    async with _write_lock:
        cur = await conn().execute(sql, params)
        await cur.close()
        return cur.rowcount


async def insert(sql: str, params: tuple | list = ()) -> int:
    """Run an INSERT. Returns lastrowid."""
    async with _write_lock:
        cur = await conn().execute(sql, params)
        await cur.close()
        return cur.lastrowid or 0


@asynccontextmanager
async def transaction():
    async with _write_lock:
        await conn().execute("BEGIN")
        try:
            yield conn()
        except BaseException:
            await conn().execute("ROLLBACK")
            raise
        else:
            await conn().execute("COMMIT")
