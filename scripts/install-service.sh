#!/usr/bin/env bash
# Render the launchd template into ~/Library/LaunchAgents (KeepAlive service
# for the institute-one server).
#
# By default this script only CREATES the rendered plist and prints the
# activation commands — it never touches launchctl. Pass --activate to also
# bootstrap + enable the service (modern launchctl syntax, with a legacy
# `load -w` fallback for older macOS).
set -euo pipefail
cd "$(dirname "$0")/.."

LABEL="com.institute-one.server"
REPO_DIR="$(pwd)"
VENV_DIR="$REPO_DIR/.venv"
PORT="${INSTITUTE_PORT:-8100}"
LOG_DIR="$HOME/.institute-one/logs"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
TEMPLATE="$REPO_DIR/scripts/$LABEL.plist.template"
# Safety net under get_cli_env()'s login-shell PATH capture (see template).
SERVICE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ACTIVATE=0
if [ "${1:-}" = "--activate" ]; then
  ACTIVATE=1
elif [ -n "${1:-}" ]; then
  echo "usage: $0 [--activate]" >&2
  exit 2
fi

if [ ! -x "$VENV_DIR/bin/uvicorn" ]; then
  echo "error: $VENV_DIR/bin/uvicorn not found — run scripts/install.sh first" >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "$AGENTS_DIR"

# Pure-bash placeholder substitution: no sed delimiter/escaping pitfalls for
# repo paths containing non-ASCII or special characters.
content="$(cat "$TEMPLATE")"
content="${content//\{\{REPO_DIR\}\}/$REPO_DIR}"
content="${content//\{\{VENV_DIR\}\}/$VENV_DIR}"
content="${content//\{\{LOG_DIR\}\}/$LOG_DIR}"
content="${content//\{\{PORT\}\}/$PORT}"
content="${content//\{\{PATH\}\}/$SERVICE_PATH}"
printf '%s\n' "$content" > "$PLIST"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST" >/dev/null
fi
echo "rendered $PLIST (repo $REPO_DIR, port $PORT)"

if [ "$ACTIVATE" = "1" ]; then
  UID_NOW="$(id -u)"
  if launchctl bootstrap "gui/$UID_NOW" "$PLIST" 2>/dev/null; then
    # enable can fail independently (e.g. odd launchctl state); never report
    # it as success — a previously disabled job would silently stay disabled
    if launchctl enable "gui/$UID_NOW/$LABEL" 2>/dev/null; then
      echo "service bootstrapped + enabled (gui/$UID_NOW/$LABEL)"
    else
      echo "service bootstrapped, but 'launchctl enable' FAILED — if it was previously disabled, run:" >&2
      echo "  launchctl enable gui/$UID_NOW/$LABEL" >&2
    fi
  elif launchctl load -w "$PLIST" 2>/dev/null; then
    echo "service loaded (legacy launchctl load -w)"
  else
    echo "error: launchctl bootstrap and load both failed." >&2
    echo "  If the service is already installed, run scripts/uninstall-service.sh first." >&2
    exit 1
  fi
fi

cat <<EOF

If a pidfile-started server is running (scripts/start.sh), stop it BEFORE
activating, or the launchd instance will crash-loop on the busy port:
  ./scripts/stop.sh

Activate (not executed by this script unless --activate):
  launchctl bootstrap gui/\$(id -u) "$PLIST"     # load + start (RunAtLoad)
  launchctl enable gui/\$(id -u)/$LABEL
  # legacy syntax (older macOS):
  launchctl load -w "$PLIST"

Manage:
  launchctl print gui/\$(id -u)/$LABEL           # status
  launchctl kickstart -k gui/\$(id -u)/$LABEL    # (re)start now
  tail -f "$LOG_DIR/launchd.err.log"
  ./scripts/uninstall-service.sh                # stop + remove

Note: while launchd manages the server there is no pidfile — scripts/stop.sh
will (correctly) refuse to touch it; stop/restart via launchctl instead.
EOF
