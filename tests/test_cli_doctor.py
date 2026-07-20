"""institute CLI + doctor (agent C6's partition, ROADMAP Phase 8).

Each doctor check is a plain synchronous function tested in isolation against
damaged tmp-home states (orphan rows, migration gaps, broken rate_limits.json,
vault ledger drift). Doctor is strictly read-only: the vault scan runs over a
read-only SQLite connection (no ``app.db`` involvement), so it is tested
directly inside the test's event loop, and a dedicated zero-write test
snapshots home + vault trees around a full subprocess doctor run. The one
remaining async bridge (ollama health) goes through ``_run_async``, which is
event-loop-safe and exercised from inside a running loop here (REVIEW-C6 M2).

Nothing here talks to the production server: every probe targets a freshly
allocated free port, and hand checks never send a prompt (CLI hands are
disabled by conftest; auth-probe tests use fake shell scripts, echo is
built-in).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest

from app import bus, cli, db
from app.config import get_settings

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- server probe / status ----------------------------------------------------

async def test_probe_server_offline(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "port", _free_port())
    (settings.home_dir / "server.pid").unlink(missing_ok=True)
    probe = cli.probe_server(settings)
    assert probe.pid is None
    assert not probe.pid_alive and not probe.pid_is_uvicorn
    assert not probe.port_open
    assert probe.health is None and not probe.up


async def test_probe_server_stale_pidfile(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "port", _free_port())
    pidfile = settings.home_dir / "server.pid"
    # spawn-and-reap a real pid so it is guaranteed dead
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    pidfile.write_text(str(dead.pid), encoding="utf-8")
    try:
        probe = cli.probe_server(settings)
        assert probe.pid == dead.pid
        # the pid may be recycled by another process, but it can never be our uvicorn
        assert not probe.pid_is_uvicorn
    finally:
        pidfile.unlink(missing_ok=True)


def test_cmd_status_offline_exit_code(monkeypatch, capsys):
    settings = get_settings()
    monkeypatch.setattr(settings, "port", _free_port())
    (settings.home_dir / "server.pid").unlink(missing_ok=True)
    rc = cli.cmd_status(settings)
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT RUNNING" in out
    assert "/health: unreachable" in out


def test_main_wires_status(monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "port", _free_port())
    (get_settings().home_dir / "server.pid").unlink(missing_ok=True)
    assert cli.main(["status"]) == 1
    assert "server:" in capsys.readouterr().out


# ---- hands ---------------------------------------------------------------------

async def test_check_hands_echo_pass_and_disabled_skip():
    c = cli.check_hands(get_settings())
    assert c.status == cli.PASS  # default + research hands are echo in tests
    assert any("echo: ok" in line for line in c.lines)
    # conftest disables every real CLI hand -> config SKIP, no binary probing
    assert any("claude" in line and "disabled by config" in line for line in c.lines)


async def test_check_hands_fails_when_default_hand_unavailable(monkeypatch):
    monkeypatch.setattr(get_settings(), "default_hand", "claude")  # disabled in tests
    c = cli.check_hands(get_settings())
    assert c.status == cli.FAIL
    assert any("default hand" in line for line in c.lines)


async def test_check_hands_fails_when_research_chain_dead(monkeypatch):
    monkeypatch.setattr(get_settings(), "research_hands", "claude,codex")  # both disabled
    c = cli.check_hands(get_settings())
    assert c.status == cli.FAIL
    assert any("research hand" in line for line in c.lines)


# ---- hands: real auth probes against fake CLI binaries (REVIEW-C6 M1) ------------

def _fake_cli(tmp_path: Path, name: str, auth_exit: int | None) -> str:
    """A fake CLI honoring --version and (optionally) its auth-status command.

    auth_exit None = the binary knows no status command (unknown-auth CLIs).
    """
    lines = [
        "#!/bin/sh",
        'if [ "$1" = "--version" ]; then echo "fake 9.9.9"; exit 0; fi',
    ]
    if auth_exit is not None:
        probe = cli.AUTH_PROBES[name]
        cond = " ] && [ ".join(f'"${i + 1}" = "{arg}"' for i, arg in enumerate(probe))
        lines.append(f"if [ {cond} ]; then exit {auth_exit}; fi")
    lines.append("exit 64")
    script = tmp_path / f"fake-{name}"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _wire_fake_cli(monkeypatch, name: str, path: str) -> None:
    """Point both the doctor's and the hand module's resolver at the fake."""
    import app.hands.base as hands_base

    def resolver(n: str) -> str | None:
        return path if n == name else None

    monkeypatch.setattr(hands_base, "resolve_cli_path", resolver)
    monkeypatch.setattr(f"app.hands.{name}_hand.resolve_cli_path", resolver, raising=False)
    monkeypatch.setattr(hands_base, "get_cli_env", lambda: dict(os.environ))
    monkeypatch.setattr(get_settings(), f"enable_{name}", True)


async def test_check_hands_auth_probe_logged_in(monkeypatch, tmp_path):
    _wire_fake_cli(monkeypatch, "claude", _fake_cli(tmp_path, "claude", auth_exit=0))
    c = cli.check_hands(get_settings())
    line = next(l for l in c.lines if l.startswith("- claude:"))
    assert "ok" in line and "authenticated" in line
    assert c.status == cli.PASS  # echo default + authenticated claude, nothing to warn


async def test_check_hands_auth_probe_logged_out_fails_default(monkeypatch, tmp_path):
    """--version succeeding must NOT make a logged-out default hand pass —
    the exact false-PASS the review flagged."""
    _wire_fake_cli(monkeypatch, "claude", _fake_cli(tmp_path, "claude", auth_exit=1))
    monkeypatch.setattr(get_settings(), "default_hand", "claude")
    c = cli.check_hands(get_settings())
    line = next(l for l in c.lines if l.startswith("- claude:"))
    assert "not logged in" in line
    assert c.status == cli.FAIL
    assert any("default hand 'claude' is not usable" in l for l in c.lines)


async def test_check_hands_auth_unknown_is_warn_not_ok(monkeypatch, tmp_path):
    """A CLI with no reliable status command reports auth unknown (WARN)."""
    assert cli.AUTH_PROBES["gemini"] is None
    _wire_fake_cli(monkeypatch, "gemini", _fake_cli(tmp_path, "gemini", auth_exit=None))
    c = cli.check_hands(get_settings())
    line = next(l for l in c.lines if l.startswith("- gemini:"))
    assert "WARN" in line and "auth unknown" in line
    assert c.status == cli.WARN


async def test_check_hands_auth_unknown_default_warns_not_fails(monkeypatch, tmp_path):
    _wire_fake_cli(monkeypatch, "gemini", _fake_cli(tmp_path, "gemini", auth_exit=None))
    monkeypatch.setattr(get_settings(), "default_hand", "gemini")
    c = cli.check_hands(get_settings())
    assert c.status == cli.WARN  # unverifiable, but not provably broken
    assert any("auth could not be verified" in l for l in c.lines)


# ---- database ------------------------------------------------------------------

async def test_check_db_clean():
    c, pending = cli.check_db(get_settings())
    assert c.status == cli.PASS
    assert pending == []
    assert "integrity ok" in c.detail


async def test_check_db_migration_gap_warns():
    row = await db.query_one("SELECT name FROM schema_migrations ORDER BY name LIMIT 1")
    await db.execute("DELETE FROM schema_migrations WHERE name = ?", (row["name"],))
    c, pending = cli.check_db(get_settings())
    assert c.status == cli.WARN
    assert pending == [row["name"]]
    assert any(row["name"] in line for line in c.lines)


async def test_check_db_ghost_migration_fails():
    await db.execute("INSERT INTO schema_migrations (name) VALUES ('9999_ghost.sql')")
    c, _ = cli.check_db(get_settings())
    assert c.status == cli.FAIL
    assert any("9999_ghost.sql" in line for line in c.lines)


async def test_check_db_missing_file_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "home", tmp_path / "empty-home")
    c, pending = cli.check_db(get_settings())
    assert c.status == cli.WARN
    assert "no database" in c.detail
    assert pending  # every migration file counts as pending


# ---- vault (read-only ledger scan, REVIEW-C6 H1) ---------------------------------

async def test_check_vault_gates(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vault_dir", None)
    assert cli.check_vault(settings).status == cli.SKIP  # unconfigured gate
    monkeypatch.undo()

    monkeypatch.setattr(settings, "home", settings.home / "nonexistent")
    assert cli.check_vault(settings).status == cli.SKIP  # no-database gate


async def _seed_ledger_row(path: str, sha: str, state: str = "clean", mode: str = "file") -> None:
    await db.execute(
        "INSERT INTO vault_index (path, artifact_kind, artifact_id, sha256, state, written_at, mode) "
        "VALUES (?,?,?,?,?,?,?)",
        (path, "briefing", "run1", sha, state, bus.now_iso(), mode),
    )


async def test_check_vault_readonly_scan_classifies_ledger_rows():
    """clean / missing / drifted / conflict all counted — no app.db, no writes.

    Runs INSIDE the test's event loop: the scan is synchronous and read-only,
    which is itself part of the H1 fix (the old bridge required a subprocess).
    """
    settings = get_settings()
    root = settings.vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)

    clean_body = "# ok\n"
    (root / "clean.md").write_text(clean_body, encoding="utf-8")
    await _seed_ledger_row("clean.md", hashlib.sha256(clean_body.encode()).hexdigest())

    await _seed_ledger_row("missing.md", "0" * 64)  # ledger row, no file

    (root / "drifted.md").write_text("human edited this\n", encoding="utf-8")
    await _seed_ledger_row("drifted.md", "1" * 64)

    (root / "conflict.md").write_text("whatever\n", encoding="utf-8")
    await _seed_ledger_row("conflict.md", "2" * 64, state="conflict")

    region_text = "---\nmanaged: institute\n---\n%% institute:begin %%\nbody\n%% institute:end %%\n"
    (root / "region.md").write_text(region_text, encoding="utf-8")
    await _seed_ledger_row(
        "region.md", hashlib.sha256(b"edited-away").hexdigest(), mode="region"
    )  # region hash mismatch -> drifted

    c = cli.check_vault(settings)
    assert c.status == cli.WARN
    assert "5 ledger rows" in c.detail
    assert "1 clean" in c.detail
    assert "1 conflict" in c.detail
    assert "1 missing" in c.detail
    assert "2 drifted" in c.detail


async def test_check_vault_empty_ledger_passes():
    c = cli.check_vault(get_settings())
    assert c.status == cli.PASS
    assert "0 ledger rows" in c.detail


# ---- asyncio bridge safety (REVIEW-C6 M2) -----------------------------------------

async def test_run_async_works_inside_running_loop():
    import asyncio

    async def sample() -> int:
        await asyncio.sleep(0)
        return 42

    assert cli._run_async(sample()) == 42


def test_run_async_works_without_loop():
    import asyncio

    async def sample() -> str:
        await asyncio.sleep(0)
        return "no-loop"

    assert cli._run_async(sample()) == "no-loop"


async def test_cmd_doctor_runs_inside_event_loop(monkeypatch, capsys):
    """The whole doctor must survive being called from async code — the old
    asyncio.run() bridges raised RuntimeError here (REVIEW-C6 M2)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "port", _free_port())
    # enable the http hand against a dead local port: exercises the real
    # _run_async(health_check()) bridge without needing an ollama install
    monkeypatch.setattr(settings, "enable_ollama", True)
    monkeypatch.setattr(settings, "ollama_host", f"http://127.0.0.1:{_free_port()}")
    settings.rate_limits_path.unlink(missing_ok=True)
    rc = cli.cmd_doctor(settings)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "- ollama: WARN (enabled but endpoint not responding)" in out
    assert "[PASS] vault" in out
    assert "summary:" in out


async def test_cmd_doctor_isolates_a_crashing_check(monkeypatch, capsys):
    """One crashing check becomes its own FAIL line; the rest still report
    (REVIEW-C6 M3)."""
    def boom(settings):
        raise TypeError("synthetic crash")

    monkeypatch.setattr(cli, "check_cron", boom)
    monkeypatch.setattr(get_settings(), "port", _free_port())
    rc = cli.cmd_doctor(get_settings())
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] cron: check crashed: TypeError: synthetic crash" in out
    assert "[PASS] disk" in out       # later checks still ran
    assert "summary:" in out


# ---- cron ------------------------------------------------------------------------

async def test_check_cron_empty_is_pass():
    c = cli.check_cron(get_settings())
    assert c.status == cli.PASS
    assert "no cron activity" in c.detail


async def test_check_cron_recent_failure_warns():
    await db.execute(
        "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok, error) VALUES (?,?,?,?,?)",
        ("briefing", bus.now_iso(), 12, 0, "boom"),
    )
    c = cli.check_cron(get_settings())
    assert c.status == cli.WARN
    assert any("briefing" in line and "FAILED" in line for line in c.lines)


async def test_check_cron_healthy_last_run_passes():
    await db.execute(
        "INSERT INTO cron_metrics (job, fired_at, duration_ms, ok) VALUES (?,?,?,?)",
        ("janitor", bus.now_iso(), 5, 1),
    )
    c = cli.check_cron(get_settings())
    assert c.status == cli.PASS
    assert any("janitor: last ok" in line for line in c.lines)


# ---- orphans ---------------------------------------------------------------------

async def _seed_orphan_task() -> None:
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, created_at) "
        "VALUES ('orph1','echo','x','running','api',?)",
        (bus.now_iso(),),
    )


async def test_check_orphans_clean():
    c = cli.check_orphans(get_settings(), server_up=False)
    assert c.status == cli.PASS


async def test_check_orphans_residue_with_server_down_warns():
    await _seed_orphan_task()
    await db.execute(
        "INSERT INTO research_queue (id, topic, status, created_at) "
        "VALUES ('rq1','NVDA','running',?)",
        (bus.now_iso(),),
    )
    c = cli.check_orphans(get_settings(), server_up=False)
    assert c.status == cli.WARN
    assert "tasks queued/running: 1" in c.detail
    assert "research running: 1" in c.detail


async def test_check_orphans_residue_with_server_up_is_live_work():
    await _seed_orphan_task()
    c = cli.check_orphans(get_settings(), server_up=True)
    assert c.status == cli.PASS
    assert "live work" in c.detail


# ---- rate_limits.json --------------------------------------------------------------

async def test_check_rate_limits_absent_is_pass():
    get_settings().rate_limits_path.unlink(missing_ok=True)
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.PASS


async def test_check_rate_limits_garbage_fails():
    get_settings().rate_limits_path.write_text("{not json", encoding="utf-8")
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.FAIL
    assert "unparseable" in c.detail


async def test_check_rate_limits_wrong_shape_fails():
    get_settings().rate_limits_path.write_text("[1, 2]", encoding="utf-8")
    assert cli.check_rate_limits(get_settings()).status == cli.FAIL


async def test_check_rate_limits_counts_active_cooldowns():
    payload = {
        "claude": {"until": time.time() + 3600, "reason": "quota_exhausted", "marked_at": time.time()},
        "codex": {"until": time.time() - 10, "reason": "rate_limit", "marked_at": time.time()},
    }
    get_settings().rate_limits_path.write_text(json.dumps(payload), encoding="utf-8")
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.PASS
    assert "1 active cooldown(s) of 2 recorded" in c.detail
    assert any("claude" in line for line in c.lines)


async def test_check_rate_limits_bad_nested_types_fail_without_raising():
    """Legal JSON, broken structure (REVIEW-C6 M3): the old code raised
    TypeError comparing str > float and killed the whole doctor."""
    path = get_settings().rate_limits_path

    path.write_text(json.dumps({"claude": {"until": "tomorrow"}}), encoding="utf-8")
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.FAIL
    assert any("claude" in l and "until is str" in l for l in c.lines)

    path.write_text(json.dumps({"codex": 42}), encoding="utf-8")
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.FAIL
    assert any("codex" in l and "expected object" in l for l in c.lines)

    # json.loads accepts bare Infinity; isfinite must reject it before
    # datetime.fromtimestamp can blow up
    path.write_text('{"gemini": {"until": Infinity}}', encoding="utf-8")
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.FAIL
    assert any("gemini" in l and "non-finite" in l for l in c.lines)


async def test_check_rate_limits_mixed_good_and_bad_entries_fail_and_name_the_bad():
    payload = {
        "claude": {"until": time.time() + 3600, "reason": "quota_exhausted"},
        "codex": {"until": None},
    }
    get_settings().rate_limits_path.write_text(json.dumps(payload), encoding="utf-8")
    c = cli.check_rate_limits(get_settings())
    assert c.status == cli.FAIL
    assert "1 malformed entry of 2" in c.detail
    assert any(l.startswith("- codex:") for l in c.lines)
    assert not any(l.startswith("- claude:") for l in c.lines)  # good entry not blamed


# ---- disk ---------------------------------------------------------------------------

_Usage = namedtuple("_Usage", "total used free")


async def test_check_disk_thresholds(monkeypatch):
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda p: _Usage(10 * 2**30, 10 * 2**30, 2**29))
    assert cli.check_disk(get_settings()).status == cli.FAIL
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda p: _Usage(10 * 2**30, 7 * 2**30, 3 * 2**30))
    assert cli.check_disk(get_settings()).status == cli.WARN
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda p: _Usage(100 * 2**30, 50 * 2**30, 50 * 2**30))
    assert cli.check_disk(get_settings()).status == cli.PASS


# ---- full doctor via subprocess (operator-shaped end-to-end run) -----------------

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, INSTITUTE_PORT=str(_free_port()))
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        capture_output=True, text=True, timeout=120, cwd=REPO, env=env,
    )


def test_doctor_subprocess_healthy_home_exits_zero():
    get_settings().rate_limits_path.unlink(missing_ok=True)
    r = _run_cli("doctor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[PASS] database" in r.stdout
    assert "[PASS] hands" in r.stdout
    assert "[PASS] vault" in r.stdout  # read-only ledger scan, empty ledger
    assert "server: not running" in r.stdout
    assert "summary:" in r.stdout and "0 fail" in r.stdout


def test_doctor_subprocess_broken_rate_limits_exits_nonzero():
    get_settings().rate_limits_path.write_text("{broken", encoding="utf-8")
    try:
        r = _run_cli("doctor")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "[FAIL] rate_limits" in r.stdout
    finally:
        get_settings().rate_limits_path.unlink(missing_ok=True)


def _tree_snapshot(*roots: Path) -> dict[str, str]:
    """path -> sha256 for every regular file under the given roots.

    SQLite ``-shm``/``-wal`` sidecars are excluded: a read-only connection to
    a WAL database legitimately (re)creates them, but can never alter the
    database or any other file — which is exactly what this snapshot pins.
    """
    snap: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith(("-shm", "-wal")):
                snap[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


async def test_doctor_subprocess_never_writes():
    """The hard H1 guarantee: a full doctor run (hands, db, vault scan over
    real ledger rows including a missing file, cron, orphans, limits, disk)
    changes NOTHING byte-for-byte under home + vault."""
    settings = get_settings()
    root = settings.vault_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    (root / "note.md").write_text("# hi\n", encoding="utf-8")
    await _seed_ledger_row("note.md", hashlib.sha256(b"# hi\n").hexdigest())
    await _seed_ledger_row("gone.md", "0" * 64)  # missing file -> vault WARN path
    settings.rate_limits_path.write_text(
        json.dumps({"claude": {"until": time.time() + 60, "reason": "quota_exhausted"}}),
        encoding="utf-8",
    )
    try:
        before = _tree_snapshot(settings.home_dir, root)
        r = _run_cli("doctor")
        after = _tree_snapshot(settings.home_dir, root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[WARN] vault" in r.stdout  # the scan really ran over the rows
        assert "1 missing" in r.stdout
        diff = sorted(set(before.items()) ^ set(after.items()))
        assert after == before, f"doctor changed files: {diff[:20]!r}"
    finally:
        settings.rate_limits_path.unlink(missing_ok=True)


# ---- packaging / scripts smoke -------------------------------------------------------

def test_console_script_declared():
    import tomllib

    with open(REPO / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["scripts"]["institute"] == "app.cli:main"


SCRIPTS = ("start.sh", "stop.sh", "install-service.sh", "uninstall-service.sh")


def test_scripts_exist_and_bash_syntax_ok():
    for name in SCRIPTS:
        path = REPO / "scripts" / name
        assert path.is_file(), f"missing scripts/{name}"
        assert os.access(path, os.X_OK), f"scripts/{name} is not executable"
        r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, f"bash -n {name}: {r.stderr}"


def test_stop_sh_has_no_broad_pkill():
    text = (REPO / "scripts" / "stop.sh").read_text(encoding="utf-8")
    # the ROADMAP-flagged hazard: a pattern kill that could hit unrelated
    # processes — must not survive in any executable (non-comment) line
    code_lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    assert not any("pkill" in l for l in code_lines)
    assert "kill -9" in text  # bounded escalation on the single pidfile pid


def test_plist_template_lints():
    template = REPO / "scripts" / "com.institute-one.server.plist.template"
    assert template.is_file()
    for key in ("KeepAlive", "RunAtLoad", "WorkingDirectory", "EnvironmentVariables",
                "ProcessType", "StandardOutPath", "StandardErrorPath"):
        assert key in template.read_text(encoding="utf-8")
    if shutil.which("plutil") is None:
        pytest.skip("plutil unavailable")
    r = subprocess.run(["plutil", "-lint", str(template)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_install_service_renders_plist_without_touching_launchctl(tmp_path):
    """Default mode: render + lint + print instructions, NO launchctl calls."""
    env = dict(os.environ, HOME=str(tmp_path), INSTITUTE_PORT="8123")
    r = subprocess.run(
        ["bash", str(REPO / "scripts" / "install-service.sh")],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    plist = tmp_path / "Library" / "LaunchAgents" / "com.institute-one.server.plist"
    assert plist.is_file()
    content = plist.read_text(encoding="utf-8")
    assert "{{" not in content, "unrendered placeholder left in plist"
    assert str(REPO) in content            # WorkingDirectory + venv path
    assert "<string>8123</string>" in content
    assert "launchd.err.log" in content
    # instructions printed, nothing activated
    assert "launchctl bootstrap" in r.stdout
    assert "uninstall-service.sh" in r.stdout
    if shutil.which("plutil"):
        lint = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout


# ---- uninstall/install against a fake launchctl (REVIEW-C6 M5 / L2) ---------------
# Real launchd is never touched: a fake launchctl script is prepended to PATH
# and HOME points at a tmp tree, so only the fake sees the calls.

def _fake_launchctl_env(tmp_path: Path, body: str, *, with_plist: bool = True):
    """(env, plist_path) with a scripted fake launchctl first on PATH."""
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / "com.institute-one.server.plist"
    if with_plist:
        plist.write_text("<plist/>", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "launchctl"
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    return env, plist


def _run_uninstall(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "uninstall-service.sh")],
        capture_output=True, text=True, timeout=30, env=env,
    )


def test_uninstall_bootout_success_removes_plist(tmp_path):
    env, plist = _fake_launchctl_env(
        tmp_path, '#!/bin/sh\ncase "$1" in print|bootout) exit 0;; esac\nexit 1\n'
    )
    r = _run_uninstall(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "booted out" in r.stdout
    assert not plist.exists()


def test_uninstall_legacy_unload_fallback_removes_plist(tmp_path):
    env, plist = _fake_launchctl_env(
        tmp_path, '#!/bin/sh\ncase "$1" in print|unload) exit 0;; esac\nexit 1\n'
    )
    r = _run_uninstall(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "legacy" in r.stdout
    assert not plist.exists()


def test_uninstall_failure_keeps_plist_and_exits_nonzero(tmp_path):
    """REVIEW-C6 M5: a loaded job that cannot be booted out must NOT lose its
    on-disk plist, and the script must not report success."""
    env, plist = _fake_launchctl_env(
        tmp_path, '#!/bin/sh\ncase "$1" in print) exit 0;; esac\nexit 1\n'
    )
    r = _run_uninstall(env)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "keeping" in r.stderr
    assert plist.exists(), "plist removed while the job is still loaded"


def test_uninstall_not_loaded_still_removes_plist(tmp_path):
    env, plist = _fake_launchctl_env(tmp_path, "#!/bin/sh\nexit 1\n")  # print fails
    r = _run_uninstall(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not loaded" in r.stdout
    assert not plist.exists()


def test_install_activate_reports_enable_failure(tmp_path):
    """REVIEW-C6 L2: bootstrap ok + enable failing must not print the combined
    'bootstrapped + enabled' success line."""
    env, _ = _fake_launchctl_env(
        tmp_path,
        '#!/bin/sh\ncase "$1" in bootstrap) exit 0;; enable) exit 1;; esac\nexit 1\n',
        with_plist=False,
    )
    env["INSTITUTE_PORT"] = "8124"
    r = subprocess.run(
        ["bash", str(REPO / "scripts" / "install-service.sh"), "--activate"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bootstrapped + enabled" not in r.stdout
    assert "FAILED" in r.stderr and "launchctl enable" in r.stderr
