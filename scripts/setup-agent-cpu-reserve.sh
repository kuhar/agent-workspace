#!/bin/bash
# One-time setup for --reserve-cores. Run this outside agent-sandbox.
set -euo pipefail

# Retain the old spelling for callers; entry now uses the host broker and needs
# no passwordless sudo rule. Existing administrator-managed rules are untouched.
if [[ $# -ne 0 && ! ( "${1:-}" == "--no-sudoers" && $# -eq 1 ) ]]; then
    echo "Usage: setup-agent-cpu-reserve.sh [--no-sudoers]" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/agent-cpu-reserve"
DESTINATION="/usr/local/libexec/agent-cpu-reserve"
RUN_SOURCE="$SCRIPT_DIR/agent-reserved-run"
RUN_DESTINATION="/usr/local/bin/agent-reserved-run"
INSTALL_USER="$(id -un)"
INSTALL_UID="$(id -u)"

if [[ "$INSTALL_UID" == "0" || ! "$INSTALL_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo "Error: run this setup as the non-root user who launches agent-sandbox." >&2
    exit 1
fi

sudo install -d -o root -g root -m 0755 "$(dirname "$DESTINATION")"
sudo install -o root -g root -m 0755 "$SOURCE" "$DESTINATION"
sudo install -o root -g root -m 0644 "$SCRIPT_DIR/agent-benchmark-profile.py" \
    "$(dirname "$DESTINATION")/agent-benchmark-profile.py"
sudo install -o root -g root -m 0644 "$SCRIPT_DIR/agent-reservation-broker.py" \
    "$(dirname "$DESTINATION")/agent-reservation-broker.py"
sudo install -o root -g root -m 0755 "$RUN_SOURCE" "$RUN_DESTINATION"
sudo install -o root -g root -m 0644 "$SCRIPT_DIR/agent-cpu-reserve.tmpfiles" \
    /etc/tmpfiles.d/agent-cpu-reserve.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/agent-cpu-reserve.conf

echo "Installed $DESTINATION"
echo "Installed $RUN_DESTINATION"
echo "Sudoers unchanged; only starting a host reservation requires sudo."
echo "agent-sandbox --reserve-cores N will ask for sudo when it starts."
echo "Inside the session, run benchmarks as: agent-reserved-run COMMAND ..."
echo "Emergency cleanup: sudo $DESTINATION reset"
echo "Standalone benchmarking: agent-reserved-run --profile stable --cores 24 -- COMMAND ..."
echo "Shared host session: agent-reserved-run --profile stable --cores 24 --session -- bash"
