#!/usr/bin/env python3
"""Minimal SDP server that advertises one HID record.

Root cause this exists for: nothing on this system listens on L2CAP PSM 1.
`sdptool browse local` still "works" because bluez 5.87 implements it over
D-Bus and prints a dummy address, so it hides the fact that there is no
over-the-air SDP server. Android discovers an input device by querying SDP on
PSM 1; with no listener there it pairs and then never connects PSM 0x11/0x13.

So we bind PSM 1 ourselves and answer the three standard queries.

Usage: sudo ./hid_sdp_server.py [--bdaddr ADDR] [--ctrl-psm 0x11] ...
Exit 0 after a clean shutdown; non-zero if PSM 1 could not be bound.
"""
import argparse
import os
import signal
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import register_hid_sdp as rec  # noqa: E402  (reuses the record builders)

SDP_PSM = 0x0001
RECORD_HANDLE = 0x00010010

PDU_ERROR_RSP = 0x01
PDU_SVC_SEARCH_REQ = 0x02
PDU_SVC_SEARCH_RSP = 0x03
PDU_SVC_ATTR_REQ = 0x04
PDU_SVC_ATTR_RSP = 0x05
PDU_SVC_SEARCH_ATTR_REQ = 0x06
PDU_SVC_SEARCH_ATTR_RSP = 0x07

ERR_INVALID_SYNTAX = 0x0001
ERR_INVALID_PDU_SIZE = 0x0002
ERR_INVALID_REQUEST_SYNTAX = 0x0003
ERR_INVALID_SVC_CLASS_ID = 0x0004
ERR_INVALID_SVC_RECORD_HANDLE = 0x0006


def log(msg):
    print("[sdpsrv] %s" % msg, flush=True)


def warn(msg):
    print("[sdpsrv] ! %s" % msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# data element reading
# --------------------------------------------------------------------------- #
_SIZE_FIXED = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16}


class DEError(Exception):
    pass


def de_read(buf, pos):
    """Read one data element. Returns (value_tuple, new_pos).

    value_tuple is (type_descriptor, payload_bytes).
    """
    if pos >= len(buf):
        raise DEError("truncated element header")
    header = buf[pos]
    pos += 1
    dtype = header >> 3
    dsize = header & 0x07

    if dsize in (5, 6, 7):
        n = {5: 1, 6: 2, 7: 4}[dsize]
        if pos + n > len(buf):
            raise DEError("truncated length")
        length = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    else:
        length = _SIZE_FIXED[dsize]

    if pos + length > len(buf):
        raise DEError("truncated payload (want %d, have %d)" % (length, len(buf) - pos))
    payload = buf[pos:pos + length]
    pos += length
    return (dtype, payload), pos


def de_read_seq(buf, pos):
    """Read a DataElementSequence, returning the list of inner elements."""
    (dtype, payload), pos = de_read(buf, pos)
    if dtype != 6:
        raise DEError("expected sequence, got type %d" % dtype)
    inner = []
    p = 0
    while p < len(payload):
        el, p = de_read(payload, p)
        inner.append(el)
    return inner, pos


def elem_uuids(elements):
    """Collect 16-bit UUIDs from a list of (type, payload) elements."""
    out = []
    for dtype, payload in elements:
        if dtype == 3 and len(payload) == 2:
            out.append(struct.unpack(">H", payload)[0])
        elif dtype == 6:
            # nested sequence (e.g. a ProtocolDescriptorList); look inside
            p = 0
            while p < len(payload):
                el, p = de_read(payload, p)
                if el[0] == 3 and len(el[1]) == 2:
                    out.append(struct.unpack(">H", el[1])[0])
    return out


def skip_continuation(buf, pos):
    """Consume the ContinuationState field. We never use it (one-shot replies)."""
    if pos >= len(buf):
        return pos
    n = buf[pos]
    return pos + 1 + n


# --------------------------------------------------------------------------- #
# PDU framing
# --------------------------------------------------------------------------- #
def make_pdu(pdu_id, txid, params):
    return bytes([pdu_id]) + struct.pack(">HH", txid, len(params)) + params


def make_error(txid, code, request_pdu, message=b"sdp error"):
    params = (
        struct.pack(">H", code)
        + bytes([request_pdu])
        + rec.de_text(message)
    )
    return make_pdu(PDU_ERROR_RSP, txid, params)


# --------------------------------------------------------------------------- #
# request handlers
# --------------------------------------------------------------------------- #
def handle_service_search(params, txid, record_handle):
    """0x02 -> 0x03. Params: pattern(DESeq), maxRecords(u16), contState."""
    try:
        pattern, pos = de_read_seq(params, 0)
    except DEError as exc:
        return make_error(txid, ERR_INVALID_REQUEST_SYNTAX, PDU_SVC_SEARCH_REQ,
                          str(exc).encode())
    uuids = elem_uuids(pattern)
    match = rec.HID_SVCLASS in uuids

    if match:
        handles = struct.pack(">I", record_handle)
        total = current = 1
    else:
        handles = b""
        total = current = 0

    resp = (
        struct.pack(">HH", total, current)
        + handles
        + b"\x00"          # ContinuationState: none
    )
    return make_pdu(PDU_SVC_SEARCH_RSP, txid, resp)


def handle_service_attribute(params, txid, record_attrs, record_handle):
    """0x04 -> 0x05. Params: handle(u32), maxBytes(u16), attrIDList(DESeq), cont."""
    if len(params) < 6:
        return make_error(txid, ERR_INVALID_REQUEST_SYNTAX, PDU_SVC_ATTR_REQ,
                          b"short params")
    handle = struct.unpack(">I", params[0:4])[0]
    if handle != record_handle:
        return make_error(txid, ERR_INVALID_SVC_RECORD_HANDLE, PDU_SVC_ATTR_REQ,
                          b"unknown handle")

    body = record_attrs
    resp = struct.pack(">H", len(body)) + body + b"\x00"
    return make_pdu(PDU_SVC_ATTR_RSP, txid, resp)


def handle_service_search_attribute(params, txid, record_attrs):
    """0x06 -> 0x07. Params: pattern(DESeq), maxBytes(u16), attrIDList(DESeq), cont."""
    try:
        pattern, pos = de_read_seq(params, 0)
    except DEError as exc:
        return make_error(txid, ERR_INVALID_REQUEST_SYNTAX, PDU_SVC_SEARCH_ATTR_REQ,
                          str(exc).encode())
    uuids = elem_uuids(pattern)
    if rec.HID_SVCLASS not in uuids:
        # Nothing of interest: an empty record list, not an error.
        resp = struct.pack(">H", 0) + b"\x00"
        return make_pdu(PDU_SVC_SEARCH_ATTR_RSP, txid, resp)

    # AttributeListsByteStream is a DataElementSequence holding one
    # DataElementSequence per matching record.
    stream = rec.de_seq([record_attrs])
    resp = struct.pack(">H", len(stream)) + stream + b"\x00"
    return make_pdu(PDU_SVC_SEARCH_ATTR_RSP, txid, resp)


def dispatch(data, record_attrs, record_handle):
    if len(data) < 5:
        return None
    pdu_id = data[0]
    txid = struct.unpack(">H", data[1:3])[0]
    plen = struct.unpack(">H", data[3:5])[0]
    params = data[5:5 + plen]

    if pdu_id == PDU_SVC_SEARCH_REQ:
        return handle_service_search(params, txid, record_handle)
    if pdu_id == PDU_SVC_ATTR_REQ:
        return handle_service_attribute(params, txid, record_attrs, record_handle)
    if pdu_id == PDU_SVC_SEARCH_ATTR_REQ:
        return handle_service_search_attribute(params, txid, record_attrs)
    return make_error(txid, ERR_INVALID_PDU_SIZE, pdu_id, b"unsupported pdu")


# --------------------------------------------------------------------------- #
# server loop
# --------------------------------------------------------------------------- #
def serve(bdaddr, ctrl_psm, int_psm):
    record_attrs = rec.build_hid_record_attrs(ctrl_psm, int_psm)

    # Bind to BDADDR_ANY rather than the adapter's own address: the phone
    # addresses us by whatever it learned from inquiry, and our own probe
    # connects via BDADDR_ANY. A wildcard bind accepts both.
    src = rec.BDADDR_ANY
    try:
        srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                            socket.BTPROTO_L2CAP)
    except (AttributeError, OSError) as exc:
        warn("L2CAP socket unavailable: %s" % exc)
        return 1

    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass

    try:
        srv.bind((src, SDP_PSM))
        srv.listen(8)
    except OSError as exc:
        warn("cannot bind L2CAP PSM 0x0001 (SDP): %s" % exc)
        warn("Something already owns the SDP channel; not starting.")
        return 1

    log("listening on %s L2CAP PSM 0x%04x (SDP)" % (src, SDP_PSM))
    if bdaddr:
        log("local adapter: %s" % bdaddr)
    log("advertising HID record handle 0x%08x, %d bytes, PSM 0x%02x/0x%02x"
        % (RECORD_HANDLE, len(record_attrs), ctrl_psm, int_psm))

    stopping = {"flag": False}

    def _stop(signum, frame):
        stopping["flag"] = True
        try:
            srv.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    srv.settimeout(1.0)
    while not stopping["flag"]:
        try:
            conn, peer = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        log("SDP query from %s" % (peer[0] if peer else "unknown"))
        conn.settimeout(5)
        try:
            while True:
                try:
                    data = conn.recv(8192)
                except socket.timeout:
                    break
                if not data:
                    break
                log("  rx %d bytes, PDU 0x%02x" % (len(data), data[0]))
                reply = dispatch(data, record_attrs, RECORD_HANDLE)
                if reply is None:
                    break
                log("  tx %d bytes, PDU 0x%02x" % (len(reply), reply[0]))
                conn.send(reply)
        except OSError as exc:
            log("  connection error: %s" % exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    log("stopped")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Serve one HID SDP record on PSM 1")
    ap.add_argument("--bdaddr", default=os.environ.get("BT_BDADDR"),
                    help="local adapter address (default: all adapters)")
    ap.add_argument("--ctrl-psm", default=rec.CTRL_PSM, type=lambda v: int(v, 0))
    ap.add_argument("--int-psm", default=rec.INT_PSM, type=lambda v: int(v, 0))
    args = ap.parse_args()
    return serve(args.bdaddr, args.ctrl_psm, args.int_psm)


if __name__ == "__main__":
    sys.exit(main())
