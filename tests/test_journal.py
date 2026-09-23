"""Tests for the on-machine history journal and its request/reply format.
Run: python -m unittest -v
"""
import json
import os
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_journal as aj  # noqa: E402


def _pkt(ts, **over):
    p = {"type": "status", "ts": ts, "cycle_state": "IDLE", "parts_produced": 0,
         "abort_count": 0, "partial_parts": 0, "exec_state": 2, "estop": False,
         "task_state": 4, "file_name": "a.ngc", "axis": {"x": {"pos": 1}},
         "joints": [{"id": 0, "fault": False}], "nml_errors": []}
    p.update(over)
    return p


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "journal.db")
        self.j = aj.Journal(self.path, interval_s=10, retain_days=14)

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def test_agent_id_is_stable_across_restarts(self):
        first = self.j.agent_id
        self.j.close()
        again = aj.Journal(self.path)
        self.assertEqual(again.agent_id, first)
        again.close()

    def test_compact_drops_heavy_fields(self):
        c = aj.compact(_pkt(1, joints=[{"id": 0, "fault": False},
                                       {"id": 2, "fault": True, "hard_limit": False}]))
        self.assertNotIn("axis", c)
        self.assertEqual(c["joints"], [{"id": 2, "fault": True, "hard_limit": False}])
        self.assertNotIn("nml_errors", c)

    def test_writes_on_change_else_on_interval(self):
        self.assertEqual(self.j.record(_pkt(1000), now=0), 1)          # first
        self.assertIsNone(self.j.record(_pkt(2000), now=1))           # nothing new
        self.assertEqual(self.j.record(_pkt(3000, cycle_state="RUNNING"), now=2), 2)
        self.assertIsNone(self.j.record(_pkt(4000, cycle_state="RUNNING"), now=5))
        self.assertEqual(self.j.record(_pkt(5000, cycle_state="RUNNING"), now=12.5), 3)
        self.assertEqual(self.j.record(_pkt(6000, cycle_state="RUNNING",
                                            parts_produced=1), now=13), 4)
        self.assertEqual(self.j.record(_pkt(7000, cycle_state="RUNNING", parts_produced=1,
                                            nml_errors=[{"kind": 11, "msg": "x"}]),
                                       now=13.5), 5)
        self.assertEqual(self.j.last_seq, 5)

    def test_prune_old_entries(self):
        now_ms = int(time.time() * 1000)
        self.j.record(_pkt(now_ms - 20 * 86_400_000), now=0)
        self.j.record(_pkt(now_ms, cycle_state="RUNNING"), now=1)
        self.assertEqual(self.j.prune(now_ms), 1)

    def _fill(self, n):
        for i in range(1, n + 1):
            self.j.record(_pkt(i * 1000, parts_produced=i), now=i)

    def test_reply_paging(self):
        self._fill(300)
        conn = aj._open(self.path)
        req = {"type": "journal_request", "from_seq": 0}
        r = aj.build_reply(conn, req, "CNC-01", self.j.agent_id, max_bytes=4000)
        self.assertTrue(r["more"])
        self.assertEqual(r["entries"][0][0], 1)
        self.assertLessEqual(len(json.dumps(r)), 4600)
        got = [e[0] for e in r["entries"]]
        while r["more"]:
            r = aj.build_reply(conn, dict(req, from_seq=got[-1]), "CNC-01",
                               self.j.agent_id, max_bytes=4000)
            got += [e[0] for e in r["entries"]]
        self.assertEqual(got, list(range(1, 301)))
        self.assertEqual((r["first_seq"], r["last_seq"]), (1, 300))
        conn.close()

    def test_reply_respects_to_seq_and_rejects_junk(self):
        self._fill(20)
        conn = aj._open(self.path)
        r = aj.build_reply(conn, {"type": "journal_request", "from_seq": 5, "to_seq": 8},
                           "M", self.j.agent_id)
        self.assertEqual([e[0] for e in r["entries"]], [6, 7, 8])
        self.assertFalse(r["more"])
        self.assertEqual(r["entries"][0][2]["parts_produced"], 6)
        self.assertIsNone(aj.build_reply(conn, {"type": "status"}, "M", "a"))
        self.assertIsNone(aj.build_reply(conn, {"type": "journal_request",
                                                "from_seq": "x"}, "M", "a"))
        conn.close()


class JournalServerTests(unittest.TestCase):
    def test_round_trip_over_udp(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.db")
            j = aj.Journal(path)
            for i in range(1, 6):
                j.record(_pkt(i * 1000, parts_produced=i), now=i)
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            srv = aj.JournalServer(path, port, "CNC-01", j.agent_id)
            self.assertTrue(srv.start())
            try:
                c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                c.settimeout(3)
                c.sendto(json.dumps({"type": "journal_request", "proto": 1,
                                     "from_seq": 2}).encode(), ("127.0.0.1", port))
                reply = json.loads(c.recvfrom(65535)[0])
                c.close()
            finally:
                srv.stop()
                j.close()
            self.assertEqual(reply["type"], "journal")
            self.assertEqual(reply["machine_name"], "CNC-01")
            self.assertEqual([e[0] for e in reply["entries"]], [3, 4, 5])


if __name__ == "__main__":
    unittest.main()
