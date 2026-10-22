"""The G-code sender against the dashboard contract (contracts/gcode/v1.json,
vendored from FloorWatch). Review finding F05: a 4.2 MB program made 4,200
chunks and FloorWatch (limit 4,096) refused it.
Run: python -m unittest -v
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import agent_gcode  # noqa: E402
import make_gcode_fixture  # noqa: E402

CONTRACT = json.load(open(os.path.join(ROOT, "contracts", "gcode", "v1.json"), encoding="utf-8"))
META = {"file_name": "big.ngc", "file_size": 0, "file_modified_ms": 1}


def sender():
    return agent_gcode.GcodeFileSender(1000, "M", 1, threaded=False, pace_s=0)


def decode(packets):
    return [json.loads(p) for p in packets]


class ContractTests(unittest.TestCase):
    def test_limits_match_the_contract(self):
        self.assertEqual(agent_gcode.MAX_PROGRAM_CHARS, CONTRACT["max_program_chars"])
        self.assertEqual(agent_gcode.MAX_CHUNKS, CONTRACT["max_chunks"])
        self.assertEqual(agent_gcode.MAX_PACKET_BYTES, CONTRACT["max_packet_bytes"])

    def test_review_f05_program_fits_the_contract(self):
        text = "G1 X1\n" * 700_000                                  # 4.2 MB, the review's case
        packets = sender().packets(dict(META, file_size=len(text)), text)
        heads = decode([packets[0], packets[-1]])
        self.assertEqual(heads[0]["type"], "gcode_file")
        self.assertLessEqual(heads[0]["total_chunks"], CONTRACT["max_chunks"])
        self.assertEqual(len(packets), heads[0]["total_chunks"])
        self.assertTrue(all(len(p) <= CONTRACT["max_packet_bytes"] for p in packets))
        self.assertEqual(heads[0]["content_sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_largest_allowed_ascii_program_is_sent(self):
        line = "G1 X12.345 Y67.890 F600\n"
        text = (line * (agent_gcode.MAX_PROGRAM_CHARS // len(line) + 1))[:agent_gcode.MAX_PROGRAM_CHARS]
        packets = sender().packets(dict(META, file_size=len(text)), text)
        first = json.loads(packets[0])
        self.assertEqual(first["type"], "gcode_file")
        self.assertLess(first["total_chunks"], 40_000)              # ~34 000 for ASCII
        self.assertLessEqual(first["total_chunks"], CONTRACT["max_chunks"])

    def test_one_character_too_many_is_refused(self):
        text = "x" * (agent_gcode.MAX_PROGRAM_CHARS + 1)
        (only,) = decode(sender().packets(dict(META, file_size=len(text)), text))
        self.assertEqual(only["type"], "gcode_file_refused")
        self.assertEqual(only["reason"], "too_large")
        self.assertEqual(only["limit_chars"], CONTRACT["max_program_chars"])
        self.assertEqual(only["limit_chunks"], CONTRACT["max_chunks"])
        self.assertEqual(only["file_name"], "big.ngc")

    def test_too_many_chunks_is_refused(self):
        with mock.patch.object(agent_gcode, "MAX_CHUNKS", 3):
            (only,) = decode(sender().packets(dict(META), "G1 X1\n" * 1000))
        self.assertEqual((only["type"], only["reason"]), ("gcode_file_refused", "too_many_chunks"))

    def test_huge_file_is_refused_without_being_read(self):
        sent = []
        s = sender()
        meta = dict(META, file_size=4 * agent_gcode.MAX_PROGRAM_CHARS + 1)
        with mock.patch("builtins.open", side_effect=AssertionError("must not read")):
            s.check_and_send("/nonexistent/huge.ngc", meta, lambda b: sent.append(b) or True)
        (only,) = decode(sent)
        self.assertEqual(only["type"], "gcode_file_refused")

    def test_the_file_is_read_off_the_calling_thread(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.ngc")
            with open(path, "w") as f:
                f.write("G0 X0\n")
            s = agent_gcode.GcodeFileSender(1000, "M", 1, pace_s=0, threaded=True)
            opened_on = []
            real_open = open

            def spy(*a, **k):
                import threading
                opened_on.append(threading.current_thread().name)
                return real_open(*a, **k)
            sent = []
            with mock.patch("builtins.open", side_effect=spy):
                s.check_and_send(path, dict(META, file_size=6), lambda b: sent.append(b) or True)
                s._worker.join(5)
        self.assertEqual(opened_on, ["gcode-send"])
        self.assertEqual(len(sent), 1)


class FixtureTests(unittest.TestCase):
    """contracts/gcode/fixtures/ is what FloorWatch's tests assemble; it must be
    exactly what this sender produces (regenerate with tools/make_gcode_fixture.py)."""

    def test_committed_fixture_matches_the_sender(self):
        folder = os.path.join(ROOT, "contracts", "gcode", "fixtures")
        with open(os.path.join(folder, "sample_program.ngc"), encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertEqual(text, make_gcode_fixture.program())
        with open(os.path.join(folder, "sample_packets.jsonl"), encoding="utf-8") as f:
            committed = [json.loads(line) for line in f]
        self.assertEqual(committed, make_gcode_fixture.packets(text))
        self.assertGreater(len(committed), 3)                       # really chunked
        self.assertEqual("".join(p["content"] for p in committed), text)


if __name__ == "__main__":
    unittest.main()
