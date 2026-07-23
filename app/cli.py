"""``institute`` CLI — lifecycle, doctor, and paper-book reconciliation.

Entry points: the ``institute`` console script (pyproject ``[project.scripts]``)
or ``python -m app.cli``. Everything is synchronous and works without the
server running: start/stop delegate to the battle-tested scripts, ``status``
probes process + port + /health from outside, and ``doctor`` reads files and
the SQLite database directly.

Doctor is strictly READ-ONLY (REVIEW-C6 H1): every database access goes
through ``file:...?mode=ro`` (sqlite3 stdlib) — including the vault drift
scan, which reads ``vault_index`` over that read-only connection and runs
the SAME shared classification pass as the server
(``app.institute.operator._classify_vault_rows``, imported lazily inside
``check_vault``: a side-effect-free import that keeps doctor's startup
module graph minimal). The doctor path never touches
``app.db`` (``init()``/``migrate()``/``close()``): those open a write
connection, switch journal mode and run DDL.

Hand checks never send a prompt (no quota burn): binary presence, a
``--version`` probe, plus the CLI's own no-prompt login-status command where
one exists (``AUTH_PROBES``); ``health_check()`` for the local-HTTP hand. The
one async bridge (that health check) runs through ``_run_async``, which is
safe to call from inside a running event loop (REVIEW-C6 M2).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

import httpx

from .config import Settings, get_settings

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

VERSION_PROBE_TIMEOUT_S = 15
DISK_FAIL_BYTES = 1 * 1024**3   # < 1 GiB free
DISK_WARN_BYTES = 5 * 1024**3   # < 5 GiB free

# Per-CLI no-prompt login-status probes (REVIEW-C6 M1): argv appended to the
# resolved binary; exit 0 = logged in, non-zero = not logged in — UNLESS the
# stderr shows the subcommand itself was not recognized (usage/unknown-command,
# see _AUTH_PROBE_USAGE_HINTS): then the upstream CLI renamed/removed its
# status command and the verdict is auth unknown, never a false FAIL. Both
# commands only read cached credentials — no prompt, no network generation,
# no quota. CLIs with no reliable non-interactive status command map to None
# and are reported as "auth unknown" (WARN) instead of ok.
AUTH_PROBES: dict[str, list[str] | None] = {
    "claude": ["auth", "status"],   # exits 0 when logged in, 1 when not
    "codex": ["login", "status"],   # exits 0 when logged in, 1 when not
    "gemini": None,
    "agy": None,
    "opencode": None,
}

# Case-insensitive stderr signatures meaning "this CLI has no such subcommand"
# (renamed/removed upstream) rather than "not logged in" (REVIEW follow-up:
# a non-zero probe exit used to be read as logged-out unconditionally).
_AUTH_PROBE_USAGE_HINTS = ("usage:", "unknown command", "unrecognized", "invalid choice")


def _run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Event-loop-safe async bridge (REVIEW-C6 M2).

    ``asyncio.run`` raises RuntimeError when this thread already runs a loop
    (doctor invoked from async code / tests); in that case the coroutine runs
    on a fresh loop in a worker thread and we block on its result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Check:
    name: str
    status: str            # PASS | WARN | FAIL | SKIP
    detail: str = ""
    lines: list[str] = field(default_factory=list)  # indented follow-up lines


# ---- server probe (shared by status + doctor) --------------------------------

@dataclass
class ServerProbe:
    pid: int | None
    pid_alive: bool
    pid_is_uvicorn: bool
    port_open: bool
    health: dict | None    # /health body when reachable, else None

    @property
    def up(self) -> bool:
        return self.health is not None


def _pid_command(pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _probe_host(host: str) -> str:
    """Connectable probe destination for a configured bind host.

    Wildcard binds (0.0.0.0/::) mean "every local interface" and are not
    connect destinations — probe through loopback instead, the same mapping
    scripts/start.sh applies to its readiness check.
    """
    return "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _probe_health(settings: Settings) -> dict | None:
    host = _probe_host(settings.host)
    if ":" in host and not host.startswith("["):  # IPv6 literal: URLs need brackets
        host = f"[{host}]"
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            resp = client.get(f"http://{host}:{settings.port}/health")
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return None


def probe_server(settings: Settings) -> ServerProbe:
    pid: int | None = None
    pidfile = settings.home_dir / "server.pid"
    if pidfile.is_file():
        try:
            raw = pidfile.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        pid = int(raw) if raw.isdigit() else None
    alive = _pid_alive(pid) if pid is not None else False
    is_uvicorn = False
    if alive:
        cmd = _pid_command(pid)  # type: ignore[arg-type]
        is_uvicorn = "uvicorn" in cmd and "app.main:app" in cmd
    return ServerProbe(
        pid=pid,
        pid_alive=alive,
        pid_is_uvicorn=is_uvicorn,
        port_open=_port_open(_probe_host(settings.host), settings.port),
        health=_probe_health(settings),
    )


# ---- doctor checks (each independently testable) ------------------------------

def _probe_cli_hand(name: str, binary: str, env: dict[str, str]) -> tuple[bool | None, str]:
    """(usable, detail) for one CLI hand — no prompt is ever sent.

    usable: True = binary runs AND auth probe (when one exists) says logged
    in; False = provably broken or logged out; None = binary runs but auth
    cannot be verified — no reliable status command, or the probe subcommand
    itself is not recognized by this CLI version (usage/unknown-command
    stderr: the upstream CLI renamed/removed it) — "auth unknown".
    """
    def _run(args: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                [binary, *args], capture_output=True, text=True,
                timeout=VERSION_PROBE_TIMEOUT_S, env=env,
                stdin=subprocess.DEVNULL,  # a probe must never wait for input
            )
        except (OSError, subprocess.SubprocessError):
            return None

    version = _run(["--version"])
    if version is None:
        return False, f"{binary} --version failed to run"
    if version.returncode != 0:
        return False, f"--version exited {version.returncode}"
    head = (version.stdout or version.stderr).strip().splitlines()
    version_str = head[0][:80] if head else "no version output"

    probe_args = AUTH_PROBES.get(name)
    if probe_args is None:
        return None, f"{version_str}; auth unknown (no non-interactive status command)"
    auth = _run(probe_args)
    if auth is None:
        return None, f"{version_str}; auth unknown ({name} {' '.join(probe_args)} failed to run)"
    if auth.returncode == 0:
        return True, f"{version_str}; authenticated ({name} {' '.join(probe_args)})"
    stderr = (auth.stderr or "").lower()
    if any(hint in stderr for hint in _AUTH_PROBE_USAGE_HINTS):
        return None, (
            f"{version_str}; auth unknown ({name} {' '.join(probe_args)} not recognized "
            "by this CLI version — status subcommand renamed/removed upstream?)"
        )
    return False, f"not logged in ({name} {' '.join(probe_args)} exited {auth.returncode})"


def check_hands(settings: Settings) -> Check:
    """Presence + auth per hand — never sends a prompt (no quota burn).

    CLI hands: binary + --version + the CLI's own login-status command
    (AUTH_PROBES). Where no reliable status command exists the hand reports
    "auth unknown" (WARN), never a false ok. The default/research verdicts
    consume these probe results — a logged-out default hand is a FAIL even
    though its binary would pass a static available() check (REVIEW-C6 M1).
    """
    from .hands import build_hands
    from .hands.base import get_cli_env, resolve_cli_path

    hands = build_hands(settings)
    lines: list[str] = []
    warn = False
    fail = False
    available = 0
    # name -> True (verified usable) | False (unusable) | None (auth unknown)
    usable: dict[str, bool | None] = {}

    for hand in hands:
        flag = getattr(settings, f"enable_{hand.name}", None)
        if flag is False:
            usable[hand.name] = False
            lines.append(f"- {hand.name}: SKIP (disabled by config)")
            continue
        if hand.hand_type == "api":
            if hand.available():
                available += 1
                usable[hand.name] = True
                lines.append(f"- {hand.name}: ok (API key configured; not called)")
            else:
                usable[hand.name] = False
                lines.append(f"- {hand.name}: SKIP (no API key configured)")
            continue
        if hand.hand_type == "http":  # ollama: local endpoint, no quota
            if _run_async(hand.health_check()):
                available += 1
                usable[hand.name] = True
                lines.append(f"- {hand.name}: ok (endpoint responding)")
            else:
                warn = True
                usable[hand.name] = False
                lines.append(f"- {hand.name}: WARN (enabled but endpoint not responding)")
            continue
        # CLI hands (incl. echo, which has no binary)
        if hand.name == "echo":
            available += 1
            usable[hand.name] = True
            lines.append("- echo: ok (built-in)")
            continue
        binary = resolve_cli_path(hand.name)
        if binary is None:
            warn = True
            usable[hand.name] = False
            lines.append(f"- {hand.name}: WARN (enabled but binary not on login-shell PATH)")
            continue
        hand_usable, detail = _probe_cli_hand(hand.name, binary, get_cli_env())
        usable[hand.name] = hand_usable
        if hand_usable:
            available += 1
            lines.append(f"- {hand.name}: ok ({detail})")
        else:
            warn = True
            lines.append(f"- {hand.name}: WARN ({detail})")

    default_usable = usable.get(settings.default_hand, False)
    if default_usable is False:
        fail = True
        lines.append(f"- FAIL: default hand '{settings.default_hand}' is not usable")
    elif default_usable is None:
        warn = True
        lines.append(
            f"- WARN: default hand '{settings.default_hand}' auth could not be verified"
        )
    research = [usable.get(n, False) for n in settings.research_hand_names]
    if not any(u is True or u is None for u in research):
        fail = True
        lines.append(
            f"- FAIL: no research hand usable ({', '.join(settings.research_hand_names)})"
        )

    status = FAIL if fail else (WARN if warn else PASS)
    return Check("hands", status, f"{available} usable / {len(hands)} registered", lines)


def _read_only_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def check_db(settings: Settings) -> tuple[Check, list[str]]:
    """PRAGMA integrity_check + schema_migrations vs migrations/*.sql diff.

    Returns (check, pending_migrations) so callers can see the gap; the vault
    scan no longer needs gating (it is read-only and tolerates a missing table).
    """
    db_path = settings.db_path
    disk = sorted(p.name for p in (_repo_root() / "migrations").glob("*.sql"))
    if not db_path.exists():
        return Check("database", WARN, f"no database at {db_path} (server never started?)"), disk
    try:
        conn = _read_only_conn(db_path)
    except sqlite3.Error as exc:
        return Check(
            "database", WARN,
            f"cannot open read-only ({exc}) — if the server just crashed this can be "
            "pending WAL recovery; it heals on the next server start",
        ), []
    try:
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        try:
            applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        except sqlite3.OperationalError:
            applied = set()
    except sqlite3.Error as exc:
        return Check("database", FAIL, f"integrity check errored: {exc}"), []
    finally:
        conn.close()

    pending = sorted(set(disk) - applied)
    ghost = sorted(applied - set(disk))
    lines: list[str] = []
    if integrity != ["ok"]:
        for row in integrity[:10]:
            lines.append(f"- integrity: {row}")
        return Check("database", FAIL, "PRAGMA integrity_check FAILED", lines), pending
    if ghost:
        lines.append(f"- ghost migrations recorded but missing on disk: {', '.join(ghost)}")
        return Check(
            "database", FAIL,
            "schema_migrations lists files this checkout does not have (code/db drift)", lines,
        ), pending
    if pending:
        lines.append(f"- pending: {', '.join(pending)}")
        return Check(
            "database", WARN,
            f"integrity ok; {len(pending)} migration(s) not applied yet "
            "(they apply at next server start)", lines,
        ), pending
    return Check(
        "database", PASS,
        f"integrity ok, {len(applied)}/{len(disk)} migrations applied",
    ), []


def check_vault(settings: Settings) -> Check:
    """Ledger vs disk drift scan — strictly read-only (REVIEW-C6 H1).

    Reads ``vault_index`` over a read-only SQLite connection and hashes vault
    files; never goes near ``app.db`` (whose init() opens a write connection,
    switches journal mode and runs the migrator). Classification reuses the
    server's ONE pass (``app.institute.operator._classify_vault_rows``) so
    doctor and writer/sweep verdicts are a single implementation.
    """
    if settings.vault_dir is None:
        return Check("vault", SKIP, "vault_dir not configured")
    if not settings.db_path.exists():
        return Check("vault", SKIP, "no database yet")
    try:
        conn = _read_only_conn(settings.db_path)
    except sqlite3.Error as exc:
        return Check("vault", WARN, f"cannot open db read-only ({exc})")
    try:
        rows = conn.execute("SELECT path, sha256, state, mode FROM vault_index").fetchall()
    except sqlite3.OperationalError as exc:
        # vault_index (0001) or its mode column (0010) not migrated yet
        return Check("vault", SKIP, f"vault_index not migrated yet ({exc})")
    except sqlite3.Error as exc:
        return Check("vault", WARN, f"cannot read vault_index ({exc})")
    finally:
        conn.close()

    root = settings.vault_dir.expanduser()
    # THE classification pass — the writer's doctor(), the operator sweep and
    # this scan share one implementation; the lazy import keeps the operator
    # module graph (bus/db/executor) out of every other CLI command. Rows are
    # plain dicts, not sqlite3.Row: the shared helper's contract is key-indexed
    # mappings from ``db.query``.
    from .institute.operator import _classify_vault_rows

    counts, _nonclean = _classify_vault_rows(root, [dict(r) for r in rows])
    detail = (
        f"{counts['total']} ledger rows: {counts['clean']} clean, "
        f"{counts['conflict']} conflict, {counts['missing']} missing, "
        f"{counts['drifted']} drifted"
    )
    bad = counts["conflict"] + counts["missing"] + counts["drifted"]
    return Check("vault", WARN if bad else PASS, detail)


def check_cron(settings: Settings) -> Check:
    """Last status per job + failures in the trailing 24h, straight from
    cron_metrics (works with the server down; the table IS the 30-day window)."""
    if not settings.db_path.exists():
        return Check("cron", SKIP, "no database yet")
    try:
        conn = _read_only_conn(settings.db_path)
    except sqlite3.Error as exc:
        return Check("cron", WARN, f"cannot open db read-only ({exc})")
    try:
        # Same per-job last-row + trailing-24h-failure knowledge as
        # app/api/meta.py's cron_health() — this is its offline twin; a
        # cron_metrics semantics change must land in both.
        last_rows = conn.execute(
            "SELECT job, ok, skipped_by_maintenance, fired_at FROM cron_metrics "
            "WHERE id IN (SELECT MAX(id) FROM cron_metrics GROUP BY job) ORDER BY job"
        ).fetchall()
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        fail_rows = conn.execute(
            "SELECT job, COUNT(*) AS n FROM cron_metrics "
            "WHERE ok = 0 AND skipped_by_maintenance = 0 AND fired_at >= ? GROUP BY job",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return Check("cron", SKIP, "cron_metrics table missing (migration not applied yet)")
    finally:
        conn.close()

    if not last_rows:
        return Check("cron", PASS, "no cron activity recorded yet")
    failed_24h = {r["job"]: r["n"] for r in fail_rows}
    lines: list[str] = []
    warn = False
    for r in last_rows:
        status = "skipped" if r["skipped_by_maintenance"] else ("ok" if r["ok"] else "FAILED")
        extra = f", {failed_24h[r['job']]} failure(s) in 24h" if r["job"] in failed_24h else ""
        if status == "FAILED" or extra:
            warn = True
        lines.append(f"- {r['job']}: last {status} at {r['fired_at']}{extra}")
    return Check(
        "cron", WARN if warn else PASS,
        f"{len(last_rows)} job(s) reporting, {sum(failed_24h.values())} failure(s) in 24h",
        lines,
    )


def check_orphans(settings: Settings, server_up: bool) -> Check:
    """queued/running residue in tasks + research_queue. Only meaningful as
    orphans when the server is down (a live server legitimately has in-flight
    rows; boot-time recovery sweeps real orphans)."""
    if not settings.db_path.exists():
        return Check("orphans", SKIP, "no database yet")
    try:
        conn = _read_only_conn(settings.db_path)
    except sqlite3.Error as exc:
        return Check("orphans", WARN, f"cannot open db read-only ({exc})")
    counts: dict[str, int] = {}
    try:
        # The same residue sets the boot-time recovery sweeps own:
        # app/router/executor.py's recover_orphans() (tasks queued/running)
        # and app/institute/research.py's recover_orphans() (research
        # 'running'). A status-vocabulary change must land in all three.
        for label, sql in (
            ("tasks", "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running')"),
            ("research_queue", "SELECT COUNT(*) FROM research_queue WHERE status = 'running'"),
        ):
            try:
                counts[label] = conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                counts[label] = 0
    finally:
        conn.close()
    total = sum(counts.values())
    detail = f"tasks queued/running: {counts['tasks']}, research running: {counts['research_queue']}"
    if total == 0:
        return Check("orphans", PASS, detail)
    if server_up:
        return Check("orphans", PASS, detail + " (server is up — likely live work, not orphans)")
    return Check(
        "orphans", WARN,
        detail + " with the server DOWN — recovery sweeps run at next boot "
        "(tasks fail as orphaned, research requeues)",
    )


def _cooldown_entry_error(cd: object) -> str | None:
    """Why one rate_limits.json entry is malformed, or None when well-formed.

    Validates the full shape the registry relies on: dict entry, ``until`` a
    finite real number (bool excluded). A single bad entry means the registry
    would throw while loading and silently "start clean" — worth a FAIL, but
    never an exception out of doctor (REVIEW-C6 M3).
    """
    if not isinstance(cd, dict):
        return f"entry is {type(cd).__name__}, expected object"
    until = cd.get("until", 0)
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return f"until is {type(until).__name__} ({until!r}), expected epoch seconds"
    if not math.isfinite(until):
        return f"until is non-finite ({until!r})"
    return None


def _fmt_epoch(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return f"epoch {ts!r} (out of datetime range)"


def check_rate_limits(settings: Settings) -> Check:
    path = settings.rate_limits_path
    if not path.exists():
        return Check("rate_limits", PASS, "no rate_limits.json (no cooldowns recorded)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Check(
            "rate_limits", FAIL,
            f"{path} is unparseable ({exc}) — the registry would silently start "
            "clean; fix or delete the file",
        )
    if not isinstance(data, dict):
        return Check("rate_limits", FAIL, f"{path} is not a JSON object")

    malformed: list[str] = []
    active: dict[str, dict] = {}
    now = time.time()
    for name, cd in sorted(data.items()):
        err = _cooldown_entry_error(cd)
        if err is not None:
            malformed.append(f"- {name}: MALFORMED ({err})")
        elif cd["until"] > now:
            active[name] = cd
    if malformed:
        return Check(
            "rate_limits", FAIL,
            f"{len(malformed)} malformed entr{'y' if len(malformed) == 1 else 'ies'} "
            f"of {len(data)} — the registry would throw on load and silently start "
            "clean; fix or delete the file",
            malformed,
        )
    lines = [
        f"- {name}: cooling until {_fmt_epoch(cd['until'])} ({cd.get('reason', '?')})"
        for name, cd in sorted(active.items())
    ]
    return Check(
        "rate_limits", PASS,
        f"parseable, {len(active)} active cooldown(s) of {len(data)} recorded", lines,
    )


def check_disk(settings: Settings) -> Check:
    target = settings.home_dir if settings.home_dir.exists() else Path.home()
    usage = shutil.disk_usage(target)
    free_gib = usage.free / 1024**3
    detail = f"{free_gib:.1f} GiB free on the volume holding {target}"
    if usage.free < DISK_FAIL_BYTES:
        return Check("disk", FAIL, detail)
    if usage.free < DISK_WARN_BYTES:
        return Check("disk", WARN, detail)
    return Check("disk", PASS, detail)


# ---- commands -----------------------------------------------------------------

def _run_script(name: str) -> int:
    script = _repo_root() / "scripts" / name
    if not script.exists():
        print(f"error: {script} not found", file=sys.stderr)
        return 2
    return subprocess.run([str(script)], cwd=_repo_root()).returncode


def cmd_start(settings: Settings) -> int:
    return _run_script("start.sh")


def cmd_stop(settings: Settings) -> int:
    return _run_script("stop.sh")


def cmd_status(settings: Settings) -> int:
    probe = probe_server(settings)
    pidfile = settings.home_dir / "server.pid"
    if probe.pid is None:
        print(f"pidfile: none at {pidfile}")
    else:
        state = (
            "alive, our uvicorn" if probe.pid_is_uvicorn
            else ("alive, NOT our uvicorn (stale/reused pid)" if probe.pid_alive else "not running (stale)")
        )
        print(f"pidfile: pid {probe.pid} ({state})")
    print(f"port {settings.port}: {'open' if probe.port_open else 'closed'}")
    if probe.health is not None:
        print(
            f"/health: ok (version {probe.health.get('version', '?')}, "
            f"time_sgt {probe.health.get('time_sgt', '?')})"
        )
    else:
        print("/health: unreachable")

    if probe.up:
        if probe.pid is None:
            print("server: RUNNING (no pidfile — likely launchd-managed; use launchctl to stop)")
        else:
            print("server: RUNNING")
        return 0
    if probe.port_open:
        print(f"server: port {settings.port} is open but /health did not answer — another process?")
    else:
        print("server: NOT RUNNING")
    return 1


def _guarded(name: str, fn: Callable[[], Check]) -> Check:
    """One crashing check must not take down the rest of the report — it
    becomes its own FAIL line instead (REVIEW-C6 M3)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: report, don't die
        return Check(name, FAIL, f"check crashed: {type(exc).__name__}: {exc}")


def cmd_doctor(settings: Settings) -> int:
    probe = probe_server(settings)
    print(f"institute doctor — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"home: {settings.home_dir}")
    if probe.up:
        print(f"server: running on port {settings.port} (/health ok)")
    else:
        print(f"server: not running (port {settings.port})")
    print()

    checks = [
        _guarded("hands", lambda: check_hands(settings)),
        _guarded("database", lambda: check_db(settings)[0]),
        _guarded("vault", lambda: check_vault(settings)),
        _guarded("cron", lambda: check_cron(settings)),
        _guarded("orphans", lambda: check_orphans(settings, server_up=probe.up)),
        _guarded("rate_limits", lambda: check_rate_limits(settings)),
        _guarded("disk", lambda: check_disk(settings)),
    ]

    for c in checks:
        print(f"[{c.status}] {c.name}: {c.detail}")
        for line in c.lines:
            print(f"       {line}")
    tally = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    print()
    print(
        f"summary: {tally[PASS]} pass, {tally[WARN]} warn, "
        f"{tally[FAIL]} fail, {tally[SKIP]} skipped"
    )
    return 1 if tally[FAIL] else 0


def cmd_reconcile_paper_book(settings: Settings, *, dry_run: bool = False) -> int:
    """Run the repeatable paper-book repair sweep against the configured DB."""
    async def _run() -> dict[str, Any]:
        from . import db
        from .institute import paper_book

        already_open = db._conn is not None
        if not already_open:
            await db.init()
        try:
            return await paper_book.reconcile(dry_run=dry_run)
        finally:
            if not already_open:
                await db.close()

    result = _run_async(_run())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    # pydantic-settings reads .env relative to CWD; the server starts from the
    # repo root, so the CLI must too or doctor would see a different config.
    os.chdir(_repo_root())
    parser = argparse.ArgumentParser(
        prog="institute",
        description="institute-one operator CLI: lifecycle/doctor/reconciliation",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="start the server in the background (scripts/start.sh)")
    sub.add_parser("stop", help="stop the pidfile-started server (scripts/stop.sh)")
    sub.add_parser("status", help="process + port + /health probe (exit 0 when healthy)")
    sub.add_parser("doctor", help="offline health report: hands/db/vault/cron/orphans/limits/disk")
    reconcile_parser = sub.add_parser(
        "reconcile-paper-book",
        help="repair missed paper-book closes/settlements and audit PIT price differences",
    )
    reconcile_parser.add_argument(
        "--dry-run", action="store_true", help="report planned operations without writing"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "reconcile-paper-book":
        return cmd_reconcile_paper_book(settings, dry_run=args.dry_run)
    handlers = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "doctor": cmd_doctor}
    return handlers[args.command](settings)


if __name__ == "__main__":
    raise SystemExit(main())
