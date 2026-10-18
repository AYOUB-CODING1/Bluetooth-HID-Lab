# Contributing

Thanks for your interest. This project is an **educational Bluetooth HID lab**.
Please keep contributions within that scope.

## Ground rules

* Only test on devices you own or are explicitly authorized to test.
* Do **not** contribute payloads, catalog entries, or scripts whose purpose is
  persistence, credential theft, destruction, denial of service, or bypassing a
  security control. Such changes will be rejected.
* Keep every claim honest. Mark anything you did not verify as
  "not tested" rather than implying it was.

## Development setup

```bash
git clone <your-fork-url>
cd <repo>
sudo apt install -y bluez python3-dbus python3-gi dbus   # runtime tools
```

The runtime Python dependencies are `dbus-python` and `PyGObject`
(`requirements.txt`); the tests are stdlib-only.

## Running the tests

The tests are fully offline (no adapter, no root, no network):

```bash
python3 -m unittest discover -s tests -v
```

Please keep them green. If you change the report descriptor or the character
mapping, add or update a test.

## Style

* Python: standard library only; PEP 8-ish; docstrings explain *why*, not just
  *what*.
* Shell: `set -euo pipefail`, quote variables, explain each verification step in
  a comment.
* No comment banners that merely restate the code.

## Adding a shortcut catalog entry

Catalog files are JSON arrays. Each entry uses:

```json
{
  "id": "unique-slug",
  "os": "android|linux|macos|windows|browser|cross",
  "name": "Human readable name",
  "keys": "human readable keys",
  "severity": "info|low|medium|high|critical",
  "impact": "What it does, in one sentence.",
  "notes": "Caveats and scope.",
  "source": "Where the usage/keys come from.",
  "tags": ["..."],
  "action": ["bridge command", "another command"]
}
```

`action` is a list of commands understood by the interactive bridge
(`type`, `key`, `combo`, `hold`, `repeat`, `delay`, `media`, `consumer`, …).

The shipped `examples/demo_shortcuts.json` is deliberately benign and is
checked by `tests/test_offline.py`, which rejects known offensive tooling
strings. Keep it that way.

## Pull request checklist

- [ ] `python3 -m unittest discover -s tests` passes.
- [ ] `bash -n scripts/*.sh` and `python3 -m py_compile src/*.py` pass.
- [ ] New behaviour is documented (README or `docs/`).
- [ ] No secrets, real MAC addresses, IPs, or personal data are included.

## Security

See the "Security and scope" section of the README. To report a problem with
this project itself, open an issue (do not include private device details).
