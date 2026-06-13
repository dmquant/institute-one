#!/usr/bin/env bash
# Stop the institute-one server: pidfile first, pkill fallback.
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HOME_DIR="${INSTITUTE_HOME:-$HOME/.institute-one}"
PIDFILE="$HOME_DIR/server.pid"

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill "$PID" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "stopped (pid $PID)"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

if pkill -f "uvicorn app.main:app" 2>/dev/null; then
  echo "stopped (pkill)"
else
  echo "not running"
fi
