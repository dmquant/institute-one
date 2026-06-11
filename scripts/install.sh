#!/usr/bin/env bash
# Install institute-one: venv + package (dev extras), then best-effort UI builds.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

(cd frontend && npm install && npm run build) || echo "skip frontend build"
(cd obsidian-plugin && npm install && npm run build) || echo "skip obsidian-plugin build"

echo "install complete"
