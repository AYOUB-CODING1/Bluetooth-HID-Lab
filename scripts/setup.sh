#!/usr/bin/env bash
#
# setup.sh - prepares the Bluetooth HID lab environment, then runs the payload
#
# Usage:       sudo scripts/setup.sh
# Restore:     sudo scripts/restore.sh
#
set -euo pipefail

# ---------------- Settings ----------------
DEVICE_NAME="HID Research Keyboard"
DEVICE_CLASS="0x002540"          # Peripheral + Keyboard
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/../src/hid_keyboard.py"
SDP_HELPER="$SCRIPT_DIR/../src/register_hid_sdp.py"
DAEMON_LOG="/tmp/bluetoothd-hid.log"
BTCTL_LOG="/tmp/bluetoothctl-hid.log"
FORGET_MAC=""
NO_SDP=0
PAYLOAD_URL=""                    # -u/--url: website to open (passed to the payload)
# private = bluetoothd on a lab-private D-Bus bus (default, isolates us from
#           pipewire/wireplumber/obexd so they can never advertise HFP/A2DP/OBEX)
# system  = the real bus (diagnostics only)
LAB_BUS_MODE="${LAB_BUS:-private}"
LAB_BUS_SOCK="/run/bluetooth-lab-bus"
LAB_BUS_ADDR="unix:path=$LAB_BUS_SOCK"
LAB_BUS_PID=""

usage() {
    cat <<EOF
Usage: sudo scripts/setup.sh [--forget <MAC>] [--forget-all-lab] [--no-sdp] [--system-bus]
                       [--tool <script>] [-u <URL>]

  --forget <MAC>       remove the pairing of a specific device on our side (useful for
                       the Authentication Failed / status 0x05 error)
  --forget-all-lab     remove all paired devices (warning: includes your headphones)
  --no-sdp             skip SDP record registration (to diagnose channel issues)
  --system-bus         use the real D-Bus bus instead of the private one (diagnostics)
  --tool <script>      payload to run in step 9 instead of src/hid_keyboard.py
                       (e.g. --tool hid_keyboard_bridge.py -> src/hid_keyboard_bridge.py)
  -u, --url <URL>      website to open (passed through to the payload), e.g.
                       sudo scripts/setup.sh --forget AA:BB:CC:DD:EE:FF -u "https://example.com"
  -h, --help           this message

Important: after every failed pairing attempt, also delete the device on your phone
     (Bluetooth settings -> long-press the name -> Forget/Remove)
     otherwise an old link key is stored and matching always fails.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --forget)
            [[ $# -ge 2 ]] || { echo "[x] --forget requires a MAC address" >&2; exit 1; }
            FORGET_MAC="$2"; shift 2 ;;
        --forget-all-lab) FORGET_MAC="__ALL__"; shift ;;
        --no-sdp) NO_SDP=1; shift ;;
        --system-bus) LAB_BUS_MODE="system"; shift ;;
        -u|--url)
            [[ $# -ge 2 ]] || { echo "[x] -u/--url requires a URL" >&2; exit 1; }
            PAYLOAD_URL="$2"; shift 2 ;;
        --tool)
            [[ $# -ge 2 ]] || { echo "[x] --tool requires a script name" >&2; exit 1; }
            case "$2" in
                */*) PYTHON_SCRIPT="$2" ;;
                *)   PYTHON_SCRIPT="$SCRIPT_DIR/../src/$2" ;;
            esac
            [[ -f "$PYTHON_SCRIPT" ]] || { echo "[x] no such script: $PYTHON_SCRIPT" >&2; exit 1; }
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[x] unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

log()  { printf '\033[1;36m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# If the script fails, re-enable the bluetooth service so the system is not left without Bluetooth
# (we skip this on Ctrl+C = code 130 so the lab daemon keeps running
#  and you can re-run the script directly)
on_fail() {
    local rc=$?
    if [[ $rc -ne 0 && $rc -ne 130 ]]; then
        printf '\033[1;33m[!]\033[0m Script failed (code %s). Re-enabling the bluetooth service...\n' "$rc"
        systemctl unmask --runtime bluetooth 2>/dev/null || true
        # Note: we do NOT kill the lab-private bus here. The trap runs on any
        # non-zero exit - including when the lab is fully running and a trivial
        # final check failed. Killing the bus would drop bluetoothd and the daemon.
        # restore.sh handles the real cleanup.
        # Restart the audio/OBEX services so the machine is not left without sound due to a lab failure
        if [[ -n "${LAB_USER:-}" && -n "${LAB_UID:-}" && -d "/run/user/${LAB_UID:-}" ]]; then
            printf '\033[1;33m[!]\033[0m Restarting audio/OBEX services...\n'
            for svc in pipewire.service pipewire-pulse.service wireplumber.service obex.service; do
                as_lab_user systemctl --user start "$svc" 2>/dev/null || true
            done
        fi
    fi
}
trap on_fail EXIT

# ---------------- 0. Privileges ----------------
[[ $EUID -eq 0 ]] || die "Run this script with sudo:  sudo scripts/setup.sh"

# ---------------- 1. Clean up old processes ----------------
# IMPORTANT: stop the systemd service first, otherwise it comes back immediately and races
# the new daemon for the D-Bus name (this is what happened in the previous attempt).
log "Stopping the default bluetooth service..."
systemctl stop bluetooth 2>/dev/null || true
systemctl reset-failed bluetooth 2>/dev/null || true
# Prevent any automatic restart during the lab (temporary only, gone after reboot)
systemctl mask --runtime bluetooth 2>/dev/null || true

log "Stopping any old processes (python / bluetoothctl / bluetoothd)..."
pkill -f 'hid_keyboard\.py' 2>/dev/null || true
pkill -f 'hid_daemon\.py' 2>/dev/null || true
pkill -f 'hid_sdp_server\.py' 2>/dev/null || true
pkill -f 'register_hid_sdp\.py' 2>/dev/null || true
pkill -f 'bluetoothctl' 2>/dev/null || true
pkill -f "dbus-daemon --session --address=$LAB_BUS_ADDR" 2>/dev/null || true
rm -f "$LAB_BUS_SOCK" 2>/dev/null || true
pkill -x bluetoothd 2>/dev/null || true
sleep 1
for _ in 1 2 3 4 5; do
    pgrep -x bluetoothd >/dev/null 2>&1 || break
    pkill -9 -x bluetoothd 2>/dev/null || true
    sleep 1
done
if pgrep -x bluetoothd >/dev/null 2>&1; then
    warn "bluetoothd is still running - it may hold PSM 0x11/0x13:"
    ps -o pid,cmd -C bluetoothd
fi

# ---------------- 2. Adapter ----------------
log "rfkill unblock bluetooth..."
rfkill unblock bluetooth 2>/dev/null || true
sleep 1

HCIDEV="$(hciconfig 2>/dev/null | sed -n 's/^\(hci[0-9]\+\):.*/\1/p' | head -1)"
[[ -n "$HCIDEV" ]] || die "No HCI adapter found. Try: lsusb / rfkill list"
HCIMAC="$(hciconfig "$HCIDEV" 2>/dev/null | sed -n 's/.*BD Address: *\([0-9A-Fa-f:]\{17\}\).*/\1/p' | tr 'a-f' 'A-F')"
[[ -n "$HCIMAC" ]] || die "Could not read BD Address from $HCIDEV"
log "Adapter: $HCIDEV  ($HCIMAC)"

# ---------------- 2b. Lab-private D-Bus bus ----------------
# The second root cause of the phone's 'connected for calls' message:
#   pipewire/wireplumber (via libspa-bluez5) and obexd register HFP/HSP/A2DP/OBEX
#   with org.bluez.ProfileManager1 on the *real system bus*, so our device advertises
#   audio services and the phone connects to it as a headset. Stopping the services is
#   unreliable (they come back automatically).
# The decisive fix: run bluetoothd on a *private* D-Bus bus nobody else knows about,
#   and export DBUS_SYSTEM_BUS_ADDRESS so bluetoothctl/busctl/dbus-python
#   (and the HID daemon) all stay on the same bus. Then audio services disappear completely.
# Note: this does not touch the real bus, so the rest of the system keeps working as-is.
if [[ "$LAB_BUS_MODE" == "private" ]]; then
    command -v dbus-daemon >/dev/null 2>&1 || die "dbus-daemon not found (apt install dbus)"
    log "Starting the lab-private D-Bus bus ($LAB_BUS_ADDR)..."
    rm -f "$LAB_BUS_SOCK" 2>/dev/null || true
    nohup dbus-daemon --session --address="$LAB_BUS_ADDR" --nofork --print-address \
          >/tmp/bluetooth-lab-bus.log 2>&1 &
    LAB_BUS_PID=$!
    for _ in $(seq 1 20); do
        [[ -S "$LAB_BUS_SOCK" ]] && break
        sleep 0.25
    done
    if [[ ! -S "$LAB_BUS_SOCK" ]]; then
        kill "$LAB_BUS_PID" 2>/dev/null || true
        die "Failed to create the lab bus (see /tmp/bluetooth-lab-bus.log)"
    fi
    export DBUS_SYSTEM_BUS_ADDRESS="$LAB_BUS_ADDR"
    # Real check: can a client request a name on this bus?
    if ! python3 -c "
import dbus
b = dbus.SystemBus()
n = b.list_names()
assert 'org.freedesktop.DBus' in n, n
" 2>/dev/null; then
        die "The lab bus does not respond to dbus-python"
    fi
    ok "Private D-Bus bus running (PID $LAB_BUS_PID) - isolated from pipewire/obexd/blueman"
else
    warn "--system-bus: using the real system bus (pipewire may register audio services)"
fi

# Read the list of UUIDs that BlueZ advertises for the adapter. This is the 'official
# truth': the phone builds its service discovery on it (src/adapter.c:property_get_uuids
# reads adapter->services, i.e. the SDP records actually registered). Having 00001124
# here is conclusive proof the HID record entered the server, unlike RegisterProfile alone.
adapter_uuids() {
    busctl --system get-property org.bluez "/org/bluez/$HCIDEV" \
           org.bluez.Adapter1 UUIDs 2>/dev/null && return 0
    if [[ -n "${DBUS_SYSTEM_BUS_ADDRESS:-}" ]]; then
        busctl --address="$DBUS_SYSTEM_BUS_ADDRESS" get-property org.bluez \
               "/org/bluez/$HCIDEV" org.bluez.Adapter1 UUIDs 2>/dev/null && return 0
    fi
    return 1
}

# ---------------- 2c. Silencing audio/OBEX services ----------------
# Additional fallback (step 2b/the private bus is the primary fix):
#   * wireplumber/pipewire (via libspa-bluez5) register HFP/HSP with
#     org.bluez.ProfileManager1, so our device advertises a Handsfree service (0x111E).
#     The phone sees 0x111E and 'connects for calls' and never opens 0x11/0x13.
#   * obexd registers MAP/OPP, and blueman may compete with us for the pairing agent.
# We stop them temporarily during the lab. restore.sh restarts them.
LAB_USER="${SUDO_USER:-}"
if [[ -z "$LAB_USER" || "$LAB_USER" == "root" ]]; then
    LAB_USER="$(logname 2>/dev/null || echo "")"
fi
LAB_UID="$(id -u "$LAB_USER" 2>/dev/null || echo "")"

if [[ -n "$LAB_USER" && -n "$LAB_UID" && -d "/run/user/$LAB_UID" ]]; then
    as_lab_user() {
        if command -v runuser >/dev/null 2>&1; then
            runuser -u "$LAB_USER" -- env \
                XDG_RUNTIME_DIR="/run/user/$LAB_UID" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$LAB_UID/bus" "$@"
        else
            sudo -u "$LAB_USER" env \
                XDG_RUNTIME_DIR="/run/user/$LAB_UID" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$LAB_UID/bus" "$@"
        fi
    }

    log "Stopping services that register unwanted profiles with bluetoothd (user: $LAB_USER)..."
    for svc in wireplumber.service obex.service pipewire-pulse.service pipewire.service; do
        if as_lab_user systemctl --user is-active --quiet "$svc" 2>/dev/null; then
            if as_lab_user systemctl --user stop "$svc" 2>/dev/null; then
                ok "stopped $svc (temporarily)"
            else
                warn "could not stop $svc"
            fi
        fi
    done
    # blueman-applet registers a pairing agent that may compete with us
    pkill -u "$LAB_USER" -f 'blueman-applet' 2>/dev/null \
        && ok "stopped blueman-applet (temporarily)" || true
    sleep 1

    # Check: did the pipewire/wireplumber/obexd processes actually go away?
    LEFT="$(ps -u "$LAB_USER" -o comm= 2>/dev/null \
        | grep -xE 'wireplumber|pipewire|pipewire-pulse|obexd' | sort -u | tr '\n' ' ' || true)"
    if [[ -n "$LEFT" ]]; then
        warn "still running: $LEFT - they may register HFP/OBEX when bluetoothd starts"
    else
        ok "no active audio/OBEX services (bluetoothd will be clean)"
    fi
else
    warn "Could not determine the user session - skipping audio service silencing."
    warn "If the phone shows 'connected for calls', run manually:"
    warn "  systemctl --user stop wireplumber pipewire pipewire-pulse obex"
fi

# ---------------- 3. Start bluetoothd in compat mode ----------------
log "Looking for the correct bluetoothd path..."
BLUEZD=""
for p in /usr/libexec/bluetooth/bluetoothd /usr/sbin/bluetoothd \
         /usr/lib/bluetooth/bluetoothd /usr/local/libexec/bluetooth/bluetoothd; do
    if [[ -x "$p" ]]; then BLUEZD="$p"; break; fi
done
[[ -n "$BLUEZD" ]] || die "bluetoothd not found. Install bluez:  sudo apt install -y bluez"
ok "bluetoothd = $BLUEZD"

# Extra plugins advertise services that are not keyboard related (audio/AVRCP/MAP/lirc...),
# so the phone sees a multi-service device and tries to talk to them. We saw in the log an
# attempt to authorize 0x111E with disconnect reason 21 - which we prevent by making the
# device a pure keyboard. Note: unknown plugin names are silently ignored (tested), so the list is safe.
NOPLUGIN_FULL="input,hostname,a2dp,avrcp,midi,sixaxis,neard,autopair,battery"
NOPLUGIN_MIN="input,hostname"

log "Starting bluetoothd --compat --noplugin=$NOPLUGIN_FULL ..."
# --noplugin accepts ONE comma-separated list. Passing --noplugin twice makes the
# second OVERWRITE the first, which silently re-enables the 'input' plugin.
# 'input' is what binds L2CAP PSM 0x11/0x13 -- if it loads, our script gets EADDRINUSE.
# 'hostname' overrides Name= from main.conf with the machine hostname.
NOPLUGIN_USED="$NOPLUGIN_FULL"
nohup "$BLUEZD" --compat --noplugin="$NOPLUGIN_FULL" --nodetach \
      >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
sleep 2

# If the daemon rejects the extended list, retry with the minimal list
# (the hostname plugin alone does not matter - the name is set directly on the adapter in step 5)
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    warn "Startup failed with --noplugin=$NOPLUGIN_FULL - retrying with $NOPLUGIN_MIN..."
    NOPLUGIN_USED="$NOPLUGIN_MIN"
    nohup "$BLUEZD" --compat --noplugin="$NOPLUGIN_MIN" --nodetach \
          >"$DAEMON_LOG" 2>&1 &
    DAEMON_PID=$!
    sleep 2
fi

if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    warn "Failed to start bluetoothd. Log:"
    tail -20 "$DAEMON_LOG" || true
    die "see $DAEMON_LOG"
fi
ok "bluetoothd running (PID $DAEMON_PID) - log: $DAEMON_LOG"

# Critical check: was the 'input' plugin actually excluded?
# If the line "Excluding (cli) input" is missing, the plugin is loaded and holds PSM 0x11/0x13.
if ! grep -q "Excluding (cli) input" "$DAEMON_LOG" 2>/dev/null; then
    warn "The 'input' plugin was not excluded! Excluding lines in the log:"
    grep "Excluding" "$DAEMON_LOG" || warn "  (no Excluding lines at all)"
    warn "The input plugin holds L2CAP PSM 0x11 and 0x13 - the script will not be able to open them."
    die "Make sure the line contains --noplugin=$NOPLUGIN_USED (a single comma-separated list)"
fi
ok "'input' plugin excluded (will not hold PSM 0x11/0x13)"

# Make sure the daemon that owns D-Bus is our daemon (with the same --noplugin=input)
# otherwise another bluetoothd (that loads the input plugin and holds PSM 0x11/0x13) is still running.
OWN_CMD="$(tr '\0' ' ' <"/proc/$DAEMON_PID/cmdline" 2>/dev/null || true)"
if [[ "$OWN_CMD" != *"--noplugin=input"* ]]; then
    warn "The current daemon is not the lab build: $OWN_CMD"
    warn "The 'input' plugin in the default bluetoothd holds PSM 0x11/0x13 - the script will not work."
    die "Retry: sudo scripts/setup.sh  or  see $DAEMON_LOG"
fi
ok "The lab daemon owns D-Bus: $OWN_CMD"

# bluetoothctl list prints the address (BD_ADDR), not 'hci0' - so we search by address
log "Waiting for the controller to appear on D-Bus..."
CTRL_LIST=""
for i in $(seq 1 30); do
    CTRL_LIST="$(bluetoothctl list </dev/null 2>/dev/null || true)"
    if printf '%s' "$CTRL_LIST" | grep -qi "$HCIMAC"; then
        break
    fi
    sleep 0.5
    if [[ $i -eq 30 ]]; then
        warn "bluetoothctl list said:"
        printf '%s\n' "$CTRL_LIST"
        warn "Last daemon log lines:"
        tail -20 "$DAEMON_LOG" || true
        die "The controller did not appear on D-Bus within 15 seconds"
    fi
done
ok "The controller appeared on D-Bus: $(printf '%s' "$CTRL_LIST" | head -1)"

# ---------------- 4. Power on the adapter ----------------
# Important: btmgmt enters "interactive mode" and waits on stdin forever.
# Without redirecting stdin=/dev/null the command gets killed by the timeout and breaks startup.
# Therefore every btmgmt command goes through mgmt() below.
mgmt()     { timeout 10 btmgmt --index "${HCIDEV#hci}" "$@" </dev/null >/dev/null 2>&1 || true; }
mgmt_info(){ timeout 10 btmgmt --index "${HCIDEV#hci}" info   </dev/null 2>/dev/null || true; }

# Write the name/class/discoverability to the adapter. Used in step 5 and also after any
# adapter restart (step 6a) because power off/on reverts the values to main.conf.
# Important note about the class: MGMT_OP_SET_DEV_CLASS expects minor *shifted* left
# by 2 (see BlueZ src/adapter.c:507: cp.minor = adapter->minor_class << 2;
# it works around a kernel bug). So we pass CLASS_MINOR << 2, not CLASS_MINOR.
# The hciconfig write is only a fallback: its raw value is overwritten on any power cycle
# because the kernel re-applies the saved MGMT value.
apply_identity() {
    mgmt name "$DEVICE_NAME"
    mgmt class "$CLASS_MAJOR" "$(( CLASS_MINOR << 2 ))"
    hciconfig "$HCIDEV" name "$DEVICE_NAME" 2>/dev/null || true    # fallback
    hciconfig "$HCIDEV" class "$DEVICE_CLASS" 2>/dev/null || true  # fallback
    bluetoothctl system-alias "$DEVICE_NAME" </dev/null >/dev/null 2>&1 || true
    hciconfig "$HCIDEV" piscan 2>/dev/null || true
    bluetoothctl discoverable on </dev/null >/dev/null 2>&1 || true
    bluetoothctl pairable on </dev/null >/dev/null 2>&1 || true
}

# btmgmt class expects <major> <minor> separately, not a full CoD value
COD=$((DEVICE_CLASS))
CLASS_MAJOR=$(( (COD >> 8) & 0x1F ))
CLASS_MINOR=$(( (COD >> 2) & 0x3F ))
log "Class $DEVICE_CLASS  =>  btmgmt class $CLASS_MAJOR $(( CLASS_MINOR << 2 ))  (major/minor $CLASS_MAJOR/$CLASS_MINOR)"

log "Powering on the adapter..."
mgmt power on
hciconfig "$HCIDEV" up 2>/dev/null || true
sleep 1

# ---------------- 5. Name + class (the most important step) ----------------
# Writes go through btmgmt (mgmt - the same one bluetoothd uses) + hciconfig as a fallback.
# Reads/verification use btmgmt info only, because hciconfig loses read access
# while bluetoothd holds the adapter.
log "Setting the name and class directly on the adapter..."
apply_identity

# Read from btmgmt info (reliable) - not from hciconfig
MGMT_INFO="$(mgmt_info)"
CURRENT_NAME="$(printf '%s\n' "$MGMT_INFO" | sed -n 's/^\s*name \(.*\)$/\1/p' | head -1)"
CURRENT_CLASS="$(printf '%s\n' "$MGMT_INFO" | sed -n 's/.*class \(0x[0-9a-fA-F]*\).*/\1/p' | head -1)"
CURRENT_ALIAS="$(bluetoothctl show </dev/null 2>/dev/null | sed -n 's/^\s*Alias: \(.*\)$/\1/p' || true)"

if [[ "$CURRENT_NAME" == "$DEVICE_NAME" ]]; then
    ok "Adapter name: $CURRENT_NAME"
else
    warn "Adapter name is '${CURRENT_NAME:-unknown}', not '$DEVICE_NAME'"
fi

if [[ "$CURRENT_ALIAS" == "$DEVICE_NAME" ]]; then
    ok "Alias (what the phone sees in the scan): $CURRENT_ALIAS"
else
    warn "Alias is '${CURRENT_ALIAS:-unknown}' - the correct name may not appear in the scan"
fi

# We verify only the major/minor fields. The upper bits (Major Service Class) may differ
# depending on who wrote the last value (bluetoothd from main.conf vs raw hciconfig),
# and what matters to the phone is major=Peripheral and minor=Keyboard.
CUR_MAJOR=""; CUR_MINOR=""
if [[ -n "$CURRENT_CLASS" ]]; then
    CUR_MAJOR=$(( (CURRENT_CLASS >> 8) & 0x1F ))
    CUR_MINOR=$(( (CURRENT_CLASS >> 2) & 0x3F ))
fi
if [[ -n "$CURRENT_CLASS" && "$CUR_MAJOR" == "$CLASS_MAJOR" && "$CUR_MINOR" == "$CLASS_MINOR" ]]; then
    ok "Device class: $CURRENT_CLASS (major=$CUR_MAJOR Peripheral, minor=$CUR_MINOR Keyboard)"
else
    warn "Class '${CURRENT_CLASS:-unknown}' = major ${CUR_MAJOR:-?}/minor ${CUR_MINOR:-?}"
    warn "Wanted major=$CLASS_MAJOR/minor=$CLASS_MINOR ($DEVICE_CLASS = keyboard)"
    warn "Without the correct class the phone may show the wrong type (the connection itself relies on SDP)"
fi

# ---------------- 6. discoverable / pairable + pairing agent ----------------
log "Enabling discoverable/pairable without timeout..."
btc() { timeout 10 bluetoothctl "$@" </dev/null >/dev/null 2>&1 || true; }
btc power on
btc discoverable-timeout 0
btc pairable-timeout 0
btc pairable on
btc discoverable on
hciconfig "$HCIDEV" piscan 2>/dev/null || true

log "Starting the daemon (pairing agent + HID record in SDP)..."
# Important note about the previous failure:
#   bluetoothctl --agent NoInputNoOutput through a pipe showed an interactive
#   "(yes/no)" prompt that nobody answered, so BlueZ canceled the request and disconnected the phone (reason 21).
#   So now we use a real D-Bus agent that answers every call programmatically,
#   and in the same process registers the HID record via ProfileManager1 (signature o s a{sv}).
DAEMON_LOG_HID="/tmp/hid-daemon.log"
: >"$DAEMON_LOG_HID"
DAEMON_ARGS=(--bdaddr "$HCIMAC")
[[ $NO_SDP -eq 1 ]] && DAEMON_ARGS+=(--no-profile)
python3 "$SCRIPT_DIR/../src/hid_daemon.py" "${DAEMON_ARGS[@]}" >"$DAEMON_LOG_HID" 2>&1 &
HID_DAEMON_PID=$!

HID_READY=0
for _ in $(seq 1 60); do
    if grep -q "^\[daemon\] ready" "$DAEMON_LOG_HID" 2>/dev/null; then HID_READY=1; break; fi
    if ! kill -0 "$HID_DAEMON_PID" 2>/dev/null; then break; fi
    sleep 0.25
done

if [[ $HID_READY -eq 1 ]]; then
    ok "Daemon ready (PID $HID_DAEMON_PID) - log: $DAEMON_LOG_HID"
    grep -E "agent registered|profile registered|verified|NOT verified" "$DAEMON_LOG_HID" | sed 's/^/    /' || true
else
    warn "The daemon did not reach the ready state. Log:"
    sed 's/^/    /' "$DAEMON_LOG_HID" 2>/dev/null || true
fi

DISC="$(bluetoothctl show </dev/null 2>/dev/null | sed -n 's/.*Discoverable: \(yes\|no\).*/\1/p' || true)"
[[ "$DISC" == "yes" ]] && ok "Discoverable: yes (no timeout)" || warn "Discoverable=$DISC - check $DAEMON_LOG_HID"

# ---------------- 6a. Confirm the HID record entered adapter->services ----------------
# A successful RegisterProfile does not prove BlueZ actually added the record: ext_adapter_probe
# returns 0 even if adapter_service_insert ignored the record. The conclusive check is to read
# the adapter's UUIDs property and look for 00001124.
# If it is missing, we power-cycle the adapter: on startup BlueZ calls
# btd_profile_foreach(probe_profile, adapter), re-inserting the records of every registered
# profile (adapter.c). Then we re-apply name/class/discoverability via apply_identity.
hid_uuid_present() {
    local uu
    uu="$(adapter_uuids || true)"
    printf '%s' "$uu" | grep -qi '00001124'
}

if [[ $NO_SDP -eq 0 ]]; then
    log "Confirming HID (0x1124) appears in the adapter UUIDs property..."
    HID_UUID_OK=0
    for _ in $(seq 1 10); do
        if hid_uuid_present; then HID_UUID_OK=1; break; fi
        sleep 0.5
    done
    if [[ $HID_UUID_OK -eq 1 ]]; then
        ok "0x1124 (HID) advertised in UUIDs - the phone will see the input service"
    else
        warn "0x1124 not visible in UUIDs after registration. Power-cycling the adapter to try the insert..."
        warn "  (current UUIDs: $(adapter_uuids || true))"
        mgmt power off
        sleep 1
        mgmt power on
        sleep 1
        apply_identity
        for _ in $(seq 1 20); do
            if hid_uuid_present; then HID_UUID_OK=1; break; fi
            sleep 0.5
        done
        if [[ $HID_UUID_OK -eq 1 ]]; then
            ok "After the restart: 0x1124 (HID) advertised in UUIDs"
        else
            warn "0x1124 still missing after the restart. UUIDs: $(adapter_uuids || true)"
            warn "This means BlueZ rejected the record (check the XML validity in the daemon log)."
        fi
    fi
fi

# ---------------- 6b. Clean up old pairings ----------------
        # The error "auth failed with status 0x05" after one second means an old link key:
# the system remembers a key from a previous attempt, the phone remembers another, so the exchange fails
# before any prompt reaches the agent. The fix: clear both sides together.
echo
echo "-- Currently paired devices --"
PAIRED="$(timeout 10 bluetoothctl devices Paired </dev/null 2>/dev/null || true)"
if [[ -n "$PAIRED" ]]; then
    printf '%s\n' "$PAIRED"
else
    log "  no paired devices"
fi

if [[ "$FORGET_MAC" == "__ALL__" ]]; then
    warn "--forget-all-lab: removing all paired devices"
    while read -r _ mac _; do
        [[ -z "$mac" ]] && continue
        log "  removing $mac"
        btc remove "$mac"
    done <<<"$PAIRED"
elif [[ -n "$FORGET_MAC" ]]; then
    mac_upper="$(printf '%s' "$FORGET_MAC" | tr 'a-f' 'A-F')"
    if printf '%s' "$PAIRED" | grep -qi "$mac_upper"; then
        log "removing the old pairing $mac_upper"
        btc remove "$mac_upper"
        # bluetoothctl remove may not delete the key file; we delete it manually
        rm -rf "/var/lib/bluetooth/$HCIMAC/$mac_upper" \
               "/var/lib/bluetooth/$HCIMAC/${mac_upper//:/}" 2>/dev/null || true
        ok "cleared the local link key $mac_upper"
    else
        warn "$mac_upper is not in the paired list - nothing to delete"
    fi
else
    warn "If the auth failed / status 0x05 error appears at pairing:"
    warn "  1) remove '$DEVICE_NAME' from the phone's Bluetooth list"
    warn "  2) run:  sudo scripts/setup.sh --forget AA:BB:CC:DD:EE:FF"
    warn "  (replace the MAC with your phone's address if different)"
fi
echo

# ---------------- 7. HID record in SDP ----------------
# The HID record is registered by hid_daemon.py via ProfileManager1 (signature o s a{sv}),
# so it enters the SDP server owned by bluetoothd itself on L2CAP PSM 1 - the very server
# the phone asks. We do not need our own SDP server, and we cannot:
# our attempt failed with EADDRINUSE because bluetoothd already reserves PSM 1.
# The real verification is in the "Final verification" section below.

# ---------------- 8. Verification ----------------
echo
echo "================= Final verification ================="
echo "-- org.bluez.Adapter1.UUIDs (the official truth - what the phone's discovery is built on) --"
ADV_UUIDS="$(adapter_uuids || true)"
if [[ -n "$ADV_UUIDS" ]]; then
    printf '%s\n' "$ADV_UUIDS" | sed 's/^/    /'
else
    warn "  could not read the property (busctl failed) - bus: ${DBUS_SYSTEM_BUS_ADDRESS:-<system>}"
fi
if printf '%s' "$ADV_UUIDS" | grep -qi '00001124'; then
    ok "0x1124 (HID) advertised - the phone will recognize us as an input device"
else
    warn "0x1124 (HID) not advertised - the phone will not see the input service (this is the bug)"
fi
echo
echo "-- btmgmt info (reliable - hciconfig reads unreliably) --"
mgmt_info | grep -E "addr |class |name |current settings" || true
echo
echo "-- bluetoothctl show --"
bluetoothctl show </dev/null 2>/dev/null | grep -iE "Name:|Alias:|Class:|Powered:|Discoverable:|Pairable:" || true
echo
echo "-- HID record in SDP --"
# Double check:
#  (1) daemon log: BlueZ answers RegisterProfile either with success or an explicit error.
#  (2) sdptool browse local: reads the records registered inside bluetoothd itself,
#      so HID (0x1124) must appear there - which is what the phone actually "sees".
HID_IN_DAEMON=0
grep -q "profile registered" "$DAEMON_LOG_HID" 2>/dev/null && HID_IN_DAEMON=1

LOCAL_SDP="$(timeout 20 sdptool browse local 2>/dev/null || true)"
HID_IN_BROWSE=0
printf '%s' "$LOCAL_SDP" | grep -qiE "0x1124|Human Interface" && HID_IN_BROWSE=1

if [[ $HID_IN_DAEMON -eq 1 ]]; then
    grep -E "profile registered|agent registered|verified|NOT verified" "$DAEMON_LOG_HID" | sed 's/^/    /' || true
else
    warn "The daemon log does not mention 'profile registered':"
    tail -20 "$DAEMON_LOG_HID" 2>/dev/null | sed 's/^/    /' || true
fi

if [[ $HID_IN_BROWSE -eq 1 ]]; then
    printf '%s' "$LOCAL_SDP" | grep -iB1 -A3 "0x1124\|Human Interface" | sed 's/^/    /' || true
    ok "The phone can read the HID record from bluetoothd's own SDP server"
elif [[ $HID_IN_DAEMON -eq 1 ]]; then
    warn "Registration succeeded but the record did not appear in sdptool browse local (may be a delay)"
else
    warn "No HID record - the phone will not discover the input device"
fi

echo "-- All advertised services (what the phone sees) --"
printf '%s' "$LOCAL_SDP" \
    | sed -n 's/.*Service Name: \(.*\)/    \1/p' | sort -u || true

# Detect the audio/OBEX services that make the phone think we are a headset ("connected for calls"):
#   HF 0x111E, AG 0x111F, HSP 0x1108/0x1112, A2DP 0x110B/0x110A,
#   AVRCP 0x110E/0x110C, GenericAudio 0x1203, MAP 0x1132/0x1133, OPP 0x1105/0x1106
BAD_RE="0x111[eE]|0x111[fF]|0x1108|0x1112|0x110[aAbBcCdDeEfF]|0x1203|0x113[23]|0x110[56]"
# Note: grep returns 1 when there is no match (which is the desired state!), and with
# set -o pipefail that would abort the whole script. So || true is mandatory here.
BAD_UUIDS="$(printf '%s' "$LOCAL_SDP" | grep -oiE "$BAD_RE" | tr 'a-f' 'A-F' | sort -u | tr '\n' ' ' || true)"
if [[ -n "${BAD_UUIDS// /}" ]]; then
    warn "audio/OBEX services are still advertised: $BAD_UUIDS"
    warn "The phone may connect as a headset ('connected for calls') instead of a keyboard."
    warn "Stop the audio services and re-run:  sudo scripts/setup.sh --forget <MAC>"
else
    ok "no audio/OBEX services advertised - our device looks like a pure keyboard"
fi
echo

# Critical check: are channels 0x11/0x13 actually available?
# If they are busy, the normal bluetoothd (input plugin) is still holding them.
log "Checking whether PSM 0x11 / 0x13 can be opened..."
PSM_TEST="$(python3 - "$HCIMAC" <<'PY'
import socket, sys
mac = sys.argv[1]
for psm in (0x11, 0x13):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        s.bind((mac, psm)); s.listen(1)
        print(f"OK 0x{psm:02x}")
    except OSError as e:
        print(f"FAIL 0x{psm:02x} errno={e.errno} {e}")
    finally:
        s.close()
PY
)"
if printf '%s' "$PSM_TEST" | grep -q FAIL; then
    printf '%s\n' "$PSM_TEST"
    warn "Likely cause: registering the HID record made bluetoothd reserve 0x11/0x13 itself."
    log "Stopping the daemon (unregisters the record), then re-checking..."
    if [[ -n "${HID_DAEMON_PID:-}" ]]; then
        kill "$HID_DAEMON_PID" 2>/dev/null || true
        sleep 1
    fi
    pkill -f 'hid_daemon\.py' 2>/dev/null || true
    sleep 0.5
    PSM_TEST2="$(python3 - "$HCIMAC" <<'PY'
import socket, sys
mac = sys.argv[1]
for psm in (0x11, 0x13):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        s.bind((mac, psm)); s.listen(1)
        print(f"OK 0x{psm:02x}")
    except OSError as e:
        print(f"FAIL 0x{psm:02x} errno={e.errno} {e}")
    finally:
        s.close()
PY
)"
    if printf '%s' "$PSM_TEST2" | grep -q FAIL; then
        printf '%s\n' "$PSM_TEST2"
        warn "The channels are still reserved. Current bluetoothd:"
        ps -o pid,cmd -C bluetoothd 2>/dev/null || true
        warn "Excluding lines in the daemon log:"
        grep "Excluding" "$DAEMON_LOG" 2>/dev/null || warn "  (none - the daemon may not have started in compat mode)"
        die "Channels 0x11/0x13 are reserved. Try: sudo scripts/setup.sh --no-sdp"
    fi
    warn "The channels were freed after unregistering the record - the device will work but without an SDP record"
    warn "(the phone will not connect as an input device; the problem is in how it was registered)"
fi
ok "HID channels (PSM 0x11 / 0x13) are available and ready"
echo "==================================================="
echo

# ---------------- 9. Run the script ----------------
[[ -f "$PYTHON_SCRIPT" ]] || die "$PYTHON_SCRIPT not found"
chmod +x "$PYTHON_SCRIPT" 2>/dev/null || true

log "Running $PYTHON_SCRIPT ..."
log "On the phone: Settings -> Bluetooth -> search -> '$DEVICE_NAME' -> pair, then tap it to connect"
[[ -n "$PAYLOAD_URL" ]] && log "Target website: $PAYLOAD_URL"
echo
# Note: we use python3 without exec so the parent stays alive and keeps the agent above
PY_ARGS=()
[[ -n "$PAYLOAD_URL" ]] && PY_ARGS+=(-u "$PAYLOAD_URL")
python3 "$PYTHON_SCRIPT" ${PY_ARGS[@]+"${PY_ARGS[@]}"}
