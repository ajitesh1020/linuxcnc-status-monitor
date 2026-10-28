"""Finding the site service at a new address (agent_mes_discovery.py, agent package K;
FloorWatch docs/MES_SERVICE_DISCOVERY.md).
Run: python -m unittest -v test_mes_discovery

ServiceMovesTests run FloorWatch's real floorwatch_service (with its discovery)
on 127.0.0.1, move it to another port, and check the agent follows. Skipped
when it is not there: FLOORWATCH_DIR must hold a FloorWatch with
src/floorwatch_service/discovery.py (default ~/floorwatch).
"""
import json
import logging
import os
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import agent_mes_discovery as md  # noqa: E402
import agent_mes_forward as mf  # noqa: E402
import agent_mes_spool as ms  # noqa: E402
import test_mes_forward as tf  # noqa: E402
from test_mes_spool import MACHINE  # noqa: E402

SITE = tf.SITE
CA = "add2f98bb0fdbd9d3495c29430d4d68ad2e6dcb4d3c952b1c487bd41e46872d7"


def beacon(site=SITE, ca=CA, port=8444, **extra):
    msg = {"type": md.BEACON, "v": 1, "site_id": site, "port": port, "ca_sha256": ca,
           "service_version": "0.1.0", "ts_ms": 1793000000000}
    msg.update(extra)
    return json.dumps(msg).encode()


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class AcceptTests(unittest.TestCase):
    def test_beacon_for_this_site_and_ca(self):
        self.assertEqual(md.accept(beacon(), SITE, CA), 8444)
        self.assertEqual(md.accept(beacon(ca=CA.upper()), SITE, CA), 8444)
        self.assertEqual(md.accept(beacon(extra_key="ignored"), SITE, CA), 8444)

    def test_everything_else_is_ignored(self):
        other = str(uuid.uuid4())
        for data in (beacon(site=other), beacon(ca="00" * 32), beacon(port=0), beacon(port=70000),
                     beacon(port="8444"), beacon(port=True), beacon(v=2), beacon(type=md.PROBE),
                     beacon(ca=None), b"not json", b"\xff\xfe", b"[]",
                     beacon(pad="x" * md.MAX_MESSAGE)):
            self.assertIsNone(md.accept(data, SITE, CA), data[:80])

    def test_probe_names_the_site(self):
        self.assertEqual(json.loads(md.probe_message(SITE)), {"type": md.PROBE, "v": 1, "site_id": SITE})


class FakeService(threading.Thread):
    """Answers probes on 127.0.0.1 like the service: the beacon goes straight back."""

    def __init__(self, answer):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port, self.answer, self.probes, self.halt = self.sock.getsockname()[1], answer, [], False
        self.start()

    def run(self):
        while not self.halt:
            try:
                data, sender = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            self.probes.append(json.loads(data))
            self.sock.sendto(self.answer, sender)

    def close(self):
        self.halt = True
        self.join(2)
        self.sock.close()


class FinderTests(unittest.TestCase):
    def test_probe_answer(self):
        svc = FakeService(beacon(port=8444))
        self.addCleanup(svc.close)
        f = md.Finder(svc.port, targets=lambda: [("127.0.0.1", "127.0.0.1")], listen=False)
        t0 = time.monotonic()
        self.assertEqual(f(SITE, CA), [("127.0.0.1", 8444, "probe")])
        self.assertLess(time.monotonic() - t0, 2.0)          # stops soon after the answer
        self.assertEqual(svc.probes, [{"type": md.PROBE, "v": 1, "site_id": SITE}])

    def test_answer_for_another_ca_is_ignored(self):
        svc = FakeService(beacon(ca="11" * 32))
        self.addCleanup(svc.close)
        f = md.Finder(svc.port, targets=lambda: [("127.0.0.1", "127.0.0.1")], listen=False, look_s=0.5)
        self.assertEqual(f(SITE, CA), [])

    def test_announcement_heard_while_looking(self):
        port = free_udp_port()
        f = md.Finder(port, targets=lambda: [], look_s=2.0)

        def announce():
            time.sleep(0.3)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(beacon(port=8445), ("127.0.0.1", port))
            s.close()
        threading.Thread(target=announce, daemon=True).start()
        self.assertEqual(f(SITE, CA), [("127.0.0.1", 8445, "announcement")])

    def test_port_held_without_sharing_means_probes_only(self):
        held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # like a service on this PC
        held.bind(("", 0))
        self.addCleanup(held.close)
        f = md.Finder(held.getsockname()[1], targets=lambda: [], look_s=0.2)
        self.assertEqual(f(SITE, CA), [])                     # no error

    def test_listening_socket_is_closed_after_looking(self):
        port = free_udp_port()
        md.Finder(port, targets=lambda: [], look_s=0.1)(SITE, CA)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # binds without sharing: port is free
        s.bind(("", port))
        s.close()

    def test_unusable_monitor_interface_sends_no_probe(self):
        targets = md.lan_targets("no-such-if0", "")
        self.assertEqual(targets(), [])
        self.assertEqual(md.lan_targets("eno1", "eno1")(), [])   # excluded


class FlakyTransport:
    """Service at the old address is gone; the new one answers like tf.FakeService."""

    def __init__(self, error=None):
        self.error = error or ConnectionRefusedError(111, "Connection refused")
        self.calls = 0

    def request(self, method, path, body=None, headers=None):
        self.calls += 1
        raise self.error


class ForwarderLookTests(tf.ForwarderBase):
    OLD, NEW = "https://192.168.0.104:8443", "https://192.168.0.110:8443"

    def setUp(self):
        super().setUp()
        self.enrol_files(url=self.OLD)
        with open(os.path.join(self.dir, mf.CA_FILE), "w") as f:
            f.write(tf.self_signed(self.tmp.name)[0])
        with open(os.path.join(self.dir, mf.CA_FILE)) as f:
            self.ca_sha = mf.ca_fingerprint(f.read())
        self.looks = []
        self.answers = [("192.168.0.110", 8443, "probe")]
        self.transports = {self.OLD: FlakyTransport()}
        self.made = []

    def finder(self, site, ca):
        self.looks.append((site, ca))
        return list(self.answers)

    def factory(self, url, d):
        self.made.append(url)
        return self.transports.get(url, self.svc)

    def fwd(self, url=None):
        return mf.Forwarder(self.spool, url or self.OLD, self.dir, MACHINE, transport_factory=self.factory,
                            wall=lambda: self.wall[0], clock=lambda: self.mono[0], rand=lambda: 1.0,
                            finder=self.finder)

    def found_file(self):
        return os.path.join(self.dir, mf.ADDRESS_FILE)

    def test_looks_after_three_failures_and_switches(self):
        self.add_cycles(3)
        fw = self.fwd()
        with self.assertLogs("agent_mes_forward", "WARNING") as logs:
            fw.step()
            fw.step()
            self.assertEqual(self.looks, [])                  # 2 failures: not yet
            self.assertEqual(fw.step(), 0.0)                  # 3rd: looked, switched
        self.assertEqual(self.looks, [(SITE, self.ca_sha)])
        self.assertIn("MES service moved: %s -> %s (probe)" % (self.OLD, self.NEW), "\n".join(logs.output))
        self.assertEqual(fw.url, self.NEW)
        self.assertEqual(fw.status["state"], "offline")      # until it has answered as the link
        fw.step()
        self.assertEqual(fw.status["state"], "ok")
        self.assertEqual(fw.sent["stored"], 3)
        doc = json.load(open(self.found_file()))
        self.assertEqual((doc["url"], doc["via"], doc["configured_url"]), (self.NEW, "probe", self.OLD))
        self.assertEqual(doc["found_at_ms"], int(self.wall[0] * 1000))
        self.assertEqual(stat.S_IMODE(os.stat(self.found_file()).st_mode), 0o600)

    def test_found_address_is_used_after_a_restart(self):
        self.test_looks_after_three_failures_and_switches()
        self.made.clear()
        fw = self.fwd()
        fw.step()
        self.assertEqual(self.made, [self.NEW])
        self.assertEqual(fw.status["state"], "ok")

    def test_config_changed_by_hand_wins_over_the_found_address(self):
        mf.save_found(self.dir, self.NEW, "probe", self.OLD)
        other = "https://10.0.0.9:8443"
        fw = self.fwd(other)
        fw.step()
        self.assertEqual(self.made, [other])
        self.assertIn(self.NEW, mf.describe(self.dir, self.OLD))
        self.assertNotIn("found on the network", mf.describe(self.dir, other))

    def test_never_looks_when_refused(self):
        for status in (401, 403):
            self.looks.clear()
            self.transports[self.OLD] = self.svc
            self.svc.fail += [ConnectionRefusedError(), ConnectionRefusedError(), status]
            fw = self.fwd()
            for _ in range(5):
                fw.step()
            self.assertEqual(fw.status["state"], "rejected")
            self.assertEqual(self.looks, [])

    def test_http_errors_reset_the_count(self):
        self.transports[self.OLD] = self.svc
        fw = self.fwd()
        self.svc.fail += [ConnectionRefusedError(), ConnectionRefusedError(), 503,
                          ConnectionRefusedError(), ConnectionRefusedError()]
        for _ in range(5):
            fw.step()
        self.assertEqual(self.looks, [])

    def test_service_certificate_not_naming_the_address_counts(self):
        err = ssl.SSLCertVerificationError(1, "hostname mismatch")
        err.verify_message = "IP address mismatch"
        self.transports[self.OLD] = FlakyTransport(err)
        fw = self.fwd()
        for _ in range(3):
            fw.step()
        self.assertEqual(len(self.looks), 1)
        self.assertEqual(fw.url, self.NEW)

    def test_candidate_failing_tls_is_not_used(self):
        err = ssl.SSLCertVerificationError(1, "unknown ca")
        err.verify_message = "unable to get local issuer certificate"
        self.transports[self.NEW] = FlakyTransport(err)
        fw = self.fwd()
        with self.assertLogs("agent_mes_forward", "WARNING") as logs:
            for _ in range(5):
                fw.step()
        self.assertEqual(fw.url, self.OLD)
        self.assertEqual(len(self.looks), 3)                  # looks again on every failed round
        text = "\n".join(logs.output)
        self.assertEqual(text.count("not used: unable to get local issuer"), 1)   # logged once
        self.assertFalse(os.path.exists(self.found_file()))

    def test_candidate_answering_non_200_is_not_used(self):
        class Busy:
            def request(self, *a, **k):
                return 503, b"{}"
        self.transports[self.NEW] = Busy()
        fw = self.fwd()
        for _ in range(3):
            fw.step()
        self.assertEqual(fw.url, self.OLD)

    def test_same_address_is_not_tried_again(self):
        self.answers = [("192.168.0.104", 8443, "announcement")]
        fw = self.fwd()
        for _ in range(4):
            fw.step()
        self.assertEqual(self.made, [self.OLD])

    def test_no_finder_never_looks(self):
        fw = mf.Forwarder(self.spool, self.OLD, self.dir, MACHINE, transport_factory=self.factory,
                          wall=lambda: self.wall[0], clock=lambda: self.mono[0], rand=lambda: 1.0)
        for _ in range(5):
            fw.step()
        self.assertEqual(fw.url, self.OLD)

    def test_check_shows_both_addresses(self):
        mf.save_found(self.dir, self.NEW, "probe", self.OLD)
        text = mf.describe(self.dir, self.OLD)
        self.assertTrue(text.startswith("%s (found on the network by probe on " % self.NEW), text)
        self.assertIn("configured %s)" % self.OLD, text)

    def test_enrolling_again_forgets_the_found_address(self):
        mf.save_found(self.dir, self.NEW, "probe", self.OLD)
        ca_pem, fp = tf.self_signed(self.tmp.name, "ca2")
        mf.enroll("https://10.0.0.9:8443", tf.code_for(fp), MACHINE, "VMC", self.dir,
                  get_ca=lambda h, p: ca_pem, site_getter=lambda h, p, c: SITE,
                  csr_maker=lambda w: (b"key", b"csr"),
                  poster=lambda h, p, c, b: (200, {"machine_id": MACHINE, "site_id": SITE,
                                                   "certificate": ca_pem, "not_after": 1}))
        self.assertFalse(os.path.exists(self.found_file()))


# ---------------------------------------------------------------------------
# Against FloorWatch's real site service, moved to another port
# ---------------------------------------------------------------------------
def _has_discovery():
    if not tf.SERVICE:
        return None, tf.WHY_NOT
    fw, _ = tf.SERVICE
    if not os.path.isfile(os.path.join(fw, "src", "floorwatch_service", "discovery.py")):
        return None, "FloorWatch in %s has no discovery (set FLOORWATCH_DIR to a newer one)" % fw
    return tf.SERVICE, ""


SERVICE, WHY_NOT = _has_discovery()


@unittest.skipUnless(SERVICE, WHY_NOT)
class ServiceMovesTests(unittest.TestCase):
    def setUp(self):
        self.fw, self.py = SERVICE
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "service")
        self.env = dict(os.environ, PYTHONPATH=os.path.join(self.fw, "src"))
        self.disc = free_udp_port()
        self.svc("init", "--name", "127.0.0.1", "--name", "localhost")
        # Own discovery port: never 5007 on the LAN; no address following in a test.
        self.svc("config", "--discovery-port", str(self.disc), "--follow-addresses", "off")
        self.proc = None
        self.addCleanup(self.stop)
        self.machine = str(uuid.uuid4())
        self.dir = os.path.join(self.tmp.name, "agent", "mes")
        self.spool = ms.Spool(os.path.join(self.tmp.name, "agent", "mes_spool.db"), self.machine,
                              "4f1c2a9e7b3d5c60", "1.7.0")
        self.addCleanup(self.spool.close)

    def svc(self, *args):
        r = subprocess.run([self.py, "-m", "floorwatch_service", "--data", self.data] + list(args),
                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60)
        return r.stdout.decode()

    def start(self, port):
        self.proc = subprocess.Popen([self.py, "-m", "floorwatch_service", "--data", self.data, "run",
                                      "--bind", "127.0.0.1", "--port", str(port)],
                                     env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("site service did not start on %d" % port)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def test_agent_follows_the_service_to_another_port(self):
        p1, p2 = tf._free_port(), tf._free_port()
        self.start(p1)
        url1 = "https://127.0.0.1:%d" % p1
        code = self.svc("enroll-code", "--note", "k", "--port", str(p1)).splitlines()[0].strip()
        mf.enroll(url1, code, self.machine, "K-TEST", self.dir)
        finder = md.Finder(self.disc, targets=lambda: [("127.0.0.1", "127.0.0.1")], listen=False)
        fw = mf.Forwarder(self.spool, url1, self.dir, self.machine, finder=finder)
        fw.step()
        self.assertEqual(fw.status["state"], "ok")
        self.stop()
        self.start(p2)                                     # same site, new port
        for i in range(3):
            f = tf.cycle_fact(i)
            self.spool.add("cycle.observed", tf.ml.cycle_payload(f), f["ended"],
                           {"execution_id": f["execution_id"]})
        with self.assertLogs("agent_mes_forward", "WARNING") as logs:
            for _ in range(10):
                fw.step()
                if fw.status["state"] == "ok" and fw.url != url1:
                    break
        url2 = "https://127.0.0.1:%d" % p2
        self.assertIn("MES service moved: %s -> %s (probe)" % (url1, url2), "\n".join(logs.output))
        for _ in range(5):
            if fw.step() != 0.0:
                break
        self.assertEqual(fw.status["state"], "ok")
        self.assertEqual(fw.sent["stored"], 3)
        self.assertEqual(mf.load_found(self.dir, url1)["url"], url2)
        # after a restart the agent goes straight to the new port
        fw2 = mf.Forwarder(self.spool, url1, self.dir, self.machine)
        fw2.step()
        self.assertEqual((fw2.url, fw2.status["state"]), (url2, "ok"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
