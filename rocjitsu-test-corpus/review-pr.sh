#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")"
exec python3 "$SCRIPT_DIR/review.py" "$@"
