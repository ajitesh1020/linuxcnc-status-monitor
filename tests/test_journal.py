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

    def test_failed_commands_written_at_once_other_commands_not(self):
        self.assertEqual(self.j.record(_pkt(1000, cmd_state=1, cmd_serial=10), now=0), 1)
        # A successful command (new serial, passing through RCS_EXEC) waits.
        self.assertIsNone(self.j.record(_pkt(2000, cmd_state=2, cmd_serial=11), now=1))
        self.assertIsNone(self.j.record(_pkt(3000, cmd_state=1, cmd_serial=11), now=2))
        # Two failed commands in a row, then a good one: three writes.
        self.assertEqual(self.j.record(_pkt(4000, cmd_state=3, cmd_serial=12), now=3), 2)
        self.assertEqual(self.j.record(_pkt(5000, cmd_state=3, cmd_serial=13), now=4), 3)
        self.assertEqual(self.j.record(_pkt(6000, cmd_state=1, cmd_serial=14), now=5), 4)
        # Closed before tearDown removes the folder: Windows will not delete an open
        # database (addCleanup would run only after tearDown).
        conn = aj._open(self.path)
        try:
            r = aj.build_reply(conn, {"type": "journal_request", "from_seq": 0},
                               "CNC-01", self.j.agent_id)
        finally:
            conn.close()
        self.assertEqual([(e[2]["cmd_state"], e[2]["cmd_serial"]) for e in r["entries"]],
                         [(1, 10), (3, 12), (3, 13), (1, 14)])

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

    def test_malformed_sequence_numbers_are_rejected_not_raised(self):
        # Review finding F06: JSON 1e309 parses as infinity; int(inf) raised
        # OverflowError, which nothing caught, and stopped the server thread.
        self._fill(5)
        conn = aj._open(self.path)
        bad = [float("inf"), float("-inf"), float("nan"), 1e309, 2.5, -1, True,
               2 ** 63, 10 ** 30, "1e309", "-3", "9" * 21, [], {}]
        for value in bad:
            for field in ("from_seq", "to_seq"):
                with self.subTest(field=field, value=value):
                    req = {"type": "journal_request", field: value}
                    self.assertIsNone(aj.build_reply(conn, req, "M", "a"))
        self.assertIsNone(aj.build_reply(conn, {"type": "journal_request",
                                                "from_seq": None}, "M", "a"))
        self.assertIsNone(aj.build_reply(conn, ["journal_request"], "M", "a"))
        conn.close()

    def test_accepted_sequence_number_forms(self):
        self._fill(5)
        conn = aj._open(self.path)
        for value in (2, 2.0, "2", " 2 "):
            with self.subTest(value=value):
                r = aj.build_reply(conn, {"type": "journal_request", "from_seq": value},
                                   "M", "a")
                self.assertEqual([e[0] for e in r["entries"]], [3, 4, 5])
        r = aj.build_reply(conn, {"type": "journal_request", "from_seq": 0,
                                  "to_seq": aj.MAX_SEQ}, "M", "a")
        self.assertEqual(len(r["entries"]), 5)
        conn.close()


def _free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _BrokenConn:
    """A connection whose writes fail (disk error, locked file...)."""

    def execute(self, *_a):
        import sqlite3
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        pass


class JournalRecoveryTests(unittest.TestCase):
    """A database problem pauses the history; it no longer ends it until restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "journal.db")
        self.free = 10 ** 12
        self.j = aj.Journal(self.path, free_bytes=lambda _p: self.free)
        self.addCleanup(self.j.close)

    def test_a_failed_write_is_retried_with_backoff_and_recovers(self):
        self.assertEqual(self.j.record(_pkt(1), now=0), 1)
        self.j._conn = _BrokenConn()
        with self.assertLogs("agent_journal", "WARNING") as logs:
            self.assertIsNone(self.j.record(_pkt(2, parts_produced=1), now=1))
            self.assertEqual(self.j.state, "failing")
            self.assertIn("disk I/O error", self.j.error)
            self.assertIsNone(self.j.record(_pkt(3, parts_produced=1), now=3))   # before 5 s
            self.assertEqual(self.j.record(_pkt(4, parts_produced=1), now=7), 2)  # reopened
        self.assertEqual(self.j.state, "ok")
        self.assertEqual(len(logs.output), 2)                        # failing, recovered
        self.assertIn("recovered", logs.output[1])

    def test_the_retry_interval_doubles_up_to_five_minutes(self):
        delays = []
        for n in range(10):
            self.j._conn = _BrokenConn()
            self.j._retry_at = 0
            with self.assertLogs("agent_journal", "WARNING"):
                self.j.record(_pkt(n, parts_produced=n), now=1000.0 * n)
            delays.append(self.j._retry_at - 1000.0 * n)
            self.j.state = "ok"
        self.assertEqual(delays[:3], [5.0, 10.0, 20.0])
        self.assertEqual(delays[-1], aj.RETRY_MAX_S)

    def test_a_journal_that_cannot_be_opened_at_start_is_opened_later(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        open(blocker, "w").close()                         # a file where the folder should be
        path = os.path.join(blocker, "journal.db")
        with self.assertLogs("agent_journal", "WARNING"):
            j = aj.Journal(path, free_bytes=lambda _p: self.free)
        self.addCleanup(j.close)
        self.assertEqual((j.state, j.agent_id, j.enabled), ("failing", "", False))
        os.remove(blocker)
        j._retry_at = 0
        with self.assertLogs("agent_journal", "WARNING"):
            self.assertEqual(j.record(_pkt(1), now=100), 1)
        self.assertEqual(j.state, "ok")
        self.assertTrue(j.agent_id)

    def test_writes_pause_when_the_disk_is_nearly_full_and_resume_with_spare(self):
        j = aj.Journal(self.path, min_free_mb=100, free_bytes=lambda _p: self.free)
        self.addCleanup(j.close)
        self.assertEqual(j.record(_pkt(1), now=0), 1)
        self.free = 50_000_000
        with self.assertLogs("agent_journal", "WARNING"):
            self.assertIsNone(j.record(_pkt(2, parts_produced=1), now=61))
        self.assertEqual(j.state, "disk_low")
        self.free = 105_000_000                            # above 100 MB but not 10 % spare
        self.assertIsNone(j.record(_pkt(3, parts_produced=1), now=122))
        self.free = 200_000_000
        with self.assertLogs("agent_journal", "WARNING"):
            self.assertEqual(j.record(_pkt(4, parts_produced=1), now=183), 2)
        self.assertEqual(j.state, "ok")

    def test_the_disk_is_checked_once_a_minute_not_every_sample(self):
        calls = []
        j = aj.Journal(self.path, free_bytes=lambda p: calls.append(p) or 10 ** 12)
        self.addCleanup(j.close)
        for t in range(120):
            j.record(_pkt(t), now=float(t))
        self.assertEqual(len(calls), 2)


class JournalServerTests(unittest.TestCase):
    def _serve(self, n=5):
        # Cleanups run last-in first-out: server and journal close before the
        # folder is removed (Windows will not delete an open database).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "j.db")
        j = aj.Journal(path)
        for i in range(1, n + 1):
            j.record(_pkt(i * 1000, parts_produced=i), now=i)
        port = _free_port()
        srv = aj.JournalServer(path, port, "CNC-01", j.agent_id, bind_host="127.0.0.1")
        self.assertTrue(srv.start())
        self.addCleanup(j.close)
        self.addCleanup(srv.stop)
        return srv, port

    @staticmethod
    def _ask(port, raw, timeout=3.0):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(timeout)
        try:
            c.sendto(raw, ("127.0.0.1", port))
            return json.loads(c.recvfrom(65535)[0])
        except socket.timeout:
            return None
        finally:
            c.close()

    def test_server_survives_malformed_requests(self):
        srv, port = self._serve()
        junk = [b'{"type":"journal_request","from_seq":1e309}',
                b'{"type":"journal_request","from_seq":-1e309}',
                b'{"type":"journal_request","to_seq":1e309}',
                b'{"type":"journal_request","from_seq":99999999999999999999999}',
                b'{"type":"journal_request","from_seq":NaN}',
                b"[" * 3000 + b"]" * 1000,          # deep nesting: RecursionError
                b"\xff\xfe not utf-8", b"", b"null", b"42", b'"x"']
        for raw in junk:
            self.assertIsNone(self._ask(port, raw, timeout=0.3), raw[:40])
        self.assertTrue(srv.alive)
        self.assertGreaterEqual(srv.bad_requests, len(junk) - 1)  # b"" may not arrive
        reply = self._ask(port, json.dumps({"type": "journal_request",
                                            "from_seq": 3}).encode())
        self.assertEqual([e[0] for e in reply["entries"]], [4, 5])

    def test_alive_and_restart_after_the_thread_stops(self):
        srv, port = self._serve()
        self.assertTrue(srv.alive)
        srv.stop()
        self.assertFalse(srv.alive)
        self.assertTrue(srv.start())
        self.assertTrue(srv.alive)
        reply = self._ask(port, json.dumps({"type": "journal_request",
                                            "from_seq": 4}).encode())
        self.assertEqual([e[0] for e in reply["entries"]], [5])

    def test_port_zero_means_off(self):
        srv = aj.JournalServer("unused.db", 0, "M", "a")
        self.assertFalse(srv.start())
        self.assertFalse(srv.alive)

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
