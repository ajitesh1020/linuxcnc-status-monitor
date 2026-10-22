"""Site-key signing (agent_auth.py, PROTOCOL.md §8.1, review F07).
Run: python -m unittest -v test_auth
"""
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_auth as auth  # noqa: E402
import agent_gcode  # noqa: E402
import agent_journal as aj  # noqa: E402
import agent_net  # noqa: E402

KEY = auth.key_bytes("correct horse battery staple")


class SignVerifyTests(unittest.TestCase):
    def test_signed_packet_is_still_json_and_verifies(self):
        raw = json.dumps({"type": "status", "packet_seq": 7}).encode()
        signed = auth.sign(raw, KEY)
        p = json.loads(signed)                      # an old dashboard still parses it
        self.assertEqual((p["type"], p["packet_seq"], len(p["sig"])), ("status", 7, 64))
        self.assertEqual(auth.verify(signed, KEY), "ok")

    def test_any_change_or_wrong_key_is_bad(self):
        signed = auth.sign(b'{"type":"status","parts_produced":3}', KEY)
        self.assertEqual(auth.verify(signed.replace(b"3", b"4", 1), KEY), "bad")
        self.assertEqual(auth.verify(signed, auth.key_bytes("another key 1234567")), "bad")

    def test_unsigned_is_reported_as_such(self):
        self.assertEqual(auth.verify(b'{"type":"status"}', KEY), "unsigned")

    def test_compact_and_empty_objects(self):
        for raw in (b"{}", b'{"a":1}', b'{"a": "\\u00fc", "b": [1, 2]}', '{"ü":"部品"}'.encode()):
            self.assertEqual(auth.verify(auth.sign(raw, KEY), KEY), "ok", raw)
            json.loads(auth.sign(raw, KEY))

    def test_no_key_leaves_the_packet_alone(self):
        self.assertEqual(auth.sign(b'{"a":1}', None), b'{"a":1}')
        self.assertIsNone(auth.key_bytes("   "))

    def test_signature_cannot_be_moved_onto_another_packet(self):
        sig = json.loads(auth.sign(b'{"a":1}', KEY))["sig"]
        forged = ('{"a":2,"sig":"%s"}' % sig).encode()
        self.assertEqual(auth.verify(forged, KEY), "bad")


class GcodeRoomTests(unittest.TestCase):
    def test_a_signed_chunk_still_fits_one_frame(self):
        sender = agent_gcode.GcodeFileSender(1000, "M" * 128, 1, threaded=False)
        meta = {"file_name": "f" * 255 + ".ngc", "file_size": 1, "file_modified_ms": 1}
        for text in ("G1 X1 Y2\n" * 3000, "(Ünïcödé — 部品 \"q\" \\)\n" * 500):
            for p in sender.packets(meta, text):
                self.assertLessEqual(len(auth.sign(p, KEY)), 1472)   # 1500 - IP - UDP

    def test_udp_sender_signs_what_it_sends(self):
        with mock.patch.object(agent_net.UdpSender, "refresh"), \
                mock.patch.object(agent_net.threading, "Thread"):
            sender = agent_net.UdpSender("127.0.0.1", 9, key=KEY)
        self.addCleanup(sender.close)
        sender._targets = ["127.0.0.1"]
        with mock.patch.object(sender, "_sock") as sock:
            sender.send(b'{"type":"status"}')
        self.assertEqual(auth.verify(sock.sendto.call_args[0][0], KEY), "ok")


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HistoryServerAuthTests(unittest.TestCase):
    def serve(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "j.db")
        j = aj.Journal(path)
        self.addCleanup(j.close)
        for i in range(1, 4):
            j.record({"type": "status", "ts": i, "parts_produced": i}, now=i)
        port = _free_port()
        srv = aj.JournalServer(path, port, "CNC-01", j.agent_id, bind_host="127.0.0.1", **kw)
        self.assertTrue(srv.start())
        self.addCleanup(srv.stop)
        return srv, port

    @staticmethod
    def ask(port, raw, timeout=0.5):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(timeout)
        try:
            c.sendto(raw, ("127.0.0.1", port))
            return c.recvfrom(65535)[0]
        except socket.timeout:
            return None
        finally:
            c.close()

    @staticmethod
    def request(nonce="nonce-0001", key=KEY):
        return auth.sign(json.dumps({"type": "journal_request", "proto": 1, "from_seq": 0,
                                     "nonce": nonce}).encode(), key)

    def test_a_signed_request_gets_a_signed_reply(self):
        srv, port = self.serve(key=KEY)
        reply = self.ask(port, self.request())
        self.assertEqual(auth.verify(reply, KEY), "ok")
        self.assertEqual([e[0] for e in json.loads(reply)["entries"]], [1, 2, 3])

    def test_unsigned_wrongly_signed_and_replayed_requests_get_nothing(self):
        srv, port = self.serve(key=KEY)
        self.assertIsNone(self.ask(port, b'{"type":"journal_request","from_seq":0,'
                                         b'"nonce":"nonce-0002"}'))
        self.assertIsNone(self.ask(port, self.request(key=auth.key_bytes("not the key 12345"))))
        self.assertIsNotNone(self.ask(port, self.request("nonce-0003")))
        self.assertIsNone(self.ask(port, self.request("nonce-0003")))          # replay
        self.assertIsNone(self.ask(port, self.request("short")))               # no real nonce
        self.assertEqual(srv.refused, 4)
        self.assertTrue(srv.alive)

    def test_allow_list(self):
        srv, port = self.serve(allow=("10.9.9.9",))
        self.assertIsNone(self.ask(port, b'{"type":"journal_request","from_seq":0}'))
        srv2, port2 = self.serve(allow=("127.0.0.1",))
        self.assertIsNotNone(self.ask(port2, b'{"type":"journal_request","from_seq":0}'))

    def test_without_a_key_nothing_changes(self):
        srv, port = self.serve()
        reply = self.ask(port, b'{"type":"journal_request","from_seq":0}')
        self.assertEqual(auth.verify(reply, KEY), "unsigned")


if __name__ == "__main__":
    unittest.main()
