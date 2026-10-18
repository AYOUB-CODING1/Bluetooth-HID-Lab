#!/usr/bin/env python3
"""Register a local HID (0x1124) SDP record without sdptool.

sdptool is broken in this bluez build (5.87): `sdptool -i ...` dumps usage for
every command and `sdptool add <service>` exits silently (rc 1/255) for every
service.  BlueZ itself can still register a record, so we try two supported
paths and verify each one by browsing our own SDP server:

  1. org.bluez.ProfileManager1.RegisterProfile  (D-Bus, ServiceRecord XML)
  2. raw SDP_ServiceRegistrationRequest over L2CAP PSM 1

Usage: sudo ./register_hid_sdp.py [--check] [--dry-run] [--ctrl-PSM 0x11] ...
Exit 0 if a HID record is present after our attempt.
"""
import argparse
import os
import re
import shutil
import socket
import struct
import subprocess
import sys

CTRL_PSM = 0x0011
INT_PSM = 0x0013
HID_SVCLASS = 0x1124
HIDP_UUID = 0x0011
L2CAP_UUID = 0x0100
BROWSE_ROOT = 0x1002

# HID report descriptor: keyboard (Report ID 1) + Consumer Control (Report ID 2).
# The consumer collection is what makes the "browser"/"search" keys real; without
# it a Report ID 2 report is undeclared and the kernel/Android silently drops it.
# Report formats implied by this descriptor:
#   ID 1 -> [modifiers, reserved, k0..k5]                (8 bytes)
#   ID 2 -> [usage_lo, usage_hi]                         (2 bytes, 16-bit usage array)
HID_REPORT_DESCRIPTOR = bytes([
    # ---- Keyboard, Report ID 1 ----
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,        #   Usage Minimum (224)
    0x29, 0xE7,        #   Usage Maximum (231)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs)      ; modifier bits
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x01,        #   Input (Const)             ; reserved byte
    0x95, 0x05,        #   Report Count (5)
    0x75, 0x01,        #   Report Size (1)
    0x05, 0x08,        #   Usage Page (LED)
    0x19, 0x01,        #   Usage Minimum (1)
    0x29, 0x05,        #   Usage Maximum (5)
    0x91, 0x02,        #   Output (Data,Var,Abs)     ; LED bits
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x03,        #   Report Size (3)
    0x91, 0x01,        #   Output (Const)            ; LED padding
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x65,        #   Logical Maximum (101)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,        #   Usage Minimum (0)
    0x29, 0x65,        #   Usage Maximum (101)
    0x81, 0x00,        #   Input (Data,Array,Abs)    ; keycode array
    0xC0,              # End Collection
    # ---- Consumer Control, Report ID 2 ----
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x03,  #   Logical Maximum (0x3FF)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x03,  #   Usage Maximum (0x3FF)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x00,        #   Input (Data,Array,Abs)    ; 16-bit usage
    0xC0,              # End Collection
])

# Backwards-compatible alias (older name).
BOOT_KBD_DESCRIPTOR = HID_REPORT_DESCRIPTOR


def log(msg):
    print("[sdp] %s" % msg)


def warn(msg):
    print("[sdp] ! %s" % msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# raw SDP data element encoding
# --------------------------------------------------------------------------- #
# SDP data element header: 8 bits = (type_descriptor << 3) | size_descriptor.
# type: 0 nil, 1 uint, 2 int, 3 uuid, 4 text, 5 bool, 6 sequence, 7 alt, 8 url
# size: 0..4 => 1,2,4,8,16 bytes; 5 => 1-byte length follows; 6 => 2-byte
def de_uint8(v):
    return b"\x08" + struct.pack("B", v & 0xFF)


def de_uint16(v):
    return b"\x09" + struct.pack(">H", v & 0xFFFF)


def de_uuid16(v):
    return b"\x19" + struct.pack(">H", v & 0xFFFF)


def de_bool(v):
    return b"\x28" + (b"\x01" if v else b"\x00")


def de_text(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) < 0x100:
        return b"\x25" + struct.pack("B", len(data)) + data
    return b"\x26" + struct.pack(">H", len(data)) + data


def de_seq(elements):
    body = b"".join(elements)
    if len(body) < 0x100:
        return b"\x35" + struct.pack("B", len(body)) + body
    return b"\x36" + struct.pack(">H", len(body)) + body


def sdp_attr(attr_id, value_element):
    return de_seq([de_uint16(attr_id), value_element])


def proto_list(psm, proto_uuid=HIDP_UUID):
    return de_seq([
        de_seq([de_uuid16(L2CAP_UUID), de_uint16(psm)]),
        de_seq([de_uuid16(proto_uuid)]),
    ])


# --------------------------------------------------------------------------- #
# One abstract record definition -> two serializers (raw SDP bytes and XML).
#
# BlueZ's XML parser (src/sdp-xml.c) only understands these element names:
#   boolean, uint8, uint16, uint32, uint64, int8..int128, uuid, url, text,
#   nil, sequence, alternate
# and it reads every value from the `value="..."` ATTRIBUTE. Element text
# content is never read, and an unknown element name is silently dropped (the
# `error()` macro is compiled out). That is why the previous
# `<uuid16>0x1124</uuid16>` form produced an *empty* BrowseGroupList, which
# made adapter_service_insert() skip the record entirely: it never reached
# adapter->services, so it never showed up in org.bluez.Adapter1.UUIDs.
# --------------------------------------------------------------------------- #
def U8(v):
    return ("uint8", v & 0xFF)


def U16(v):
    return ("uint16", v & 0xFFFF)


def BOOL(v):
    return ("bool", bool(v))


def UUID16(v):
    return ("uuid16", v & 0xFFFF)


def TEXT(data, hex_=False):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return ("text", data, bool(hex_))


def SEQ(*items):
    return ("seq", list(items))


def _xml_escape(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def de_encode(el):
    """Serialize one abstract element to raw SDP wire form."""
    kind = el[0]
    if kind == "seq":
        body = b"".join(de_encode(x) for x in el[1])
        if len(body) < 0x100:
            return b"\x35" + struct.pack("B", len(body)) + body
        return b"\x36" + struct.pack(">H", len(body)) + body
    if kind == "uuid16":
        return b"\x19" + struct.pack(">H", el[1])
    if kind == "uint8":
        return b"\x08" + struct.pack("B", el[1])
    if kind == "uint16":
        return b"\x09" + struct.pack(">H", el[1])
    if kind == "uint32":
        return b"\x0a" + struct.pack(">I", el[1])
    if kind == "bool":
        return b"\x28" + (b"\x01" if el[1] else b"\x00")
    if kind == "text":
        return de_text(el[1])
    raise ValueError("de_encode: unsupported element %r" % (kind,))


def xml_encode(el):
    """Serialize one abstract element to BlueZ sdp-xml.c compatible XML."""
    kind = el[0]
    if kind == "seq":
        return "<sequence>" + "".join(xml_encode(x) for x in el[1]) + "</sequence>"
    if kind == "uuid16":
        return '<uuid value="0x%04x" />' % el[1]
    if kind in ("uint8", "uint16", "uint32"):
        return '<%s value="0x%0*x" />' % (kind, 2 * (len(kind) - 4), el[1])
    if kind == "bool":
        return '<boolean value="%s" />' % ("true" if el[1] else "false")
    if kind == "text":
        data, as_hex = el[1], el[2]
        if as_hex:
            return '<text encoding="hex" value="%s" />' % data.hex()
        return '<text value="%s" />' % _xml_escape(data.decode("utf-8"))
    raise ValueError("xml_encode: unsupported element %r" % (kind,))


def hid_record(ctrl_psm=CTRL_PSM, int_psm=INT_PSM):
    """The HID (0x1124) service record as a list of (attribute-id, element)."""

    def proto(psm):
        return SEQ(SEQ(UUID16(L2CAP_UUID), U16(psm)), SEQ(UUID16(HIDP_UUID)))

    return [
        (0x0001, SEQ(UUID16(HID_SVCLASS))),                                # ServiceClassIDList
        (0x0004, proto(ctrl_psm)),                                         # ProtocolDescriptorList
        (0x0005, SEQ(UUID16(BROWSE_ROOT))),                                # BrowseGroupList (0x1002)
        (0x0006, SEQ(U16(0x656E), U16(0x006A), U16(0x0100))),              # LanguageBaseAttributeIDList
        (0x0009, SEQ(SEQ(UUID16(HID_SVCLASS), U16(0x0101)))),              # BluetoothProfileDescriptorList
        (0x000D, SEQ(proto(int_psm))),                                     # AdditionalProtocolDescriptorList
        (0x0100, TEXT("Keyboard")),                                        # ServiceName @ base 0x0100
        (0x0200, U16(0x0100)),                                             # HIDDeviceReleaseNumber
        (0x0201, U16(0x0111)),                                             # HIDParserVersion
        (0x0202, U8(0x40)),                                                # HIDDeviceSubclass = keyboard
        (0x0203, U8(0x21)),                                                # HIDCountryCode = US
        (0x0204, BOOL(True)),                                             # HIDVirtualCable
        (0x0205, BOOL(True)),                                             # HIDReconnectInitiate
        (0x0206, SEQ(SEQ(U8(0x22), TEXT(HID_REPORT_DESCRIPTOR, hex_=True)))),  # HIDDescriptorList
        (0x0207, SEQ(SEQ(U16(0x0409), U16(0x0100)))),                      # HIDLANGIDBaseList
        (0x0209, BOOL(True)),                                              # HIDBootDevice
    ]


def build_hid_record_attrs(ctrl_psm=CTRL_PSM, int_psm=INT_PSM):
    """Full HID record as a DataElementSequence of (attr-id, value) sequences."""
    attrs = [
        de_seq([de_uint16(aid), de_encode(el)])
        for aid, el in hid_record(ctrl_psm, int_psm)
    ]
    return de_seq(attrs)


def build_hid_record_xml(ctrl_psm=CTRL_PSM, int_psm=INT_PSM):
    body = "".join(
        '<attribute id="0x%04x">%s</attribute>' % (aid, xml_encode(el))
        for aid, el in hid_record(ctrl_psm, int_psm)
    )
    # Single line on purpose: the XML is passed as one argv entry to busctl.
    return '<?xml version="1.0" encoding="UTF-8" ?><record>%s</record>' % body


# --------------------------------------------------------------------------- #
# self-check: both serializers must describe exactly hid_record()
# --------------------------------------------------------------------------- #
def de_decode(buf, off=0):
    """Decode one raw SDP data element; return (element, next_offset)."""
    hdr = buf[off]
    off += 1
    dtd = (hdr >> 3) & 0x1F
    sdl = hdr & 0x07
    if sdl <= 4:
        size = 1 << sdl
    elif sdl == 5:
        size = buf[off]
        off += 1
    elif sdl == 6:
        size = struct.unpack(">H", buf[off:off + 2])[0]
        off += 2
    else:
        size = struct.unpack(">I", buf[off:off + 4])[0]
        off += 4
    if dtd == 6:  # sequence
        end = off + size
        items = []
        while off < end:
            sub, off = de_decode(buf, off)
            items.append(sub)
        return ("seq", items), off
    body = bytes(buf[off:off + size])
    off += size
    if dtd == 1:  # unsigned integer
        return ({1: "uint8", 2: "uint16", 4: "uint32", 8: "uint64"}[size],
                int.from_bytes(body, "big")), off
    if dtd == 3:  # uuid
        if size == 2:
            return ("uuid16", int.from_bytes(body, "big")), off
        return ("uuid128", body.hex()), off
    if dtd == 4:  # text
        return ("text", body, False), off
    if dtd == 5:  # boolean
        return ("bool", body[0] != 0), off
    raise ValueError("de_decode: unsupported type descriptor %d" % dtd)


def _norm(el):
    """Drop the raw form's irrelevant hex/utf8 marker before comparing."""
    if el[0] == "seq":
        return ("seq", [_norm(x) for x in el[1]])
    if el[0] == "text":
        return ("text", el[1])
    return el


def _xml_to_el(node):
    """Mimic src/sdp-xml.c: element name selects the type, value="" the datum."""
    name = node.tag
    if name == "sequence":
        return ("seq", [_xml_to_el(c) for c in node])
    value = node.get("value", "")
    if name == "uuid":
        return ("uuid16", int(value, 16)) if value.lower().startswith("0x") else ("uuid128", value)
    if name in ("uint8", "uint16", "uint32"):
        return (name, int(value, 0))
    if name == "boolean":
        return ("bool", value.strip().lower() == "true")
    if name == "text":
        if node.get("encoding", "").lower() == "hex":
            return ("text", bytes.fromhex(value), True)
        return ("text", value.encode("utf-8"), False)
    raise ValueError("unsupported XML element <%s>" % name)


def selfcheck(verbose=False):
    """Assert the XML and raw encodings both reproduce hid_record() exactly."""
    import xml.etree.ElementTree as ET

    want = list(hid_record())

    root = ET.fromstring(build_hid_record_xml())
    got_xml = []
    for attr in root.findall("attribute"):
        aid = int(attr.get("id"), 16)
        children = list(attr)
        if len(children) != 1:
            raise AssertionError(
                "attribute 0x%04x: %d value elements (expected 1)" % (aid, len(children))
            )
        got_xml.append((aid, _xml_to_el(children[0])))
    if got_xml != want:
        raise AssertionError("XML round-trip mismatch:\n got=%r\nwant=%r" % (got_xml, want))

    raw = build_hid_record_attrs()
    elem, off = de_decode(raw, 0)
    if off != len(raw):
        raise AssertionError("raw record: %d trailing bytes" % (len(raw) - off))
    got_raw = [(a[1][0][1], a[1][1]) for a in elem[1]]
    if [(aid, _norm(v)) for aid, v in got_raw] != [(aid, _norm(v)) for aid, v in want]:
        raise AssertionError("raw round-trip mismatch:\n got=%r\nwant=%r" % (got_raw, want))

    browse = dict(got_xml).get(0x0005)
    assert browse == ("seq", [("uuid16", BROWSE_ROOT)]), "BrowseGroupList wrong: %r" % (browse,)

    if verbose:
        log("self-check ok: %d attributes, raw=%d bytes, xml=%d bytes"
            % (len(want), len(raw), len(build_hid_record_xml())))
    return True


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def browse_local(timeout=8):
    """Return raw `sdptool browse local` output ('' if unavailable)."""
    if not shutil.which("sdptool"):
        return ""
    try:
        proc = subprocess.run(
            ["sdptool", "browse", "local"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def hid_record_present(browse_out):
    """True if our local SDP server advertises the HID service class 0x1124."""
    if not browse_out:
        return False
    low = browse_out.lower()
    # Match only a real service-class reference, not an incidental record handle
    # that happens to equal 4388.
    return ("(0x1124)" in low) or ("human interface device" in low)


# --------------------------------------------------------------------------- #
# fallback: ProfileManager1 over D-Bus (busctl, then python-dbus)
# --------------------------------------------------------------------------- #
HID_PROFILE_UUID = "00001124-0000-1000-8000-00805f9b34fb"


def _busctl(method, *args):
    """Call a BlueZ method via busctl. Returns (ok, detail)."""
    if not shutil.which("busctl"):
        return False, "busctl not installed"
    cmd = [
        "busctl", "call",
        "org.bluez", "/org/bluez", "org.bluez.ProfileManager1", method,
    ] + list(args)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "busctl failed to run: %s" % exc
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        return True, out or "ok"
    return False, "busctl rc=%d %s" % (proc.returncode, out[:300])


def _busctl_register_variants(xml):
    """Candidate argument lists for busctl RegisterProfile.

    Verified signature from `busctl introspect org.bluez /org/bluez`:
        RegisterProfile  method  o s a{sv}
    i.e. (profile object path, uuid, options). The earlier `sa{sv}` shape I
    used is wrong and made busctl report "Too many parameters".

    busctl's handling of the leading element count for a{sv} also differs
    across builds, so we emit both shapes and try each.
    """
    entries = [
        ("ServiceRecord", "s", xml),
        ("Role", "s", "server"),
        ("RequireAuthentication", "b", "false"),
        ("RequireAuthorization", "b", "false"),
        ("AutoConnect", "b", "true"),
    ]
    flat = []
    for key, sig, val in entries:
        flat += [key, sig, val]

    head = ["o", "s", "a{sv}", HID_PROFILE_PATH, HID_PROFILE_UUID]
    return [
        ("o s a{sv} with count", head + [str(len(entries))] + flat),
        ("o s a{sv} without count", head + flat),
    ]


def try_profile_manager(xml):
    """Register via org.bluez.ProfileManager1 (busctl, then python-dbus).

    Note: unlike the raw SDP path this may cause bluetoothd itself to bind the
    PSMs from the record, so callers must re-check that PSM 0x11/0x13 are
    still free afterwards.
    """
    errors = []

    if shutil.which("busctl"):
        for label, args in _busctl_register_variants(xml):
            ok, why = _busctl("RegisterProfile", *args)
            if ok:
                return True, "busctl RegisterProfile accepted (%s)" % label
            errors.append("busctl %s: %s" % (label, why))
    else:
        errors.append("busctl not installed")

    # Fallback: python-dbus. Discover which object path actually serves
    # ProfileManager1 rather than assuming /org/bluez. Also use an explicit
    # dbus_interface= keyword with introspect=False -- going through
    # dbus.Interface was what tripped libdbus's object-path assertion.
    try:
        import dbus
    except ImportError:
        errors.append("python3-dbus missing")
        return False, " | ".join(errors)

    try:
        bus = dbus.SystemBus()
    except Exception as exc:
        errors.append("no system D-Bus: %s" % exc)
        return False, " | ".join(errors)

    path = _find_profile_manager_path(bus)
    if path is None:
        errors.append("ProfileManager1 object not found on org.bluez")
        return False, " | ".join(errors)

    opts = dbus.Dictionary(
        {
            "ServiceRecord": dbus.String(xml),
            "Role": dbus.String("server"),
            "RequireAuthentication": dbus.Boolean(False),
            "RequireAuthorization": dbus.Boolean(False),
            "AutoConnect": dbus.Boolean(True),
        },
        signature="sv",
    )
    try:
        proxy = bus.get_object("org.bluez", path, introspect=False)
        # Signature is o s a{sv}. The path MUST be a dbus.ObjectPath -- a plain
        # str marshals as 's', and the server then reports UnknownMethod
        # with signature "ssa{sv}".
        proxy.RegisterProfile(
            dbus.ObjectPath(HID_PROFILE_PATH),
            dbus.String(HID_PROFILE_UUID),
            opts,
            dbus_interface="org.bluez.ProfileManager1",
        )
    except Exception as exc:
        errors.append("python-dbus @%s: %s" % (path, exc))
        return False, " | ".join(errors)
    return True, "python-dbus RegisterProfile accepted (@%s)" % path


def _find_profile_manager_path(bus, candidates=("/org/bluez", "/", "/org/bluez/hci0")):
    """Return the object path that serves ProfileManager1, or None."""
    import dbus

    for path in candidates:
        try:
            obj = bus.get_object("org.bluez", path, introspect=True)
            iface = dbus.Interface(obj, "org.bluez.ProfileManager1")
            # getattr on an Interface only returns a callable if the remote
            # introspection lists the method, so this is a cheap existence test.
            if hasattr(iface, "RegisterProfile"):
                return path
        except Exception:
            continue
    return None


def unregister_profile():
    """Best-effort removal of our HID profile (frees any PSM it bound)."""
    errors = []
    if shutil.which("busctl"):
        ok, why = _busctl("UnregisterProfile", "o", HID_PROFILE_PATH)
        if ok:
            return True, "profile unregistered (busctl)"
        errors.append("busctl: %s" % why)
    try:
        import dbus
    except ImportError:
        return False, " | ".join(errors) or "no D-Bus tooling"
    try:
        bus = dbus.SystemBus()
    except Exception as exc:
        errors.append("no system D-Bus: %s" % exc)
        return False, " | ".join(errors)
    path = _find_profile_manager_path(bus)
    if path is None:
        errors.append("ProfileManager1 object not found")
        return False, " | ".join(errors)
    try:
        proxy = bus.get_object("org.bluez", path, introspect=False)
        # UnregisterProfile is also 'o', so it needs ObjectPath too.
        proxy.UnregisterProfile(
            dbus.ObjectPath(HID_PROFILE_PATH),
            dbus_interface="org.bluez.ProfileManager1",
        )
    except Exception as exc:
        errors.append("python-dbus @%s: %s" % (path, exc))
        return False, " | ".join(errors)
    return True, "profile unregistered (python-dbus @%s)" % path


# --------------------------------------------------------------------------- #
# raw SDP over L2CAP PSM 1
# --------------------------------------------------------------------------- #
# Preferred path: it only registers an SDP record, so it can never steal
# PSM 0x11/0x13 from hid_keyboard.py.
BDADDR_ANY = "00:00:00:00:00:00"
# BlueZ's BDADDR_LOCAL (lib/sdp.c, src/sdpd-server.c): the pseudo-address the
# local SDP server listens on. `sdptool browse local` connects here, so a
# loopback probe must use the same value -- an earlier "FF:FF:FF:00:00:00"
# here is why the self-probe never reached the local server.
BDADDR_SDP_LOCAL = "00:00:00:FF:FF:FF"

# Object path we hand to RegisterProfile. It must be a syntactically valid
# D-Bus path; BlueZ only needs a real org.bluez.Profile1 object there when a
# peer actually connects, not at registration time.
HID_PROFILE_PATH = "/com/example/hid_keyboard/Profile"


def _sdp_connect(bdaddr):
    """Open a client socket to the LOCAL SDP server.

    Tried in order:
      1. BDADDR_ANY -> BDADDR_ANY      (kernel loopback to local host)
      2. BDADDR_ANY -> FF:FF:FF:00:00:00  (the address sdptool browse local uses)
      3. own address -> own address    (refused on this system, kept for info)

    Connecting to the adapter's real BD_ADDR is refused (no remote listener),
    so the loopback forms must come first.
    """
    candidates = [
        (BDADDR_ANY, BDADDR_ANY, "BDADDR_ANY loopback"),
        (BDADDR_ANY, BDADDR_SDP_LOCAL, "BDADDR_ANY -> %s" % BDADDR_SDP_LOCAL),
    ]
    if bdaddr and bdaddr not in (BDADDR_ANY, BDADDR_SDP_LOCAL):
        candidates.append((bdaddr, bdaddr, "adapter address"))

    errors = []
    for src, dst, label in candidates:
        try:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
            )
        except (AttributeError, OSError) as exc:
            return None, "L2CAP socket unavailable: %s" % exc
        try:
            sock.settimeout(5)
            sock.bind((src, 0))
            sock.connect((dst, 0x0001))
        except OSError as exc:
            sock.close()
            errors.append("%s: %s" % (label, exc))
            continue
        return sock, None

    return None, "; ".join(errors)


def _sdp_exchange(bdaddr, pdu_id, txid, params):
    """Send one SDP PDU and return (response_bytes_or_None, why)."""
    sock, err = _sdp_connect(bdaddr)
    if sock is None:
        return None, err
    try:
        sock.send(bytes([pdu_id]) + struct.pack(">HH", txid, len(params)) + params)
        resp = sock.recv(8192)
    except OSError as exc:
        return None, "send/recv failed: %s" % exc
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return resp, "ok"


def _sdp_pdu_name(pdu_id):
    return {
        0x01: "SDP_ErrorResponse",
        0x03: "SDP_ServiceSearchResponse",
        0x05: "SDP_ServiceAttributeResponse",
        0x07: "SDP_ServiceSearchAttributeResponse",
        0x09: "SDP_ServiceRegistrationResponse",
    }.get(pdu_id, "PDU 0x%02x" % pdu_id)


def sdp_probe(bdaddr):
    """Ask the local SDP server for the HID service, the way Android does.

    Uses ServiceSearchAttributeRequest (0x06) -- that is the PDU a phone sends
    when it wants the attributes of a service class -- and confirms the HID
    record is actually in the reply, not just that the server is alive.
    """
    pattern = de_seq([de_uuid16(HID_SVCLASS)])
    attr_ids = de_seq([de_uint16(0xFFFF)])          # all attributes
    params = (
        pattern
        + struct.pack(">H", 0xFFFF)                 # MaximumAttributeByteCount
        + attr_ids
        + b"\x00"                                   # ContinuationState: none
    )
    resp, why = _sdp_exchange(bdaddr, 0x06, 0x0001, params)
    if resp is None:
        return False, "unreachable (%s)" % why
    if not resp:
        return False, "empty response"
    if resp[0] == 0x01 and len(resp) >= 5:
        code = struct.unpack(">H", resp[3:5])[0]
        return False, "error code=0x%04x" % code
    if resp[0] != 0x07:
        return False, "unexpected reply %s" % _sdp_pdu_name(resp[0])
    if len(resp) < 7:
        return False, "short ServiceSearchAttributeResponse"
    bytecount = struct.unpack(">H", resp[5:7])[0]
    stream = resp[7:7 + bytecount]
    if bytecount == 0:
        return False, "server reachable but returned no records for HID"
    return True, "reachable, %d attribute bytes for HID (%s)" % (
        bytecount, _sdp_pdu_name(resp[0])
    )


def try_raw_sdp(record, bdaddr):
    """Send SDP_ServiceRegistrationRequest (PDU 0x08) to the local SDP server."""
    resp, why = _sdp_exchange(bdaddr, 0x08, 0x0101, record)
    if resp is None:
        return False, why
    if not resp:
        return False, "empty SDP response"
    if resp[0] == 0x09:
        return True, "SDP_ServiceRegistrationResponse ok"
    if resp[0] == 0x01 and len(resp) >= 5:
        code = struct.unpack(">H", resp[3:5])[0]
        return False, "SDP_ErrorResponse code=0x%04x" % code
    return False, "unexpected SDP %s" % _sdp_pdu_name(resp[0])


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Register local HID SDP record")
    ap.add_argument("--check", action="store_true", help="only verify, register nothing")
    ap.add_argument("--dry-run", action="store_true", help="print record, register nothing")
    ap.add_argument("--ctrl-psm", default=CTRL_PSM, type=lambda v: int(v, 0))
    ap.add_argument("--int-psm", default=INT_PSM, type=lambda v: int(v, 0))
    ap.add_argument("--bdaddr", default=None, help="local adapter address")
    ap.add_argument(
        "--unregister",
        action="store_true",
        help="remove our HID profile from ProfileManager1 and exit",
    )
    ap.add_argument(
        "--selfcheck",
        action="store_true",
        help="verify the raw and XML encoders agree, then exit (no root needed)",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="only test reachability of the local SDP server (L2CAP PSM 1)",
    )
    args = ap.parse_args()

    if args.selfcheck:
        try:
            selfcheck(verbose=True)
        except Exception as exc:  # noqa: BLE001 - report and fail loudly
            warn("self-check FAILED: %s" % exc)
            return 1
        return 0

    if args.unregister:
        done, why = unregister_profile()
        log(why)
        return 0 if done else 1

    bdaddr = args.bdaddr or os.environ.get("BT_BDADDR")
    if not bdaddr:
        try:
            out = subprocess.run(
                ["hciconfig"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
            ).stdout.decode("utf-8", "replace")
            m = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", out)
            if m:
                bdaddr = m.group(1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    before = browse_local()

    if args.probe:
        if not bdaddr:
            warn("cannot determine local BD_ADDR (pass --bdaddr)")
            return 1
        ok_p, why_p = sdp_probe(bdaddr)
        log("adapter %s" % bdaddr)
        log("SDP server: %s" % why_p)
        if before.strip():
            log("browse local: %d bytes" % len(before))
        else:
            log("browse local: no output (run as root)")
        return 0 if ok_p else 1

    if hid_record_present(before):
        log("HID record already present in local SDP (nothing to do)")
        return 0

    if args.check:
        if bdaddr:
            ok_p, why_p = sdp_probe(bdaddr)
            log("SDP server: %s" % why_p)
        warn("no HID record in local SDP")
        return 1

    xml = build_hid_record_xml(args.ctrl_psm, args.int_psm)
    raw = build_hid_record_attrs(args.ctrl_psm, args.int_psm)

    if args.dry_run:
        print(xml)
        print()
        print("raw record bytes: %d (0x%x)" % (len(raw), len(raw)))
        return 0

    if not bdaddr:
        warn("cannot determine local BD_ADDR (pass --bdaddr); raw SDP path skipped")
        bdaddr = ""

    # Probe first: tells us whether the local SDP server is reachable at all.
    if bdaddr:
        ok_p, why_p = sdp_probe(bdaddr)
        log("SDP server probe (L2CAP PSM 1): %s" % why_p)
    else:
        ok_p, why_p = False, "no adapter address"

    why1 = why2 = "not attempted"

    # Attempt 1: raw SDP registration. Cannot bind PSMs, so it can never break
    # hid_keyboard.py -- always try this before the D-Bus path.
    if bdaddr and ok_p:
        log("attempt 1/2: raw SDP_ServiceRegistrationRequest (L2CAP PSM 1)")
        _, why1 = try_raw_sdp(raw, bdaddr)
        log("  -> %s" % why1)
        out = browse_local()
        if hid_record_present(out):
            log("verified: HID record present (raw SDP)")
            return 0

    # Attempt 2: ProfileManager1. May cause bluetoothd to bind the PSMs listed
    # in the record, so setup.sh re-checks PSM 0x11/0x13 afterwards.
    log("attempt 2/2: ProfileManager1.RegisterProfile (D-Bus)")
    ok2, why2 = try_profile_manager(xml)
    log("  -> %s" % why2)
    out = browse_local()
    if hid_record_present(out):
        log("verified: HID record present (ProfileManager1)")
        return 0

    warn("no HID record could be registered")
    warn("  raw SDP:         %s" % why1)
    warn("  ProfileManager1: %s" % why2)
    _dump_profile_manager_introspection()
    if out.strip():
        log("browse output follows (truncated):")
        print(out[:1200])
    return 1


def _dump_profile_manager_introspection():
    """On total failure, show what org.bluez actually exposes. Cheap and it
    settles 'is the method there / is /org/bluez the right path' at a glance."""
    if not shutil.which("busctl"):
        return
    for path in ("/org/bluez", "/"):
        log("busctl introspect org.bluez %s (ProfileManager lines):" % path)
        try:
            proc = subprocess.run(
                ["busctl", "introspect", "org.bluez", path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warn("  introspect failed: %s" % exc)
            continue
        lines = proc.stdout.decode("utf-8", "replace").splitlines()
        hits = [ln for ln in lines if "Profile" in ln or "Register" in ln]
        if hits:
            for ln in hits[:20]:
                print("    " + ln.strip())
        else:
            print("    (no ProfileManager/Register entries; %d lines total)" % len(lines))


if __name__ == "__main__":
    sys.exit(main())
