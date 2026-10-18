#!/usr/bin/env python3
"""
hid_keyboard_bridge.py - Interactive Bluetooth HID keyboard bridge.

Turns your REAL keyboard into the target's keyboard: anything you type in the
terminal is forwarded over the same Bluetooth HID stack used by hid_keyboard.py
(L2CAP PSM 0x11 control / 0x13 interrupt, Report ID 1 keyboard + Report ID 2
consumer control). It can load a JSON catalog of keyboard shortcuts that you
can search, inspect, and execute (see examples/demo_shortcuts.json).

It reuses the low-level report/control code and key maps from hid_keyboard.py, so
the SDP record registered by hid_daemon.py stays valid.

Run it after the lab is up:
    sudo scripts/setup.sh --tool hid_keyboard_bridge.py   # prepares everything, then runs this
    # or, if the lab is already running:
    sudo python3 src/hid_keyboard_bridge.py

Interactive keys:
    - Type normally ......... the characters are sent to the target as keystrokes
    - Enter/Backspace/Tab/Esc/arrows/F1-F12/Home/End/PgUp/PgDn/Del ... forwarded
    - Ctrl+letter ........... forwarded as Ctrl+letter
    - Alt+letter ............ forwarded as Alt+letter (terminal permitting)
    - Ctrl+] ................ switch to COMMAND mode (see `help`)
    - Ctrl+C ................ quit

Batch (no interaction) examples:
    sudo python3 src/hid_keyboard_bridge.py --type "hello world"
    sudo python3 src/hid_keyboard_bridge.py --keys "ctrl+alt+del"
    sudo python3 src/hid_keyboard_bridge.py --list android
    sudo python3 src/hid_keyboard_bridge.py --show android-home
    sudo python3 src/hid_keyboard_bridge.py --run demo-open-test-page
"""

import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty

import hid_keyboard as hid   # reuse key maps + report/control helpers

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(REPO_ROOT, "examples", "demo_shortcuts.json")
BRIDGE_LOG = "/tmp/hid-bridge.log"

# ---------------- Modifiers (HID Usage Page 0x07, modifier byte) ----------------
MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_GUI = 0x01, 0x02, 0x04, 0x08
MOD_RCTRL, MOD_RSHIFT, MOD_RALT, MOD_RGUI = 0x10, 0x20, 0x40, 0x80

MOD_NAMES = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL, "shift": MOD_SHIFT, "alt": MOD_ALT,
    "option": MOD_ALT, "opt": MOD_ALT, "gui": MOD_GUI, "win": MOD_GUI,
    "super": MOD_GUI, "meta": MOD_GUI, "cmd": MOD_GUI, "command": MOD_GUI,
    "rctrl": MOD_RCTRL, "rshift": MOD_RSHIFT, "ralt": MOD_RALT, "rgui": MOD_RGUI,
}

# ---------------- Named keys (HID Usage Page 0x07) ----------------
KEYS = {
    "enter": (0, 40), "return": (0, 40), "esc": (0, 41), "escape": (0, 41),
    "backspace": (0, 42), "bksp": (0, 42), "tab": (0, 43), "space": (0, 44),
    "spacebar": (0, 44), "capslock": (0, 57), "caps": (0, 57),
    "printscreen": (0, 70), "prtsc": (0, 70), "sysrq": (0, 70),
    "scrolllock": (0, 71), "pause": (0, 72), "break": (0, 72),
    "insert": (0, 73), "ins": (0, 73), "home": (0, 74),
    "pageup": (0, 75), "pgup": (0, 75), "delete": (0, 76), "del": (0, 76),
    "end": (0, 77), "pagedown": (0, 78), "pgdn": (0, 78),
    "right": (0, 79), "rightarrow": (0, 79), "left": (0, 80),
    "leftarrow": (0, 80), "down": (0, 81), "downarrow": (0, 81),
    "up": (0, 82), "uparrow": (0, 82), "numlock": (0, 83),
    "menu": (0, 101), "app": (0, 101), "power": (0, 102),
}
for _i in range(1, 13):
    KEYS[f"f{_i}"] = (0, 57 + _i)

# ---------------- Consumer Control usages (HID Usage Page 0x0C) ----------------
CONSUMER = {
    "play": 0x00CD, "pause": 0x00CD, "playpause": 0x00CD, "next": 0x00B5,
    "prev": 0x00B6, "previous": 0x00B6, "stop": 0x00B7, "mute": 0x00E2,
    "volup": 0x00E9, "volumeup": 0x00E9, "voldown": 0x00EA, "volumedown": 0x00EA,
    "browser": 0x0196, "search": 0x0221, "home": 0x0223, "back": 0x0224,
    "forward": 0x0225, "email": 0x018A, "calc": 0x0192, "calculator": 0x0192,
    "sleep": 0x0032, "wake": 0x0033, "ac_power": 0x0030,
}

# ---------------- Terminal escape sequences ----------------
CSI_KEYS = {
    b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
    b"[H": "home", b"[F": "end", b"[1~": "home", b"[4~": "end",
    b"[2~": "insert", b"[3~": "delete", b"[5~": "pageup", b"[6~": "pagedown",
    b"OP": "f1", b"OQ": "f2", b"OR": "f3", b"OS": "f4",
    b"[15~": "f5", b"[17~": "f6", b"[18~": "f7", b"[19~": "f8",
    b"[20~": "f9", b"[21~": "f10", b"[23~": "f11", b"[24~": "f12",
}

_send_lock = threading.Lock()
_state = {"conn": None, "held": 0, "sticky": 0}


# =====================================================================
#  Low-level sending
# =====================================================================
def _send(conn, report):
    with _send_lock:
        conn.sendall(report)


def kbd(conn, modifier, key, hold=0.04):
    """Press modifier+key, release. The modifier is sent in its own report first
    (some Android HID stacks ignore a modifier that arrives with the key)."""
    modifier |= _state["sticky"]
    if modifier:
        _send(conn, hid.make_kbd_report(modifier, 0))
        time.sleep(0.03)
    _send(conn, hid.make_kbd_report(modifier, key))
    time.sleep(hold)
    _send(conn, hid.make_kbd_report(0, 0))
    time.sleep(0.03)


def kbd_down(conn, modifier, key):
    _send(conn, hid.make_kbd_report(modifier | _state["sticky"], key))


def kbd_release(conn):
    _send(conn, hid.make_kbd_report(0, 0))


def consumer(conn, usage, hold=0.08):
    lo, hi = usage & 0xFF, (usage >> 8) & 0xFF
    _send(conn, bytes([0xA1, 0x02, lo, hi]))
    time.sleep(hold)
    _send(conn, bytes([0xA1, 0x02, 0x00, 0x00]))
    time.sleep(0.03)


def char_report(ch):
    """Map a printable character to (modifier, keycode)."""
    if ch in hid.LOWER:
        return (0, hid.LOWER[ch])
    if ch.isalpha() and ch.lower() in hid.LOWER:
        return (MOD_SHIFT, hid.LOWER[ch.lower()])
    if ch in hid.DIGIT:
        return (0, hid.DIGIT[ch])
    if ch in hid.SHIFT_DIGITS:
        return (MOD_SHIFT, hid.SHIFT_DIGITS[ch])
    if ch in hid.SYMBOLS:
        return (0, hid.SYMBOLS[ch])
    if ch in hid.SHIFT_SYMBOLS:
        return (MOD_SHIFT, hid.SHIFT_SYMBOLS[ch])
    if ch in ("\n", "\r"):
        return (0, 40)
    if ch == "\t":
        return (0, 43)
    return None


def type_text(conn, text, delay=0.02):
    for ch in text:
        r = char_report(ch)
        if r:
            kbd(conn, r[0], r[1])
        time.sleep(delay)


# =====================================================================
#  Named keys / combos
# =====================================================================
def tap_named(conn, name):
    name = name.strip().lower()
    if name in MOD_NAMES:                 # a bare modifier: press+release it alone
        kbd(conn, MOD_NAMES[name], 0)
        return
    if name in KEYS:
        kbd(conn, *KEYS[name])
        return
    if len(name) == 1:
        r = char_report(name)
        if r:
            kbd(conn, r[0], r[1])
            return
    print(f"    ! unknown key: {name}")


def parse_combo(spec):
    """'ctrl+alt+del' -> (modifier_byte, keycode, label)."""
    toks = [t.strip().lower() for t in spec.split("+") if t.strip()]
    mod = 0
    key = 0
    for t in toks:
        if t in MOD_NAMES:
            mod |= MOD_NAMES[t]
        elif t in KEYS:
            key = KEYS[t][1]
        elif len(t) == 1:
            r = char_report(t)
            key = r[1] if r else 0
            if r:
                mod |= r[0]
        else:
            raise ValueError(t)
    return mod, key


def press_combo(conn, spec, release=True, hold=0.05):
    mod, key = parse_combo(spec)
    if mod:
        kbd_down(conn, mod, 0)
        time.sleep(0.03)
    kbd_down(conn, mod, key)
    if release:
        time.sleep(hold)
        kbd_release(conn)
    else:
        _state["held"] = mod


def parse_mods(spec):
    mod = 0
    for t in spec.replace("+", " ").split():
        if t in MOD_NAMES:
            mod |= MOD_NAMES[t]
    return mod


def sysrq(conn, letters):
    """Hold Alt+SysRq (PrintScreen) while tapping each letter - the Magic SysRq
    protocol. e.g. sysrq reisub, sysrq b, sysrq ei."""
    _send(conn, hid.make_kbd_report(MOD_ALT, 0))      # Alt alone in its own report first
    time.sleep(0.03)
    _send(conn, hid.make_kbd_report(MOD_ALT, 70))     # Alt + SysRq held
    time.sleep(0.15)
    for c in letters:
        r = char_report(c)
        if not r:
            continue
        _send(conn, hid.make_kbd_report(MOD_ALT, r[1]))
        time.sleep(0.05)
        _send(conn, hid.make_kbd_report(MOD_ALT, 70))
        time.sleep(0.05)
    _send(conn, hid.make_kbd_report(0, 0))
    time.sleep(0.05)


def release_all(conn):
    kbd_release(conn)
    _state["held"] = 0
    _state["sticky"] = 0


def get_clipboard():
    for cmd in (["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["wl-paste"]):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5)
            return out.decode(errors="replace")
        except Exception:
            continue
    return ""


# =====================================================================
#  Catalog
# =====================================================================
_catalog = None


def load_catalog():
    global _catalog
    if _catalog is None:
        try:
            with open(CATALOG_PATH, encoding="utf-8") as f:
                _catalog = json.load(f)
        except OSError as e:
            print(f"[!] cannot read catalog {CATALOG_PATH}: {e}")
            _catalog = []
    return _catalog


def list_shortcuts(filt=""):
    filt = filt.lower()
    rows = []
    for e in load_catalog():
        blob = " ".join(str(e.get(k, "")) for k in
                        ("id", "os", "name", "keys", "severity", "impact", "tags")).lower()
        if not filt or filt in blob:
            rows.append(e)
    if not rows:
        print(f"    (no catalog entries match {filt!r})")
        return
    print(f"    {'ID':<26} {'OS':<9} {'SEV':<9} {'KEYS':<18} NAME")
    for e in rows:
        print(f"    {e['id']:<26} {e.get('os',''):<9} {e.get('severity',''):<9} "
              f"{e.get('keys',''):<18} {e.get('name','')}")
    print(f"    ({len(rows)} entries; use `show <id>` or `run <id>`)")


def show_shortcut(sid):
    for e in load_catalog():
        if e["id"].lower() == sid.lower():
            print(f"    id       : {e['id']}")
            print(f"    os       : {e.get('os','')}")
            print(f"    name     : {e.get('name','')}")
            print(f"    keys     : {e.get('keys','')}")
            print(f"    severity : {e.get('severity','')}")
            print(f"    impact   : {e.get('impact','')}")
            print(f"    notes    : {e.get('notes','')}")
            print(f"    source   : {e.get('source','')}")
            print(f"    action   : {e.get('action',[])}")
            return
    print(f"    ! no such id: {sid}")


def run_shortcut(conn, sid):
    for e in load_catalog():
        if e["id"].lower() == sid.lower():
            sev = e.get("severity", "")
            print(f"    >> {e['id']} [{sev}] {e.get('keys','')} - {e.get('name','')}")
            if e.get("impact"):
                print(f"       impact: {e['impact']}")
            for line in e.get("action", []):
                do_command(conn, line)
                time.sleep(0.15)
            return
    print(f"    ! no such id: {sid}")


def print_catalog(filter_=""):
    list_shortcuts(filter_)


# =====================================================================
#  Command interpreter
# =====================================================================
HELP = """    Commands (in command mode, entered after Ctrl+]) :

      help                      show this help
      type <text>               type a string literally ("send" also works)
      key <name> [name...]      tap named keys: enter esc tab backspace delete
                                up down left right home end pgup pgdn f1..f12
                                space capslock insert printscreen menu power
                                (a bare modifier name taps that modifier, e.g. `key shift`)
      combo <a>+<b>+<c>         press a combo, e.g. combo ctrl+alt+del, combo gui+r
      hold <modifiers>          hold modifiers down (e.g. hold alt / hold ctrl+shift)
      sysrq <letters>           Magic SysRq: holds Alt+SysRq and taps letters,
                                e.g. sysrq reisub, sysrq b, sysrq ei
      release                   release sticky modifiers and everything held
      repeat <n> <key>          tap a key n times
      delay <seconds>           wait
      media <name>              play pause next prev stop mute volup voldown
                                browser search home back forward email calc
      consumer <hex|dec>        raw Consumer Control usage (e.g. consumer 0x0196)
      clip                      type the local clipboard
      file <path>               type a file's contents
      shortcuts [filter]        list the shortcut catalog (examples/demo_shortcuts.json)
      show <id>                 details of one catalog entry
      run <id>                  execute a catalog entry (e.g. run demo-open-test-page)
      raw <hex bytes>           send raw bytes on the interrupt channel (advanced)
      quit                      leave

    Anything else is ignored. Ctrl+C always quits."""


def do_command(conn, line):
    line = line.strip()
    if not line:
        return True
    if line.startswith(":"):
        line = line[1:].strip()
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("quit", "exit", "q"):
        return False
    if cmd in ("help", "?"):
        print(HELP)
    elif cmd in ("type", "text", "send", "string"):
        type_text(conn, arg)
    elif cmd == "key":
        for name in arg.split():
            tap_named(conn, name)
    elif cmd == "tap":
        tap_named(conn, arg)
    elif cmd == "combo":
        press_combo(conn, arg)
    elif cmd == "hold":
        m = parse_mods(arg)
        if not m:
            print("    ! hold expects modifiers, e.g. hold alt  /  hold ctrl+shift")
        else:
            _state["sticky"] = m
            kbd_down(conn, m, 0)
    elif cmd == "sysrq":
        sysrq(conn, arg.replace(" ", "").lower())
    elif cmd == "release":
        release_all(conn)
    elif cmd == "repeat":
        n, _, k = arg.partition(" ")
        try:
            for _ in range(int(n)):
                tap_named(conn, k.strip())
        except ValueError:
            print("    ! usage: repeat <n> <key>")
    elif cmd == "delay":
        try:
            time.sleep(float(arg))
        except ValueError:
            print("    ! usage: delay <seconds>")
    elif cmd == "media":
        u = CONSUMER.get(arg.lower())
        if u is None:
            print(f"    ! unknown media key: {arg}")
        else:
            consumer(conn, u)
    elif cmd == "consumer":
        try:
            consumer(conn, int(arg, 0))
        except ValueError:
            print("    ! usage: consumer <hex|dec>")
    elif cmd == "clip":
        txt = get_clipboard()
        if not txt:
            print("    ! clipboard empty or no xclip/xsel/wl-paste")
        else:
            type_text(conn, txt)
    elif cmd == "file":
        try:
            with open(arg, encoding="utf-8", errors="replace") as f:
                type_text(conn, f.read())
        except OSError as e:
            print(f"    ! {e}")
    elif cmd == "shortcuts":
        list_shortcuts(arg)
    elif cmd == "show":
        show_shortcut(arg)
    elif cmd == "run":
        run_shortcut(conn, arg)
    elif cmd == "raw":
        try:
            _send(conn, bytes.fromhex(arg.replace(" ", "")))
        except ValueError:
            print("    ! usage: raw <hex bytes>")
    else:
        print(f"    ! unknown command: {cmd}  (try `help`)")
    return True


# =====================================================================
#  Raw terminal live loop
# =====================================================================
def _read_escape(fd):
    """Read what follows ESC within a short window. Return a key name or an
    ('alt', ch) tuple, or 'esc'."""
    buf = b""
    end = time.time() + 0.04
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
        if not r:
            break
        buf += os.read(fd, 1)
        if buf in CSI_KEYS:
            return CSI_KEYS[buf]
        if any(k.startswith(buf) for k in CSI_KEYS):
            continue
        break
    if buf in CSI_KEYS:
        return CSI_KEYS[buf]
    if buf and buf[:1] != b"[":
        return ("alt", buf[:1].decode("latin-1"))
    return "esc"


def live_loop(conn):
    fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("[!] stdin is not a terminal - cannot run LIVE mode. "
              "Use --type/--keys/--run for non-interactive use.")
        return
    old = termios.tcgetattr(fd)
    _state["conn"] = conn

    def restore(*_):
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    signal.signal(signal.SIGINT, lambda *a: (restore(), print("\n[-] bye"), sys.exit(0)))

    try:
        tty.setraw(fd)
        print("[*] LIVE mode - your keyboard now drives the target.")
        print("    Type freely. Ctrl+] = commands (help), Ctrl+C = quit.\n")
        while True:
            ch = os.read(fd, 1)
            if not ch:
                break
            b = ch[0]

            if b == 0x03:                     # Ctrl+C
                break
            if b == 0x1D:                     # Ctrl+] -> command mode
                restore()
                try:
                    line = input("\n[bridge] ")
                except EOFError:
                    break
                tty.setraw(fd)
                if not do_command(conn, line):
                    break
                continue
            if b == 0x1B:                     # ESC / escape sequence
                k = _read_escape(fd)
                if k == "esc":
                    tap_named(conn, "esc")
                elif isinstance(k, tuple):    # Alt+char
                    r = char_report(k[1])
                    if r:
                        kbd(conn, MOD_ALT | r[0], r[1])
                else:
                    tap_named(conn, k)
                continue
            if b in (0x0A, 0x0D):
                tap_named(conn, "enter")
                continue
            if b == 0x09:
                tap_named(conn, "tab")
                continue
            if b in (0x08, 0x7F):
                tap_named(conn, "backspace")
                continue
            if 0x01 <= b <= 0x1A:             # Ctrl+letter
                letter = chr(b + 0x60)        # 0x01->'a'
                r = char_report(letter)
                if r:
                    kbd(conn, MOD_CTRL, r[1])
                continue
            if 0x1C <= b <= 0x1F:             # Ctrl+\ ^ _ (rare)
                r = char_report(chr(b + 0x40))
                if r:
                    kbd(conn, MOD_CTRL, r[1])
                continue
            text = ch.decode("utf-8", errors="ignore")
            r = char_report(text)
            if r:
                kbd(conn, r[0], r[1])
    finally:
        restore()


# =====================================================================
#  Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Interactive Bluetooth HID keyboard bridge")
    ap.add_argument("--type", dest="text", help="type this text, then exit")
    ap.add_argument("--file", help="type this file's contents, then exit")
    ap.add_argument("--keys", help="tap a combo (e.g. ctrl+alt+del), then exit")
    ap.add_argument("--run", help="run a catalog entry id, then exit")
    ap.add_argument("--list", nargs="?", const="", default=None,
                    metavar="FILTER", help="list the catalog (optional filter) and exit")
    ap.add_argument("--show", metavar="ID", help="show a catalog entry and exit")
    ap.add_argument("--ensure", action="store_true",
                    help="also (re)apply the adapter name/class as root")
    ap.add_argument("--keep", action="store_true",
                    help="after --type/--file/--keys/--run, stay in LIVE mode instead of exiting")
    args = ap.parse_args()

    if args.list is not None:
        list_shortcuts(args.list)
        return
    if args.show:
        show_shortcut(args.show)
        return

    dev, mac = hid.find_adapter()
    if not mac:
        print("[!] No Bluetooth adapter found. Run `hciconfig` / `rfkill unblock bluetooth`.")
        sys.exit(1)
    print(f"[*] Adapter: {dev}  BD_ADDR: {mac}")
    if args.ensure and os.geteuid() == 0:
        hid.ensure_adapter(dev)

    try:
        ls_ctl = hid.listen(hid.HID_CONTROL_PSM, mac)
        ls_int = hid.listen(hid.HID_INTERRUPT_PSM, mac)
    except OSError as e:
        print(f"[!] Failed to open L2CAP channels: {e}")
        print("    The lab must be running:  sudo scripts/setup.sh --tool hid_keyboard_bridge.py")
        sys.exit(1)

    print(f"[*] On the phone: tap '{hid.DEVICE_NAME}' in Bluetooth settings to connect.")
    print("[*] Waiting for the HID control + interrupt channels...")

    while True:
        try:
            c_ctl, addr = ls_ctl.accept()
            print(f"[+] Control channel   <- {addr[0]}")
            threading.Thread(target=hid.control_handler, args=(c_ctl,), daemon=True).start()

            c_int, addr2 = ls_int.accept()
            print(f"[+] Interrupt channel <- {addr2[0]}  :: BRIDGE LIVE ::")
            print(f"[*] Report protocol: {'Boot' if hid.hid_state['boot'] else 'Report'}")

            batch = any((args.text, args.file, args.keys, args.run))
            if batch:
                if args.run:
                    run_shortcut(c_int, args.run)
                if args.keys:
                    press_combo(c_int, args.keys)
                if args.text:
                    type_text(c_int, args.text)
                if args.file:
                    with open(args.file, encoding="utf-8", errors="replace") as f:
                        type_text(c_int, f.read())
                if not args.keep:
                    print("[*] done; exiting.")
                    return
                print("[*] batch done - entering LIVE mode (Ctrl+C to quit).")
            live_loop(c_int)

            c_int.close()
            print("[*] connection closed - waiting for the next one...\n")
        except socket.timeout:
            print("[*] ... still waiting ...")
        except KeyboardInterrupt:
            print("\n[-] bye")
            return
        except OSError as e:
            print(f"[!] connection dropped: {e}")


if __name__ == "__main__":
    main()
