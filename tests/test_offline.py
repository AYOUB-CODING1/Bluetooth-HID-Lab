#!/usr/bin/env python3
"""Offline tests: no Bluetooth adapter, no root, no network required.

Run from the repository root:

    python3 -m unittest discover -s tests -v
    # or simply
    python3 tests/test_offline.py
"""

import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import register_hid_sdp as sdp          # noqa: E402
import hid_keyboard as hk               # noqa: E402
import hid_keyboard_bridge as bridge    # noqa: E402

CATALOG = os.path.join(REPO_ROOT, "examples", "demo_shortcuts.json")

# Tokens that must never appear in a shipped example catalog. This is a guard
# against accidentally re-introducing offensive tooling into the public repo.
# Word boundaries keep short tokens (e.g. "rmi") from matching inside ordinary
# words such as "terminal".
FORBIDDEN_TOKENS = [
    r"\bpowershell\b", r"\bcmd\.exe\b", r"\bnet user\b", r"\bnet localgroup\b",
    r"\bsysrq\b", r"\brmi\b", r"\bdd if=", r"\brm -rf\b", r"\bcurl\b",
    r"\bwget\b", r"\bbase64\b", r"/dev/sd", r"\bmkfs\b", r"\bshutdown\b",
    r"\breboot\b", r"\bnircmd\b", r"reverse shell", r"bind shell",
    r"\bmeterpreter\b",
]


class TestReportDescriptor(unittest.TestCase):
    def test_selfcheck_passes(self):
        self.assertTrue(sdp.selfcheck(verbose=False))

    def test_descriptor_declares_two_report_ids(self):
        d = sdp.HID_REPORT_DESCRIPTOR
        self.assertEqual(len(d), 90)
        self.assertIn(0x85, d)  # REPORT_ID item is present

    def test_keyboard_report_uses_report_id_1(self):
        hk.hid_state["boot"] = False
        self.assertEqual(
            hk.make_kbd_report(2, 7),
            bytes([0xA1, 0x01, 0x02, 0, 7, 0, 0, 0, 0, 0]),
        )
        hk.hid_state["boot"] = True
        self.assertEqual(
            hk.make_kbd_report(2, 7),
            bytes([0xA1, 0x02, 0, 7, 0, 0, 0, 0, 0]),
        )
        hk.hid_state["boot"] = False  # restore default


class TestCharMapping(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(hk.char_report("a"), (0, 4))

    def test_uppercase_adds_shift(self):
        # The regression: uppercase used to be dropped entirely.
        self.assertEqual(hk.char_report("A"), (2, 4))
        self.assertEqual(hk.char_report("Z"), (2, 4 + 25))

    def test_digits_and_shifted_digits(self):
        self.assertIsNotNone(hk.char_report("5"))
        self.assertEqual(hk.char_report("5")[0], 0)
        self.assertEqual(hk.char_report("?")[0], 2)

    def test_unmapped(self):
        self.assertIsNone(hk.char_report("\n"))
        self.assertIsNone(hk.char_report("\t"))

    def test_type_text_emits_uppercase(self):
        sent = []

        class FakeConn:
            def sendall(self, data):
                sent.append(bytes(data))

        hk.hid_state["boot"] = False
        hk.type_text(FakeConn(), "A")
        hk.hid_state["boot"] = False
        # Expect: shift-down, shift+A, release.
        self.assertEqual(sent[0], bytes([0xA1, 0x01, 0x02, 0, 0, 0, 0, 0, 0, 0]))
        self.assertEqual(sent[1], bytes([0xA1, 0x01, 0x02, 0, 4, 0, 0, 0, 0, 0]))
        self.assertEqual(sent[2], bytes([0xA1, 0x01, 0x00, 0, 0, 0, 0, 0, 0, 0]))


class TestDemoCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CATALOG, encoding="utf-8") as f:
            cls.entries = json.load(f)

    def test_required_fields(self):
        for e in self.entries:
            for field in ("id", "os", "name", "keys", "severity"):
                self.assertIn(field, e, e.get("id"))
            self.assertIsInstance(e.get("action", []), list)

    def test_ids_unique(self):
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_offensive_tooling(self):
        blob = json.dumps(self.entries).lower()
        for token in FORBIDDEN_TOKENS:
            self.assertIsNone(re.search(token, blob), "forbidden token %r in catalog" % token)

    def test_bridge_loads_catalog(self):
        rows = bridge.load_catalog()
        self.assertEqual(len(rows), len(self.entries))


class TestExamples(unittest.TestCase):
    def test_demo_page_exists(self):
        page = os.path.join(REPO_ROOT, "examples", "demo_page", "index.html")
        with open(page, encoding="utf-8") as f:
            self.assertIn("Bluetooth HID Lab", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
