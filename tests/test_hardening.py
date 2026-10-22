"""Agent hardening: background program scan, rate-limited warning log,
non-blocking UDP socket.
Run: python -m unittest -v test_hardening
"""
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_net  # noqa: E402
import program_tracker  # noqa: E402
from program_tracker import ProgramScanner  # noqa: E402

PROGRAM = "%\nG21 G90\nG0 X0 Y0\nG1 X10 F100\nG1 Y10\nM30\n%\n"


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name="p.ngc"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", newline="") as f:
            f.write(text)
        return path

    def test_a_small_file_is_scanned_at_once(self):
        s = ProgramScanner(background_bytes=1000)
        s.load(self.write(PROGRAM))
        self.assertFalse(s.pending)
        self.assertEqual((s.info.end_line, s.info.tail_line), (6, 5))

    def test_a_big_file_is_scanned_off_the_calling_thread(self):
        gate = threading.Event()
        real = program_tracker.ProgramInfo.from_lines.__func__

        def slow(cls, lines, path=""):
            gate.wait(5)
            return real(cls, lines, path)

        s = ProgramScanner(background_bytes=10)
        with mock.patch.object(program_tracker.ProgramInfo, "from_lines", classmethod(slow)):
            s.load(self.write(PROGRAM))
            self.assertTrue(s.pending)                     # load() returned during the scan
            self.assertEqual(s.info.end_line, -1)          # blank until it is done
            gate.set()
            s.wait()
        self.assertFalse(s.pending)
        self.assertEqual(s.info.end_line, 6)

    def test_a_scan_overtaken_by_a_newer_load_is_discarded(self):
        gate = threading.Event()
        real = program_tracker.ProgramInfo.from_lines.__func__
        calls = []

        def slow_first(cls, lines, path=""):
            calls.append(path)
            if len(calls) == 1:
                gate.wait(5)
            return real(cls, lines, path)

        s = ProgramScanner(background_bytes=10)
        first, second = self.write(PROGRAM, "a.ngc"), self.write("G0 X1\nM2\n", "b.ngc")
        with mock.patch.object(program_tracker.ProgramInfo, "from_lines",
                               classmethod(slow_first)):
            s.load(first)
            s.load(second)
            s.wait()
            gate.set()
            threading.Event().wait(0.1)                   # let the old thread finish
        self.assertEqual((s.info.path, s.info.end_line), (second, 2))


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        import importlib
        sys.modules.setdefault("linuxcnc", mock.MagicMock())
        self.status = importlib.import_module("status")

    def record(self, msg="Journal failing: %s"):
        return logging.LogRecord("x", logging.WARNING, __file__, 1, msg, ("e",), None)

    def test_the_same_message_passes_once_a_minute_then_counts_what_was_held(self):
        t = [0.0]
        f = self.status._RateLimit(60.0, clock=lambda: t[0])
        self.assertTrue(f.filter(self.record()))
        for _ in range(100):
            t[0] += 0.1
            self.assertFalse(f.filter(self.record()))
        t[0] = 61.0
        r = self.record()
        self.assertTrue(f.filter(r))
        self.assertIn("[100 similar held back]", r.getMessage())

    def test_different_messages_are_independent(self):
        f = self.status._RateLimit(60.0, clock=lambda: 0.0)
        self.assertTrue(f.filter(self.record("a %s")))
        self.assertTrue(f.filter(self.record("b %s")))


class NonBlockingSendTests(unittest.TestCase):
    def test_the_socket_never_blocks_and_a_full_buffer_keeps_it(self):
        with mock.patch.object(agent_net.UdpSender, "refresh"), \
                mock.patch.object(agent_net.threading, "Thread"):
            sender = agent_net.UdpSender("127.0.0.1", 9)
        self.addCleanup(sender.close)
        sender._targets = ["127.0.0.1"]
        self.assertFalse(sender._sock.getblocking())
        sock = sender._sock
        with mock.patch.object(sender, "_sock") as fake:
            fake.sendto.side_effect = BlockingIOError
            self.assertFalse(sender.send(b"x"))
            self.assertIs(sender._sock, fake)              # not torn down and re-made
        self.assertEqual(sender.would_block, 1)
        sender._sock = sock


if __name__ == "__main__":
    unittest.main()
