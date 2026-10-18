# Technical report

Findings collected while building this lab on **Kali Linux, BlueZ 5.87, Python
3.14** with an Android phone as the HID host.

Each finding is tagged:

* **[verified]** — reproduced repeatedly on the reference setup and explained by
  the relevant source behaviour.
* **[observed]** — the effect was seen, but the exact mechanism was not isolated
  further.
* **[hypothesis]** — plausible explanation, not proven here.

## 1. `bluetoothd --compat` is only half the story [verified]

Even in compat mode, the stock `input` plugin binds L2CAP PSM `0x11` and `0x13`
itself, so the emulator gets `EADDRINUSE`. The plugin must be excluded with a
**single** comma-separated `--noplugin=` list:

```
--noplugin=input,hostname,a2dp,avrcp,midi,sixaxis,neard,autopair,battery
```

Passing `--noplugin` twice makes the second occurrence overwrite the first,
silently re-enabling `input`. A user-supplied `--tool`/`-u` workflow that
restarted `bluetoothd` by hand reproduced exactly this trap.

## 2. The phone showed "connected for calls", not a keyboard [verified]

`pipewire`/`wireplumber` (via `libspa-bluez5`) and `obexd` register HFP/HSP,
A2DP and OBEX profiles with `org.bluez.ProfileManager1` on the real system bus.
As a result the device advertised `0x111E` (Handsfree) and the phone connected
as a headset.

Stopping those user services is unreliable (they respawn). The robust fix is to
run `bluetoothd` on a **private D-Bus bus** that nothing else knows about:

```
dbus-daemon --session --address=unix:path=/run/bluetooth-lab-bus
export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/bluetooth-lab-bus
```

Audio services then never appear, and `setup.sh` verifies this by scanning
`sdptool browse local` for the audio/OBEX UUID range.

## 3. `Adapter1.UUIDs` is the authoritative record check [verified]

`RegisterProfile` can return success while the SDP record is silently rejected
by `adapter_service_insert`. Reading `org.bluez.Adapter1.UUIDs` and looking for
`00001124` (HID) is the conclusive test. If it is missing, power-cycling the
adapter makes BlueZ re-probe every registered profile and re-insert the record.

## 4. Device class minor must be pre-shifted [verified]

`MGMT_OP_SET_DEV_CLASS` expects the minor class bits shifted left by 2 (BlueZ
works around a kernel bug). Class `0x002540` decodes to major = Peripheral,
minor = Keyboard; the value written to `btmgmt class` is `minor << 2`.

## 5. A 16-bit Consumer Control usage array [verified]

The original descriptor declared the consumer collection as an 8-bit usage
array, which truncated usages above `0xFF` (e.g. `0x0196` browser, `0x0223`
home). Making it a **16-bit** array fixed those. Consumer reports are
`0xA1 02 lo hi`.

## 6. The modifier must be sent in its own report first [verified]

On the reference Android stack, a modifier that appears in the *same* report as
the key is sometimes dropped, so `Shift+letter` (uppercase) silently vanished.
Sending `modifier` alone, then `modifier+key`, then release, works reliably.

## 7. `ch.upper()` vs `ch.lower()` bug [verified]

`type_text` looked up the shift usage with `ch.upper() in LOWER`, which is
always `False` because `LOWER` only contains `'a'..'z'`. Every uppercase letter
was therefore dropped. The correct lookup is `ch.lower()`. This is now covered
by an offline test (`char_report`).

## 8. Android caches the report descriptor per bond [verified]

Changing the descriptor after pairing has no effect until the bond is removed on
**both** sides and pairing is repeated. `setup.sh --forget <MAC>` clears the
host side; the phone side must be cleared in Bluetooth settings.

## 9. `status 0x05` auth failures are stale link keys [verified]

An `Authentication Failed (status 0x05)` about a second after connecting means
one side still has an old link key. Clearing both sides together resolves it.

## 10. `hciconfig` reads fail while `bluetoothd` owns the adapter [verified]

Use `btmgmt info` for reads during the lab; `hciconfig` writes still work as a
fallback. `btmgmt` also enters an interactive mode, so every invocation is
wrapped with `</dev/null` and a timeout.

## 11. `set -o pipefail` vs a desired "no match" [verified]

`grep` returning 1 when it finds no audio UUIDs is the *success* case, but under
`set -e`+`pipefail` it aborts the script. Every such probe ends with `|| true`.

## 12. SDP XML element format in BlueZ 5.87 [observed]

Element names are limited to `boolean, uint8, uint16, uint32, uint64, uint128,
int8..int128, uuid, url, text, nil, sequence, alternate`; values live in the
`value="…"` attribute, and `<text encoding="hex">` is used for hex text. A
BrowseGroupList (`0x0005`, with the public browse root `0x1002`) is required for
the record to be visible.

## 13. Payload ordering matters for browser navigation [observed]

A reliable "open this URL" sequence was:

```
HOME (consumer 0x0223)  →  browser consumer 0x0196 (?)
                        →  Ctrl+L  →  type URL  →  Enter
```

Timings (1.5 s / 6 s / 1 s) were tuned by observation, not derived from a spec,
and vary by device. See `PAYLOAD_*` constants in `src/hid_keyboard.py`.

## 14. What this does **not** do [verified]

The emulator is a normal Bluetooth HID peripheral. It does **not** bypass
pairing, authentication, encryption, OS security policy, or browser sandboxing.
Its only capability is the ability to act as a keyboard/remote that a user has
paired and connected. All impact statements in this repository are scoped to
that capability.
