"""Keyence SR-X reader client (agent_srx.py) against the fake reader
(tools/fake_srx.py); docs/SCANNER_KEYENCE_SRX_WORK.md, package 1.
Run: python -m unittest -v test_srx
"""
import ftplib
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import agent_srx as srx  # noqa: E402
from fake_srx import FakeSrx  # noqa: E402


class ClassifyLineTests(unittest.TestCase):
    def test_ok_reply_for_the_pending_command(self):
        self.assertEqual(srx.classify_line(b"OK,SAVE", "SAVE"), ("reply", srx.SrxReply(True, "SAVE")))
        self.assertEqual(srx.classify_line(b"OK,KEYENCE,SR-X100,1.00", "KEYENCE"),
                         ("reply", srx.SrxReply(True, "KEYENCE", ["SR-X100", "1.00"])))
        self.assertEqual(srx.classify_line(b"OK,RP,1", "RP"), ("reply", srx.SrxReply(True, "RP", ["1"])))

    def test_spaces_after_commas_as_the_manuals_print_them(self):
        self.assertEqual(srx.classify_line(b"OK, KEYENCE, SR-750, 2.1", "KEYENCE"),
                         ("reply", srx.SrxReply(True, "KEYENCE", ["SR-750", "2.1"])))

    def test_error_reply(self):
        kind, r = srx.classify_line(b"ER,TUNE,02", "TUNE")
        self.assertEqual(kind, "reply")
        self.assertFalse(r.ok)
        self.assertEqual((r.cmd, r.error_code, r.fields), ("TUNE", "02", []))
        self.assertEqual(r.error_text, "parameter out of range")
        self.assertIsNone(srx.classify_line(b"ER,X", "X")[1].error_code)
        self.assertEqual(srx.SrxReply(False, "X", error_code="77").error_text, "error 77")

    def test_bare_ok_er_for_the_commands_that_answer_so(self):
        for name in ("TUNE", "TQUIT", "RESET", "BCLR", "QUIT"):
            self.assertEqual(srx.classify_line(b"OK", name), ("reply", srx.SrxReply(True, name)))
            self.assertEqual(srx.classify_line(b"ER", name), ("reply", srx.SrxReply(False, name)))
        self.assertEqual(srx.classify_line(b"OK,QUIT", "QUIT"), ("reply", srx.SrxReply(True, "QUIT")))
        self.assertEqual(srx.classify_line(b"OK", "SAVE"), ("data", b"OK"))
        self.assertEqual(srx.classify_line(b"OK", None), ("data", b"OK"))

    def test_basic_command_responses_are_stray_not_data(self):
        for pending in (None, "KEYENCE"):
            self.assertEqual(srx.classify_line(b"OK,LON", pending), ("stray", srx.SrxReply(True, "LON")))
            self.assertEqual(srx.classify_line(b"ER,LOFF,20", pending),
                             ("stray", srx.SrxReply(False, "LOFF", error_code="20")))

    def test_everything_else_is_read_data(self):
        cases = [
            (b"ABC123", "KEYENCE"), (b"ERROR", None), (b"OK,SAVE", "KEYENCE"),   # reply to another command
            (b"OK,SAVE", None),                                                  # nothing pending
            (b"OK,LONG-CODE", None), (b"ok,SAVE", "SAVE"), (b"OK,", "SAVE"),
            (b"Tuning SUCCEEDED,850ms,00000000", "TUNE"), (b"", "SAVE"),
            (b"OK,\xff", "SAVE"), (b"ER,SAVEX,00", "SAVE"), (b"]d1ABC", None),
        ]
        for line, pending in cases:
            self.assertEqual(srx.classify_line(line, pending), ("data", line), (line, pending))

    def test_command_name(self):
        self.assertEqual(srx.command_name("TUNE,01"), "TUNE")
        self.assertEqual(srx.command_name("LON"), "LON")

    def test_parse_endpoint(self):
        self.assertEqual(srx.parse_endpoint("tcp://192.168.100.100:9004"), ("192.168.100.100", 9004))
        self.assertEqual(srx.parse_endpoint("tcp://10.0.0.5"), ("10.0.0.5", 9004))
        self.assertEqual(srx.parse_endpoint("10.0.0.5:9100/"), ("10.0.0.5", 9100))
        self.assertEqual(srx.parse_endpoint("tcp://[fe80::1]:9004"), ("fe80::1", 9004))
        for bad in ("tcp://", "tcp://h:x", "tcp://h:0", "tcp://h:70000", ":9004"):
            with self.assertRaises(ValueError, msg=bad):
                srx.parse_endpoint(bad)


class FakeBase(unittest.TestCase):
    codes = ()

    def setUp(self):
        self.fake = FakeSrx(codes=self.codes).start()
        self.addCleanup(self.fake.stop)
        self.c = srx.SrxClient("127.0.0.1", self.fake.port, timeout=1.0)
        self.addCleanup(self.c.close)
        self.c.connect()
        self.assertTrue(self.fake.wait_clients(1))


class CommandTests(FakeBase):
    codes = ("ABC123", "DM\x1d01")

    def test_info(self):
        self.assertEqual(self.c.info(), {"model": "SR-X100", "firmware": "1.00", "serial": None,
                                         "mac": "0001FC000001"})
        self.assertEqual(self.fake.received, ["KEYENCE", "EMAC"])

    def test_info_with_a_serial_command(self):
        self.fake.serial_command = "SERIAL"
        with mock.patch.object(srx, "SERIAL_COMMAND", "SERIAL"):
            self.assertEqual(self.c.info()["serial"], "FAKE0001")

    def test_info_never_invents(self):
        with mock.patch.object(srx, "INFO_COMMAND", "NOPE"), mock.patch.object(srx, "MAC_COMMAND", "NADA"):
            self.assertEqual(self.c.info(), {"model": None, "firmware": None, "serial": None, "mac": None})

    def test_lon_no_reply_then_read_data_in_order(self):
        t = time.monotonic()
        self.assertEqual(self.c.command("LON"), srx.SrxReply(True, "LON"))
        self.assertLess(time.monotonic() - t, 0.5)
        self.assertEqual(self.c.read_line(1), b"ABC123")
        self.assertTrue(self.c.command("LON").ok)
        self.assertEqual(self.c.read_line(1), b"DM\x1d01")
        self.assertTrue(self.c.command("LON").ok)
        self.assertEqual(self.c.read_line(1), b"ERROR")           # script empty: no-read
        self.assertTrue(self.c.command("LOFF").ok)
        self.assertIsNone(self.c.read_line(0.1))
        self.assertEqual(self.fake.received, ["LON", "LON", "LON", "LOFF"])

    def test_basic_command_response_enabled_is_not_a_scan(self):
        self.fake.basic_response = True
        self.c.command("LON")
        self.assertEqual(self.c.read_line(1), b"ABC123")
        self.c.command("LOFF")
        self.assertIsNone(self.c.read_line(0.2))

    def test_data_arriving_while_waiting_for_a_reply_is_kept(self):
        self.fake.reply_delay_s = 0.1
        threading.Timer(0.02, lambda: self.fake.push(b"PUSHED1")).start()
        self.assertTrue(self.c.command("BCLR").ok)
        self.assertEqual(self.c.read_line(1), b"PUSHED1")

    def test_data_in_the_same_packet_as_the_reply_is_kept(self):
        self.fake.silent = True
        raw = self.fake._clients[0]
        threading.Timer(0.05, lambda: raw.sendall(
            b"EARLY\rOK,LON\rOK,SAVE\rCODE1\rOK,SAVE\r")).start()
        self.assertEqual(self.c.command("SAVE"), srx.SrxReply(True, "SAVE"))
        self.assertEqual(self.c.read_line(1), b"EARLY")       # OK,LON dropped (stray)
        self.assertEqual(self.c.read_line(1), b"CODE1")
        self.assertEqual(self.c.read_line(1), b"OK,SAVE")     # no command pending: data
        self.assertIsNone(self.c.read_line(0.1))

    def test_pushed_reads(self):
        self.fake.push(b"R1")
        self.fake.push(b"R2")
        self.assertEqual(self.c.read_line(1), b"R1")
        self.assertEqual(self.c.read_line(1), b"R2")

    def test_error_reply_and_unknown_command(self):
        r = self.c.command("TUNE,99")
        self.assertEqual((r.ok, r.cmd, r.error_code), (False, "TUNE", "02"))
        r = self.c.command("BOGUS")
        self.assertEqual((r.ok, r.error_code), (False, "00"))

    def test_settings_read_write_save_reset(self):
        self.assertEqual(self.c.command("RP,101").fields, ["0"])
        self.assertTrue(self.c.command("WP,101,1").ok)
        self.assertEqual(self.c.command("RP,101").fields, ["1"])
        self.assertFalse(self.c.command("RP,999").ok)
        self.assertTrue(self.c.command("RESET").ok)              # unsaved change lost
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(1)
        self.assertFalse(self.c.connected)
        self.c.connect()
        self.assertEqual(self.c.command("RP,101").fields, ["0"])
        self.c.command("WP,101,3")
        self.c.command("SAVE")
        self.c.command("RESET")
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(1)
        self.c.connect()
        self.assertEqual(self.c.command("RP,101").fields, ["3"])

    def test_tuning_result_and_tquit(self):
        self.assertTrue(self.c.command("TUNE,01").ok)
        self.assertEqual(self.c.read_line(1), b"Tuning SUCCEEDED,850ms,00000000")
        self.fake.tune_s = 0.3
        self.assertTrue(self.c.command("TUNE,02").ok)
        self.assertTrue(self.c.command("TQUIT").ok)
        self.assertIsNone(self.c.read_line(0.5))

    def test_reading_rate_test_streams_until_quit(self):
        self.assertTrue(self.c.command("TEST1").ok)
        lines = [self.c.read_line(1) for _ in range(3)]
        self.assertEqual(lines, [b"ABC123:01:100%:92"] * 3)
        self.assertTrue(self.c.command("QUIT").ok)
        rest = []
        while True:
            line = self.c.read_line(0.2)
            if line is None:
                break
            rest.append(line)
        self.assertLessEqual(len(rest), 2)                        # the stream stopped
        self.fake.quit_reply = "OK"                               # the manual's other wording
        self.c.command("TEST1")
        self.assertTrue(self.c.command("QUIT").ok)

    def test_focus_tuning(self):
        self.assertEqual(self.c.command("FTUNE"), srx.SrxReply(True, "FTUNE"))
        self.assertEqual(self.c.read_line(1), b"Focus Tuning SUCCEEDED")

    def test_time_and_status(self):
        self.assertTrue(self.c.command("TMSET,20261201101500").ok)
        self.assertEqual(self.c.command("TMGET").fields, ["20261201101500"])
        self.assertEqual(self.c.command("ERRSTAT").fields, ["none"])

    def test_load_and_factory_defaults(self):
        self.c.command("WP,101,1")
        self.c.command("LOAD")
        self.assertEqual(self.c.command("RP,101").fields, ["0"])
        self.c.command("WP,101,1")
        self.c.command("SAVE")
        self.c.command("DFLT")
        self.assertEqual(self.c.command("RP,101").fields, ["0"])

    def test_bad_commands_refused_before_sending(self):
        for bad in ("", "LON\r", "A\nB", "LÖN"):
            with self.assertRaises(ValueError):
                self.c.command(bad)
        self.assertEqual(self.fake.received, [])

    def test_no_reply_commands(self):
        self.assertEqual(srx.NO_REPLY_COMMANDS, {"LON", "LOFF", "PRON", "PROFF"})
        t = time.monotonic()
        self.assertEqual(self.c.command("PROFF", timeout=2), srx.SrxReply(True, "PROFF"))
        self.assertLess(time.monotonic() - t, 0.5)


class FaultTests(FakeBase):
    def test_slow_reply_within_timeout(self):
        self.fake.reply_delay_s = 0.3
        self.assertTrue(self.c.command("KEYENCE", timeout=2).ok)

    def test_no_reply_times_out_and_the_connection_stays(self):
        self.fake.silent = True
        t = time.monotonic()
        with self.assertRaises(srx.SrxTimeout):
            self.c.command("KEYENCE", timeout=0.3)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertTrue(self.c.connected)
        self.fake.silent = False
        self.assertTrue(self.c.command("KEYENCE").ok)

    def test_late_reply_after_a_timeout_is_not_read_data(self):
        self.fake.reply_delay_s = 0.4
        with self.assertRaises(srx.SrxTimeout):
            self.c.command("KEYENCE", timeout=0.1)
        with self.assertRaises(srx.SrxTimeout):
            self.c.command("TUNE,01", timeout=0.1)
        self.fake.reply_delay_s = 0
        self.fake.push(b"REAL")
        lines = []
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            line = self.c.read_line(0.1)
            if line is not None:
                lines.append(line)
        self.assertEqual(set(lines), {b"REAL", b"Tuning SUCCEEDED,850ms,00000000"})
        self.assertNotIn(b"OK", lines)
        self.assertFalse(any(x.startswith(b"OK,KEYENCE") for x in lines))

    def test_read_line_timeout(self):
        t = time.monotonic()
        self.assertIsNone(self.c.read_line(0.2))
        self.assertLess(time.monotonic() - t, 0.6)

    def test_half_lines_are_joined(self):
        self.fake.split_lines = True
        self.assertEqual(self.c.command("KEYENCE").fields, ["SR-X100", "1.00"])
        self.fake.push(b"SPLITCODE")
        self.assertEqual(self.c.read_line(1), b"SPLITCODE")

    def test_disconnect_mid_line(self):
        self.fake.disconnect_mid_line = True
        self.fake.push(b"HALFCODE123")
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(1)
        self.assertFalse(self.c.connected)
        self.assertEqual(self.c.partial, 1)
        with self.assertRaises(srx.SrxConnectionError):
            self.c.command("KEYENCE")
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(0.1)

    def test_queued_lines_survive_the_disconnect(self):
        self.fake.push(b"Q1")
        time.sleep(0.1)
        self.fake.drop_all()
        self.assertEqual(self.c.read_line(1), b"Q1")
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(1)

    def test_reader_reboot_then_reconnect(self):
        self.fake.drop_all()
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(1)
        self.c.connect()
        self.assertTrue(self.c.command("KEYENCE").ok)

    def test_second_client_refused(self):
        self.fake.second_client = "refuse"
        c2 = srx.SrxClient("127.0.0.1", self.fake.port, timeout=0.5)
        c2.connect()                                    # TCP accepts, then the reader closes
        with self.assertRaises(srx.SrxConnectionError):
            c2.read_line(1)
        c2.close()
        self.assertTrue(self.c.command("KEYENCE").ok)   # the first is untouched

    def test_second_client_kicks_the_first(self):
        self.fake.second_client = "kick"
        c2 = srx.SrxClient("127.0.0.1", self.fake.port, timeout=0.5)
        self.addCleanup(c2.close)
        c2.connect()
        with self.assertRaises(srx.SrxConnectionError):
            self.c.read_line(1)
        self.assertTrue(c2.command("KEYENCE").ok)

    def test_second_client_allowed(self):
        c2 = srx.SrxClient("127.0.0.1", self.fake.port, timeout=0.5)
        self.addCleanup(c2.close)
        c2.connect()
        self.assertTrue(self.fake.wait_clients(2))
        self.assertTrue(c2.command("KEYENCE").ok)
        self.assertTrue(self.c.command("KEYENCE").ok)

    def test_connect_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(srx.SrxConnectionError):
            srx.SrxClient("127.0.0.1", port, timeout=0.5).connect()

    def test_not_connected(self):
        c = srx.SrxClient("127.0.0.1", self.fake.port)
        with self.assertRaises(srx.SrxConnectionError):
            c.command("LON")
        with self.assertRaises(srx.SrxConnectionError):
            c.read_line(0.1)

    def test_keepalive_on(self):
        self.assertEqual(self.c._sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)


class BoundsTests(FakeBase):
    def test_overlong_line_dropped_and_the_next_kept(self):
        self.fake.push(b"X" * 5000)
        self.fake.push(b"NEXT")
        self.assertEqual(self.c.read_line(1), b"NEXT")
        self.assertEqual(self.c.overlong, 1)

    def test_line_at_the_limit_kept(self):
        self.fake.push(b"Y" * srx.MAX_LINE)
        self.assertEqual(self.c.read_line(1), b"Y" * srx.MAX_LINE)
        self.assertEqual(self.c.overlong, 0)

    def test_huge_unterminated_stream_stays_bounded(self):
        raw = self.fake._clients[0]
        raw.sendall(b"Z" * 100000)
        self.assertIsNone(self.c.read_line(0.3))
        self.assertLessEqual(len(self.c._buf), srx.MAX_LINE + 1)
        self.assertEqual(self.c.overlong, 1)
        raw.sendall(b"ZZZ\rAFTER\r")
        self.assertEqual(self.c.read_line(1), b"AFTER")

    def test_queue_bounded(self):
        for i in range(srx.MAX_QUEUED + 10):
            self.c._queue(b"%d" % i)
        self.assertEqual(len(self.c._data), srx.MAX_QUEUED)
        self.assertEqual(self.c.dropped, 10)
        self.assertEqual(self.c.read_line(0), b"10")


class TerminatorTests(unittest.TestCase):
    def test_crlf_and_lf(self):
        for term in ("crlf", "lf"):
            with FakeSrx(codes=["C1"], terminator=term) as fake:
                fake.split_lines = True            # CRLF split across reads
                with srx.SrxClient("127.0.0.1", fake.port, timeout=1, terminator=term) as c:
                    self.assertTrue(c.command("LON").ok)
                    self.assertEqual(c.read_line(1), b"C1")

    def test_bad_terminator(self):
        with self.assertRaises(ValueError):
            srx.SrxClient("h", terminator="etx")


class FtpTests(unittest.TestCase):
    """ftplib against a scripted FTP object (no FTP server in the standard library)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ftp = mock.MagicMock()
        self.ftp.nlst.return_value = ["IMG_001.jpg", "IMG_002.jpg"]
        self.ftp.retrbinary.side_effect = lambda cmd, cb: (cb(b"\xff\xd8"), cb(b"\xff\xd9"))
        p = mock.patch.object(srx.ftplib, "FTP", return_value=self.ftp)
        self.FTP = p.start()
        self.addCleanup(p.stop)
        self.c = srx.SrxClient("10.0.0.5", timeout=2)

    def test_list_images(self):
        self.assertEqual(self.c.list_images(), ["IMG_001.jpg", "IMG_002.jpg"])
        self.FTP.assert_called_with(timeout=2)
        self.ftp.connect.assert_called_with("10.0.0.5", 21)
        self.ftp.cwd.assert_called_with("IMAGE")
        self.ftp.quit.assert_called_once()

    def test_empty_folder(self):
        self.ftp.nlst.side_effect = ftplib.error_perm("550 No files found")
        self.assertEqual(self.c.list_images(), [])

    def test_get_image_atomic(self):
        path = self.c.get_image("IMG_001.jpg", self.tmp.name)
        self.assertEqual(path, os.path.join(self.tmp.name, "IMG_001.jpg"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8\xff\xd9")
        self.ftp.retrbinary.assert_called_once()
        self.assertEqual(os.listdir(self.tmp.name), ["IMG_001.jpg"])

    def test_get_image_too_big_leaves_nothing(self):
        with self.assertRaises(srx.SrxError):
            self.c.get_image("IMG_001.jpg", self.tmp.name, max_bytes=3)
        self.assertEqual(os.listdir(self.tmp.name), [])
        self.ftp.quit.assert_called_once()

    def test_bad_names(self):
        for bad in ("", "../x", "a/b", "..", "a\\b"):
            with self.assertRaises(ValueError):
                self.c.get_image(bad, self.tmp.name)

    def test_login_failure_closes(self):
        self.ftp.login.side_effect = ftplib.error_perm("530 Login incorrect")
        with self.assertRaises(ftplib.error_perm):
            self.c.list_images()
        self.ftp.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
