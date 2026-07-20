#!/usr/bin/env bash
# Stop the institute-one server — pidfile-started servers ONLY.
#
# Kills exactly the pid recorded in the pidfile (TERM, bounded wait, then
# SIGKILL that single pid). The old broad `pkill -f "uvicorn app.main:app"`
# fallback is gone (ROADMAP Phase 8): it could take down any similarly-named
# process — another checkout, another port, or a launchd-managed instance
# that KeepAlive would immediately resurrect into a kill/restart fight.
# launchd-managed servers keep no pidfile — stop those via
# scripts/uninstall-service.sh or launchctl bootout.
set -euo pipefail

PIDFILE="$HOME/.institute-one/server.pid"
PATTERN="uvicorn.*app\.main:app"

if [ ! -f "$PIDFILE" ]; then
  echo "not running (no pidfile at $PIDFILE)"
  echo "note: launchd-managed servers keep no pidfile — use scripts/uninstall-service.sh or launchctl."
  exit 0
fi

PID="$(cat "$PIDFILE")"
case "$PID" in
  ""|*[!0-9]*)
    echo "stale pidfile (unparseable pid '$PID'); removing, nothing killed"
    rm -f "$PIDFILE"
    exit 0
    ;;
esac

if ! kill -0 "$PID" 2>/dev/null; then
  echo "stale pidfile (pid $PID not running); removing"
  rm -f "$PIDFILE"
  exit 0
fi

# pid-reuse guard: never kill a recycled pid that is not our uvicorn
if ! ps -p "$PID" -o command= 2>/dev/null | grep -Eq "$PATTERN"; then
  echo "stale pidfile (pid $PID is not the institute-one uvicorn — pid reuse); removing, nothing killed"
  rm -f "$PIDFILE"
  exit 0
fi

kill "$PID" 2>/dev/null || true
for _ in $(seq 1 20); do  # up to ~10s of graceful shutdown
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "stopped (pid $PID)"
    exit 0
  fi
  sleep 0.5
done

echo "pid $PID still alive after 10s; sending SIGKILL"
kill -9 "$PID" 2>/dev/null || true
sleep 0.5
if kill -0 "$PID" 2>/dev/null; then
  echo "error: pid $PID survived SIGKILL" >&2
  exit 1
fi
rm -f "$PIDFILE"
echo "stopped (pid $PID, forced)"
