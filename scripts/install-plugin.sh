#!/usr/bin/env bash
# Install (or update) the Obsidian plugin into a vault.
# Usage: ./scripts/install-plugin.sh /path/to/YourVault
set -euo pipefail
cd "$(dirname "$0")/.."

VAULT="${1:-}"
if [ -z "$VAULT" ] || [ ! -d "$VAULT" ]; then
  echo "usage: $0 /path/to/YourVault   (the folder that contains .obsidian/)" >&2
  exit 1
fi
if [ ! -d "$VAULT/.obsidian" ]; then
  echo "error: $VAULT does not look like an Obsidian vault (no .obsidian/)" >&2
  exit 1
fi

# build if needed: rebuild when ANY bundled input is newer than main.js —
# every src/*.ts plus roadmap/backlog.json (inlined at build time)
needs_build=0
if [ ! -f obsidian-plugin/main.js ]; then
  needs_build=1
else
  for f in obsidian-plugin/src/*.ts roadmap/backlog.json; do
    if [ "$f" -nt obsidian-plugin/main.js ]; then
      needs_build=1
      break
    fi
  done
fi
if [ "$needs_build" -eq 1 ]; then
  echo "building plugin…"
  (cd obsidian-plugin && npm install --silent && npm run build)
fi

DEST="$VAULT/.obsidian/plugins/institute-one"
mkdir -p "$DEST"
cp obsidian-plugin/manifest.json obsidian-plugin/main.js obsidian-plugin/styles.css "$DEST/"
echo "installed to $DEST"
echo "→ In Obsidian: Settings → Community plugins → enable “Institute One” (toggle off/on to update)."
