#!/usr/bin/env bash
# Reverse of install-service.sh: boot the service out of launchd (modern
# syntax, legacy `unload -w` fallback) and remove the rendered plist.
#
# Failure ordering matters (REVIEW-C6 M5): the plist is only removed when the
# job is confirmed not loaded or was successfully booted out. If the job is
# loaded but bootout AND unload both fail, the plist stays on disk and the
# script exits non-zero — deleting it would leave a running job with no
# on-disk definition, which is not a completed uninstall.
set -euo pipefail

LABEL="com.institute-one.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NOW="$(id -u)"

if command -v launchctl >/dev/null 2>&1 && launchctl print "gui/$UID_NOW/$LABEL" >/dev/null 2>&1; then
  if launchctl bootout "gui/$UID_NOW/$LABEL" 2>/dev/null; then
    echo "service booted out (gui/$UID_NOW/$LABEL)"
  elif [ -f "$PLIST" ] && launchctl unload -w "$PLIST" 2>/dev/null; then
    echo "service unloaded (legacy launchctl unload -w)"
  else
    echo "error: could not unload $LABEL — the job is still loaded; keeping $PLIST" >&2
    echo "  retry manually: launchctl bootout gui/$UID_NOW/$LABEL" >&2
    exit 1
  fi
else
  echo "service not loaded"
fi

if [ -f "$PLIST" ]; then
  rm "$PLIST"
  echo "removed $PLIST"
else
  echo "no plist at $PLIST"
fi
