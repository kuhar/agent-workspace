#!/bin/bash
# One-time setup for --reserve-cores. Run this outside agent-sandbox.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/agent-cpu-reserve"
DESTINATION="/usr/local/libexec/agent-cpu-reserve"
RUN_SOURCE="$SCRIPT_DIR/agent-reserved-run"
RUN_DESTINATION="/usr/local/bin/agent-reserved-run"
INSTALL_USER="$(id -un)"
INSTALL_UID="$(id -u)"
SUDOERS_DESTINATION="/etc/sudoers.d/agent-cpu-reserve-$INSTALL_UID"
SUDOERS_TEMP="$(mktemp)"
trap 'rm -f "$SUDOERS_TEMP"' EXIT

if [[ "$INSTALL_UID" == "0" || ! "$INSTALL_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo "Error: run this setup as the non-root user who launches agent-sandbox." >&2
    exit 1
fi

sudo install -d -o root -g root -m 0755 "$(dirname "$DESTINATION")"
sudo install -o root -g root -m 0755 "$SOURCE" "$DESTINATION"
sudo install -o root -g root -m 0755 "$RUN_SOURCE" "$RUN_DESTINATION"

printf '%s ALL=(root) NOPASSWD: %s exec *\n' \
    "$INSTALL_USER" "$DESTINATION" >"$SUDOERS_TEMP"
sudo visudo -cf "$SUDOERS_TEMP"
sudo install -o root -g root -m 0440 "$SUDOERS_TEMP" "$SUDOERS_DESTINATION"

echo "Installed $DESTINATION"
echo "Installed $RUN_DESTINATION"
echo "Installed $SUDOERS_DESTINATION for reserved commands only"
echo "agent-sandbox --reserve-cores N will ask for sudo when it starts."
echo "Inside the session, run benchmarks as: agent-reserved-run COMMAND ..."
echo "Emergency cleanup: sudo $DESTINATION reset"
