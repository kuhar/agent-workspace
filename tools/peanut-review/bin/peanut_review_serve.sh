#!/bin/bash
# Launch the peanut-review web server with sensible defaults for local use,
# inheriting the calling shell's environment so `gh` picks up the user's
# real PAT / `gh auth` token (essential for "Push to GitHub").
#
# Defaults:
#   root      $HOME/reviews   (override: $PR_ROOT or --root)
#   host      127.0.0.1       (override: $PR_HOST or --host)
#   port      27183           (override: $PR_PORT or --port)
#   base-url  /pr             (override: $PR_BASE_URL or --base-url)
#
# Any extra flags are forwarded to `peanut-review serve`.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
PACKAGE_DIR="$(dirname "$SCRIPT_DIR")"

ROOT="${PR_ROOT:-$HOME/reviews}"
HOST="${PR_HOST:-127.0.0.1}"
PORT="${PR_PORT:-27183}"
BASE_URL="${PR_BASE_URL:-/pr}"

mkdir -p "$ROOT"

if [[ -x "$PACKAGE_DIR/.venv/bin/python" ]]; then
  PY="$PACKAGE_DIR/.venv/bin/python"
else
  PY="python3"
  export PYTHONPATH="${PACKAGE_DIR}${PYTHONPATH:+:$PYTHONPATH}"
fi

exec "$PY" -m peanut_review serve \
  --root "$ROOT" \
  --host "$HOST" \
  --port "$PORT" \
  --base-url "$BASE_URL" \
  "$@"
