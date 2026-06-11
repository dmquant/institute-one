#!/usr/bin/env bash
# Start the institute-one server in the background (pidfile + nohup log).
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source .venv/bin/activate

PORT="${INSTITUTE_PORT:-8100}"
HOME_DIR="$HOME/.institute-one"
mkdir -p "$HOME_DIR/logs"

if [ -f "$HOME_DIR/server.pid" ] && kill -0 "$(cat "$HOME_DIR/server.pid")" 2>/dev/null; then
  echo "already running (pid $(cat "$HOME_DIR/server.pid"))"
  exit 0
fi

nohup uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  > "$HOME_DIR/logs/server.log" 2>&1 &
echo $! > "$HOME_DIR/server.pid"

echo "started (pid $(cat "$HOME_DIR/server.pid"), port $PORT, log $HOME_DIR/logs/server.log)"
