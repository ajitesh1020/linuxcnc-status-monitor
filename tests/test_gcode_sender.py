"""Tests for streaming the loaded G-code program (agent_gcode.py).
Run: python -m unittest -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_gcode  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Net:
    """A fake UDP send: records packets, can be told to fail."""

    def __init__(self):
        self.packets = []
        self.ok = True

    def __call__(self, payload):
        if not self.ok:
            return False
        self.packets.append(payload)
        return True

    def program(self):
        parts = {}
        total = 0
        for raw in self.packets:
            p = json.loads(raw)
            parts[p["chunk_index"]] = p["content"]
            total = p["total_chunks"]
        return "".join(parts[i] for i in range(total)) if len(parts) == total else None


class SenderCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.net = Net()
        self.sender = agent_gcode.GcodeFileSender(
            50_000, "VMC-01", 1, resend_s=60.0, clock=self.clock, pace_s=0, threaded=False)

    def write(self, text, name="part.ngc"):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        st = os.stat(path)
        return path, {"file_name": name, "file_size": st.st_size,
                      "file_modified_ms": int(st.st_mtime * 1000)}

    def push(self, path, meta):
        self.sender.check_and_send(path, meta, self.net)


class PacketSizeTests(SenderCase):
    def test_every_packet_fits_one_ethernet_frame(self):
        text = "G1 X1.000 Y2.000 (comment)\n" * 20000          # 540 KB, many newlines
        path, meta = self.write(text)
        self.push(path, meta)
        self.assertGreater(len(self.net.packets), 100)
        self.assertLessEqual(max(len(p) for p in self.net.packets), agent_gcode.MAX_PACKET_BYTES)
        self.assertEqual(self.net.program(), text)

    def test_a_config_still_asking_for_50_kb_chunks_cannot_make_a_huge_packet(self):
        path, meta = self.write("G0 X0\n" * 30000)
        self.push(path, meta)
        self.assertLessEqual(max(len(p) for p in self.net.packets), agent_gcode.MAX_PACKET_BYTES)

    def test_non_ascii_text_survives_and_still_fits(self):
        text = "(Ünïcödé — 部品 ✓)\n" * 4000
        path, meta = self.write(text)
        self.push(path, meta)
        self.assertLessEqual(max(len(p) for p in self.net.packets), agent_gcode.MAX_PACKET_BYTES)
        self.assertEqual(self.net.program(), text)

    def test_an_empty_program_is_still_announced(self):
        path, meta = self.write("")
        self.push(path, meta)
        self.assertEqual(len(self.net.packets), 1)
        self.assertEqual(self.net.program(), "")

    def test_chunks_carry_the_protocol_fields(self):
        path, meta = self.write("G0 X0\n")
        self.push(path, meta)
        p = json.loads(self.net.packets[0])
        self.assertEqual((p["type"], p["proto"], p["machine_name"], p["file_name"]),
                         ("gcode_file", 1, "VMC-01", "part.ngc"))
        self.assertEqual((p["chunk_index"], p["total_chunks"]), (0, 1))


class WhenToSendTests(SenderCase):
    def test_sent_once_then_quiet_until_the_resend_time(self):
        path, meta = self.write("G0 X0\n")
        self.push(path, meta)
        n = len(self.net.packets)
        self.clock.t += 30
        self.push(path, meta)
        self.assertEqual(len(self.net.packets), n)

    def test_sent_again_every_resend_interval_for_a_late_dashboard(self):
        path, meta = self.write("G0 X0\n")
        self.push(path, meta)
        n = len(self.net.packets)
        self.clock.t += 61
        self.push(path, meta)
        self.assertEqual(len(self.net.packets), 2 * n)

    def test_resend_can_be_turned_off(self):
        self.sender = agent_gcode.GcodeFileSender(
            1000, "M", 1, resend_s=0, clock=self.clock, pace_s=0, threaded=False)
        path, meta = self.write("G0 X0\n")
        self.push(path, meta)
        n = len(self.net.packets)
        self.clock.t += 10_000
        self.push(path, meta)
        self.assertEqual(len(self.net.packets), n)

    def test_a_changed_file_goes_out_at_once(self):
        path, meta = self.write("G0 X0\n")
        self.push(path, meta)
        path2, meta2 = self.write("G0 X1\nG0 X2\n", name="other.ngc")
        self.net.packets.clear()
        self.push(path2, meta2)
        self.assertEqual(self.net.program(), "G0 X1\nG0 X2\n")

    def test_reset_sends_the_file_again_after_a_linuxcnc_restart(self):
        path, meta = self.write("G0 X0\n")
        self.push(path, meta)
        self.net.packets.clear()
        self.sender.reset()
        self.push(path, meta)
        self.assertEqual(self.net.program(), "G0 X0\n")

    def test_no_file_loaded_sends_nothing(self):
        self.sender.check_and_send("", {"file_name": "", "file_size": 0,
                                        "file_modified_ms": 0}, self.net)
        self.assertEqual(self.net.packets, [])


class FailureTests(SenderCase):
    def test_a_failed_send_is_not_remembered_as_sent(self):
        path, meta = self.write("G0 X0\n")
        self.net.ok = False
        self.push(path, meta)
        self.net.ok = True
        self.clock.t += agent_gcode.RETRY_AFTER_FAILURE_S + 1
        self.push(path, meta)
        self.assertEqual(self.net.program(), "G0 X0\n")

    def test_retry_waits_rather_than_hammering_a_dead_network(self):
        path, meta = self.write("G0 X0\n")
        self.net.ok = False
        self.push(path, meta)
        self.net.ok = True
        self.clock.t += 2                                        # too soon
        self.push(path, meta)
        self.assertEqual(self.net.packets, [])

    def test_an_unreadable_file_does_not_raise_and_is_retried_later(self):
        meta = {"file_name": "gone.ngc", "file_size": 5, "file_modified_ms": 1}
        self.push(os.path.join(self.dir.name, "gone.ngc"), meta)
        self.assertEqual(self.net.packets, [])
        path, meta = self.write("G0 X0\n", name="gone.ngc")
        self.clock.t += agent_gcode.RETRY_AFTER_FAILURE_S + 1
        self.push(path, meta)
        self.assertEqual(self.net.program(), "G0 X0\n")

    def test_a_send_that_raises_is_treated_as_failed(self):
        def boom(_payload):
            raise RuntimeError("socket gone")
        path, meta = self.write("G0 X0\n")
        self.sender.check_and_send(path, meta, boom)
        self.clock.t += agent_gcode.RETRY_AFTER_FAILURE_S + 1
        self.push(path, meta)
        self.assertEqual(self.net.program(), "G0 X0\n")


class ThreadedTests(unittest.TestCase):
    def test_a_big_program_is_sent_off_the_calling_thread_and_completes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "big.ngc")
            text = "G1 X1 Y1\n" * 30000
            with open(path, "w", newline="") as f:
                f.write(text)
            st = os.stat(path)
            meta = {"file_name": "big.ngc", "file_size": st.st_size,
                    "file_modified_ms": int(st.st_mtime * 1000)}
            net = Net()
            sender = agent_gcode.GcodeFileSender(1000, "M", 1, pace_s=0)
            sender.check_and_send(path, meta, net)
            sender._worker.join(timeout=20)
            self.assertFalse(sender._worker.is_alive())
            self.assertEqual(net.program(), text)


if __name__ == "__main__":
    unittest.main()
