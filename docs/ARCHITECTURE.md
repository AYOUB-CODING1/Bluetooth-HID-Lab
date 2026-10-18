# Architecture

This document describes how the components fit together. It is written to be
read alongside the source in `src/` and the two shell entry points in `scripts/`.

## Bird's-eye view

```
        Android phone                    Linux host (this repo)
        (or any HID host)                ─────────────────────
   ┌────────────────────┐               ┌─────────────────────────────────────┐
   │  Bluetooth stack   │               │  bluetoothd --compat --noplugin=…   │
   │  HID host driver   │◀── L2CAP ────▶│    §2  private D-Bus bus           │
   │  SDP client        │   PSM 1 (SDP) │    §6  pairing agent (hid_daemon)   │
   └────────────────────┘   PSM 0x11    │        HID record via ProfileManager│
                            PSM 0x13    │    §9  payload (hid_keyboard.py)    │
                                        └─────────────────────────────────────┘
```

## Components

| File | Role |
|------|------|
| `scripts/setup.sh` | Brings the whole lab up: stops the stock service, starts `bluetoothd` in compat mode on a private D-Bus bus, sets the adapter name/class, registers the HID record, verifies everything, then runs the payload. |
| `scripts/restore.sh` | Undoes everything: stops the daemon/payload, restarts the audio services and the stock `bluetoothd`, restores the adapter identity, cleans `main.conf`. |
| `scripts/gui.sh` | Thin launcher for the terminal dashboard. |
| `src/register_hid_sdp.py` | The HID **report descriptor** and the **SDP record** (raw + XML encoders), plus a self-check and offline verification helpers. |
| `src/hid_daemon.py` | D-Bus agent that auto-accepts pairing (NoInputNoOutput) and registers the HID profile with `org.bluez.ProfileManager1`. |
| `src/hid_keyboard.py` | The keyboard/consumer-control emulator: opens PSM 0x11/0x13, sends HID reports, runs the payload. Also exports the reusable low-level helpers. |
| `src/hid_keyboard_bridge.py` | Interactive bridge: forwards your real keyboard to the target and executes catalog shortcuts. Reuses `hid_keyboard.py`. |
| `src/lab_tui.py` | Curses dashboard that runs `restore.sh` + `setup.sh` and streams the logs. |
| `examples/demo_shortcuts.json` | Benign example catalog used by the bridge. |
| `examples/demo_page/` | A static page you can serve locally as a safe navigation target. |

## Report protocol

```
Keyboard  (Report ID 1): 0xA1 01 [mod] 00 k0 k1 k2 k3 k4 k5
Consumer  (Report ID 2): 0xA1 02 [usage_lo] [usage_hi]
```

The report descriptor in `register_hid_sdp.py` declares both. Modifiers:
`0x01` LCtrl, `0x02` LShift, `0x04` LAlt, `0x08` LGui.

Because Android caches the report descriptor **per bonded device**, the record
must be correct *before* pairing; changing the descriptor later requires
removing the bond on both sides and pairing again.

## Control channel

The control channel (PSM 0x11) is consumed by a small state machine
(`control_handler` in `hid_keyboard.py`) that answers the requests a HID host
sends: `SET_PROTOCOL (0x70)`, `SET_IDLE (0x90)`, `GET_REPORT (0x40)`,
`GET_PROTOCOL (0x60)`, `SET_REPORT (0x50)`, and `HID_CONTROL` including
`VIRTUAL_CABLE_UNPLUG (0x15)`.

## Sequencing

`setup.sh` runs in numbered phases (see the comments in the file):

0. privileges
1. stop the stock service and stale processes
2. adapter + private D-Bus bus + silence audio/OBEX services
3. start `bluetoothd` in compat mode (single `--noplugin` list)
4. power the adapter on
5. set name + class (major = Peripheral, minor = Keyboard)
6. discoverable/pairable + agent + register the HID record, then confirm it
   appears in `Adapter1.UUIDs`
7. (the HID record lives in `bluetoothd`'s own SDP server)
8. final verification (UUIDs, `btmgmt info`, `sdptool browse local`, PSM probe)
9. run the payload

Each verification step is defensive and prints what it checked and what it
expects, so a failure is visible rather than silent.
