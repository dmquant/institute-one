#!/usr/bin/env bash
# Stop the institute-one server: pidfile first, pkill fallback.
PIDFILE="$HOME/.institute-one/server.pid"

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
