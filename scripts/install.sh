#!/usr/bin/env sh
set -eu

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$REPO_DIR"

MODE="${1:-default}"
if [ "$MODE" = "dev" ]; then
  python -m pip install -e ".[dev]"
elif [ "$MODE" = "all" ]; then
  python -m pip install -e ".[all]"
else
  python -m pip install -e ".[vector,models]"
fi

echo "Installed workspace-docs-mcp."
echo "Next: workspace-docs --help"

