"""Scanner commands of lcnc-status-agent (agent_scanner_cli.py),
docs/SCANNER_KEYENCE_SRX_WORK.md package 3, against tools/fake_srx.py.
Run: python -m unittest -v test_scanner_cli
"""
import io
import json
import os
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
sys.path.insert(0, HERE)

import agent_mes_status as mst  # noqa: E402
import agent_scan_tcp as st  # noqa: E402
import agent_scanner_cli as cli  # noqa: E402
from fake_srx import FakeSrx  # noqa: E402
from test_scan_tcp import FakeSpool, wait_for, CONTRACT  # noqa: E402

MAC = "0001FC000001"


class Base(unittest.TestCase):
    codes = ("PCB-1", "PCB-2", "PCB-3")
    enrolled = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = os.path.join(self.tmp.name, "agent")
        os.makedirs(self.config_dir)
        self.fake = FakeSrx(codes=self.codes, mac=MAC).start()
        self.addCleanup(self.fake.stop)
        self.fake.tune_s = 0.05
        self.cfg_path = os.path.join(self.config_dir, "config.yaml")
        lines = ['machine_name: "M1"']
        if self.enrolled:
            lines += ['scanners: "A"', 'scanner_A_mode: "tcp"', 'scanner_A_host: "127.0.0.1"',
                      'scanner_A_port: "%d"' % self.fake.port, 'scanner_A_mac: "%s"' % MAC]
        with open(self.cfg_path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def ctx(self):
        import status
        return cli.Context(self.cfg_path, self.config_dir, status.load_raw_config(self.cfg_path),
                           sleep=lambda s: time.sleep(min(s, 0.05)))

    def config(self):
        import status
        return status.load_raw_config(self.cfg_path)

    def lock_file(self):
        return st.lock_path(self.config_dir, "A")


class HelperTests(unittest.TestCase):
    def test_code_mask_round_trip(self):
        self.assertEqual(cli.code_mask(["qr", "datamatrix"]), "3")
        self.assertEqual(cli.code_mask(["code128"]), "400")
        self.assertEqual(cli.code_types("11FFFF")[:3], ["qr", "datamatrix", "pdf417"])
        self.assertEqual(cli.code_types("3"), ["qr", "datamatrix"])
        with self.assertRaises(cli.CliError):
            cli.code_mask(["qr", "nope"])
        with self.assertRaises(cli.CliError):
            cli.code_mask([])

    def test_to_hex(self):
        self.assertEqual(cli.to_hex("ERROR"), "4552524F52")

    def test_expected_output(self):
        c = st.ScannerConfig("A", terminator="crlf", no_read="NG")
        exp = {(k, n): v for k, n, v, _ in cli.expected_output(c)}
        self.assertEqual(exp[("N", "005")], "FF")
        self.assertEqual(exp[("N", "006")], "0D0A")
        self.assertEqual(exp[("P", "302")], "1")
        self.assertEqual(exp[("P", "205")], "4E47")


class EnrollTests(Base):
    enrolled = False

    def test_enroll_writes_the_keys(self):
        doc = cli.enroll(self.ctx(), "tcp://127.0.0.1:%d" % self.fake.port, "A")
        self.assertEqual((doc["model"], doc["mac"], doc["firmware"], doc["serial"]), ("SR-X100", MAC, "1.00", None))
        raw = self.config()
        self.assertEqual(raw["scanners"], "A")
        self.assertEqual((raw["scanner_A_mode"], raw["scanner_A_host"], raw["scanner_A_port"],
                          raw["scanner_A_mac"], raw["scanner_A_model"]),
                         ("tcp", "127.0.0.1", str(self.fake.port), MAC, "SR-X100"))
        self.assertEqual(raw["machine_name"], "M1")                       # the rest untouched
        cfgs, problems = st.scanner_configs(raw)
        self.assertEqual((problems, cfgs[0].port), ([], self.fake.port))
        self.assertFalse(os.path.exists(self.lock_file()))                 # lock released

    def test_second_scanner_appended(self):
        with open(self.cfg_path, "a") as f:
            f.write('scanners: "B"\n')
        cli.enroll(self.ctx(), "tcp://127.0.0.1:%d" % self.fake.port, "A")
        self.assertEqual(self.config()["scanners"], "B,A")

    def test_not_an_srx_refused_unless_forced(self):
        self.fake.model = "SR-2000"
        with self.assertRaises(cli.CliError) as e:
            cli.enroll(self.ctx(), "tcp://127.0.0.1:%d" % self.fake.port, "A")
        self.assertIn("--force", str(e.exception))
        self.assertNotIn("scanners", self.config())
        self.assertEqual(cli.enroll(self.ctx(), "tcp://127.0.0.1:%d" % self.fake.port, "A", force=True)["model"],
                         "SR-2000")

    def test_bad_name_and_url(self):
        for name in ("Scanner A", "", "a-b"):
            with self.assertRaises(cli.CliError):
                cli.enroll(self.ctx(), "tcp://127.0.0.1:9004", name)
        with self.assertRaises(cli.CliError):
            cli.enroll(self.ctx(), "tcp://127.0.0.1:x", "A")

    def test_unreachable(self):
        self.fake.stop()
        with self.assertRaises(Exception):
            cli.enroll(self.ctx(), "tcp://127.0.0.1:%d" % self.fake.port, "A")
        self.assertNotIn("scanners", self.config())
        self.assertFalse(os.path.exists(self.lock_file()))


class LockTests(Base):
    def test_lock_atomic_and_exclusive(self):
        a = cli.SetupLock(self.config_dir, "A", "millwright", os.getpid())
        self.assertTrue(a.acquire())
        self.assertFalse(a.acquire())                                      # already mine
        doc = cli.read_lock(self.lock_file())
        self.assertEqual((doc["owner"], doc["pid"]), ("millwright", os.getpid()))
        b = cli.SetupLock(self.config_dir, "A", "cli", os.getppid())
        with self.assertRaises(cli.CliError):
            b.acquire()
        b.release()                                                        # not b's: stays
        self.assertTrue(os.path.exists(self.lock_file()))
        a.release()
        self.assertFalse(os.path.exists(self.lock_file()))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.lock_file())) if "tmp" in n], [])

    def test_dead_holder_and_stale_and_damaged_replaced(self):
        os.makedirs(os.path.dirname(self.lock_file()), exist_ok=True)
        for doc in ({"owner": "millwright", "pid": 999999999, "since": cli.utc_now_text()},
                    {"owner": "cli", "pid": os.getppid(), "since": "2020-01-01T00:00:00Z"}, "junk"):
            with open(self.lock_file(), "w") as f:
                f.write(doc if isinstance(doc, str) else json.dumps(doc))
            lock = cli.SetupLock(self.config_dir, "A")
            self.assertTrue(lock.acquire(), doc)
            lock.release()

    def test_steps_under_millwrights_lock(self):
        ctx = self.ctx()
        pid = os.getppid()                                                 # a live process that isn't us
        cli.step(ctx, "A", "lock", [], "millwright", pid)
        with self.assertRaises(cli.CliError):
            cli.step(ctx, "A", "identify", [])                              # someone else: refused
        r = cli.step(ctx, "A", "identify", [], "millwright", pid)
        self.assertEqual(r["mac"], MAC)
        self.assertTrue(os.path.exists(self.lock_file()))                  # kept between steps
        self.assertTrue(cli.step(ctx, "A", "unlock", [], "millwright", pid)["released"])
        self.assertFalse(os.path.exists(self.lock_file()))


class HandoverTests(Base):
    """A running ScanWorker holds the reader (which refuses a second client):
    the CLI's lock makes the worker let go, and it comes back afterwards."""

    def test_agent_hands_over_and_takes_back(self):
        self.fake.second_client = "refuse"
        w = st.ScanWorker(st.scanner_configs(self.config())[0][0], FakeSpool(), self.config_dir, None,
                          st.ContractFeatures(CONTRACT))
        w.start()
        self.addCleanup(w.stop)
        writer = mst.StatusWriter(mst.status_path(self.config_dir),
                                  lambda: mst.build("m", None, None, None, scanners=[w.status]),
                                  min_interval=0.05)
        writer.start()
        self.addCleanup(writer.stop)
        self.assertTrue(wait_for(lambda: w.status["state"] == "connected"))
        self.assertTrue(wait_for(lambda: (mst.read(mst.status_path(self.config_dir))[0] or {})
                                 .get("scanners", [{}])[0].get("state") == "connected"))
        r = cli.step(self.ctx(), "A", "identify", [])
        self.assertEqual(r["model"], "SR-X100")
        self.assertTrue(wait_for(lambda: w.status["state"] == "connected", 5))


class CommandTests(Base):
    def test_info(self):
        doc = cli.info_cmd(self.ctx(), "A")
        self.assertEqual((doc["host"], doc["mac"], doc["enrolled"], doc["calibration"], doc["agent"]),
                         ("127.0.0.1", MAC, True, None, "unknown"))
        with self.assertRaises(cli.CliError):
            cli.info_cmd(self.ctx(), "B")

    def test_not_enrolled_and_replaced(self):
        with open(self.cfg_path, "a") as f:
            f.write('scanner_A_mac: "0001FC0000FF"\n')
        with self.assertRaises(cli.CliError) as e:
            cli.step(self.ctx(), "A", "identify", [])
        self.assertIn("scanner replaced", str(e.exception))
        self.assertFalse(os.path.exists(self.lock_file()))
        with open(self.cfg_path, "a") as f:
            f.write('scanner_A_mac: ""\n')
        with self.assertRaises(cli.CliError) as e:
            cli.step(self.ctx(), "A", "identify", [])
        self.assertIn("not enrolled", str(e.exception))

    def test_scanner_test_reader_trigger(self):
        out = []
        t = threading.Timer(0.3, lambda: [self.fake.push(x) for x in (b"]d1R1", b"ERROR", b"A\x07")])
        t.start()
        for line in cli.test_cmd(self.ctx(), "A", 3):
            out.append(line)
        self.assertEqual([o.get("result") for o in out[:3]], ["scan", "no_read", "reject"])
        self.assertEqual(out[0]["code"], "R1")
        self.assertEqual(out[0]["stored_as"]["symbology"], "]d1")
        self.assertEqual(out[-1], {"type": "summary", "reads": 1, "no_reads": 1, "rejected": 1,
                                   "no_read_rate": 33.3})
        self.assertNotIn("LON", self.fake.received)

    def test_scanner_test_agent_trigger(self):
        with open(self.cfg_path, "a") as f:
            f.write('scanner_A_trigger: "agent"\n')
        out = list(cli.test_cmd(self.ctx(), "A", 4))
        self.assertEqual([o.get("code") for o in out[:3]], ["PCB-1", "PCB-2", "PCB-3"])
        self.assertEqual(out[3]["result"], "no_read")
        self.assertEqual(self.fake.received.count("LON"), 4)
        self.assertTrue(wait_for(lambda: self.fake.received.count("LOFF") == 4))   # the fake logs in its thread

    def test_backup_and_restore(self):
        ctx = self.ctx()
        self.fake.settings["RB"]["01300"] = "3"
        b = cli.backup(ctx, "A")
        self.assertEqual(b["unread"], [])
        with open(b["file"]) as f:
            doc = json.load(f)
        self.assertEqual((doc["mac"], doc["settings"]["B,01300"], doc["settings"]["N,200"]),
                         (MAC, "3", "192.168.100.100"))
        self.assertEqual(os.path.dirname(b["file"]), os.path.join(self.config_dir, "scanner", "A"))
        self.fake.settings["RB"]["01300"] = "11FFFF"
        self.fake.settings["RN"]["200"] = "10.9.9.9"
        r = cli.restore(ctx, "A", b["file"])
        self.assertEqual(self.fake.saved["RB"]["01300"], "3")              # written and SAVEd
        self.assertEqual(self.fake.saved["RN"]["200"], "10.9.9.9")         # network kept
        self.assertIn("N,200", r["skipped"])
        cli.restore(ctx, "A", b["file"], with_network=True)
        self.assertEqual(self.fake.saved["RN"]["200"], "192.168.100.100")

    def test_restore_refuses_other_files(self):
        p = os.path.join(self.tmp.name, "x.json")
        for content in ("[]", '{"schema": "other"}', "nope"):
            with open(p, "w") as f:
                f.write(content)
            with self.assertRaises(cli.CliError):
                cli.restore(self.ctx(), "A", p)
        self.assertNotIn("SAVE", self.fake.received)

    def test_restore_writes_only_known_numbers(self):
        p = os.path.join(self.tmp.name, "b.json")
        with open(p, "w") as f:
            json.dump({"schema": "lcnc-agent.scanner_settings/1",
                       "settings": {"P,302": "1", "P,999": "x", "N,006": None, "Q,1": "2"}}, f)
        r = cli.restore(self.ctx(), "A", p)
        self.assertEqual((r["written"], sorted(r["skipped"])), (1, ["N,006", "P,999", "Q,1"]))


class StepTests(Base):
    def test_network(self):
        ctx = self.ctx()
        self.assertEqual(cli.step(ctx, "A", "network", [])["ip"], "192.168.100.100")
        r = cli.step(ctx, "A", "network-set", ["10.0.0.50", "24", "10.0.0.1"])
        self.assertTrue(r["saved"])
        self.assertEqual(self.fake.saved["RN"]["200"], "10.0.0.50")
        self.assertEqual(self.config()["scanner_A_host"], "10.0.0.50")
        for bad in (["10.0.0.256", "24", "0.0.0.0"], ["10.0.0.5", "31", "0.0.0.0"], ["x"]):
            with self.assertRaises(cli.CliError):
                cli.step(ctx, "A", "network-set", bad)

    def test_time_sync(self):
        r = cli.step(self.ctx(), "A", "time-sync", ["10.0.0.1"], wall=lambda: 1795000000.0)
        self.assertEqual(self.fake.clock, time.strftime("%Y%m%d%H%M%S", time.gmtime(1795000000)))
        self.assertEqual((r["sntp"], self.fake.settings["RN"]["520"]), ("10.0.0.1", "10.0.0.1"))

    def test_focus(self):
        r = cli.step(self.ctx(), "A", "focus", [])
        self.assertEqual((r["succeeded"], r["position"]), (True, None))
        self.fake.ftune_result = "Focus Tuning FAILED"
        self.assertFalse(cli.step(self.ctx(), "A", "focus", [])["succeeded"])

    def test_codes_and_bank_name(self):
        ctx = self.ctx()
        self.assertEqual(cli.step(ctx, "A", "codes", ["1"])["types"][:2], ["qr", "datamatrix"])
        r = cli.step(ctx, "A", "codes-set", ["2", "datamatrix,qr"])
        self.assertEqual((r["mask"], self.fake.settings["RB"]["02300"]), ("3", "3"))
        self.assertNotEqual(self.fake.saved["RB"]["02300"], "3")           # not in flash before save
        cli.step(ctx, "A", "bank-name", ["2", "PCB DM small"])
        self.assertEqual(self.fake.settings["RB"]["02625"], "PCB DM small")
        for args in (["17", "qr"], ["1", "nope"]):
            with self.assertRaises(cli.CliError):
                cli.step(ctx, "A", "codes-set", args)
        with self.assertRaises(cli.CliError):
            cli.step(ctx, "A", "bank-name", ["1", "a,b"])

    def test_tune(self):
        r = cli.step(self.ctx(), "A", "tune", ["1"])
        self.assertEqual((r["succeeded"], r["ms"], r["advice"], r["reason"], r["exposure"], r["gain"]),
                         (True, 850, None, None, "0010", "10"))
        self.fake.tune_result = "Tuning FAILED,1200ms,00000201"
        r = cli.step(self.ctx(), "A", "tune", ["3"])
        self.assertEqual((r["succeeded"], r["advice"], r["reason"]),
                         (False, "check installation, lighting and printing", "no code found"))
        self.assertIn("TUNE,03", self.fake.received)

    def test_rate_test(self):
        r = cli.step(self.ctx(), "A", "rate-test", ["30"])
        self.assertEqual((r["triggers"], r["reading_rate"], r["matching_level"], r["matching_threshold"],
                          r["passed"]), (30, 100.0, 92, 70, True))
        self.fake.test_line = "ABC:01:97%:92"
        self.assertFalse(cli.step(self.ctx(), "A", "rate-test", ["10"])["passed"])
        self.fake.test_line = "ABC:01:100%:60"
        r = cli.step(self.ctx(), "A", "rate-test", ["10", "2"])
        self.assertFalse(r["passed"])
        self.assertTrue(r["hints"])
        self.assertIn("TEST1,02", self.fake.received)
        self.assertTrue(wait_for(lambda: self.fake.received.count("QUIT") == 3))

    def test_output_check_and_fix(self):
        ctx = self.ctx()
        r = cli.step(ctx, "A", "output-check", [])
        self.assertFalse(r["ok"])
        self.assertEqual([(d["kind"], d["number"], d["want"]) for d in r["differences"]], [("P", "302", "1")])
        cli.step(ctx, "A", "output-fix", [])
        self.assertTrue(cli.step(ctx, "A", "output-check", [])["ok"])

    def test_trigger_test(self):
        r = cli.step(self.ctx(), "A", "trigger-test", [])
        self.assertEqual((r["result"], r["stored_as"]["raw"]), ("scan", "PCB-1"))

    def test_save_writes_calibration_and_keeps_history(self):
        ctx = self.ctx()
        cli.step(ctx, "A", "codes-set", ["1", "datamatrix"])
        answers = {"by": None, "distance_mm": 120, "angle_deg": 12, "focus": {"mode": "fixed", "position": None},
                   "symbologies": ["datamatrix"], "banks": [{"bank": 1, "name": "PCB DM small"}],
                   "test": {"triggers": 100, "reading_rate": 100.0, "matching_level": 92, "read_ms": None,
                            "passed": True}}
        p = os.path.join(self.tmp.name, "answers.json")
        with open(p, "w") as f:
            json.dump(answers, f)
        r = cli.step(ctx, "A", "save", [p])
        self.assertEqual(self.fake.saved["RB"]["01300"], "2")              # SAVEd to flash
        with open(r["calibration"]) as f:
            doc = json.load(f)
        self.assertEqual(set(doc), {"scanner", "model", "serial", "firmware", "at", "by", "distance_mm",
                                    "angle_deg", "focus", "symbologies", "banks", "test", "passed", "backup"})
        self.assertEqual((doc["model"], doc["serial"], doc["passed"], doc["distance_mm"]),
                         ("SR-X100", None, True, 120))
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(r["calibration"]), doc["backup"])))
        time.sleep(1.1)
        answers["test"]["passed"] = False
        with open(p, "w") as f:
            json.dump(answers, f)
        r2 = cli.step(ctx, "A", "save", [p])
        self.assertFalse(r2["passed"])
        self.assertEqual(len(cli.calibration_history(ctx, "A")), 1)
        self.assertEqual(cli.info_cmd(ctx, "A")["calibration"]["passed"], False)

    def test_factory_reset_needs_two_confirms(self):
        ctx = self.ctx()
        for n in (0, 1):
            with self.assertRaises(cli.CliError):
                cli.step(ctx, "A", "factory-reset", [], confirm=n)
        self.assertNotIn("DFLT", self.fake.received)
        self.assertTrue(cli.step(ctx, "A", "factory-reset", [], confirm=2)["reset"])
        self.assertTrue(wait_for(lambda: self.fake.received[-2:] == ["DFLT", "SAVE"]))

    def test_image(self):
        with mock.patch.object(cli.srx.SrxClient, "list_images", return_value=["IMG_002.bmp", "IMG_001.bmp"]), \
                mock.patch.object(cli.srx.SrxClient, "get_image", side_effect=lambda n, d: os.path.join(d, n)):
            r = cli.step(self.ctx(), "A", "image", [])
        self.assertTrue(r["image"].endswith("images/IMG_002.bmp"))

    def test_unknown_step(self):
        with self.assertRaises(cli.CliError):
            cli.step(self.ctx(), "A", "firmware-update", [])
        self.assertFalse(os.path.exists(self.lock_file()))


class CalibrateWizardTests(Base):
    def test_whole_wizard(self):
        answers = iter(["y",                 # 2 clock
                        "120", "12",         # 3 mounting
                        "y",                 # 4 focus
                        "datamatrix,qr",     # 5 codes
                        "1", "PCB DM", "",   # 6 tune bank 1, name, done
                        "20",                # 7 rate test
                        "y",                 # 8 fix output
                        "Asha",              # 10 name
                        "y"])                # save
        said = []
        doc = cli.calibrate(self.ctx(), "A", ask=lambda q: next(answers), say=said.append)
        self.assertTrue(doc["saved"])
        self.assertTrue(doc["passed"])
        with open(doc["calibration"]) as f:
            c = json.load(f)
        self.assertEqual((c["by"], c["distance_mm"], c["angle_deg"], c["symbologies"], c["banks"]),
                         ("Asha", 120, 12, ["qr", "datamatrix"], [{"bank": 1, "name": "PCB DM"}]))
        self.assertEqual(c["test"]["reading_rate"], 100.0)
        self.assertEqual(self.fake.saved["RP"]["302"], "1")                # output fixed and saved
        self.assertFalse(os.path.exists(self.lock_file()))
        self.assertTrue(any("PASS" in s for s in said))

    def test_not_saved(self):
        answers = iter(["n", "", "", "n", "", "", "10", "n", "", "n"])
        doc = cli.calibrate(self.ctx(), "A", ask=lambda q: next(answers), say=lambda s: None)
        self.assertFalse(doc["saved"])
        self.assertNotIn("SAVE", self.fake.received)
        self.assertFalse(os.path.exists(self.lock_file()))


class StatusEntryTests(Base):
    """lcnc-status-agent --scanner-* through status.py."""

    def run_cli(self, *argv):
        import status
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["lcnc-status-agent", "--config", self.cfg_path] + list(argv)), \
                mock.patch.object(status, "USER_CONFIG_DIR", self.config_dir), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            rc = status._run_scanner(status._parse_args(), self.cfg_path)
        return rc, out.getvalue()

    def test_json_outputs(self):
        rc, out = self.run_cli("--scanner-info", "A", "--json")
        self.assertEqual((rc, json.loads(out)["mac"]), (0, MAC))
        rc, out = self.run_cli("--scanner-setup", "A", "identify", "--json")
        self.assertEqual((rc, json.loads(out)["model"]), (0, "SR-X100"))
        rc, out = self.run_cli("--scanner-setup", "A", "nope", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("unknown step", json.loads(out)["error"])
        rc, out = self.run_cli("--scanner-setup", "A", "factory-reset", "--confirm", "--confirm", "--json")
        self.assertEqual((rc, json.loads(out)["reset"]), (0, True))

    def test_test_streams_json_lines(self):
        with open(self.cfg_path, "a") as f:
            f.write('scanner_A_trigger: "agent"\n')
        rc, out = self.run_cli("--scanner-test", "A", "--count", "2", "--json")
        lines = [json.loads(x) for x in out.splitlines()]
        self.assertEqual((rc, [x["type"] for x in lines]), (0, ["read", "read", "summary"]))

    def test_no_scanner_command(self):
        import status
        with mock.patch.object(sys, "argv", ["lcnc-status-agent"]):
            self.assertIsNone(status._run_scanner(status._parse_args(), None))

    def test_unreachable_reader_is_a_message(self):
        self.fake.stop()
        rc, out = self.run_cli("--scanner-info", "A", "--json")
        self.assertEqual(rc, 0)                                             # info reads files only
        rc, out = self.run_cli("--scanner-backup", "A", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("cannot connect", json.loads(out)["error"])


if __name__ == "__main__":
    unittest.main()
