#!/usr/bin/env bash
# gui.sh - launch the Bluetooth HID lab terminal dashboard.
# It re-execs itself with sudo (lab_tui.py performs the elevation).
#
#   ./scripts/gui.sh
#   ./scripts/gui.sh -u "https://example.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/../src/lab_tui.py" "$@"
