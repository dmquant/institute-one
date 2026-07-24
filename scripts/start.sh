#!/usr/bin/env bash
# Start the institute-one server in the background (pidfile + bounded nohup log).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/uvicorn ]; then
  echo "error: project venv is missing or incomplete — run scripts/install.sh" >&2
  exit 1
fi

# Values are emitted with shlex.quote by a trusted project helper.
eval "$(.venv/bin/python scripts/runtime-config.py)"
HOST="$INSTITUTE_RUNTIME_HOST"
PORT="$INSTITUTE_RUNTIME_PORT"
HOME_DIR="$INSTITUTE_RUNTIME_HOME"
LOG_DIR="$HOME_DIR/logs"
LOG_FILE="$LOG_DIR/server.log"
PIDFILE="$HOME_DIR/server.pid"
mkdir -p "$LOG_DIR"

# stale-pidfile detection (aligned with stop.sh): "already running" only when
# the pid is alive AND still our uvicorn — a recycled pid must not block starts
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null && ps -p "$PID" -o command= 2>/dev/null | grep -Eq "uvicorn.*app\.main:app"; then
    echo "already running (pid $PID)"
    exit 0
  fi
  echo "removing stale pidfile (pid $PID)"
  rm -f "$PIDFILE"
fi

LOG_MAX_BYTES="${INSTITUTE_LOG_MAX_BYTES:-10485760}"
case "$LOG_MAX_BYTES" in
  ""|*[!0-9]*)
    echo "error: INSTITUTE_LOG_MAX_BYTES must be a non-negative integer" >&2
    exit 2
    ;;
esac
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -ge "$LOG_MAX_BYTES" ]; then
  rm -f "$LOG_FILE.3"
  [ ! -f "$LOG_FILE.2" ] || mv -f "$LOG_FILE.2" "$LOG_FILE.3"
  [ ! -f "$LOG_FILE.1" ] || mv -f "$LOG_FILE.1" "$LOG_FILE.2"
  mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

nohup .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" \
  > "$LOG_FILE" 2>&1 &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"

# Wildcard bind addresses are not connectable health-check destinations.
PROBE_HOST="$HOST"
case "$PROBE_HOST" in
  "0.0.0.0"|"::"|"[::]") PROBE_HOST="127.0.0.1" ;;
esac

READY=0
for _ in $(seq 1 80); do  # up to 20s, while also catching immediate crashes
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  if .venv/bin/python -c '
import http.client, sys
conn = http.client.HTTPConnection(sys.argv[1], int(sys.argv[2]), timeout=0.5)
try:
    conn.request("GET", "/health")
    response = conn.getresponse()
    raise SystemExit(0 if response.status == 200 else 1)
finally:
    conn.close()
' "$PROBE_HOST" "$PORT" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.25
done

if [ "$READY" != "1" ]; then
  echo "error: server failed to become healthy within 20s" >&2
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$PID" 2>/dev/null || true
  fi
  wait "$PID" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "last server log lines:" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  exit 1
fi

echo "started (pid $PID, http://$HOST:$PORT, log $LOG_FILE)"
