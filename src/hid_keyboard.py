#!/usr/bin/env python3
"""
Bluetooth HID Keyboard Emulator - educational lab.

Emulates a Bluetooth HID keyboard (Report ID 1) plus a consumer-control remote
(Report ID 2) over L2CAP PSM 0x11 (control) and PSM 0x13 (interrupt).

Use only on devices you own or have explicit written authorization to test.

Usage:
    sudo scripts/setup.sh                    # prepares everything, then runs this
    sudo python3 src/hid_keyboard.py          # run directly after preparation
    sudo python3 src/hid_keyboard.py --check  # bind-only check, no phone (bind + exit)
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request

# ============ Settings ============
PAYLOAD_URL = "https://www.google.com"  # <-- change to your message/meme URL
DEVICE_NAME = "HID Research Keyboard"
DEVICE_CLASS = "0x002540"                # Peripheral + Keyboard
HID_CONTROL_PSM = 0x0011
HID_INTERRUPT_PSM = 0x0013
ACCEPT_TIMEOUT = 30.0                    # wait for both HID channels before retrying

# ============ Keycode tables (HID Usage Page 0x07 - US Layout) ============
LOWER = {chr(ord('a') + i): 4 + i for i in range(26)}
DIGIT = {'1': 30, '2': 31, '3': 32, '4': 33, '5': 34,
         '6': 35, '7': 36, '8': 37, '9': 38, '0': 39}
SHIFT_DIGITS = {'!': 30, '@': 31, '#': 32, '$': 33, '%': 34,
                '^': 35, '&': 36, '*': 37, '(': 38, ')': 39}
SYMBOLS = {' ': 44, '-': 45, '=': 46, '[': 47, ']': 48, '\\': 49,
           ';': 51, "'": 52, '`': 53, ',': 54, '.': 55, '/': 56}
SHIFT_SYMBOLS = {'_': 45, '+': 46, '{': 47, '}': 48, '|': 49,
                 ':': 51, '"': 52, '~': 53, '<': 54, '>': 55, '?': 56}

# Special keys
KEY_ENTER = 40
KEY_ESC = 0x29          # acts as the "Back" button on most Android devices
KEY_HOME = 0x4A         # acts as the "Home" button on most Android devices
KEY_L = 0x0F            # letter L (for the Ctrl+L = focus address bar shortcut)
MOD_LCTRL = 0x01        # Left Ctrl

# Consumer Control (Usage Page 0x0C) - these are actually declared now in the
# report descriptor (Report ID 2) inside register_hid_sdp.py; otherwise the
# kernel/Android silently ignores them.
USAGE_AL_WEB_BROWSER = 0x0196  # AL Internet Browser  -> KEY_WWW
USAGE_AC_SEARCH = 0x0221       # AC Search            -> KEY_SEARCH

# Default strategy for opening the URL:
#   "browser" = browser key, then Ctrl+L, then type the URL
#   "search"  = search key, then type the URL
OPEN_STRATEGY = "browser"

send_lock = threading.Lock()

# Connection state shared between the control thread and the payload thread.
# "boot" = True  => 8-byte reports without a Report ID (Boot Protocol)
# "boot" = False => Report Protocol format (Report ID = 0x01)
# The HIDP default protocol is Report Protocol, and our advertised descriptor
# defines Report ID 1 and 2, so we must start in Report mode, not Boot.
hid_state = {"boot": False}


# ---------- Adapter discovery ----------
def find_adapter():
    """Return (adapter_name, mac) for the first HCI adapter present."""
    try:
        out = subprocess.check_output(["hciconfig"], stderr=subprocess.DEVNULL,
                                      timeout=10).decode()
    except (OSError, subprocess.SubprocessError):
        return None, None

    dev = None
    mac = None
    for line in out.splitlines():
        m = re.match(r"^(hci\d+):", line)
        if m:
            dev = m.group(1)
            mac = None
        m = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", line)
        if m and dev and mac is None:
            mac = m.group(1).upper()
            return dev, mac
    return None, None


def get_adapter_mac():
    dev, mac = find_adapter()
    if mac:
        return mac
    raise RuntimeError(
        "Could not detect the adapter address (BD Address).\n"
        "  Make sure the adapter exists:  hciconfig\n"
        "  and the service is running:    sudo /usr/libexec/bluetooth/bluetoothd --compat --noplugin=input &"
    )


def bluetoothd_cmdlines():
    """Return the command lines of the current bluetoothd processes (diagnostics)."""
    try:
        out = subprocess.check_output(["pgrep", "-a", "-x", "bluetoothd"],
                                      stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    except Exception:
        return ["(no bluetoothd running)"]
    return out.splitlines() if out else ["(no bluetoothd running)"]


def input_plugin_excluded(log_path="/tmp/bluetoothd-hid.log"):
    """Was the 'input' plugin actually excluded?

    We rely on the daemon log rather than the command text, because passing
    --noplugin twice makes the second one override the first, so --noplugin=input
    can appear in the command line even though the plugin is loaded.
    """
    try:
        with open(log_path, "r", errors="replace") as f:
            return "Excluding (cli) input" in f.read()
    except OSError:
        return False


# ---------- Safe command runners ----------
# Very important: btmgmt enters "interactive mode" and waits on stdin forever.
# Without stdin=DEVNULL it hangs and is then killed by the subprocess timeout,
# which would crash the script. So every btmgmt/bluetoothctl command goes
# through run_safe().
def run_safe(cmd, timeout=10, capture=False):
    """Run a command and never let it crash the script on failure or timeout."""
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,          # so btmgmt does not enter interactive mode
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[!] {cmd[0]} timed out ({timeout}s) - skipped")
        return None
    except OSError as e:
        print(f"[!] Could not run {cmd[0]}: {e}")
        return None


def cod_to_major_minor(cod):
    """Convert CoD (e.g. 0x002540) into the major/minor expected by `btmgmt class`."""
    c = int(cod, 16)
    return (c >> 8) & 0x1F, (c >> 2) & 0x3F


# ---------- Adapter setup (only when running as root) ----------
def ensure_adapter(dev):
    """Set the name/class/discoverability.

    Writes go through btmgmt (the mgmt interface - the same one bluetoothd uses)
    with hciconfig as a fallback. Reads/verification use `btmgmt info` only,
    because hciconfig loses read access while bluetoothd holds the adapter.
    """
    if os.geteuid() != 0:
        return
    idx = dev.replace("hci", "") if dev else "0"
    major, minor = cod_to_major_minor(DEVICE_CLASS)

    # mgmt expects minor shifted left by 2 (BlueZ works around a kernel quirk:
    # src/adapter.c does minor_class << 2 before sending it).
    for c in (
        ["btmgmt", "--index", idx, "power", "on"],
        ["btmgmt", "--index", idx, "name", DEVICE_NAME],
        ["btmgmt", "--index", idx, "class", str(major), str(minor << 2)],
        # Fallback: the hciconfig writes work even if the mgmt command is refused
        ["hciconfig", dev, "name", DEVICE_NAME],
        ["hciconfig", dev, "class", DEVICE_CLASS],
        ["hciconfig", dev, "piscan"],
        # The alias is what actually shows up in the phone's scan
        ["bluetoothctl", "system-alias", DEVICE_NAME],
        ["bluetoothctl", "discoverable-timeout", "0"],
        ["bluetoothctl", "pairable-timeout", "0"],
        ["bluetoothctl", "pairable", "on"],
        ["bluetoothctl", "discoverable", "on"],
    ):
        run_safe(c)

    print(f"[*] Adapter {dev} prepared: name='{DEVICE_NAME}' class={DEVICE_CLASS}")

    # Verify the actual values via btmgmt info (reliable)
    res = run_safe(["btmgmt", "--index", idx, "info"], timeout=10, capture=True)
    if res is None or not res.stdout:
        return
    out = res.stdout.decode(errors="replace")
    m = re.search(r"^\s*name (.*)$", out, re.M)
    c = re.search(r"\bclass (0x[0-9a-fA-F]+)", out)
    if m and m.group(1).strip() != DEVICE_NAME:
        print(f"[!] Actual adapter name '{m.group(1).strip()}' - "
              f"'{DEVICE_NAME}' may not appear in the phone's scan")
    if c:
        status = "OK" if int(c.group(1), 16) == int(DEVICE_CLASS, 16) else "WRONG"
        print(f"[*] Adapter class: {c.group(1)} ({status})")


def check_payload_url(url):
    """Verify the payload URL does not return 404 before starting."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            code = r.status
    except Exception:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                code = r.status
        except Exception as e:
            print(f"[!] Could not verify PAYLOAD_URL ({url}): {e}")
            print("    The URL will still be typed on the phone - but an error page may appear.")
            return
    if code >= 400:
        print(f"[!] Warning: PAYLOAD_URL returns HTTP {code}: {url}")
        print("    Change the PAYLOAD_URL constant at the top of the file to a working URL.")
    else:
        print(f"[*] PAYLOAD_URL OK (HTTP {code}): {url}")


def listen(psm, mac):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((mac, psm))
    s.listen(2)
    s.settimeout(ACCEPT_TIMEOUT)
    print(f"[*] Listening on {mac} L2CAP PSM 0x{psm:04x}")
    return s


# ---------- Sending keyboard reports ----------
def make_kbd_report(modifier, key):
    """Build a keyboard report in the correct format for the agreed protocol."""
    body = bytes([modifier, 0, key, 0, 0, 0, 0, 0])       # Boot: 8 bytes
    if not hid_state["boot"]:
        body = bytes([0x01]) + body                        # Report Protocol: Report ID=1
    return bytes([0xA1]) + body                            # 0xA1 = DATA input


def send_boot_report(conn, modifier, key):
    with send_lock:
        conn.sendall(make_kbd_report(modifier, key))


def key_press(conn, modifier, code, hold=0.05):
    # Send the modifier in its OWN report first, then the key. Some Android HID
    # stacks drop a modifier that appears in the same first report as the key
    # (which is why Shift+letter - uppercase - silently vanished before).
    if modifier:
        send_boot_report(conn, modifier, 0)
        time.sleep(0.04)
    send_boot_report(conn, modifier, code)
    time.sleep(hold)
    send_boot_report(conn, 0, 0)
    time.sleep(0.05)


def consumer_press(conn, usage):
    # Consumer Control report (Report ID 2): a 16-bit-wide usage array (two bytes).
    # Matches the report descriptor in register_hid_sdp.py: [0xA1, 0x02, lo, hi].
    lo, hi = usage & 0xFF, (usage >> 8) & 0xFF
    with send_lock:
        try:
            conn.sendall(bytes([0xA1, 0x02, lo, hi]))
            time.sleep(0.08)
            conn.sendall(bytes([0xA1, 0x02, 0x00, 0x00]))
        except OSError:
            pass


def char_report(ch):
    """Map one character to (modifier, usage), or None if it cannot be typed.

    Uppercase letters are Shift + the lowercase usage. The shift modifier is
    looked up with ch.lower(); using ch.upper() here was a bug that silently
    dropped every uppercase letter.
    """
    if ch in LOWER:
        return 0, LOWER[ch]
    if ch.isalpha() and ch.lower() in LOWER:
        return 2, LOWER[ch.lower()]                        # Shift + letter
    if ch in DIGIT:
        return 0, DIGIT[ch]
    if ch in SHIFT_DIGITS:
        return 2, SHIFT_DIGITS[ch]
    if ch in SYMBOLS:
        return 0, SYMBOLS[ch]
    if ch in SHIFT_SYMBOLS:
        return 2, SHIFT_SYMBOLS[ch]
    return None


def type_text(conn, text):
    for ch in text:
        rep = char_report(ch)
        if rep is not None:
            key_press(conn, rep[0], rep[1])
        time.sleep(0.05)


# ---------- Control channel handling (HID handshakes) ----------
def control_handler(conn):
    try:
        while True:
            data = conn.recv(64)
            if not data:
                break
            ttype = data[0] & 0xF0
            param = data[1] if len(data) > 1 else 0x00

            if ttype == 0x10:                     # HID_CONTROL (param in the low nibble)
                ctrl = data[0] & 0x0F
                if ctrl == 0x05:                  # VIRTUAL_CABLE_UNPLUG
                    print("    [control] HID_CONTROL virtual-cable-unplug -> closing")
                    try:
                        conn.sendall(bytes([0x00, 0x00]))   # HANDSHAKE: SUCCESSFUL
                    except OSError:
                        pass
                    break
                print(f"    [control] HID_CONTROL param=0x{ctrl:x} -> HANDSHAKE success")
                conn.sendall(bytes([0x00, 0x00]))
            elif ttype == 0x70:                   # SET_PROTOCOL
                hid_state["boot"] = (param & 0x01) == 0x00
                print(f"    [control] SET_PROTOCOL -> "
                      f"{'Boot' if hid_state['boot'] else 'Report'}")
                conn.sendall(bytes([0x00, 0x00]))      # HANDSHAKE: SUCCESSFUL
            elif ttype in (0x90, 0x50):           # SET_IDLE / SET_REPORT
                conn.sendall(bytes([0x00, 0x00]))      # HANDSHAKE: SUCCESSFUL
            elif ttype == 0x40:                   # GET_REPORT
                conn.sendall(bytes([0x00, 0x01]))      # HANDSHAKE: ERR_NOT_READY
            elif ttype == 0x60:                   # GET_PROTOCOL
                conn.sendall(bytes([0x00, 0x00 if hid_state["boot"] else 0x01]))
            else:
                print(f"    [control] unknown request 0x{data[0]:02x}")
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ---------- Payload: what happens on connect ----------
def fire_payload(conn, url, strategy=OPEN_STRATEGY):
    print("[+] >>> Injecting HID keystrokes now! <<<")
    time.sleep(0.6)

    print("    [1/4] HOME (make sure we are on the home screen)...")
    key_press(conn, 0, KEY_HOME, hold=0.06)
    time.sleep(1.5)

    if strategy == "search":
        print("    [2/4] Consumer 'Search' (0x0221) -> KEYCODE_SEARCH ...")
        consumer_press(conn, USAGE_AC_SEARCH)
        time.sleep(3.0)
    else:
        print("    [2/4] Consumer 'Internet Browser' (0x0196) ...")
        consumer_press(conn, USAGE_AL_WEB_BROWSER)
        time.sleep(6.0)                            # give the browser time to appear
        print("          Ctrl+L (focus the address/search bar)...")
        key_press(conn, MOD_LCTRL, KEY_L, hold=0.06)
        time.sleep(1.0)

    print(f"    [3/4] Typing URL: {url}")
    type_text(conn, url)
    time.sleep(0.6)

    print("    [4/4] Pressing ENTER...")
    key_press(conn, 0, KEY_ENTER, hold=0.06)
    time.sleep(0.5)
    print("[+] Payload delivered! Check the phone screen.")


# ---------- Main loop ----------
def main():
    ap = argparse.ArgumentParser(description="Bluetooth HID Keyboard Emulator (educational lab)")
    ap.add_argument("--check", action="store_true",
                    help="just open both channels and verify, then exit (no phone)")
    ap.add_argument("-u", "--url", default=None,
                    help="website to open (overrides PAYLOAD_URL), e.g. -u example.com")
    ap.add_argument("-s", "--strategy", choices=("browser", "search"),
                    default=OPEN_STRATEGY,
                    help="how to open the URL: browser key or search key")
    args = ap.parse_args()

    url = args.url or PAYLOAD_URL
    strategy = args.strategy

    print("=" * 60)
    print("   Bluetooth HID Keyboard Emulator - Educational Lab")
    print("=" * 60)

    dev, mac = find_adapter()
    if not mac:
        print("[!] No Bluetooth adapter found.")
        print("    Run:  hciconfig    /    rfkill unblock bluetooth")
        sys.exit(1)

    print(f"[*] Adapter: {dev}  BD_ADDR: {mac}")

    if os.geteuid() == 0:
        ensure_adapter(dev)
    else:
        print("[!] Not running as root - name/class will not be set automatically.")
        print("    Use:  sudo python3 src/hid_keyboard.py")

    check_payload_url(url)

    try:
        mac = get_adapter_mac()
        ls_ctl = listen(HID_CONTROL_PSM, mac)
        ls_int = listen(HID_INTERRUPT_PSM, mac)
    except OSError as e:
        errno = getattr(e, "errno", None)
        print(f"[!] Failed to open L2CAP channels: {e}")

        if errno == 98:
            # Errno 98 = Address already in use. It is not an old copy of the
            # script - the 'input' plugin in bluetoothd occupies PSM 0x11/0x13 itself.
            print("    Cause: the HID channels (PSM 0x11 / 0x13) are already occupied.")
            print("    Candidates:")
            for line in bluetoothd_cmdlines():
                print(f"        {line}")

            # Caution: passing --noplugin twice makes the second one override the
            # first, so the input plugin gets loaded even though it appears in the command!
            for line in bluetoothd_cmdlines():
                if line.count("--noplugin") > 1:
                    print("        ^ trap: --noplugin passed twice, the second one overrides the first.")
                    print("          Use a single list: --noplugin=input,hostname")

            # The authoritative verdict is the daemon log, not the command text
            if input_plugin_excluded():
                print("    Note: the log says the 'input' plugin is excluded -")
                print("    so a previous bind is still holding it. Re-run:  sudo scripts/setup.sh")
            else:
                print("    Log confirms: the 'input' plugin is loaded and holds PSM 0x11/0x13.")
                print("    Fix: run the setup script that excludes this plugin:")
                print("              sudo scripts/setup.sh")
                print("    (running python3 directly will not work while the default daemon is running)")
            print("    Then to return to normal:  sudo scripts/restore.sh")
            sys.exit(1)

        hint = {
            13: "Permission denied - run the script with sudo.",
            19: "No such device - the adapter is down: sudo hciconfig hci0 up",
        }.get(errno, "")
        if hint:
            print(f"    {hint}")
        sys.exit(1)

    if args.check:
        print("[+] CHECK OK - HID channels ready on PSM 0x11 and 0x13.")
        print("    You can now run the script without --check and pair from the phone.")
        ls_ctl.close()
        ls_int.close()
        return

    print("[*] Waiting for a device to pair & connect HID channels...")
    print(f"[*] On your phone: pair '{DEVICE_NAME}', then TAP it in")
    print("    Bluetooth settings to connect the input device.")

    while True:
        try:
            c_ctl, addr = ls_ctl.accept()
            print(f"[+] HID Control channel  <- {addr[0]}")
            threading.Thread(target=control_handler, args=(c_ctl,), daemon=True).start()

            c_int, addr2 = ls_int.accept()
            print(f"[+] HID Interrupt channel <- {addr2[0]}  :: KEYBOARD IS LIVE ::")
            print(f"[*] Report protocol: {'Boot' if hid_state['boot'] else 'Report'}"
                  f"  |  strategy: {strategy}")
            fire_payload(c_int, url, strategy)

            c_int.close()
            print("[*] Waiting for next connection (Ctrl+C to quit)...\n")
        except socket.timeout:
            print("[*] ... waiting for a HID connection (timeout, still listening) ...")
        except KeyboardInterrupt:
            print("\n[-] Shutting down gracefully.")
            sys.exit(0)
        except OSError as e:
            print(f"[!] Connection dropped: {e}")


if __name__ == "__main__":
    main()
