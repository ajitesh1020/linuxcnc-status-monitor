"""Enrolment and forwarding to the site service (agent_mes_forward.py, MES plan P2.5).
Run: python -m unittest -v test_mes_forward

Unit tests use a fake service. RealServiceTests run FloorWatch's
floorwatch_service on 127.0.0.1 (a free port, a temp folder) and are skipped
when it is not there: set FLOORWATCH_DIR (default ~/floorwatch) and, if needed,
FW_SERVICE_PYTHON (a Python with cryptography >= 42; default ~/fw-svc-venv or
~/fw-venv).
"""
import gzip
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_mes_forward as mf  # noqa: E402
import agent_mes_link as ml  # noqa: E402
import agent_mes_spool as ms  # noqa: E402
from test_mes_spool import MACHINE, mw_event  # noqa: E402

V = ms.contract()
SITE = "5b0e7c1a-9d2f-4e3b-a1c4-7f6e5d4c3b2a"
EP = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
HAVE_OPENSSL = shutil.which("openssl") is not None


def self_signed(directory, name="ca"):
    """(pem text, sha256 hex) of a throwaway self-signed certificate."""
    key, crt = os.path.join(directory, name + ".key"), os.path.join(directory, name + ".pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-keyout", key, "-out", crt, "-subj", "/CN=test-ca", "-days", "2"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(crt, encoding="ascii") as f:
        pem = f.read()
    return pem, mf.ca_fingerprint(pem)


def code_for(fp, secret="K7QXM2PA9TRF"):
    return "%s-%s-%s-%s" % (secret[:4], secret[4:8], secret[8:12], fp[:16])


def cycle_fact(i=0):
    t = 1_800_000_000.0 + i * 60
    return {"started": t, "ended": t + 30, "outcome": "completed", "program_path": "/p/a.ngc",
            "execution_id": str(uuid.uuid4()), "method": "none", "operator_session_id": None}


class CodeTests(unittest.TestCase):
    def test_dashes_spaces_and_case_do_not_matter(self):
        for c in ("K7QX-M2PA-9TRF-3fa9c0d1e2b4a6f8", "k7qxm2pa9trf3FA9C0D1E2B4A6F8",
                  " K7QX M2PA 9TRF 3fa9c0d1e2b4a6f8 "):
            self.assertEqual(mf.parse_code(c)[1], "3fa9c0d1e2b4a6f8")

    def test_bad_codes(self):
        for c in ("", "K7QX-M2PA-9TRF", "K7QX-M2PA-9TRF-3fa9c0d1e2b4a6fz", "K7QX-M2PA-9TR-3fa9c0d1e2b4a6f8"):
            with self.assertRaises(mf.EnrolError):
                mf.parse_code(c)

    def test_url_must_be_https(self):
        self.assertEqual(mf.split_url("https://10.0.0.5:8443"), ("10.0.0.5", 8443))
        self.assertEqual(mf.split_url("https://svc.local"), ("svc.local", 8443))
        with self.assertRaises(ValueError):
            mf.split_url("http://10.0.0.5:8443")


@unittest.skipUnless(HAVE_OPENSSL, "openssl command not installed")
class EnrolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ca_pem, self.fp = self_signed(self.tmp.name)
        self.dir = os.path.join(self.tmp.name, "agent", "mes")
        self.posted = []
        self.site = SITE            # what GET /v1/health says
        self.order = []

    def poster(self, status=200, doc=None):
        def post(host, port, ca_file, body):
            with open(ca_file, encoding="ascii") as f:
                self.assertEqual(f.read(), self.ca_pem)        # trusts only the checked CA
            self.posted.append(body)
            self.order.append("post")
            return status, doc if doc is not None else {
                "machine_id": body["machine_id"], "site_id": SITE, "certificate": self.ca_pem,
                "ca": self.ca_pem, "not_after": 1_900_000_000_000}
        return post

    def site_getter(self, host, port, ca_file):
        with open(ca_file, encoding="ascii") as f:
            self.assertEqual(f.read(), self.ca_pem)            # asked over TLS trusting only the checked CA
        self.order.append("site")
        return self.site

    def enroll(self, code, poster=None, machine=MACHINE, before_send=None):
        return mf.enroll("https://127.0.0.1:8443", code, machine, "VMC-01", self.dir,
                         get_ca=lambda h, p: self.ca_pem, poster=poster or self.poster(),
                         site_getter=self.site_getter, before_send=before_send)

    def test_site_is_checked_before_the_code_is_sent(self):
        seen = []
        self.enroll(code_for(self.fp), before_send=lambda site: (seen.append(site), self.order.append("guard")))
        self.assertEqual(seen, [SITE])
        self.assertEqual(self.order, ["site", "guard", "post"])

    def test_guard_refusal_sends_nothing_and_keeps_files(self):
        self.enroll(code_for(self.fp))
        with open(os.path.join(self.dir, mf.ENROLMENT_FILE), "rb") as f:
            before = f.read()
        self.posted.clear()

        def refuse(site):
            raise mf.EnrolError("no")
        with self.assertRaises(mf.EnrolError):
            self.enroll(code_for(self.fp), before_send=refuse)
        self.assertEqual(self.posted, [])
        with open(os.path.join(self.dir, mf.ENROLMENT_FILE), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([mf.KEY_FILE, mf.CERT_FILE, mf.CA_FILE,
                                                               mf.ENROLMENT_FILE]))

    def test_answer_for_another_site_than_health_said_is_refused(self):
        self.site = "cb6a4a6c-7625-4042-8f42-b4f28b7bb75d"
        with self.assertRaises(mf.EnrolError) as cm:
            self.enroll(code_for(self.fp))
        self.assertIn("said it is site", str(cm.exception))
        self.assertIsNone(mf.load_enrolment(self.dir))

    def test_service_that_does_not_say_its_site_still_enrols(self):
        self.site = None
        seen = []
        self.assertEqual(self.enroll(code_for(self.fp), before_send=seen.append)["site_id"], SITE)
        self.assertEqual(seen, [None])

    def test_wrong_fingerprint_stops_before_anything_is_sent(self):
        other = ("0" if self.fp[0] != "0" else "1") + self.fp[1:16]
        with self.assertRaises(mf.EnrolError) as cm:
            self.enroll(code_for(other))
        self.assertIn("nothing was sent", str(cm.exception))
        self.assertEqual(self.posted, [])                        # the code never left the machine
        self.assertFalse(os.path.exists(os.path.join(self.dir, mf.KEY_FILE)))

    def test_success_writes_private_files(self):
        doc = self.enroll(code_for(self.fp))
        self.assertEqual(doc, {"service_url": "https://127.0.0.1:8443", "site_id": SITE,
                               "machine_id": MACHINE, "not_after": 1_900_000_000_000})
        body = self.posted[0]
        self.assertEqual(body["code"], code_for(self.fp))
        self.assertEqual(body["machine_id"], MACHINE)
        self.assertIn("BEGIN CERTIFICATE REQUEST", body["csr"])
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for n in (mf.KEY_FILE, mf.CERT_FILE, mf.CA_FILE, mf.ENROLMENT_FILE):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, n)).st_mode), 0o600, n)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([mf.KEY_FILE, mf.CERT_FILE, mf.CA_FILE,
                                                               mf.ENROLMENT_FILE]))
        self.assertEqual(mf.load_enrolment(self.dir)["site_id"], SITE)
        self.assertIn("site %s" % SITE + "", mf.describe(self.dir, "https://127.0.0.1:8443"))
        self.assertNotIn("PRIVATE", mf.describe(self.dir, "https://127.0.0.1:8443"))

    def test_refusal_keeps_the_earlier_enrolment(self):
        self.enroll(code_for(self.fp))
        with open(os.path.join(self.dir, mf.KEY_FILE), "rb") as f:
            key = f.read()
        with self.assertRaises(mf.EnrolError) as cm:
            self.enroll(code_for(self.fp), self.poster(403, {"error": "code_used"}))
        self.assertIn("code_used", str(cm.exception))
        with open(os.path.join(self.dir, mf.KEY_FILE), "rb") as f:
            self.assertEqual(f.read(), key)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([mf.KEY_FILE, mf.CERT_FILE, mf.CA_FILE,
                                                               mf.ENROLMENT_FILE]))

    def test_answer_with_another_ca_is_refused(self):
        other_pem, _ = self_signed(self.tmp.name, "other")
        doc = {"machine_id": MACHINE, "site_id": "s", "certificate": other_pem, "ca": other_pem,
               "not_after": 1}
        with self.assertRaises(mf.EnrolError):
            self.enroll(code_for(self.fp), self.poster(200, doc))
        self.assertIsNone(mf.load_enrolment(self.dir))

    def test_no_machine_uuid(self):
        with self.assertRaises(mf.EnrolError):
            self.enroll(code_for(self.fp), machine="")

    def test_set_config_value_replaces_or_appends_and_keeps_mode(self):
        cfg = os.path.join(self.tmp.name, "config.yaml")
        with open(cfg, "w") as f:
            f.write('machine_name: "VMC"\nmes_service_url: ""\n')
        os.chmod(cfg, 0o600)
        mf.set_config_value(cfg, "mes_service_url", "https://10.0.0.5:8443")
        with open(cfg) as f:
            self.assertEqual(f.read(), 'machine_name: "VMC"\nmes_service_url: "https://10.0.0.5:8443"\n')
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
        mf.set_config_value(cfg, "mes_enabled", "x")
        with open(cfg) as f:
            self.assertTrue(f.read().endswith('mes_enabled: "x"\n'))


# ---------------------------------------------------------------------------
# Forwarder against a fake service
# ---------------------------------------------------------------------------
OLD_SITE = "7844c24c-f48b-4f71-b0e9-a18edf34e157"


class WaitingGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mes_spool.db")
        self.spool = ms.Spool(self.path, MACHINE, "4f1c2a9e7b3d5c60", "1.7.0")
        self.addCleanup(self.spool.close)
        self.out = []

    def own(self, n):
        for i in range(n):
            f = cycle_fact(i)
            self.spool.add("cycle.observed", ml.cycle_payload(f), f["ended"], {"execution_id": f["execution_id"]})

    def guard(self, discard=False):
        return mf.waiting_guard(self.path, discard, out=self.out.append)

    def states(self):
        return [r[0] for r in self.spool._db.execute("SELECT state FROM spool ORDER BY seq")]

    def test_nothing_waiting_or_same_site_or_millwright_only(self):
        self.guard()(SITE)                                       # empty spool
        self.spool.append([mw_event(1, EP)])                     # site null: accepted anywhere
        self.guard()(SITE)
        self.spool.set_site_id(SITE)
        self.own(2)
        self.guard()(SITE)                                       # same site
        self.assertEqual(self.out, [])
        self.assertEqual(self.states(), ["pending"] * 3)

    def test_other_site_refuses_and_says_how(self):
        self.spool.set_site_id(OLD_SITE)
        self.own(3)
        with self.assertRaises(mf.EnrolError) as cm:
            self.guard()(SITE)
        msg = str(cm.exception)
        for part in ("3 event(s)", OLD_SITE, SITE, "site_mismatch", "Nothing was sent", "--mes-status",
                     "--discard-waiting"):
            self.assertIn(part, msg)
        self.assertEqual(self.states(), ["pending"] * 3)       # untouched

    def test_unknown_new_site_refuses_too(self):
        self.spool.set_site_id(OLD_SITE)
        self.own(1)
        with self.assertRaises(mf.EnrolError) as cm:
            self.guard()(None)
        self.assertIn("does not say which site", str(cm.exception))

    def test_discard_drops_only_the_other_sites_events(self):
        self.spool.append([mw_event(1, EP)])
        self.spool.set_site_id(OLD_SITE)
        self.own(2)
        self.guard(discard=True)(SITE)
        self.assertEqual(self.states(), ["pending", "dropped", "dropped"])
        reason = self.spool._db.execute("SELECT reason FROM spool WHERE seq=2").fetchone()[0]
        self.assertIn(OLD_SITE, reason)
        self.assertIn(SITE, reason)
        self.assertEqual(len(self.out), 1)
        self.assertIn("Discarded 2", self.out[0])


class FakeService:
    """Speaks the API: capabilities, events with receipts, dedup by event_id."""

    def __init__(self, types=("cycle.observed", "operator.session_started"), limits=None):
        self.types = list(types)
        self.limits = dict(mf.DEFAULT_LIMITS, **(limits or {}))
        self.stored = {}
        self.batches = []            # decoded event lists, one per POST
        self.raw_sizes = []          # bytes before gzip
        self.fail = []               # queued: int status, an exception, or "store-then-break"
        self.status_of = {}          # event type -> receipt status to give
        self.calls = []

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path))
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, BaseException):
                raise f
            if isinstance(f, int):
                return f, json.dumps({"error": "x"}).encode()
            if f == "store-then-break":
                self._events(body, headers)
                raise http.client.RemoteDisconnected("connection broken after the request")
        if (method, path) == ("GET", "/v1/capabilities"):
            return 200, json.dumps({"contracts": {"fw.mes.event/1": {"event_types": self.types}},
                                    "limits": self.limits}).encode()
        if (method, path) == ("POST", "/v1/events"):
            return 200, json.dumps({"receipts": self._events(body, headers)}).encode()
        return 404, b"{}"

    def _events(self, body, headers):
        if (headers or {}).get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.raw_sizes.append(len(body))
        events = json.loads(body.decode())["events"]
        self.batches.append(events)
        out = []
        for e in events:
            eid = e["event_id"]
            st = self.status_of.get(e["type"])
            if st is None:
                st = "duplicate" if eid in self.stored else "stored"
            self.stored.setdefault(eid, e)
            out.append({"event_id": eid, "status": st})
        return out


class ForwarderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = os.path.join(self.tmp.name, "agent")
        self.dir = os.path.join(self.agent, "mes")
        self.wall = [1_800_000_000.0]
        self.mono = [1000.0]
        self.spool = ms.Spool(os.path.join(self.agent, "mes_spool.db"), MACHINE, "4f1c2a9e7b3d5c60",
                              "1.7.0", now=lambda: self.wall[0])
        self.addCleanup(self.spool.close)
        self.svc = FakeService()

    def enrol_files(self, machine=MACHINE, not_after=1_900_000_000_000, url="https://127.0.0.1:8443"):
        os.makedirs(self.dir, exist_ok=True)
        for n in (mf.KEY_FILE, mf.CERT_FILE, mf.CA_FILE):
            with open(os.path.join(self.dir, n), "w") as f:
                f.write("x")
        with open(os.path.join(self.dir, mf.ENROLMENT_FILE), "w") as f:
            json.dump({"service_url": url, "site_id": SITE, "machine_id": machine,
                       "not_after": not_after}, f)

    def forwarder(self, url="https://127.0.0.1:8443"):
        return mf.Forwarder(self.spool, url, self.dir, MACHINE,
                            transport_factory=lambda u, d: self.svc,
                            wall=lambda: self.wall[0], clock=lambda: self.mono[0], rand=lambda: 1.0)

    def add_cycles(self, n):
        for i in range(n):
            f = cycle_fact(i)
            self.spool.add("cycle.observed", ml.cycle_payload(f), f["ended"],
                           {"execution_id": f["execution_id"]})

    def states(self):
        return dict(self.spool._db.execute("SELECT seq, state FROM spool"))


class ForwarderTests(ForwarderBase):
    def test_not_enrolled_without_url_or_files(self):
        fw = self.forwarder(url="")
        self.assertEqual(fw.step(), mf.RECHECK_S)
        self.assertEqual(fw.status, {"state": "not_enrolled"})
        fw = self.forwarder()
        fw.step()
        self.assertEqual(fw.status, {"state": "not_enrolled"})
        self.assertEqual(self.svc.calls, [])

    def test_enrolment_for_another_machine_is_not_used(self):
        self.enrol_files(machine=str(uuid.uuid4()))
        fw = self.forwarder()
        fw.step()
        self.assertEqual(fw.status["state"], "not_enrolled")
        self.assertEqual(self.svc.calls, [])

    def test_forwards_and_acks_every_receipt_status(self):
        self.enrol_files()
        self.add_cycles(2)
        self.spool.append([mw_event(1, EP), mw_event(2, EP)])
        self.svc.status_of = {"operator.session_started": "rejected"}
        fw = self.forwarder()
        self.assertEqual(fw.step(), 0.0)                     # sent something: go again at once
        self.assertEqual(set(self.states().values()), {"acked"})
        self.assertEqual(fw.status, {"state": "ok", "last_ok_ms": 1_800_000_000_000})
        self.assertEqual(self.svc.calls[0], ("GET", "/v1/capabilities"))
        self.assertEqual(fw.sent["stored"], 2)
        self.assertEqual(fw.sent["rejected"], 2)
        # the bytes sent are the spool's bytes
        sent = [V.encode(e) for e in self.svc.batches[0]]
        self.assertEqual(sent, [b for _, b in sorted(
            (s, b.encode()) for s, b in self.spool._db.execute("SELECT seq, body FROM spool"))])
        self.assertEqual(fw.step(), mf.IDLE_POLL_S)        # nothing left
        self.assertEqual(self.spool.site_id, SITE)    # set from the enrolment (§4.4)

    def test_quarantined_and_duplicate_are_final_too(self):
        self.enrol_files()
        self.add_cycles(1)
        self.svc.status_of = {"cycle.observed": "quarantined"}
        fw = self.forwarder()
        fw.step()
        self.assertEqual(set(self.states().values()), {"acked"})

    def test_503_marks_nothing_and_backs_off_with_jitter(self):
        self.enrol_files()
        self.add_cycles(3)
        fw = self.forwarder()
        fw.step()                                            # capabilities + all sent
        self.add_cycles(2)
        before = self.states()
        waits = []
        self.svc.fail = [503] * 12
        for _ in range(12):
            waits.append(fw.step())
        self.assertEqual(self.states(), before)              # nothing more acked
        self.assertEqual(fw.status["state"], "offline")
        self.assertEqual(fw.status["last_ok_ms"], 1_800_000_000_000)
        self.assertEqual(waits[:4], [1.0, 2.0, 4.0, 8.0])   # rand=1.0: the full backoff
        self.assertEqual(max(waits), mf.BACKOFF_MAX_S)
        fw.rand = lambda: 0.0
        self.svc.fail = [503]
        self.assertEqual(fw.step(), mf.BACKOFF_MAX_S / 2)  # jitter: half to all
        waiting = [json.loads(b)["event_id"] for _, b in self.spool.pending()]
        self.assertEqual(len(waiting), 2)
        self.assertEqual(fw.step(), 0.0)                     # back: the same events go
        self.assertEqual([e["event_id"] for e in self.svc.batches[-1]], waiting)
        self.assertEqual(set(self.states().values()), {"acked"})
        self.assertEqual(fw.status["state"], "ok")

    def test_timeout_and_broken_connection_mark_nothing(self):
        self.enrol_files()
        fw = self.forwarder()
        fw.step()
        self.add_cycles(2)
        for exc in (socket.timeout("timed out"), ConnectionResetError(104, "reset"),
                    http.client.RemoteDisconnected("gone"), ssl.SSLEOFError("eof")):
            self.svc.fail = [exc]
            self.assertGreater(fw.step(), 0)
            self.assertEqual(set(self.states().values()), {"pending"}, exc)
            self.assertEqual(fw.status["state"], "offline")
        fw.step()
        self.assertEqual(set(self.states().values()), {"acked"})

    def test_lost_receipt_resend_answers_duplicate_nothing_doubled(self):
        self.enrol_files()
        self.add_cycles(3)
        fw = self.forwarder()
        fw._load()
        fw._capabilities()
        self.svc.fail = ["store-then-break"]
        fw.send_once()
        self.assertEqual(len(self.svc.stored), 3)            # the service committed
        self.assertEqual(set(self.states().values()), {"pending"})
        fw.send_once()
        self.assertEqual(set(self.states().values()), {"acked"})
        self.assertEqual(len(self.svc.stored), 3)
        self.assertEqual(fw.sent["duplicate"], 3)

    def test_only_listed_types_are_sent_others_stay(self):
        self.enrol_files()
        self.svc.types = ["cycle.observed"]
        self.spool.append([mw_event(1, EP)])
        self.add_cycles(2)
        self.spool.append([mw_event(2, EP)])
        fw = self.forwarder()
        fw.step()
        sent = [e["type"] for b in self.svc.batches for e in b]
        self.assertEqual(sent, ["cycle.observed"] * 2)
        st = self.states()
        self.assertEqual(sorted(st.values()), ["acked", "acked", "pending", "pending"])
        # the service learns the type; the next capabilities check (hourly) picks it up
        self.svc.types.append("operator.session_started")
        self.assertEqual(fw.step(), mf.IDLE_POLL_S)
        self.mono[0] += mf.CAPS_EVERY_S
        fw.step()
        self.assertEqual(set(self.states().values()), {"acked"})

    def test_batches_respect_the_service_limits(self):
        self.enrol_files()
        self.svc.limits = {"max_batch_events": 7, "max_batch_bytes": 4000, "max_event_bytes": 16384}
        self.add_cycles(40)
        fw = self.forwarder()
        for _ in range(30):
            if fw.step() != 0.0:
                break
        self.assertEqual(set(self.states().values()), {"acked"})
        self.assertTrue(all(len(b) <= 7 for b in self.svc.batches))
        self.assertTrue(all(n <= 4000 for n in self.svc.raw_sizes), self.svc.raw_sizes)
        self.assertGreater(len(self.svc.batches), 40 // 7)
        ids = [e["event_id"] for b in self.svc.batches for e in b]
        self.assertEqual(len(ids), len(set(ids)))           # oldest first, each once
        seqs = [e["producer"]["seq"] for b in self.svc.batches for e in b]
        self.assertEqual(seqs, sorted(seqs))

    def test_event_over_the_limit_is_kept_not_sent(self):
        self.enrol_files()
        self.add_cycles(2)
        self.svc.limits = {"max_event_bytes": 100}
        fw = self.forwarder()
        fw.step()
        self.assertEqual(set(self.states().values()), {"pending"})
        self.assertEqual(self.svc.batches, [])

    def test_413_halves_the_batch(self):
        self.enrol_files()
        self.add_cycles(10)
        fw = self.forwarder()
        fw._load()
        fw._capabilities()
        self.svc.fail = [413]
        self.assertEqual(fw.send_once(), 0)
        self.assertEqual(fw.batch_cap, 5)
        self.assertEqual(fw.send_once(), 5)

    def test_revoked_stops_until_enrolled_again(self):
        self.enrol_files()
        self.add_cycles(2)
        fw = self.forwarder()
        self.svc.fail = [403]
        self.assertEqual(fw.step(), mf.RECHECK_S)
        self.assertEqual(fw.status["state"], "rejected")
        n = len(self.svc.calls)
        for _ in range(3):
            self.assertEqual(fw.step(), mf.RECHECK_S)
        self.assertEqual(len(self.svc.calls), n)             # nothing sent with that certificate
        time.sleep(0.01)
        with open(os.path.join(self.dir, mf.CERT_FILE), "w") as f:
            f.write("new certificate")                       # enrolled again
        fw.step()
        self.assertEqual(fw.status["state"], "ok")
        self.assertEqual(set(self.states().values()), {"acked"})

    def test_expired_certificate(self):
        self.enrol_files(not_after=int(self.wall[0] * 1000) - 1)
        fw = self.forwarder()
        self.assertEqual(fw.step(), mf.RECHECK_S)
        self.assertEqual(fw.status["state"], "cert_expired")
        self.assertEqual(self.svc.calls, [])
        self.assertIn("EXPIRED", mf.describe(self.dir, "https://127.0.0.1:8443", lambda: self.wall[0]))

    def test_tls_alert_certificate_expired(self):
        self.enrol_files()
        fw = self.forwarder()
        err = ssl.SSLError(1, "[SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] sslv3 alert certificate expired")
        err.reason = "SSLV3_ALERT_CERTIFICATE_EXPIRED"
        self.svc.fail = [err]
        fw.step()
        self.assertEqual(fw.status["state"], "cert_expired")

    def test_service_certificate_mismatch_is_offline_not_rejected(self):
        self.enrol_files()
        fw = self.forwarder()
        err = ssl.SSLCertVerificationError(1, "hostname mismatch")
        err.verify_message = "Hostname mismatch"
        self.svc.fail = [err]
        fw.step()
        self.assertEqual(fw.status["state"], "offline")

    def test_receipt_for_another_event_is_not_acked(self):
        self.enrol_files()
        self.add_cycles(1)
        fw = self.forwarder()
        fw._load()
        fw._capabilities()
        real = self.svc.request
        self.svc.request = lambda m, p, b=None, h=None: (200, json.dumps(
            {"receipts": [{"event_id": str(uuid.uuid4()), "status": "stored"}]}).encode())
        self.assertEqual(fw.send_once(), 0)
        self.assertEqual(set(self.states().values()), {"pending"})
        self.svc.request = real

    def test_thread_starts_and_stops(self):
        self.enrol_files()
        self.add_cycles(3)
        fw = self.forwarder()
        fw.start()
        deadline = time.time() + 5
        while time.time() < deadline and set(self.states().values()) != {"acked"}:
            time.sleep(0.02)
        fw.stop()
        self.assertFalse(fw.is_alive())
        self.assertEqual(set(self.states().values()), {"acked"})


# ---------------------------------------------------------------------------
# Against FloorWatch's real site service
# ---------------------------------------------------------------------------
def _service_setup():
    fw = os.path.expanduser(os.environ.get("FLOORWATCH_DIR", "~/floorwatch"))
    if not os.path.isdir(os.path.join(fw, "src", "floorwatch_service")):
        return None, "no floorwatch_service in %s (set FLOORWATCH_DIR)" % fw
    cands = [os.environ.get("FW_SERVICE_PYTHON", ""), "~/fw-svc-venv/bin/python", "~/fw-venv/bin/python"]
    for c in cands:
        py = os.path.expanduser(c) if c else ""
        if py and os.path.isfile(py):
            r = subprocess.run([py, "-c", "import cryptography, sys; "
                                "sys.exit(int(cryptography.__version__.split('.')[0]) < 42)"])
            if r.returncode == 0:
                return (fw, py), ""
    return None, "no Python with cryptography >= 42 (set FW_SERVICE_PYTHON)"


SERVICE, WHY_NOT = _service_setup() if HAVE_OPENSSL else (None, "openssl not installed")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(SERVICE, WHY_NOT)
class RealServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw, cls.py = SERVICE
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = os.path.join(cls.tmp.name, "service")
        cls.port = _free_port()
        cls.url = "https://127.0.0.1:%d" % cls.port
        cls.svc("init", "--name", "127.0.0.1", "--name", "localhost")
        cls.proc = subprocess.Popen(cls.cmd("run", "--bind", "127.0.0.1", "--port", str(cls.port)),
                                    env=dict(os.environ, PYTHONPATH=os.path.join(cls.fw, "src")),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            cls.proc.kill()
            raise RuntimeError("site service did not start: %s" % cls.proc.stderr.read().decode())

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.proc.stderr.close()
        cls.tmp.cleanup()

    @classmethod
    def cmd(cls, *args):
        return [cls.py, "-m", "floorwatch_service", "--data", cls.data] + list(args)

    @classmethod
    def svc(cls, *args):
        env = dict(os.environ, PYTHONPATH=os.path.join(cls.fw, "src"))
        r = subprocess.run(cls.cmd(*args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           check=True, timeout=60)
        return r.stdout.decode()

    @classmethod
    def new_code(cls):
        return cls.svc("enroll-code", "--note", "test", "--port", str(cls.port)).splitlines()[0].strip()

    def ledger(self, sql):
        import sqlite3
        with sqlite3.connect(os.path.join(self.data, "ledger.db")) as db:
            return db.execute(sql).fetchall()

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.machine = str(uuid.uuid4())
        self.dir = os.path.join(self.t.name, "agent", "mes")
        self.spool = ms.Spool(os.path.join(self.t.name, "agent", "mes_spool.db"), self.machine,
                              "4f1c2a9e7b3d5c60", "1.7.0")
        self.addCleanup(self.spool.close)

    def enroll(self, code=None):
        return mf.enroll(self.url, code or self.new_code(), self.machine, "TEST", self.dir)

    def add_cycles(self, n):
        for i in range(n):
            f = cycle_fact(i)
            self.spool.add("cycle.observed", ml.cycle_payload(f), f["ended"],
                           {"execution_id": f["execution_id"]})

    def drain(self, fw, rounds=50):
        for _ in range(rounds):
            if fw.step() != 0.0:
                break

    def pending(self):
        return self.spool._db.execute("SELECT COUNT(*) FROM spool WHERE state='pending'").fetchone()[0]

    def test_wrong_fingerprint_does_not_use_the_code(self):
        code = self.new_code()
        secret, fp = code[:-16], code[-16:]
        bad = secret + ("0" if fp[0] != "0" else "1") + fp[1:]
        with self.assertRaises(mf.EnrolError):
            self.enroll(bad)
        self.assertIsNone(mf.load_enrolment(self.dir))
        doc = self.enroll(code)                          # the code was never sent: still valid
        self.assertEqual(doc["machine_id"], self.machine)

    def test_enrol_forward_and_resend_duplicate(self):
        doc = self.enroll()
        self.assertTrue(doc["not_after"] > time.time() * 1000)
        self.add_cycles(5)
        self.spool.append([mw_event(1, str(uuid.uuid4()))])
        fw = mf.Forwarder(self.spool, self.url, self.dir, self.machine)
        self.drain(fw)
        self.assertEqual(fw.status["state"], "ok", fw.status)
        self.assertEqual(self.pending(), 0)
        self.assertEqual(fw.sent["stored"], 6)
        n = self.ledger("SELECT COUNT(*) FROM raw_events")[0][0]
        self.assertGreaterEqual(n, 6)
        # receipts lost: everything goes again and answers duplicate
        self.spool._db.execute("UPDATE spool SET state='pending'")
        fw2 = mf.Forwarder(self.spool, self.url, self.dir, self.machine)
        self.drain(fw2)
        self.assertEqual(fw2.sent["duplicate"], 6)
        self.assertEqual(self.ledger("SELECT COUNT(*) FROM raw_events")[0][0], n)

    def test_kill_9_after_commit_before_marking(self):
        """A real kill -9 of a forwarding process between the service's 200 and
        the spool update: after restart nothing is lost or doubled."""
        self.enroll()
        self.add_cycles(4)
        self.spool.close()
        child = textwrap.dedent("""
            import os, signal, sys
            sys.path.insert(0, %r)
            import agent_mes_forward as mf, agent_mes_spool as ms
            s = ms.Spool(%r, %r, "4f1c2a9e7b3d5c60", "1.7.0")
            s.mark_acked = lambda seqs: os.kill(os.getpid(), signal.SIGKILL)
            fw = mf.Forwarder(s, %r, %r, %r)
            for _ in range(10):
                fw.step()
            sys.exit(3)
        """) % (ROOT, self.spool.path, self.machine, self.url, self.dir, self.machine)
        r = subprocess.run([sys.executable, "-c", child], timeout=60)
        self.assertEqual(r.returncode, -signal.SIGKILL)
        before = self.ledger("SELECT COUNT(*) FROM raw_events")[0][0]
        self.spool = ms.Spool(self.spool.path, self.machine, "4f1c2a9e7b3d5c60", "1.7.0")
        self.addCleanup(self.spool.close)
        self.assertEqual(self.pending(), 4)               # nothing was marked
        fw = mf.Forwarder(self.spool, self.url, self.dir, self.machine)
        self.drain(fw)
        self.assertEqual(self.pending(), 0)
        self.assertEqual(fw.sent["duplicate"], 4)
        self.assertEqual(self.ledger("SELECT COUNT(*) FROM raw_events")[0][0], before)

    def test_revoked_agent_is_rejected(self):
        self.enroll()
        fw = mf.Forwarder(self.spool, self.url, self.dir, self.machine)
        self.drain(fw)
        self.assertEqual(fw.status["state"], "ok")
        self.svc("revoke", self.machine)
        self.add_cycles(1)
        fw.step()
        self.assertEqual(fw.status["state"], "rejected")
        self.assertEqual(self.pending(), 1)
        # enrolled again with a new code: forwarding resumes
        time.sleep(0.01)
        self.enroll()
        self.drain(fw)
        self.assertEqual(fw.status["state"], "ok")
        self.assertEqual(self.pending(), 0)

    def test_health_tells_the_site_over_the_checked_ca(self):
        host, port = mf.split_url(self.url)
        ca = os.path.join(self.t.name, "ca.pem")
        with open(ca, "w") as f:
            f.write(mf.fetch_ca(host, port))
        site = mf.fetch_site(host, port, ca)
        self.assertTrue(site and mf.UUID4_RE.match(site), site)
        self.assertIsNone(mf.fetch_site(host, _free_port(), ca))    # nothing listening

    def test_re_enrol_with_waiting_events_of_another_site(self):
        """Events stamped for an earlier site: refused, code unused; then
        --discard-waiting enrols with the same code and they are never sent."""
        self.spool.set_site_id(OLD_SITE)
        self.add_cycles(3)
        self.spool.append([mw_event(1, str(uuid.uuid4()))])          # MillWright, site null
        code = self.new_code()
        guard = mf.waiting_guard(self.spool.path, False, out=lambda m: None)
        with self.assertRaises(mf.EnrolError) as cm:
            mf.enroll(self.url, code, self.machine, "TEST", self.dir, before_send=guard)
        self.assertIn("3 event(s)", str(cm.exception))
        self.assertIsNone(mf.load_enrolment(self.dir))
        out = []
        doc = mf.enroll(self.url, code, self.machine, "TEST", self.dir,          # same code: not used before
                        before_send=mf.waiting_guard(self.spool.path, True, out=out.append))
        self.assertIn("Discarded 3", out[0])
        self.assertEqual(self.pending(), 1)
        fw = mf.Forwarder(self.spool, self.url, self.dir, self.machine)
        self.drain(fw)
        self.assertEqual(fw.status["state"], "ok", fw.status)
        self.assertEqual(self.pending(), 0)
        self.assertEqual(fw.sent["stored"], 1)                      # only the MillWright event
        self.assertEqual(fw.sent["rejected"], 0)
        self.assertEqual(self.spool.site_id, doc["site_id"])

    def test_capabilities_limits_are_used(self):
        self.enroll()
        fw = mf.Forwarder(self.spool, self.url, self.dir, self.machine)
        fw.step()
        self.assertIn("cycle.observed", fw.types)
        self.assertGreater(fw.limits["max_batch_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
