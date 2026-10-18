#!/usr/bin/env bash
#
# restore.sh - removes the HID lab and returns Bluetooth to its normal state
#
# Usage:   sudo scripts/restore.sh
#
set -uo pipefail

log()  { printf '\033[1;36m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "[x] Run with sudo:  sudo scripts/restore.sh" >&2; exit 1; }

log "Stopping the HID script..."
pkill -f 'hid_keyboard\.py' 2>/dev/null || true

# The daemon unregisters the HID record and the pairing agent on SIGTERM,
# so give it a chance before force-killing it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log "Stopping the HID daemon (unregisters the record and agent)..."
pkill -TERM -f 'hid_daemon\.py' 2>/dev/null || true
sleep 1.5
pkill -f 'hid_daemon\.py' 2>/dev/null || true

log "Stopping any leftovers (SDP server / bluetoothctl agent / registration helper)..."
pkill -f 'hid_sdp_server\.py' 2>/dev/null || true
pkill -f 'register_hid_sdp\.py' 2>/dev/null || true
pkill -f 'bluetoothctl --agent' 2>/dev/null || true

log "Stopping the manual bluetoothd..."
pkill -x bluetoothd 2>/dev/null || true
sleep 1

# Stop the lab's private D-Bus bus (if setup.sh started it)
log "Stopping the lab private D-Bus bus..."
pkill -f 'dbus-daemon --session --address=unix:path=/run/bluetooth-lab-bus' 2>/dev/null || true
rm -f /run/bluetooth-lab-bus 2>/dev/null || true

# Restart the audio/OBEX services that setup.sh stopped during the lab
LAB_USER="${SUDO_USER:-}"
if [[ -z "$LAB_USER" || "$LAB_USER" == "root" ]]; then
    LAB_USER="$(logname 2>/dev/null || echo "")"
fi
LAB_UID="$(id -u "$LAB_USER" 2>/dev/null || echo "")"
if [[ -n "$LAB_USER" && -n "$LAB_UID" && -d "/run/user/$LAB_UID" ]]; then
    as_lab_user() {
        if command -v runuser >/dev/null 2>&1; then
            runuser -u "$LAB_USER" -- env XDG_RUNTIME_DIR="/run/user/$LAB_UID" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$LAB_UID/bus" "$@"
        else
            sudo -u "$LAB_USER" env XDG_RUNTIME_DIR="/run/user/$LAB_UID" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$LAB_UID/bus" "$@"
        fi
    }
    log "Restarting audio/OBEX services..."
    for svc in pipewire.service pipewire-pulse.service wireplumber.service obex.service; do
        as_lab_user systemctl --user start "$svc" 2>/dev/null \
            && ok "restarted $svc" || true
    done
    as_lab_user systemctl --user start wireplumber.service 2>/dev/null || true
else
    warn "Could not determine the user session - restart audio services manually if needed:"
    warn "  systemctl --user start pipewire pipewire-pulse wireplumber obex"
fi

log "Restoring main.conf to defaults if needed..."
MAINCONF="/etc/bluetooth/main.conf"
if [[ -f "$MAINCONF" ]]; then
    # Only our own lab values are commented out. If an older private build set a
    # different Name= here, remove that line manually.
    sed -i \
        -e 's/^Name = HID Research Keyboard$/#Name = BlueZ X.YZ/' \
        -e 's/^Class = 0x002540$/#Class = 0x000000/' \
        "$MAINCONF" 2>/dev/null || true
    ok "main.conf cleaned up"
fi

log "Restarting the standard bluetooth service..."
systemctl unmask --runtime bluetooth 2>/dev/null || true
systemctl reset-failed bluetooth 2>/dev/null || true
systemctl restart bluetooth 2>/dev/null || true
sleep 2

if systemctl is-active --quiet bluetooth; then
    ok "bluetooth.service is running"
else
    warn "bluetooth.service is not running - check:  journalctl -u bluetooth -e"
fi

HCIDEV="$(hciconfig 2>/dev/null | sed -n 's/^\(hci[0-9]\+\):.*/\1/p' | head -1)"
if [[ -n "$HCIDEV" ]]; then
    IDX="${HCIDEV#hci}"
    # Important: btmgmt enters interactive mode and waits on stdin - pass stdin=/dev/null
    # or it gets killed by the timeout. Also btmgmt class expects <major> <minor>, not a full CoD.
    ORIG_COD=0x7c010c
    btmgmt --index "$IDX" name "$(hostname)" </dev/null >/dev/null 2>&1 || true
    # MGMT expects minor shifted (BlueZ src/adapter.c: cp.minor = minor_class << 2)
    btmgmt --index "$IDX" class $(( (ORIG_COD >> 8) & 0x1F )) $(( ((ORIG_COD >> 2) & 0x3F) << 2 )) \
        </dev/null >/dev/null 2>&1 || true
    # Fallback via hciconfig (its writes work even if the mgmt command is refused)
    hciconfig "$HCIDEV" name "$(hostname)" 2>/dev/null || true
    hciconfig "$HCIDEV" class "$ORIG_COD" 2>/dev/null || true
    bluetoothctl reset-alias </dev/null >/dev/null 2>&1 || true
    bluetoothctl discoverable on </dev/null >/dev/null 2>&1 || true
    bluetoothctl pairable on </dev/null >/dev/null 2>&1 || true
    hciconfig "$HCIDEV" piscan 2>/dev/null || true
    echo
    echo "-- btmgmt info --"
    timeout 10 btmgmt --index "$IDX" info </dev/null 2>/dev/null \
        | grep -E "addr |class |name |current settings" || true
fi

echo
ok "Restore complete. Bluetooth is back to normal now."
echo "   (to start the lab again:  sudo scripts/setup.sh)"
