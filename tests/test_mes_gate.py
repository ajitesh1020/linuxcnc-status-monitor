"""Cycle gate verdict for MillWright (agent_mes_gate.py, MES plan P5.5): the feedback
poll in the forwarder and mw_data/cycle_gate.json (contract: MillWright
docs/MES_CYCLE_GATE.md).
Run: python -m unittest -v test_mes_gate

GateFileAcceptedByMillWright reads the written file with MillWright's own parser
(panels/mw_gate.py) and is skipped when MillWright is not there: set
MILLWRIGHT_DIR (default ~/millwright).
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_mes_forward as mf  # noqa: E402
import agent_mes_gate as mg  # noqa: E402
from test_mes_forward import FakeService, ForwarderBase  # noqa: E402
from test_mes_spool import MACHINE  # noqa: E402

GATE = {"policy": "advisory", "validated": False, "reasons": ["nothing_bound"], "order": "WO-1042"}


def feedback(gate=None, next_since=0, **kw):
    doc = {"machine_id": MACHINE, "now_ms": 1_800_000_000_000, "since": 0, "next_since": next_since,
           "scans": [], "bound": [], "order": "WO-1042"}
    if gate is not False:
        doc["cycle_gate"] = dict(GATE) if gate is None else gate
    doc.update(kw)
    return doc


class GateOfTests(unittest.TestCase):
    def test_valid_verdicts_pass_unchanged(self):
        for g in (GATE, {"policy": "enforce", "validated": True, "reasons": [], "order": None},
                  {"policy": "off", "validated": False, "reasons": ["no_order", "nothing_bound"], "order": 12},
                  dict(GATE, extra="kept")):
            self.assertIs(mg.gate_of({"cycle_gate": g}), g)

    def test_anything_else_is_not_written(self):
        bad = [None, [], "x", {"policy": "strict", "validated": False, "reasons": ["a"], "order": None},
               dict(GATE, validated="no"), dict(GATE, reasons="no_order"), dict(GATE, reasons=[3]),
               dict(GATE, reasons=[""]), dict(GATE, reasons=["x" * 65]), dict(GATE, reasons=["a"] * 33),
               dict(GATE, validated=True), dict(GATE, validated=False, reasons=[]),
               dict(GATE, order=["WO"]), dict(GATE, order=True), dict(GATE, order="x" * 129)]
        for g in bad:
            self.assertIsNone(mg.gate_of({"cycle_gate": g}), g)
        self.assertIsNone(mg.gate_of({}))
        self.assertIsNone(mg.gate_of(None))


class GateWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mw = self.tmp.name
        self.path = mg.gate_path(self.mw)
        self.w = mg.GateWriter(self.mw, MACHINE, wall=lambda: 1_800_000_000.5)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_writes_the_contract_document(self):
        self.assertTrue(self.w(feedback(), 1_800_000_000_123))
        self.assertEqual(self.read(), {"schema": "lcnc-agent.cycle_gate/1", "fetched_at_ms": 1_800_000_000_123,
                                       "written_at_ms": 1_800_000_000_500, "machine_id": MACHINE,
                                       "cycle_gate": GATE})
        self.assertEqual([n for n in os.listdir(self.mw)], [mg.FILE_NAME])   # no temp file left

    def test_rewritten_every_time_so_the_age_moves(self):
        self.w(feedback(), 1_000)
        self.w(feedback(), 2_000)
        self.assertEqual(self.read()["fetched_at_ms"], 2_000)
        self.assertEqual(self.w.written, 2)

    def test_untouched_without_or_with_a_bad_cycle_gate(self):
        self.w(feedback(), 1_000)
        with self.assertLogs("agent_mes_gate", "INFO") as logs:
            self.assertFalse(self.w(feedback(gate=False), 2_000))      # an older service
            self.assertFalse(self.w(feedback(gate=False), 3_000))      # logged once
            self.assertFalse(self.w(feedback(gate={"policy": "x"}), 4_000))
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(self.read()["fetched_at_ms"], 1_000)

    def test_no_millwright_no_file(self):
        self.assertFalse(mg.GateWriter("", MACHINE)(feedback(), 1_000))

    def test_write_error_is_logged_and_leaves_the_old_file(self):
        self.w(feedback(), 1_000)
        with mock.patch.object(mg.os, "replace", side_effect=OSError("disk full")), \
                self.assertLogs("agent_mes_gate", "ERROR"):
            self.assertFalse(self.w(feedback(), 2_000))
        self.assertEqual(self.read()["fetched_at_ms"], 1_000)
        self.assertEqual(os.listdir(self.mw), [mg.FILE_NAME])           # temp file removed

    def test_fsync_before_rename(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(mg.os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
                mock.patch.object(mg.os, "replace",
                                  side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
            self.w(feedback(), 1_000)
        self.assertEqual(order, ["fsync", "replace"])


class FeedbackService(FakeService):
    """FakeService plus the feedback endpoint."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.feedback = feedback(next_since=5)
        self.feedback_status = 200

    def request(self, method, path, body=None, headers=None):
        if method == "GET" and path.startswith("/v1/agents/%s/feedback" % MACHINE) and not self.fail:
            self.calls.append((method, path))
            if self.feedback_status != 200:
                return self.feedback_status, b'{"error": "x"}'
            return 200, json.dumps(self.feedback).encode()
        return super().request(method, path, body, headers)


class ForwarderFeedbackTests(ForwarderBase):
    def setUp(self):
        super().setUp()
        self.svc = FeedbackService()
        self.got = []

    def fwd(self, on_feedback=None):
        return mf.Forwarder(self.spool, "https://127.0.0.1:8443", self.dir, MACHINE,
                            transport_factory=lambda u, d: self.svc,
                            wall=lambda: self.wall[0], clock=lambda: self.mono[0], rand=lambda: 1.0,
                            on_feedback=on_feedback or (lambda doc, ms: self.got.append((doc, ms))))

    def fb_calls(self):
        return [p for m, p in self.svc.calls if "/feedback" in p]

    def test_polls_every_15_s_and_follows_next_since(self):
        self.enrol_files()
        fw = self.fwd()
        fw.step()
        self.assertEqual(self.fb_calls(), ["/v1/agents/%s/feedback?since=0" % MACHINE])
        self.assertEqual(self.got[0][1], 1_800_000_000_000)
        self.assertEqual(self.got[0][0]["cycle_gate"], GATE)
        self.mono[0] += 14
        fw.step()
        self.assertEqual(len(self.fb_calls()), 1)
        self.mono[0] += 1
        self.wall[0] += 15
        fw.step()
        self.assertEqual(self.fb_calls()[-1], "/v1/agents/%s/feedback?since=5" % MACHINE)
        self.assertEqual(self.got[-1][1], 1_800_000_015_000)
        self.assertEqual(fw.status["state"], "ok")

    def test_never_asked_without_a_callback(self):
        self.enrol_files()
        fw = mf.Forwarder(self.spool, "https://127.0.0.1:8443", self.dir, MACHINE,
                          transport_factory=lambda u, d: self.svc, clock=lambda: self.mono[0])
        fw.step()
        self.assertEqual(self.fb_calls(), [])

    def test_404_asks_again_hourly_and_forwarding_goes_on(self):
        self.enrol_files()
        self.add_cycles(2)
        self.svc.feedback_status = 404
        fw = self.fwd()
        with self.assertLogs("agent_mes_forward", "WARNING"):
            fw.step()
        self.assertEqual(set(self.states().values()), {"acked"})          # events still sent
        self.assertEqual(self.got, [])
        self.mono[0] += 600
        fw.step()
        self.assertEqual(len(self.fb_calls()), 1)
        self.mono[0] += mf.FEEDBACK_MISSING_S
        fw.step()
        self.assertEqual(len(self.fb_calls()), 2)

    def test_errors_and_garbage_reach_no_callback(self):
        self.enrol_files()
        fw = self.fwd()
        self.svc.feedback_status = 500
        fw.step()
        self.svc.feedback_status = 200
        self.svc.feedback = ["not", "an", "object"]
        self.mono[0] += 15
        fw.step()
        self.assertEqual(self.got, [])
        self.assertEqual(fw._fb_since, 0)

    def test_network_failure_leaves_the_file_and_sets_offline(self):
        self.enrol_files()
        fw = self.fwd(on_feedback=mg.GateWriter(self.agent, MACHINE, wall=lambda: self.wall[0]))
        fw.step()
        path = mg.gate_path(self.agent)
        with open(path) as f:
            first = f.read()
        self.mono[0] += 15
        self.svc.fail = [ConnectionRefusedError("down")]
        fw.step()
        self.assertEqual(fw.status["state"], "offline")
        with open(path) as f:
            self.assertEqual(f.read(), first)

    def test_a_failing_callback_never_stops_forwarding(self):
        self.enrol_files()
        self.add_cycles(1)

        def boom(doc, ms):
            raise RuntimeError("bad")
        fw = self.fwd(on_feedback=boom)
        with self.assertLogs("agent_mes_forward", "ERROR"):
            fw.step()
        self.assertEqual(set(self.states().values()), {"acked"})

    def test_401_on_feedback_stops_like_any_refusal(self):
        self.enrol_files()
        fw = self.fwd()
        self.svc.feedback_status = 401
        with self.assertLogs("agent_mes_forward", "ERROR"):
            self.assertEqual(fw.step(), mf.RECHECK_S)
        self.assertEqual(fw.status["state"], "rejected")


def _millwright_gate():
    root = os.path.expanduser(os.environ.get("MILLWRIGHT_DIR", "~/millwright"))
    path = os.path.join(root, "millwright", "panels", "mw_gate.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("mw_gate_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod             # dataclasses look their module up
    spec.loader.exec_module(mod)
    return mod


MW_GATE = _millwright_gate()


@unittest.skipIf(MW_GATE is None, "MillWright (panels/mw_gate.py) not found: set MILLWRIGHT_DIR")
class GateFileAcceptedByMillWright(unittest.TestCase):
    def test_millwright_reads_what_the_agent_writes(self):
        with tempfile.TemporaryDirectory() as d:
            for g in (GATE, {"policy": "enforce", "validated": True, "reasons": [], "order": None},
                      {"policy": "enforce", "validated": False, "reasons": ["no_order", "over_quantity"],
                       "order": 7}):
                mg.GateWriter(d, MACHINE, wall=lambda: 1_800_000_000.0)(feedback(gate=g), 1_799_999_990_000)
                v = MW_GATE.read_verdict(mg.gate_path(d), now=1_800_000_000.0)
                self.assertEqual((v.state, v.policy, v.validated, v.reasons, v.order, v.age_s),
                                 ("ok", g["policy"], g["validated"], g["reasons"], g["order"], 10.0))


if __name__ == "__main__":
    unittest.main()
