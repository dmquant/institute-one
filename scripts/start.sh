#!/usr/bin/env bash
# Start the institute-one server in the background (pidfile + nohup log).
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source .venv/bin/activate

PORT="${INSTITUTE_PORT:-8100}"
HOME_DIR="$HOME/.institute-one"
mkdir -p "$HOME_DIR/logs"

# stale-pidfile detection (aligned with stop.sh): "already running" only when
# the pid is alive AND still our uvicorn — a recycled pid must not block starts
if [ -f "$HOME_DIR/server.pid" ]; then
  PID="$(cat "$HOME_DIR/server.pid")"
  if kill -0 "$PID" 2>/dev/null && ps -p "$PID" -o command= 2>/dev/null | grep -Eq "uvicorn.*app\.main:app"; then
    echo "already running (pid $PID)"
    exit 0
  fi
  echo "removing stale pidfile (pid $PID)"
  rm -f "$HOME_DIR/server.pid"
fi

nohup uvicorn app.main:app --host "${INSTITUTE_HOST:-127.0.0.1}" --port "$PORT" \
  > "$HOME_DIR/logs/server.log" 2>&1 &
echo $! > "$HOME_DIR/server.pid"

echo "started (pid $(cat "$HOME_DIR/server.pid"), port $PORT, log $HOME_DIR/logs/server.log)"
