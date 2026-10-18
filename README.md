# Bluetooth HID Lab

> Make a Linux machine pair with an Android phone (or any Bluetooth host) as a
> **real Bluetooth keyboard and consumer-control remote**, then send keystrokes,
> media keys and text to it — used to study how the HID profile, BlueZ's SDP
> registration, and an Android HID host really behave.


[![tests: offline](https://img.shields.io/badge/tests-offline-brightgreen.svg)](tests/test_offline.py)
![platform: linux](https://img.shields.io/badge/platform-linux-lightgrey.svg)

<img width="1680" height="1050" alt="Screenshot From 2026-10-18 11-44-02" src="https://github.com/user-attachments/assets/c6bc0fca-ec03-485f-b20c-4c63ba9d98d5" />

**Authorized/educational use only. See [Legal and ethical use](#3-legal-and-ethical-use).**

---

## 1. What this project is

A from-scratch implementation of the Bluetooth **HID** profile on Linux. It:

- runs `bluetoothd` in a controlled, isolated mode;
- registers a valid HID **SDP record** (with a hand-written report descriptor);
- opens the HID L2CAP channels (`PSM 0x11` control, `0x13` interrupt);
- pairs with the target as a keyboard, and
- emits keyboard (Report ID 1) and consumer-control (Report ID 2) reports.

It was built to answer concrete engineering questions: *why does an Android
phone say "connected for calls"?*, *why does `RegisterProfile` succeed while the
phone sees no input service?*, *why do uppercase letters disappear?* The answers
are written up in [`docs/RESEARCH.md`](docs/RESEARCH.md).

## 2. What this project is not

- It does **not** exploit, bypass, or weaken Bluetooth pairing, authentication,
  encryption, OS security policy, or browser sandboxing.
- It does **not** give you any privilege you do not already have on a device you
  have paired and connected.
- It is **not** an Android root tool, an ADB replacement, or a way to install or
  extract files. The keyboard can only do what a keyboard can do.
- It does **not** spoof a real product. The advertised device name is a
  configurable string; the default is neutral (`HID Research Keyboard`).

If you are looking for an attack tool, this is not it.

## 3. Legal and ethical use

Only use this lab on hardware **you own**, or on hardware for which you have
**explicit, written authorization** to test. Pairing a keyboard to someone
else's device without permission is likely illegal and is not what this project
is for. You are responsible for how you use it.

## 4. Features

- Pure Python + BlueZ; the only Python dependencies are `dbus-python` and
  `PyGObject` (see [`requirements.txt`](requirements.txt)).
- Correct HID report descriptor (keyboard + 16-bit consumer-control report).
- Robust SDP registration with a real verification path (adapter UUIDs).
- Isolated `bluetoothd` so audio/OBEX services never make the phone think it is
  a headset.
- Interactive keyboard bridge (forward your real keyboard / run shortcuts).
- Terminal dashboard (curses) that runs and streams the whole sequence.
- Offline test suite (no adapter, no root, no network).
- A safe demo catalog and a local demo web page.

## 5. Requirements

- Linux with **BlueZ** (developed and verified on BlueZ **5.87**).
- A Bluetooth adapter the kernel exposes as `hci*`.
- `python3` (3.10+; verified on 3.14), and these system tools:
  `dbus-daemon`, `bluetoothctl`, `btmgmt`, `hciconfig` (bluez-deprecated),
  `sdptool`, `rfkill`.
- `python3-dbus` and `python3-gi` (PyGObject) — the Python dependencies, listed
  in [`requirements.txt`](requirements.txt).
- `sudo`/root (the lab manipulates the adapter and the Bluetooth service).
- A target device that supports the HID **host** role (e.g. Android).

```bash
sudo apt update
sudo apt install -y bluez bluez-deprecated python3-dbus python3-gi dbus rfkill
# (or, for the Python packages only: pip install -r requirements.txt)
```

## 6. Repository layout

```
.
├── README.md
├── CONTRIBUTING.md
├── requirements.txt
├── LICENSE
├── docs/
│   ├── ARCHITECTURE.md      # components and data flow
│   └── RESEARCH.md          # the technical report (findings)
├── src/
│   ├── hid_keyboard.py         # emulator + low-level HID helpers
│   ├── register_hid_sdp.py     # report descriptor + SDP record
│   ├── hid_daemon.py           # pairing agent + profile registration
│   ├── hid_keyboard_bridge.py  # interactive bridge
│   ├── hid_sdp_server.py       # optional standalone SDP fallback
│   └── lab_tui.py              # curses dashboard
├── scripts/
│   ├── setup.sh             # bring the lab up, then run the payload
│   ├── restore.sh           # return the system to normal
│   └── gui.sh               # launch the dashboard
├── examples/
│   ├── demo_shortcuts.json  # benign example catalog
│   └── demo_page/           # a page you can serve locally
└── tests/
    └── test_offline.py      # offline test suite
```

## 7. Quick start

```bash
git clone <this-repo>
cd bluethoth-HID

# 1. (optional) sanity-check without any hardware:
python3 -m unittest discover -s tests -v

# 2. Bring the lab up and pair:
sudo scripts/setup.sh -u "https://example.com"

# 3. On the phone: Settings -> Bluetooth -> scan -> "HID Research Keyboard"
#    -> pair, then tap it to connect.
```

To open a different page or run a different payload, see
[§12 Configuration](#12-configuration-reference) and
[§9 Interactive keyboard bridge](#9-interactive-keyboard-bridge).

> **Pairing tip:** after any failed attempt, remove the device on the **phone**
> too, and re-run with `--forget` (see [§13 Troubleshooting](#13-troubleshooting)).

## 8. How it works

The short version:

1. `setup.sh` stops the stock Bluetooth service and starts `bluetoothd
   --compat` on a **private D-Bus bus** that `pipewire`/`obexd` do not know
   about, so no audio/OBEX profiles are advertised.
2. It sets the adapter name and class (major = Peripheral, minor = Keyboard).
3. `hid_daemon.py` answers pairing automatically and registers the HID profile
   (`org.bluez.ProfileManager1`), which puts the SDP record into `bluetoothd`'s
   own SDP server on PSM 1.
4. `setup.sh` **verifies** the record by checking that the adapter advertises
   UUID `00001124`, then probes that PSM `0x11`/`0x13` are free.
5. The payload (`hid_keyboard.py` by default) opens the HID channels and sends
   reports.

Full detail, including the report descriptor and control-channel state machine,
is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The reasoning behind each
design choice is in [`docs/RESEARCH.md`](docs/RESEARCH.md).

## 9. Interactive keyboard bridge

`src/hid_keyboard_bridge.py` turns your terminal into the target's keyboard.

```bash
# bring the lab up with the bridge as the payload:
sudo scripts/setup.sh --tool hid_keyboard_bridge.py

# or, if the lab is already running:
sudo python3 src/hid_keyboard_bridge.py
```

- Type normally → the characters are sent to the target.
- `Ctrl+]` → command mode (`type`, `key`, `combo`, `hold`, `release`, `repeat`,
  `delay`, `media`, `consumer`, `file`, `shortcuts`, `show`, `run`, `help`).
- `Ctrl+C` → quit.

Batch examples (no interaction):

```bash
sudo python3 src/hid_keyboard_bridge.py --type "hello world"
sudo python3 src/hid_keyboard_bridge.py --keys "ctrl+alt+del"
sudo python3 src/hid_keyboard_bridge.py --list android
sudo python3 src/hid_keyboard_bridge.py --show demo-consumer-home
```

## 10. Shortcut catalog

The bridge can load a JSON catalog of shortcuts. The shipped catalog,
[`examples/demo_shortcuts.json`](examples/demo_shortcuts.json), is intentionally
**benign**: navigation keys, media keys, and text entry. It demonstrates the
format without shipping anything destructive.

```bash
sudo python3 src/hid_keyboard_bridge.py --list
sudo python3 src/hid_keyboard_bridge.py --run demo-open-test-page
```

To keep it safe, `tests/test_offline.py` rejects catalog entries containing
known offensive tooling strings, and the bridge never requires a catalog to
operate — you can always drive it with direct commands.

A navigation demo you can reproduce end-to-end uses the local page in
`examples/demo_page/`:

```bash
python3 -m http.server 8000 --directory examples/demo_page
# then, from the bridge:
#   run demo-open-test-page
```

## 11. Terminal dashboard

```bash
sudo scripts/gui.sh -u "https://example.com"
```

It runs `scripts/restore.sh` then `scripts/setup.sh` in order, streams every log
line into a scrolling pane, and records exit codes. It is only a front-end — the
underlying commands are exactly the ones above.

## 12. Configuration reference

| Where | Setting | Meaning |
|-------|---------|---------|
| `scripts/setup.sh` | `DEVICE_NAME` | name the phone sees (default neutral) |
| `scripts/setup.sh` | `DEVICE_CLASS` | CoD; default `0x002540` (Peripheral/Keyboard) |
| `scripts/setup.sh` | `--forget <MAC>` | remove a stale bond on our side |
| `scripts/setup.sh` | `--no-sdp` | skip SDP registration (diagnostics) |
| `scripts/setup.sh` | `--system-bus` | use the real D-Bus bus (diagnostics) |
| `scripts/setup.sh` | `--tool <script>` | payload to run instead of the default |
| `scripts/setup.sh` | `-u/--url <URL>` | URL passed to the payload |
| `src/hid_keyboard.py` | `PAYLOAD_URL` | default URL |
| `src/hid_keyboard.py` | `OPEN_STRATEGY` | `browser` (Ctrl+L) or `search` |

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Phone says **"connected for calls"** | audio/OBEX services advertised | stop them / use the private bus (default). Re-run `sudo scripts/setup.sh --forget <MAC>` |
| **Authentication Failed / status 0x05** | stale link key on one side | remove the device on the phone **and** run `sudo scripts/setup.sh --forget <MAC>` |
| `EADDRINUSE` on PSM 0x11/0x13 | the `input` plugin is loaded | use a **single** `--noplugin=` list (the script does this) |
| Device pairs but shows **no input service** | SDP record not inserted | script power-cycles the adapter and re-checks UUID `00001124` |
| **Uppercase letters are missing** | modifier sent in the same report / lookup bug | fixed in this repo (`char_report`); covered by tests |
| Descriptor change has no effect | Android caches it **per bond** | re-pair on both sides |
| `hciconfig` shows wrong/blank values | `bluetoothd` owns the adapter | read `btmgmt info` instead |

## 14. Restoring your system

```bash
sudo scripts/restore.sh
```

This stops the lab, restarts the audio services and the stock `bluetoothd`,
restores the adapter name/class, and cleans `main.conf`.

## 15. Testing

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/*.py
bash -n scripts/*.sh
```

The unit tests need no adapter, no root and no network. Live behaviour must be
verified by running the lab against a real host.

## 16. Technical report

The interesting findings — private D-Bus isolation, the class-minor shift, the
16-bit consumer usage array, modifier-first reports, descriptor caching, and
more — are documented in [`docs/RESEARCH.md`](docs/RESEARCH.md), each tagged
**[verified]**, **[observed]** or **[hypothesis]**.

## 17. Security and scope

This tool is a normal HID peripheral. Its entire capability is "type on a device
that has agreed to pair with it." It provides no persistence, no privilege
escalation, and no way to bypass an OS or browser security control. The example
catalog is deliberately benign and the download/delivery workflows that earlier
private versions used are **not** part of this repository.

## 18. Contributing and license

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
