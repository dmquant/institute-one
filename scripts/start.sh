#!/usr/bin/env bash
# Start the institute-one server in the background (pidfile + nohup log).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# shellcheck disable=SC1091
source .venv/bin/activate

PORT="${INSTITUTE_PORT:-8100}"
HOST="${INSTITUTE_HOST:-127.0.0.1}"
HOME_DIR="${INSTITUTE_HOME:-$HOME/.institute-one}"
mkdir -p "$HOME_DIR/logs"

if [ -f "$HOME_DIR/server.pid" ] && kill -0 "$(cat "$HOME_DIR/server.pid")" 2>/dev/null; then
  PID="$(cat "$HOME_DIR/server.pid")"
  STAT="$(ps -p "$PID" -o stat= 2>/dev/null | tr -d ' ')"
  if [ -n "$STAT" ] && [[ "$STAT" != Z* ]]; then
    echo "already running (pid $PID)"
    exit 0
  fi
  rm -f "$HOME_DIR/server.pid"
fi

.venv/bin/python scripts/_daemon_start.py "$HOST" "$PORT" "$HOME_DIR/logs/server.log" "$HOME_DIR/server.pid"

echo "started (pid $(cat "$HOME_DIR/server.pid"), host $HOST, port $PORT, log $HOME_DIR/logs/server.log)"
