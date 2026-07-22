#!/usr/bin/env bash
# Install institute-one: healthy Python venv + package + reproducible UI builds.
set -euo pipefail
cd "$(dirname "$0")/.."

python_is_usable() {
  "$1" -c 'import ssl, struct, subprocess, sys, venv; raise SystemExit(sys.version_info < (3, 11))' \
    >/dev/null 2>&1
}

choose_python() {
  local candidate
  if [ -n "${PYTHON:-}" ]; then
    if command -v "$PYTHON" >/dev/null 2>&1 && python_is_usable "$PYTHON"; then
      command -v "$PYTHON"
      return 0
    fi
    echo "error: PYTHON=$PYTHON is unavailable, unhealthy, or older than 3.11" >&2
    return 1
  fi

  # Prefer stable supported minors over an unqualified python3 that may have
  # just advanced to a newly released or locally broken Homebrew build.
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_usable "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  echo "error: no healthy Python >= 3.11 found (set PYTHON=/path/to/python)" >&2
  return 1
}

PYTHON_BIN="$(choose_python)"
echo "using $($PYTHON_BIN --version 2>&1) at $PYTHON_BIN"

# A venv is generated state. Rebuild it automatically when its interpreter
# cannot import core extension modules (for example after a Homebrew upgrade).
if [ -e .venv ] && { [ ! -x .venv/bin/python ] || ! python_is_usable .venv/bin/python; }; then
  echo "existing .venv is unusable; rebuilding it" >&2
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install -e ".[dev]"

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm is required to build the frontend and Obsidian plugin" >&2
  exit 1
fi

build_node_project() {
  local project="$1"
  if [ ! -f "$project/package-lock.json" ]; then
    echo "error: $project/package-lock.json is required for reproducible installs" >&2
    return 1
  fi
  (
    cd "$project"
    npm ci
    npm run build
  )
}

build_node_project frontend
build_node_project obsidian-plugin

echo "install complete"
