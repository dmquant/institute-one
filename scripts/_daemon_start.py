#!/usr/bin/env python3
"""Start institute-one as a detached local daemon.

This exists because some agent shells clean up their whole process group after a
command finishes. `nohup cmd &` keeps stdout safe but does not create a new
session; `start_new_session=True` does.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: _daemon_start.py HOST PORT LOG PIDFILE", file=sys.stderr)
        return 2
    host, port, log_path, pidfile = sys.argv[1:]
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab", buffering=0) as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", port],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    Path(pidfile).write_text(str(proc.pid), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
