#!/usr/bin/env bash
# Stop the institute-one server: pidfile first, pkill fallback.
PIDFILE="$HOME/.institute-one/server.pid"

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null; then
    COMMAND="$(ps -p "$PID" -o command= 2>/dev/null || true)"
    if [[ "$COMMAND" != *"uvicorn app.main:app"* ]]; then
      echo "refusing to signal pid $PID: pidfile does not point to institute-one uvicorn" >&2
      exit 1
    fi
    if ! kill "$PID" 2>/dev/null; then
      echo "failed to signal pid $PID" >&2
      exit 1
    fi
    for _ in {1..30}; do
      if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "stopped (pid $PID)"
        exit 0
      fi
      sleep 0.1
    done

    # Long-lived SSE connections can keep uvicorn in graceful shutdown.
    # A second interrupt tells uvicorn to close those connections immediately.
    kill -INT "$PID" 2>/dev/null || true
    for _ in {1..20}; do
      if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "stopped (pid $PID, forced after graceful timeout)"
        exit 0
      fi
      sleep 0.1
    done

    echo "failed to stop pid $PID; pidfile retained" >&2
    exit 1
  fi
  rm -f "$PIDFILE"
fi

if pkill -f "uvicorn app.main:app" 2>/dev/null; then
  echo "stopped (pkill)"
else
  echo "not running"
fi
