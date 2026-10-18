#!/usr/bin/env python3
"""One process that does everything BlueZ needs from us:

  1. Registers an org.bluez.Agent1 (NoInputNoOutput) that auto-accepts every
     pairing/authorization callback. The old `bluetoothctl --agent` pipe
     rendered an interactive yes/no prompt that nobody answered, so BlueZ
     cancelled the request and the phone dropped the link (reason 21).
  2. Registers an org.bluez.Profile1 object and calls
     ProfileManager1.RegisterProfile(object_path, uuid, options) with an HID
     ServiceRecord but NO PSM, so bluetoothd installs the HID record into its
     SDP server (which it owns on L2CAP PSM 1) while leaving PSM 0x11/0x13
     free for hid_keyboard.py to serve.

The Profile1 object must exist at the path we pass: the D-Bus signature is
`o s a{sv}` and BlueZ calls NewConnection/RequestDisconnection on that path.

Usage: sudo ./hid_daemon.py [--bdaddr ADDR]
"""
import argparse
import os
import signal
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import register_hid_sdp as rec  # noqa: E402

AGENT_PATH = "/com/example/hid_keyboard/Agent"
PROFILE_PATH = rec.HID_PROFILE_PATH
PROFILE_UUID = rec.HID_PROFILE_UUID
AGENT_IFACE = "org.bluez.Agent1"
PROFILE_IFACE = "org.bluez.Profile1"


def log(msg):
    print("[daemon] %s" % msg, flush=True)


def warn(msg):
    print("[daemon] ! %s" % msg, file=sys.stderr, flush=True)


def _addr(device):
    try:
        tail = str(device).rsplit("/", 1)[-1]
        if tail.startswith("dev_"):
            return tail[4:].replace("_", ":")
    except Exception:
        pass
    return str(device)


# --------------------------------------------------------------------------- #
class Agent(dbus.service.Object):
    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        log("Agent.Release()")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log("RequestPinCode(%s) -> 0000" % _addr(device))
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log("DisplayPinCode(%s, %s)" % (_addr(device), pincode))

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        log("RequestPasskey(%s) -> 0" % _addr(device))
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log("DisplayPasskey(%s, %06d)" % (_addr(device), int(passkey)))

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log("RequestConfirmation(%s, %06d) -> accepted"
            % (_addr(device), int(passkey)))

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log("RequestAuthorization(%s) -> accepted" % _addr(device))

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log("AuthorizeService(%s, %s) -> accepted" % (_addr(device), uuid))

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        log("Agent.Cancel()")


# --------------------------------------------------------------------------- #
class Profile(dbus.service.Object):
    """BlueZ only calls these if it owns the connection itself. We deliberately
    do not hand it a PSM, so in practice they stay quiet -- but the object must
    exist for RegisterProfile to be accepted."""

    @dbus.service.method(PROFILE_IFACE, in_signature="", out_signature="")
    def Release(self):
        log("Profile.Release()")

    @dbus.service.method(PROFILE_IFACE, in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, properties):
        log("Profile.NewConnection(%s) -> closing (we serve HID ourselves)"
            % _addr(device))
        try:
            os.close(fd.take())
        except Exception:
            pass

    @dbus.service.method(PROFILE_IFACE, in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        log("Profile.RequestDisconnection(%s)" % _addr(device))


# --------------------------------------------------------------------------- #
def register_agent(bus, agent):
    mgr = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1"
    )
    mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    mgr.RequestDefaultAgent(AGENT_PATH)
    log("agent registered (NoInputNoOutput) at %s" % AGENT_PATH)
    return mgr


def register_profile(bus, profile, xml):
    pm = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1"
    )
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
    # Signature is `o s a{sv}`: the path must be a dbus.ObjectPath, not a str.
    pm.RegisterProfile(
        dbus.ObjectPath(PROFILE_PATH),
        dbus.String(PROFILE_UUID),
        opts,
    )
    log("profile registered: uuid=%s path=%s" % (PROFILE_UUID, PROFILE_PATH))
    return pm


def find_adapter(bus, bdaddr):
    """Return (object_path, properties) of the matching org.bluez.Adapter1."""
    om = dbus.Interface(
        bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager"
    )
    fallback = None
    for path, ifaces in om.GetManagedObjects().items():
        props = ifaces.get("org.bluez.Adapter1")
        if props is None:
            continue
        if bdaddr and str(props.get("Address", "")).upper() == bdaddr.upper():
            return path, props
        if fallback is None:
            fallback = (path, props)
    return fallback if fallback else (None, None)


def verify_hid_in_uuids(bus, bdaddr, tries=10):
    """A successful RegisterProfile does NOT prove the record reached
    adapter->services: ext_adapter_probe returns 0 even when the record is
    dropped. The authoritative check is Adapter1.UUIDs (src/adapter.c reads it
    from adapter->services), which is exactly what the phone discovers."""
    want = PROFILE_UUID.lower()
    path, props = find_adapter(bus, bdaddr)
    if path is None:
        warn("verify: no org.bluez.Adapter1 found on this bus")
        return False
    for i in range(tries):
        uuids = []
        try:
            props = dbus.Interface(
                bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties"
            ).GetAll("org.bluez.Adapter1")
            uuids = [str(u).lower() for u in props.get("UUIDs", [])]
        except Exception as exc:
            warn("verify: reading UUIDs failed: %s" % exc)
            return False
        if want in uuids:
            log("verified: %s is advertised in Adapter1.UUIDs (%s)" % (want, path))
            return True
        GLib.usleep(500 * 1000)
    warn("NOT verified: %s missing from Adapter1.UUIDs on %s" % (want, path))
    warn("  UUIDs now: %s" % (", ".join(uuids) if uuids else "<empty>"))
    return False


def main():
    ap = argparse.ArgumentParser(description="BlueZ agent + HID SDP record")
    ap.add_argument("--bdaddr", default=None)
    ap.add_argument("--ctrl-psm", default=rec.CTRL_PSM, type=lambda v: int(v, 0))
    ap.add_argument("--int-psm", default=rec.INT_PSM, type=lambda v: int(v, 0))
    ap.add_argument(
        "--no-profile",
        action="store_true",
        help="register the pairing agent only, no HID SDP record",
    )
    args = ap.parse_args()

    xml = rec.build_hid_record_xml(args.ctrl_psm, args.int_psm)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    agent_obj = Agent(bus, AGENT_PATH)
    profile_obj = Profile(bus, PROFILE_PATH)

    agent_mgr = None
    profile_mgr = None

    # bluetoothd may still be coming up; retry the D-Bus calls for a while.
    for attempt in range(1, 120):
        try:
            agent_mgr = register_agent(bus, agent_obj)
            break
        except Exception as exc:
            if attempt == 1:
                warn("agent registration pending: %s" % exc)
            GLib.usleep(500 * 1000)
    if agent_mgr is None:
        warn("could not register agent")
        return 1

    for attempt in range(1, 40):
        if args.no_profile:
            log("--no-profile: skipping HID SDP registration")
            break
        try:
            profile_mgr = register_profile(bus, profile_obj, xml)
            break
        except Exception as exc:
            if attempt == 1:
                warn("profile registration pending: %s" % exc)
            if attempt == 39:
                warn("could not register profile: %s" % exc)
                return 1
            GLib.usleep(500 * 1000)

    if not args.no_profile:
        verify_hid_in_uuids(bus, args.bdaddr)

    log("ready")

    loop = GLib.MainLoop()
    stopping = {"flag": False}

    def _stop(*_):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        log("shutting down")
        for mgr, fn, arg in (
            (profile_mgr, "UnregisterProfile", PROFILE_PATH),
            (agent_mgr, "UnregisterAgent", AGENT_PATH),
        ):
            if mgr is None:
                continue
            try:
                getattr(mgr, fn)(arg)
            except Exception:
                pass
        loop.quit()

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _stop)

    try:
        loop.run()
    except KeyboardInterrupt:
        _stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
