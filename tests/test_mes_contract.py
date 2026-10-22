"""The vendored MES event contract (contracts/mes/v1, canonical in FloorWatch;
MES plan P1.1): the copy is unchanged, its checker works on this agent's
Python, and the agent's own identifiers fit the envelope.
Run: python -m unittest -v
"""
import json
import os
import re
import sys
import unittest
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONTRACT = os.path.join(ROOT, "contracts", "mes", "v1")
sys.path.insert(0, CONTRACT)

import validate  # noqa: E402  (the contract's own standard-library checker)

VALID = os.path.join(CONTRACT, "fixtures", "valid")
INVALID = os.path.join(CONTRACT, "fixtures", "invalid")


def _bytes(folder, name):
    with open(os.path.join(folder, name), "rb") as f:
        return f.read()


def _agent_version():
    with open(os.path.join(ROOT, "status.py"), encoding="utf-8") as f:
        return re.search(r'^AGENT_VERSION: str = "([^"]+)"', f.read(), re.M).group(1)


class VendoredContractTests(unittest.TestCase):
    def test_copy_is_unchanged_and_pinned(self):
        self.assertEqual(validate.manifest_problems(CONTRACT), [],
                         "never edit the copy: change FloorWatch's and run "
                         "tools/vendor_mes_contract.py there")
        with open(os.path.join(ROOT, "contracts", "mes", "CONTRACT_SOURCE"),
                  encoding="utf-8") as f:
            source = dict(line.split(": ", 1) for line in f.read().splitlines() if line)
        self.assertEqual(source["repository"], "ajitesh1020/floorwatch")
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(source["manifest_sha256"], validate.manifest_digest(CONTRACT))

    def test_fixtures_on_this_python(self):
        for name in sorted(os.listdir(VALID)):
            self.assertEqual(validate.validate_bytes(_bytes(VALID, name)), [], name)
        expected = json.loads(_bytes(INVALID, "EXPECTED.json").decode("utf-8"))["expected"]
        for name, want in expected.items():
            errors = validate.validate_bytes(_bytes(INVALID, name + ".json"))
            self.assertIn((want["path"], want["code"]), [(e.path, e.code) for e in errors], name)

    def test_agent_identifiers_fit_the_envelope(self):
        with open(os.path.join(VALID, "scanner_state.json"), encoding="utf-8") as f:
            event = json.load(f)
        event["source"] = {"app": "linuxcnc-status-agent", "version": _agent_version(),
                           "record": "spool:1"}
        event["clock"]["boot_id"] = uuid.uuid4().hex[:16]     # status.py session_id
        event["producer"]["id"] = str(uuid.uuid4())
        event["producer"]["epoch"] = str(uuid.uuid4())
        self.assertEqual(validate.validate_bytes(validate.encode(event)), [])


if __name__ == "__main__":
    unittest.main()
