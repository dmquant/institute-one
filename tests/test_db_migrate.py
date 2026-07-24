"""Atomic migrations: per-file transaction (script + ledger row commit together),
statement splitting, and the pre-atomic crash-window recovery path (F1-6).

The old migrate() ran executescript(sql) — which auto-commits — and wrote the
schema_migrations row afterwards. A crash in between left the schema changed
but the file unrecorded; replaying 0005's ALTER TABLE then aborted with
"duplicate column name" and wedged boot.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from app import db
from app.config import get_settings

MIGRATIONS = sorted((Path(db.__file__).resolve().parent.parent / "migrations").glob("*.sql"))


# ---- split + hygiene of the real migration files ----------------------------

def test_real_migration_files_have_no_transaction_statements():
    """_split_statements wraps each file in ONE explicit transaction — a
    BEGIN/COMMIT/ROLLBACK (or ATTACH/VACUUM) inside a script would break it."""
    assert MIGRATIONS, "no migration files found"
    forbidden = ("BEGIN", "COMMIT", "ROLLBACK", "END", "ATTACH", "VACUUM")
    for path in MIGRATIONS:
        for stmt in db._split_statements(path.read_text(encoding="utf-8")):
            head = db._strip_leading_comments(stmt).split(None, 1)
            assert head, f"{path.name}: empty statement survived the split"
            assert head[0].upper() not in forbidden, f"{path.name}: {head[0]} in script"


def test_split_statements_matches_executescript_result():
    """Statement-by-statement replay builds the exact same schema as
    executescript over the whole chain (objects and columns identical)."""
    def schema(c: sqlite3.Connection) -> set[tuple]:
        return {
            tuple(r) for r in c.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }

    split_conn = sqlite3.connect(":memory:")
    script_conn = sqlite3.connect(":memory:")
    for path in MIGRATIONS:
        sql = path.read_text(encoding="utf-8")
        for stmt in db._split_statements(sql):
            split_conn.execute(stmt)
        script_conn.executescript(sql)
    assert schema(split_conn) == schema(script_conn)
    assert schema(split_conn)  # sanity: the chain actually built something


def test_0042_adopts_unambiguous_r4_children_across_crash_windows():
    """Upgrade compatibility: R4 stored lease/attempt but no child id.
    0042 conservatively binds the sole child created inside that lease window,
    for both marker+queued and completed-before-marker crash states."""
    c = sqlite3.connect(":memory:")
    for path in MIGRATIONS:
        if path.name >= "0042_":
            continue
        c.executescript(path.read_text(encoding="utf-8"))

    def source(source_id: str, *, marker: bool) -> None:
        c.execute(
            "INSERT INTO tasks "
            "(id, requested_hand, prompt, status, source, error, workspace_dir, "
            " timeout_s, fallback_chain, created_at, finished_at, "
            " revival_lease_id, revival_leased_at, revival_attempts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id, "echo", "retry", "rate_limited", "test",
                "quota\n[rate-limit-revival:claimed]" if marker else "quota",
                "/tmp/r5", 60, '["echo"]',
                "2026-07-20T00:00:00+00:00", "2026-07-20T00:00:01+00:00",
                f"lease-{source_id}", "2026-07-20T00:00:02+00:00", 1,
            ),
        )

    def child(child_id: str, source_id: str, status: str) -> None:
        c.execute(
            "INSERT INTO tasks "
            "(id, requested_hand, prompt, status, source, workspace_dir, timeout_s, "
            " fallback_chain, lineage_root, created_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                child_id, "echo", "retry", status, "test", "/tmp/r5", 60,
                '["echo"]', source_id, "2026-07-20T00:00:03+00:00",
                "2026-07-20T00:00:04+00:00" if status == "completed" else None,
            ),
        )

    source("legacy-queued", marker=True)
    child("legacy-child-q", "legacy-queued", "queued")
    source("legacy-complete", marker=False)
    child("legacy-child-c", "legacy-complete", "completed")

    migration = next(p for p in MIGRATIONS if p.name.startswith("0042_"))
    for stmt in db._split_statements(migration.read_text(encoding="utf-8")):
        c.execute(stmt)

    assert c.execute(
        "SELECT revival_task_id FROM tasks WHERE id='legacy-queued'"
    ).fetchone()[0] == "legacy-child-q"
    assert c.execute(
        "SELECT revived_from_task_id FROM tasks WHERE id='legacy-child-q'"
    ).fetchone()[0] == "legacy-queued"
    assert c.execute(
        "SELECT revival_task_id FROM tasks WHERE id='legacy-complete'"
    ).fetchone()[0] == "legacy-child-c"
    assert c.execute(
        "SELECT revived_from_task_id FROM tasks WHERE id='legacy-child-c'"
    ).fetchone()[0] == "legacy-complete"


def test_split_statements_semicolons_in_strings_and_comments():
    sql = (
        "-- comment; with semicolon\n"
        "CREATE TABLE a (x TEXT DEFAULT 'v;1');\n"
        "/* block; comment */\n"
        "INSERT INTO a VALUES ('b;c')"  # unterminated tail statement
    )
    stmts = db._split_statements(sql)
    assert len(stmts) == 2
    c = sqlite3.connect(":memory:")
    for s in stmts:
        c.execute(s)
    assert c.execute("SELECT x FROM a").fetchall() == [("b;c",)]


# ---- fresh-database chain (runs via conftest's db.init) ----------------------

async def test_fresh_db_applies_every_file_once():
    rows = await db.query("SELECT name FROM schema_migrations ORDER BY name")
    assert [r["name"] for r in rows] == [p.name for p in MIGRATIONS]
    # replay is a no-op, not an error
    await db.migrate(db.conn())
    rows2 = await db.query("SELECT name FROM schema_migrations ORDER BY name")
    assert rows2 == rows


# ---- pre-migration backup: pending files on a live DB snapshot first ---------

def _pre_migrate_backups() -> set[Path]:
    return set(get_settings().backups_dir.glob("pre-migrate-*.db"))


async def test_pending_migrations_on_live_db_write_pre_migrate_backup(tmp_path, monkeypatch):
    """A live DB (ledger table present) with >=1 pending file is first
    snapshotted to backups/pre-migrate-<timestamp>.db — once per migrate()
    call, not per file — and the snapshot predates the new schema."""
    before = _pre_migrate_backups()
    mig = tmp_path / "0001_premig_probe.sql"
    mig.write_text("CREATE TABLE pre_migrate_probe (x TEXT);\n", encoding="utf-8")
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    await db.migrate(db.conn())

    new = _pre_migrate_backups() - before
    assert len(new) == 1
    snapshot = new.pop()
    assert not Path(str(snapshot) + ".tmp").exists()  # no temp residue
    con = sqlite3.connect(snapshot)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # taken BEFORE the file applied: no probe table, no ledger row for it
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'pre_migrate_probe'"
        ).fetchone() is None
        assert con.execute(
            "SELECT 1 FROM schema_migrations WHERE name = '0001_premig_probe.sql'"
        ).fetchone() is None
    finally:
        con.close()
    assert await db.query_one(
        "SELECT 1 FROM sqlite_master WHERE name = 'pre_migrate_probe'"
    ) is not None


async def test_fresh_database_writes_no_pre_migrate_backup(tmp_path):
    """A brand-new database has no prior state to protect: applying the whole
    chain must NOT write a pre-migrate backup."""
    before = _pre_migrate_backups()
    c = await aiosqlite.connect(tmp_path / "fresh.db", isolation_level=None)
    try:
        c.row_factory = aiosqlite.Row
        await db.migrate(c)  # every file pending, but nothing to lose yet
        row = await (await c.execute("SELECT COUNT(*) AS n FROM schema_migrations")).fetchone()
        assert row["n"] == len(MIGRATIONS)
    finally:
        await c.close()
    assert _pre_migrate_backups() == before


async def test_up_to_date_database_writes_no_pre_migrate_backup():
    """Nothing pending -> no snapshot (the common every-boot path)."""
    before = _pre_migrate_backups()
    await db.migrate(db.conn())
    assert _pre_migrate_backups() == before


# ---- the F1-6 crash window: script committed, ledger row missing -------------

async def test_replay_after_lost_ledger_row_recovers_alter_table():
    """Simulate the pre-atomic crash: 0005 executed (work_date column exists)
    but its schema_migrations row is gone. Replaying migrate() must skip the
    duplicate ADD COLUMN, re-run the idempotent index, and restore the ledger
    row instead of wedging boot."""
    n = await db.execute(
        "DELETE FROM schema_migrations WHERE name = '0005_research_hardening.sql'"
    )
    assert n == 1

    await db.migrate(db.conn())  # old code: OperationalError: duplicate column name

    row = await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0005_research_hardening.sql'"
    )
    assert row is not None
    cols = [r["name"] for r in await db.query("PRAGMA table_info(research_log)")]
    assert cols.count("work_date") == 1


async def test_replay_after_lost_ledger_row_all_files():
    """The same recovery must hold for EVERY migration file (the rest are
    CREATE ... IF NOT EXISTS and thus naturally idempotent)."""
    await db.execute("DELETE FROM schema_migrations")
    await db.migrate(db.conn())
    rows = await db.query("SELECT name FROM schema_migrations ORDER BY name")
    assert [r["name"] for r in rows] == [p.name for p in MIGRATIONS]


async def test_replay_lost_0028_ledger_preserves_later_task_columns_and_data():
    """0028 rebuilds ``tasks`` to widen its status CHECK. If only its ledger
    row is lost after 0039--0043, replay must not DROP the later protocol
    columns (or their values) while those later files remain recorded."""
    await db.execute(
        "INSERT INTO tasks "
        "(id, prompt, status, source, created_at, revival_lease_id, "
        "revival_attempts, revival_task_id, mailbox_dispatch_id) "
        "VALUES ('rebuild-proof', 'p', 'rate_limited', 'test', "
        "'2026-07-21T00:00:00+00:00', 'lease-proof', 2, 'child-proof', 42)"
    )
    n = await db.execute(
        "DELETE FROM schema_migrations WHERE name = '0028_task_overcommitted.sql'"
    )
    assert n == 1

    await db.migrate(db.conn())

    row = await db.query_one(
        "SELECT revival_lease_id, revival_attempts, revival_task_id, "
        "mailbox_dispatch_id FROM tasks WHERE id = 'rebuild-proof'"
    )
    assert row == {
        "revival_lease_id": "lease-proof",
        "revival_attempts": 2,
        "revival_task_id": "child-proof",
        "mailbox_dispatch_id": 42,
    }
    assert await db.query_one(
        "SELECT name FROM schema_migrations "
        "WHERE name = '0028_task_overcommitted.sql'"
    ) is not None
    assert await db.query_one(
        "SELECT name FROM sqlite_master WHERE name = 'tasks_rebuild_0028'"
    ) is None


# ---- per-file atomicity: failure rolls back schema AND ledger ----------------

async def test_failed_migration_rolls_back_script_and_ledger(tmp_path, monkeypatch):
    good = tmp_path / "0001_good.sql"
    good.write_text("CREATE TABLE mig_probe_ok (x TEXT);\n", encoding="utf-8")
    bad = tmp_path / "0002_bad.sql"
    bad.write_text(
        "CREATE TABLE mig_probe_partial (x TEXT);\n"
        "INSERT INTO mig_probe_missing VALUES (1);\n",  # fails: no such table
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(sqlite3.OperationalError):
        await db.migrate(db.conn())

    # file 1 committed whole; file 2 rolled back whole — no half-applied schema
    names = {r["name"] for r in await db.query("SELECT name FROM schema_migrations")}
    assert "0001_good.sql" in names
    assert "0002_bad.sql" not in names
    master = {
        r["name"] for r in await db.query(
            "SELECT name FROM sqlite_master WHERE name LIKE 'mig_probe%'"
        )
    }
    assert master == {"mig_probe_ok"}

    # fixing the script and replaying picks up exactly where it left off
    bad.write_text("CREATE TABLE mig_probe_partial (x TEXT);\n", encoding="utf-8")
    await db.migrate(db.conn())
    names = {r["name"] for r in await db.query("SELECT name FROM schema_migrations")}
    assert "0002_bad.sql" in names


async def test_add_column_guard_only_skips_existing_columns(tmp_path, monkeypatch):
    """The recovery guard must not swallow legitimate new ALTERs: a genuinely
    new column is still added; only an already-present one is skipped."""
    await db.execute("CREATE TABLE probe_alter (a TEXT)")
    mig = tmp_path / "0001_alter.sql"
    mig.write_text(
        "ALTER TABLE probe_alter ADD COLUMN a TEXT;\n"  # exists -> skipped
        "ALTER TABLE probe_alter ADD COLUMN b TEXT;\n"  # new -> applied
        "-- leading comment\nALTER TABLE probe_alter ADD COLUMN b TEXT;\n",  # dup of b -> skipped
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    await db.migrate(db.conn())

    cols = [r["name"] for r in await db.query("PRAGMA table_info(probe_alter)")]
    assert cols == ["a", "b"]


# ---- REVIEW-B1 H1: a COMMIT-stage failure must also roll back -----------------

async def test_commit_failure_rolls_back_and_stays_retryable(tmp_path, monkeypatch):
    """Inject one synthetic COMMIT failure: the file must roll back whole (no
    schema, no half-recorded ledger row), the connection must exit the
    transaction (no 'cannot start a transaction within a transaction' on
    retry), and the retry must succeed."""
    mig = tmp_path / "0001_commit_probe.sql"
    mig.write_text("CREATE TABLE mig_commit_probe (x TEXT);\n", encoding="utf-8")
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    c = db.conn()
    real_execute = c.execute
    armed = {"on": True}

    async def flaky_execute(sql, *args, **kwargs):
        if armed["on"] and isinstance(sql, str) and sql.strip().upper() == "COMMIT":
            armed["on"] = False  # fail exactly once; ROLLBACK + retry pass through
            raise sqlite3.OperationalError("synthetic commit failure")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(c, "execute", flaky_execute)

    with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
        await db.migrate(c)

    assert c.in_transaction is False  # rolled back, not wedged mid-transaction
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_commit_probe.sql'"
    ) is None
    assert await db.query_one(
        "SELECT name FROM sqlite_master WHERE name = 'mig_commit_probe'"
    ) is None

    await db.migrate(c)  # the same connection is reusable and the retry lands
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_commit_probe.sql'"
    ) is not None
    assert await db.query_one(
        "SELECT name FROM sqlite_master WHERE name = 'mig_commit_probe'"
    ) is not None


async def test_init_failure_closes_and_clears_connection(monkeypatch):
    """A migrate() failure inside init() must not leak a half-initialized
    connection into the module global — a same-process retry would skip
    migrate() entirely on the `_conn is not None` fast path."""
    await db.close()
    assert db._conn is None

    async def boom(c):
        raise sqlite3.OperationalError("synthetic migrate failure")

    monkeypatch.setattr(db, "migrate", boom)
    with pytest.raises(sqlite3.OperationalError, match="synthetic migrate failure"):
        await db.init()
    assert db._conn is None  # closed and cleared, not half-initialized

    monkeypatch.undo()
    c = await db.init()  # clean retry fully re-initializes
    assert db._conn is c
    assert await db.query_one("SELECT name FROM schema_migrations LIMIT 1") is not None


# ---- REVIEW-B1 M2: the guard must reject drifted/garbled definitions ----------

async def test_add_column_guard_rejects_incompatible_existing_column(tmp_path, monkeypatch):
    """Review counterexample: an existing `mode INTEGER` must NOT satisfy a
    migration declaring `mode TEXT NOT NULL DEFAULT 'file' CHECK (...)` — that
    is schema drift, and silently recording the file would hide it."""
    await db.execute("CREATE TABLE probe_drift (id TEXT, mode INTEGER)")
    mig = tmp_path / "0001_drift.sql"
    mig.write_text(
        "ALTER TABLE probe_drift ADD COLUMN mode TEXT NOT NULL DEFAULT 'file' "
        "CHECK (mode IN ('file', 'region'));\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(db.MigrationRecoveryError, match="probe_drift.mode"):
        await db.migrate(db.conn())

    # nothing recorded, nothing changed
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_drift.sql'"
    ) is None
    cols = {r["name"]: r["type"] for r in await db.query("PRAGMA table_info(probe_drift)")}
    assert cols["mode"] == "INTEGER"


async def test_add_column_guard_matching_definition_variants_skip(tmp_path, monkeypatch):
    """Quoted identifiers, case differences and whitespace in the type must
    still be recognized as the SAME definition (SQLite is case-insensitive)."""
    await db.execute(
        "CREATE TABLE probe_match (a TEXT, "
        "WORK_DATE TEXT, "
        "mode TEXT NOT NULL DEFAULT 'file', "
        "sized VARCHAR(30))"
    )
    mig = tmp_path / "0001_match.sql"
    mig.write_text(
        'ALTER TABLE "probe_match" ADD COLUMN work_date text;\n'
        "ALTER TABLE probe_match ADD COLUMN [mode] TEXT NOT NULL DEFAULT 'file';\n"
        "ALTER TABLE probe_match ADD COLUMN sized varchar ( 30 );\n"
        "ALTER TABLE probe_match ADD COLUMN fresh TEXT;\n",  # genuinely new
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    await db.migrate(db.conn())  # all three duplicates skipped, fresh applied

    cols = [r["name"] for r in await db.query("PRAGMA table_info(probe_match)")]
    assert cols == ["a", "WORK_DATE", "mode", "sized", "fresh"]


async def test_add_column_guard_unbalanced_quotes_fall_through_to_sqlite(tmp_path, monkeypatch):
    """Review counterexample: `ALTER TABLE "probe ADD COLUMN a TEXT;` must not
    be 'recovered' by the guard — it reaches SQLite and raises its own syntax
    error, and nothing is recorded."""
    await db.execute("CREATE TABLE probe_quote (a TEXT)")
    mig = tmp_path / "0001_quote.sql"
    mig.write_text('ALTER TABLE "probe_quote ADD COLUMN a TEXT;\n', encoding="utf-8")
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(sqlite3.OperationalError):
        await db.migrate(db.conn())
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_quote.sql'"
    ) is None


# ---- S4-P0-01: CHECK/REFERENCES must be PROVEN, not assumed -------------------
# PRAGMA table_info cannot see them; the guard proves the full declaration
# against the stored CREATE TABLE in sqlite_master (which SQLite extends
# verbatim on ADD COLUMN) and refuses recovery when the proof fails.

async def test_replay_after_lost_ledger_row_recovers_check_column():
    """The audit's ready-made case: 0010 adds vault_index.mode WITH a CHECK.
    A pre-atomic crash replay must prove the CHECK against sqlite_master and
    recover — neither wedge boot nor blind-skip on the pragma-visible parts."""
    n = await db.execute(
        "DELETE FROM schema_migrations WHERE name = '0010_analyst_memory.sql'"
    )
    assert n == 1

    await db.migrate(db.conn())

    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0010_analyst_memory.sql'"
    ) is not None
    cols = [r["name"] for r in await db.query("PRAGMA table_info(vault_index)")]
    assert cols.count("mode") == 1
    # the CHECK is intact: the guard certified the column, nothing re-ran
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO vault_index (path, artifact_kind, artifact_id, sha256, "
            "written_at, mode) VALUES ('p.md','note','r1','x','2026-01-01T00:00:00Z','bogus')"
        )


async def test_add_column_guard_replays_check_and_references_columns(tmp_path, monkeypatch):
    """A genuine crash replay (script applied, ledger row lost) must still be
    certified for declarations carrying REFERENCES and multi-line CHECKs:
    sqlite_master holds the declaration verbatim, so the proof succeeds."""
    await db.execute("CREATE TABLE probe_ref_parent (id TEXT PRIMARY KEY)")
    await db.execute("CREATE TABLE probe_cons (a TEXT)")
    mig = tmp_path / "0001_cons.sql"
    mig.write_text(
        "ALTER TABLE probe_cons ADD COLUMN parent_id TEXT "
        "REFERENCES probe_ref_parent(id) ON DELETE SET NULL;\n"
        "ALTER TABLE probe_cons ADD COLUMN mode TEXT NOT NULL DEFAULT 'file'\n"
        "  CHECK (mode IN ('file', 'region'));\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)
    await db.migrate(db.conn())  # first apply: both columns are genuinely new

    n = await db.execute("DELETE FROM schema_migrations WHERE name = '0001_cons.sql'")
    assert n == 1
    await db.migrate(db.conn())  # the replay certifies both duplicates

    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_cons.sql'"
    ) is not None
    cols = [r["name"] for r in await db.query("PRAGMA table_info(probe_cons)")]
    assert cols == ["a", "parent_id", "mode"]


async def test_add_column_guard_rejects_missing_declared_check(tmp_path, monkeypatch):
    """The S4-P0-01 counterexample: type/NOT NULL/DEFAULT all match via PRAGMA
    but the existing column LACKS the declared CHECK — certifying that as a
    replay would silently drop a constraint. Must refuse, record nothing."""
    await db.execute(
        "CREATE TABLE probe_nocheck (id TEXT, mode TEXT NOT NULL DEFAULT 'file')"
    )
    mig = tmp_path / "0001_nocheck.sql"
    mig.write_text(
        "ALTER TABLE probe_nocheck ADD COLUMN mode TEXT NOT NULL DEFAULT 'file' "
        "CHECK (mode IN ('file', 'region'));\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(db.MigrationRecoveryError, match="probe_nocheck.mode"):
        await db.migrate(db.conn())
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_nocheck.sql'"
    ) is None


async def test_add_column_guard_rejects_check_text_drift(tmp_path, monkeypatch):
    """An existing CHECK whose text differs from the declaration is drift."""
    await db.execute(
        "CREATE TABLE probe_drift_check (mode TEXT NOT NULL DEFAULT 'file' "
        "CHECK (mode IN ('file')))"
    )
    mig = tmp_path / "0001_drift_check.sql"
    mig.write_text(
        "ALTER TABLE probe_drift_check ADD COLUMN mode TEXT NOT NULL DEFAULT 'file' "
        "CHECK (mode IN ('file', 'region'));\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(db.MigrationRecoveryError, match="probe_drift_check.mode"):
        await db.migrate(db.conn())
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_drift_check.sql'"
    ) is None


async def test_add_column_guard_rejects_references_target_drift(tmp_path, monkeypatch):
    """A foreign key pointing at a DIFFERENT table than declared is drift even
    though PRAGMA table_info reports identical type/notnull/default."""
    await db.execute("CREATE TABLE probe_parent_a (id TEXT PRIMARY KEY)")
    await db.execute("CREATE TABLE probe_parent_b (id TEXT PRIMARY KEY)")
    await db.execute(
        "CREATE TABLE probe_fk_drift (x TEXT REFERENCES probe_parent_b(id))"
    )
    mig = tmp_path / "0001_fk_drift.sql"
    mig.write_text(
        "ALTER TABLE probe_fk_drift ADD COLUMN x TEXT REFERENCES probe_parent_a(id);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(db.MigrationRecoveryError, match="probe_fk_drift.x"):
        await db.migrate(db.conn())
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_fk_drift.sql'"
    ) is None


async def test_add_column_guard_rejects_undeclared_existing_constraints(tmp_path, monkeypatch):
    """The reverse hole: the existing column carries a CHECK the declaration
    never made. PRAGMA compares equal; the full-text proof must refuse."""
    await db.execute(
        "CREATE TABLE probe_extra (mode TEXT NOT NULL DEFAULT 'file' CHECK (mode != 'x'))"
    )
    mig = tmp_path / "0001_extra.sql"
    mig.write_text(
        "ALTER TABLE probe_extra ADD COLUMN mode TEXT NOT NULL DEFAULT 'file';\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "_migrations_dir", lambda: tmp_path)

    with pytest.raises(db.MigrationRecoveryError, match="probe_extra.mode"):
        await db.migrate(db.conn())
    assert await db.query_one(
        "SELECT name FROM schema_migrations WHERE name = '0001_extra.sql'"
    ) is None


# ---- connection pragmas -------------------------------------------------------

async def test_connection_pragmas_are_wal_and_synchronous_normal():
    """Boot pragmas: WAL for concurrent reads under the single-writer lock, and
    synchronous=NORMAL — commits skip the per-transaction fsync; durability is
    bounded by the events table plus the nightly VACUUM INTO backup instead."""
    row = await db.query_one("PRAGMA journal_mode")
    assert row["journal_mode"] == "wal"
    row = await db.query_one("PRAGMA synchronous")
    assert row["synchronous"] == 1  # NORMAL (0=OFF, 1=NORMAL, 2=FULL)
