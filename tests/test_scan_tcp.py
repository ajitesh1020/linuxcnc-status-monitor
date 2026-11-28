"""Production reading from Keyence SR-X readers on TCP (agent_scan_tcp.py),
docs/SCANNER_KEYENCE_SRX_WORK.md package 2, against tools/fake_srx.py and the
real MES spool.
Run: python -m unittest -v test_scan_tcp
"""
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import agent_mes_spool as ms  # noqa: E402
import agent_mes_status as mst  # noqa: E402
import agent_scan_tcp as st  # noqa: E402
import agent_srx as srx  # noqa: E402
from fake_srx import FakeSrx  # noqa: E402

MACHINE = "8d7c6b5a-4f3e-4d2c-8b1a-0f9e8d7c6b5a"
MAC = "0001FC000001"
CONTRACT = os.path.join(ROOT, "contracts", "mes", "v1")


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def contract_13(tmp):
    """A copy of the vendored contract with the 1.3 additions (section 4.3), for
    ContractFeatures only."""
    d = os.path.join(tmp, "contract13")
    shutil.copytree(CONTRACT, d)
    for name, edit in (("scan.rejected_local", lambda s: s["properties"]["reason"]["enum"].append("no_read")),
                       ("scanner.state", lambda s: (s["properties"]["state"]["enum"].append("setup"),
                                                    s["properties"].update(model={"type": "string"},
                                                                           serial={"type": "string"},
                                                                           firmware={"type": "string"})))):
        p = os.path.join(d, "events", name + ".schema.json")
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        edit(s)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(s, f)
    return d


class FakeSpool:
    """Records add() calls; full=True raises SpoolFull unless forced."""

    def __init__(self):
        self.events = []
        self.full = False
        self.lock = threading.Lock()

    def add(self, type_, payload, occurred, correlation=None, force=False):
        if self.full and not force:
            raise ms.SpoolFull("full")
        with self.lock:
            self.events.append((type_, dict(payload), dict(correlation or {})))
        return len(self.events)

    def of(self, type_):
        with self.lock:
            return [p for t, p, _ in self.events if t == type_]

    def states(self):
        return [p["state"] for p in self.of("scanner.state")]


def cfg(**kw):
    kw.setdefault("mac", MAC)
    return st.ScannerConfig("A", **kw)


# ---------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def test_keys_of_section_4_1(self):
        raw = {"scanners": "A, B", "scanner_A_mode": "tcp", "scanner_A_host": "10.0.0.7",
               "scanner_A_port": 9004, "scanner_A_mac": "00:01:fc:00:00:01", "scanner_A_serial": "",
               "scanner_A_model": "SR-X100", "scanner_A_trigger": "agent", "scanner_A_no_read": "NG",
               "scanner_A_terminator": "crlf", "scanner_A_max_len": 2000, "scanner_A_no_read_warn": 3,
               "scanner_A_pass_rate": 99, "scanner_B_mode": "hid"}
        cfgs, problems = st.scanner_configs(raw)
        self.assertEqual(problems, [])
        self.assertEqual([c.name for c in cfgs], ["A"])                  # B is hid: another adapter
        c = cfgs[0]
        self.assertEqual((c.host, c.port, c.trigger, c.no_read, c.terminator, c.max_len, c.no_read_warn,
                          c.pass_rate, c.model), ("10.0.0.7", 9004, "agent", "NG", "crlf", 2000, 3.0, 99.0,
                                                  "SR-X100"))
        self.assertEqual(st.normalize_mac(c.mac), MAC)

    def test_defaults(self):
        cfgs, _ = st.scanner_configs({"scanners": "A", "scanner_A_mode": "tcp"})
        c = cfgs[0]
        self.assertEqual((c.host, c.port, c.trigger, c.no_read, c.terminator, c.max_len, c.no_read_warn,
                          c.pass_rate), ("192.168.100.100", 9004, "reader", "ERROR", "cr", 4096, 5.0, 98.0))

    def test_problems_leave_only_that_scanner_out(self):
        raw = {"scanners": "A,B,C,D,bad name", "scanner_A_mode": "tcp", "scanner_B_mode": "tcp",
               "scanner_B_port": "x", "scanner_C_mode": "tcp", "scanner_C_trigger": "plc",
               "scanner_D_mode": "tcp", "scanner_D_max_len": 5000}
        cfgs, problems = st.scanner_configs(raw)
        self.assertEqual([c.name for c in cfgs], ["A"])
        self.assertEqual(len(problems), 4)

    def test_no_scanners(self):
        self.assertEqual(st.scanner_configs({}), ([], []))

    def test_normalize_mac(self):
        self.assertEqual(st.normalize_mac("00-01-fc-00-00-01"), MAC)
        for bad in (None, "", "0001FC", "zz"):
            self.assertEqual(st.normalize_mac(bad), "")


class ContractFeatureTests(unittest.TestCase):
    def test_vendored_contract_is_before_1_3(self):
        f = st.ContractFeatures(CONTRACT)
        self.assertFalse(f.no_read or f.setup or f.v13)
        self.assertEqual(f.state_fields, set())

    def test_1_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = st.ContractFeatures(contract_13(tmp))
        self.assertTrue(f.v13)
        self.assertEqual(f.state_fields, {"model", "serial", "firmware"})

    def test_missing_contract(self):
        self.assertFalse(st.ContractFeatures("/nonexistent").v13)


class ClassifyReadTests(unittest.TestCase):
    def test_plain_code(self):
        self.assertEqual(st.classify_read(b"ABC-123", cfg()),
                         ("scan", {"raw": "ABC-123", "raw_encoding": "utf8", "framing": "cr"}))

    def test_aim_symbology_in_front(self):
        self.assertEqual(st.classify_read(b"]d1PCB0042", cfg(terminator="crlf")),
                         ("scan", {"raw": "PCB0042", "raw_encoding": "utf8", "symbology": "]d1",
                                   "framing": "crlf"}))
        self.assertEqual(st.classify_read(b"]Q1", cfg())[1]["raw"], "]Q1")    # nothing after it: data

    def test_gs1_separator_goes_base64(self):
        kind, p = st.classify_read(b"]d2\x1d0104012345678901", cfg())
        self.assertEqual((kind, p["raw_encoding"], p["symbology"]), ("scan", "base64", "]d2"))
        self.assertEqual(base64.b64decode(p["raw"]), b"\x1d0104012345678901")

    def test_not_utf8_goes_base64(self):
        kind, p = st.classify_read(b"\xff\xfe", cfg())
        self.assertEqual((kind, p["raw_encoding"], base64.b64decode(p["raw"])), ("scan", "base64", b"\xff\xfe"))

    def test_utf8_text(self):
        self.assertEqual(st.classify_read("Ünit-7".encode(), cfg())[1]["raw"], "Ünit-7")

    def test_no_read(self):
        self.assertEqual(st.classify_read(b"ERROR", cfg()), ("no_read", {}))
        self.assertEqual(st.classify_read(b"NG", cfg(no_read="NG")), ("no_read", {}))
        self.assertEqual(st.classify_read(b"ERROR", cfg(no_read="NG"))[0], "scan")
        self.assertEqual(st.classify_read(b"ERRORX", cfg())[0], "scan")

    def test_too_long(self):
        self.assertEqual(st.classify_read(b"x" * 11, cfg(max_len=10)),
                         ("reject", {"reason": "too_long", "partial_len": 11}))
        self.assertEqual(st.classify_read(b"x" * 10, cfg(max_len=10))[0], "scan")

    def test_control_chars(self):
        for bad in (b"AB\x07C", b"A\tB", b"A\x7fB", b"\x00"):
            self.assertEqual(st.classify_read(bad, cfg()),
                             ("reject", {"reason": "control_chars", "partial_len": len(bad)}), bad)

    def test_display_code(self):
        self.assertEqual(st.display_code(b"01\x1d21X\x04"), "01<GS>21X<EOT>")
        self.assertEqual(len(st.display_code(b"x" * 500)), 128)


class LockAndRequestFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_lock_age_from_since_or_mtime(self):
        p = os.path.join(self.tmp.name, "A.setup.lock")
        self.assertIsNone(st.lock_age(p, time.time()))
        with open(p, "w") as f:
            json.dump({"owner": "cli", "pid": 1, "since": "2026-12-01T10:00:00Z"}, f)
        self.assertAlmostEqual(st.lock_age(p, st._parse_utc("2026-12-01T12:30:00Z")), 9000)
        with open(p, "w") as f:
            f.write("not json")
        self.assertLess(st.lock_age(p, time.time()), 5)

    def test_parse_utc(self):
        self.assertEqual(st._parse_utc("1970-01-01T00:00:10.5Z"), 10.5)
        for bad in ("2026-12-01 10:00:00", "2026-12-01T10:00:00+01:00"):
            with self.assertRaises(ValueError):
                st._parse_utc(bad)

    def test_read_request(self):
        p = os.path.join(self.tmp.name, "scan_request.json")
        self.assertIsNone(st.read_request(p))
        for bad in ("[]", "{}", '{"scanner": "A"}', '{"scanner": "A", "request_id": ""}', "x"):
            with open(p, "w") as f:
                f.write(bad)
            self.assertIsNone(st.read_request(p), bad)
        with open(p, "w") as f:
            json.dump({"scanner": "A", "request_id": "r1", "at": "2026-12-01T10:00:00Z"}, f)
        self.assertEqual(st.read_request(p)["request_id"], "r1")


# ---------------------------------------------------------------------------
class WorkerBase(unittest.TestCase):
    codes = ()
    v13 = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = os.path.join(self.tmp.name, "agent")
        self.mw_data = os.path.join(self.tmp.name, "mw_data")
        os.makedirs(os.path.join(self.config_dir, "scanner"))
        os.makedirs(self.mw_data)
        self.fake = FakeSrx(codes=self.codes, mac=MAC).start()
        self.addCleanup(self.fake.stop)
        self.features = st.ContractFeatures(contract_13(self.tmp.name) if self.v13 else CONTRACT)
        self.spool = FakeSpool()

    def worker(self, start=True, **kw):
        kw.setdefault("host", "127.0.0.1")
        kw.setdefault("port", self.fake.port)
        w = st.ScanWorker(cfg(**kw), self.spool, self.config_dir, self.mw_data, self.features)
        if start:
            w.start()
            self.addCleanup(w.stop)
        return w

    def connected(self, w):
        self.assertTrue(wait_for(lambda: w.status["state"] == "connected"), w.status)
        self.assertTrue(self.fake.wait_clients(1))

    def lock(self, since=None):
        p = st.lock_path(self.config_dir, "A")
        doc = {"owner": "millwright", "pid": os.getpid(),
               "since": since or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(p, "w") as f:
            json.dump(doc, f)
        return p


class ReadingTests(WorkerBase):
    def test_reads_become_scan_received_with_new_scan_ids(self):
        w = self.worker()
        self.connected(w)
        self.fake.push(b"PCB-0001")
        self.fake.push(b"]d1PCB-0002")
        self.assertTrue(wait_for(lambda: len(self.spool.of("scan.received")) == 2))
        scans = [(p, c) for t, p, c in self.spool.events if t == "scan.received"]
        self.assertEqual(scans[0][0], {"scanner_id": "A", "raw": "PCB-0001", "raw_encoding": "utf8",
                                       "framing": "cr"})
        self.assertEqual(scans[1][0]["symbology"], "]d1")
        self.assertNotEqual(scans[0][1]["scan_id"], scans[1][1]["scan_id"])
        self.assertEqual(w.status["last_read"]["code"], "PCB-0002")
        self.assertEqual(w.status["last_read"]["status"], "received")
        self.assertEqual(w.status["last_read"]["scan_id"], scans[1][1]["scan_id"])
        self.assertEqual(self.spool.states(), ["connected"])
        self.assertEqual(self.fake.received, ["KEYENCE", "EMAC"])     # listens only: no LON

    def test_no_read_before_contract_1_3_sends_no_event(self):
        w = self.worker()
        self.connected(w)
        self.fake.push(b"ERROR")
        self.assertTrue(wait_for(lambda: w.status["triggers"] == 1))
        self.assertEqual(self.spool.of("scan.rejected_local"), [])
        self.assertEqual(self.spool.of("scan.received"), [])
        self.assertEqual(w.status["last_read"]["reason"], "no_read")
        self.assertEqual(w.status["last_read"]["status"], "rejected")
        self.assertEqual(w.status["no_read_rate"], 100.0)

    def test_rejections(self):
        w = self.worker(max_len=10)
        self.connected(w)
        self.fake.push(b"x" * 11)
        self.fake.push(b"A\x07B")
        self.fake.push(b"y" * 5000)                  # over the client's 4096 limit
        self.assertTrue(wait_for(lambda: len(self.spool.of("scan.rejected_local")) == 3))
        r = self.spool.of("scan.rejected_local")
        self.assertEqual([(x["reason"], x["partial_len"]) for x in r],
                         [("too_long", 11), ("control_chars", 3), ("too_long", 5000)])
        self.assertEqual(self.spool.of("scan.received"), [])

    def test_rate_limit(self):
        w = self.worker()
        self.connected(w)
        for i in range(st.MAX_RATE + 5):
            w.handle_line(b"C%d" % i)
        self.assertEqual(len(self.spool.of("scan.received")), st.MAX_RATE)
        self.assertEqual(w.status["rate_limited"], 5)

    def test_spool_full(self):
        w = self.worker()
        self.connected(w)
        self.spool.full = True
        w.handle_line(b"PCB-FULL")
        self.assertEqual(self.spool.of("scan.received"), [])
        self.assertEqual(self.spool.of("scan.rejected_local"),
                         [{"scanner_id": "A", "reason": "spool_full", "partial_len": 8}])
        self.assertEqual(w.status["last_read"]["reason"], "spool_full")

    def test_drift_warning(self):
        w = self.worker(start=False, no_read_warn=5)
        for i in range(st.MIN_TRIGGERS_FOR_RATE - 1):
            w.handle_line(b"ERROR")
        self.assertFalse(w.status["retune_suggested"])          # too few triggers to judge
        w.handle_line(b"ERROR")
        self.assertTrue(w.status["retune_suggested"])
        for i in range(st.WINDOW):
            w._window.append(False)
        w.handle_line(b"OK1")
        self.assertFalse(w.status["retune_suggested"])
        for i in range(5):
            w.handle_line(b"ERROR")
        self.assertEqual(w.status["no_read_rate"], 5.0)          # 5 of the last 100: not above 5 %
        self.assertFalse(w.status["retune_suggested"])
        w.handle_line(b"ERROR")
        self.assertTrue(w.status["retune_suggested"])
        self.assertEqual(w.status["triggers"], st.WINDOW)


class Contract13Tests(WorkerBase):
    v13 = True

    def test_no_read_event_and_state_fields(self):
        w = self.worker()
        self.connected(w)
        self.fake.push(b"ERROR")
        self.assertTrue(wait_for(lambda: self.spool.of("scan.rejected_local")))
        self.assertEqual(self.spool.of("scan.rejected_local"),
                         [{"scanner_id": "A", "reason": "no_read", "partial_len": 0}])
        self.assertEqual(self.spool.of("scanner.state")[0],
                         {"scanner_id": "A", "state": "connected", "model": "SR-X100", "firmware": "1.00"})

    def test_setup_state(self):
        w = self.worker()
        self.connected(w)
        self.lock()
        self.assertTrue(wait_for(lambda: w.status["state"] == "setup"))
        self.assertEqual(self.spool.of("scanner.state")[-1]["state"], "setup")


class IdentityTests(WorkerBase):
    def test_replaced_unit_is_refused(self):
        w = self.worker(mac="0001FC0000FF")
        self.assertTrue(wait_for(lambda: w.status["state"] == "error"))
        self.assertEqual(w.status["detail"], "scanner replaced: run Scanner setup")
        self.fake.push(b"PCB")
        time.sleep(0.2)
        self.assertEqual(self.spool.of("scan.received"), [])
        self.assertTrue(self.fake.wait_clients(0))               # no socket kept open

    def test_not_enrolled(self):
        w = self.worker(mac="")
        self.assertTrue(wait_for(lambda: w.status["state"] == "error"))
        self.assertIn("--scanner-enroll", w.status["detail"])

    def test_mac_formats_compared_normalised(self):
        w = self.worker(mac="00:01:fc:00:00:01")
        self.connected(w)


class ConnectionTests(WorkerBase):
    def test_reader_away_then_back(self):
        w = self.worker()
        self.connected(w)
        self.fake.drop_all()                                     # reboot / cable pull
        self.assertTrue(wait_for(lambda: w.status["state"] == "disconnected"))
        self.assertTrue(wait_for(lambda: w.status["state"] == "connected", 4))   # backoff 1 s
        self.fake.push(b"AFTER")
        self.assertTrue(wait_for(lambda: self.spool.of("scan.received")))
        self.assertEqual(self.spool.states(), ["connected", "disconnected", "connected"])

    def test_no_reader_backoff_and_one_state_event(self):
        port = self.fake.port
        self.fake.stop()
        w = st.ScanWorker(cfg(host="127.0.0.1", port=port), self.spool, self.config_dir, None, self.features)
        w.start()
        self.addCleanup(w.stop)
        time.sleep(2.5)                                          # tries at 0, 1, 3 s
        self.assertEqual(self.spool.states(), ["disconnected"])
        self.assertEqual(w.status["state"], "disconnected")

    def test_stop_is_prompt(self):
        w = self.worker()
        self.connected(w)
        t = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertFalse(w.is_alive())
        self.assertTrue(self.fake.wait_clients(0))


class SetupLockTests(WorkerBase):
    def test_handover_within_1_s_and_back(self):
        w = self.worker()
        self.connected(w)
        p = self.lock()
        t = time.monotonic()
        self.assertTrue(self.fake.wait_clients(0, 1.0))
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertTrue(wait_for(lambda: w.status["state"] == "setup"))
        st_ev = self.spool.of("scanner.state")[-1]
        self.assertEqual(st_ev["state"], "disconnected")         # before contract 1.3
        self.assertTrue(st_ev["detail"].startswith("setup:"))
        os.remove(p)
        t = time.monotonic()
        self.assertTrue(wait_for(lambda: w.status["state"] == "connected", 5))
        self.assertLess(time.monotonic() - t, 5.0)

    def test_lock_present_at_start(self):
        self.lock()
        w = self.worker()
        self.assertTrue(wait_for(lambda: w.status["state"] == "setup"))
        time.sleep(0.3)
        self.assertEqual(self.fake.connections, 0)

    def test_stale_lock_removed(self):
        p = self.lock(since="2020-01-01T00:00:00Z")
        w = self.worker()
        self.connected(w)
        self.assertFalse(os.path.exists(p))


class AgentTriggerTests(WorkerBase):
    codes = ("TRIG-1",)

    def request(self, scanner="A", rid=None, at=None, **kw):
        doc = {"scanner": scanner, "request_id": rid or str(uuid.uuid4()),
               "at": at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        doc.update(kw)
        tmp = os.path.join(self.mw_data, "scan_request.json.tmp")
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, os.path.join(self.mw_data, "scan_request.json"))
        return doc

    def test_request_sends_lon_reads_loff(self):
        w = self.worker(trigger="agent")
        self.connected(w)
        self.request()
        self.assertTrue(wait_for(lambda: self.spool.of("scan.received")))
        self.assertEqual(self.spool.of("scan.received")[0]["raw"], "TRIG-1")
        self.assertTrue(wait_for(lambda: "LOFF" in self.fake.received))
        self.assertEqual(self.fake.received, ["KEYENCE", "EMAC", "LON", "LOFF"])

    def test_no_read_on_request(self):
        self.fake.codes.clear()
        w = self.worker(trigger="agent")
        self.connected(w)
        self.request()
        self.assertTrue(wait_for(lambda: w.status["triggers"] == 1))
        self.assertEqual(w.status["last_read"]["reason"], "no_read")

    def test_ignored_requests(self):
        self.request(rid="before-start")                        # there before the agent started
        w = self.worker(trigger="agent")
        self.connected(w)
        self.request(scanner="B")
        self.request(at="2020-01-01T00:00:00Z")                 # too old
        time.sleep(0.6)
        self.assertNotIn("LON", self.fake.received)

    def test_reader_trigger_ignores_requests(self):
        w = self.worker(trigger="reader")
        self.connected(w)
        self.request()
        time.sleep(0.6)
        self.assertNotIn("LON", self.fake.received)

    def test_same_request_fires_once(self):
        self.fake.codes.extend(["TRIG-2"])
        w = self.worker(trigger="agent")
        self.connected(w)
        doc = self.request()
        self.assertTrue(wait_for(lambda: "LOFF" in self.fake.received))
        self.request(rid=doc["request_id"])
        time.sleep(0.6)
        self.assertEqual(self.fake.received.count("LON"), 1)


class RealSpoolTests(WorkerBase):
    """Every event the worker makes passes the vendored contract (the spool
    validates before storing)."""

    def test_events_are_valid_and_durable(self):
        path = os.path.join(self.config_dir, "mes_spool.db")
        spool = ms.Spool(path, MACHINE, "4f1c2a9e7b3d5c60", "1.9.0")
        self.addCleanup(spool.close)
        self.spool = spool
        w = self.worker(max_len=50)
        self.assertTrue(wait_for(lambda: w.status["state"] == "connected"))
        for line in (b"PCB-1", b"]d2\x1d0104012345678901", b"\xff", b"x" * 51, b"A\x07", b"ERROR"):
            self.fake.push(line)
        self.assertTrue(wait_for(lambda: w.status["triggers"] == 6))
        self.lock()
        self.assertTrue(wait_for(lambda: w.status["state"] == "setup"))
        with sqlite3.connect(path) as db:
            rows = [(t, json.loads(b)) for t, b in db.execute("SELECT type, body FROM spool ORDER BY seq")]
        types = [t for t, _ in rows]
        self.assertEqual(types, ["scanner.state", "scan.received", "scan.received", "scan.received",
                                 "scan.rejected_local", "scan.rejected_local", "scanner.state"])
        for _, e in rows:
            self.assertEqual(ms.contract().validate_event(e), [])
        scans = [e for t, e in rows if t == "scan.received"]
        self.assertTrue(all(e["correlation"]["scan_id"] for e in scans))
        self.assertEqual(scans[1]["payload"]["raw_encoding"], "base64")


class StatusDocTests(WorkerBase):
    def test_mes_status_scanners_and_last_scan(self):
        w = self.worker()
        self.connected(w)
        self.fake.push(b"PCB-9")
        self.assertTrue(wait_for(lambda: w.status["last_read"]))
        doc = mst.build(MACHINE, None, None, None, scan=st.last_scan([w]), scanners=[w.status])
        self.assertEqual(doc["scan"]["code"], "PCB-9")
        self.assertEqual(set(doc["scan"]), {"code", "status", "reason", "at_ms"})
        self.assertEqual(doc["scanners"][0]["id"], "A")
        self.assertEqual(doc["scanners"][0]["state"], "connected")
        lines = mst.format_lines(dict(doc, updated_at_ms=int(time.time() * 1000)), 0, "")
        self.assertIn("  scanner A: connected, no-read 0.0% of 1  [green]", lines)
        self.assertEqual(mst.build(MACHINE, None, None, None)["scanners"], [])
        self.assertIsNone(st.last_scan([]))

    def test_retune_line(self):
        doc = mst.build(MACHINE, None, None, None, scanners=[
            {"id": "A", "state": "error", "detail": "scanner replaced: run Scanner setup",
             "no_read_rate": None, "retune_suggested": True}])
        lines = mst.format_lines(dict(doc, updated_at_ms=int(time.time() * 1000)), 0, "")
        self.assertIn("  scanner A: error — scanner replaced: run Scanner setup  [red]", lines)
        self.assertIn("  scanner A: needs tuning (run Scanner setup)  [amber]", lines)


class StartWorkersTests(unittest.TestCase):
    def test_nothing_configured(self):
        self.assertEqual(st.start_workers({}, FakeSpool(), "/tmp", None, CONTRACT), [])

    def test_raw_config_keeps_scanner_keys(self):
        import status
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "config.yaml")
            with open(p, "w") as f:
                f.write('scanners: "A"\nscanner_A_mode: "tcp"\nscanner_A_port: 9100\n'
                        'scanner_A_mac: "0001FC000001"  # from --scanner-enroll\n')
            raw = status.load_raw_config(p)
            self.assertEqual(status.load_config(p)["scanners"], "A")
            self.assertEqual(status.load_raw_config(os.path.join(tmp, "none.yaml")), {})
        cfgs, problems = st.scanner_configs(raw)
        self.assertEqual((problems, [(c.name, c.port, c.mac) for c in cfgs]), ([], [("A", 9100, MAC)]))


if __name__ == "__main__":
    unittest.main()
