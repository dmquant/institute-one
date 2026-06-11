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

# build if needed
if [ ! -f obsidian-plugin/main.js ] || [ obsidian-plugin/src/main.ts -nt obsidian-plugin/main.js ]; then
  echo "building plugin…"
  (cd obsidian-plugin && npm install --silent && npm run build)
fi

DEST="$VAULT/.obsidian/plugins/institute-one"
mkdir -p "$DEST"
cp obsidian-plugin/manifest.json obsidian-plugin/main.js "$DEST/"
echo "installed to $DEST"
echo "→ In Obsidian: Settings → Community plugins → enable “Institute One” (toggle off/on to update)."
