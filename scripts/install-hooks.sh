#!/usr/bin/env bash
# Opt-in: point git at the committed hooks (scripts/git-hooks/pre-commit).
# Local-only automation — no CI service involved. Undo with:
#   git config --unset core.hooksPath
set -euo pipefail
cd "$(dirname "$0")/.."

chmod +x scripts/git-hooks/pre-commit
git config core.hooksPath scripts/git-hooks
echo "git hooks installed (core.hooksPath=scripts/git-hooks)"
