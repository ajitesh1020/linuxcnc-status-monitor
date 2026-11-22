"""tools/fleet_sim_gate.py (MES plan P5.5): the simulator switches the site's
cycle gate policy, every machine's agent writes cycle_gate.json, and `check`
reports every machine that did not follow.

Run: python -m unittest -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import agent_mes_gate  # noqa: E402
import fleet_sim  # noqa: E402
import fleet_sim_gate as G  # noqa: E402
import fleet_sim_mes as M  # noqa: E402

NAMES = ["SIM-001", "SIM-002", "SIM-003"]


def verdict(policy, validated=False, reasons=("no_order", "nothing_bound")):
    return {"policy": policy, "validated": validated, "reasons": list(reasons), "order": None}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeSite:
    """The service's setting and each machine's file, as the agent would write it."""

    def __init__(self, wall, lag=None, broken=()):
        self.policy, self.wall, self.lag = "off", wall, lag or {}
        self.set_at = 0.0
        self.sets = []
        self.broken = set(broken)
        self.validated_on = set()

    def set_policy(self, p):
        if p in self.broken:
            raise RuntimeError("HTTP 403 forbidden")
        self.policy = p
        self.sets.append(p)
        self.set_at = self.wall()

    def read(self, name):
        if self.wall() - self.set_at < self.lag.get(name, 10.0):
            return None if not self.sets[:-1] else self._doc(self.sets[-2] if len(self.sets) > 1 else "off", name)
        return self._doc(self.policy, name)

    def _doc(self, p, name):
        g = verdict(p, True, []) if name in self.validated_on else verdict(p)
        return {"schema": agent_mes_gate.SCHEMA, "fetched_at_ms": int((self.wall() - 2) * 1000), "cycle_gate": g}


def run_phases(site, clock, policies=("off", "advisory", "enforce"), seconds=400, **kw):
    ph = G.GatePhases(NAMES, list(policies), site.set_policy, site.read, clock=clock, wall=clock,
                      hold_s=kw.pop("hold_s", 90), wait_s=kw.pop("wait_s", 60))
    while clock.t < seconds:
        ph.step()
        clock.t += 1.0
    return ph


class Policies(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(G.parse_policies(" off, advisory ,enforce"), ["off", "advisory", "enforce"])
        self.assertEqual(G.parse_policies(""), [])
        with self.assertRaises(ValueError):
            G.parse_policies("off,strict")


class ReadGateFile(unittest.TestCase):
    def test_reads_what_the_agent_writes_and_refuses_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            path = agent_mes_gate.gate_path(d)
            self.assertIsNone(G.read_gate_file(path))
            agent_mes_gate.GateWriter(d, str(uuid.uuid4()))({"cycle_gate": verdict("advisory")}, 1_800_000_000_000)
            doc = G.read_gate_file(path)
            self.assertEqual(doc["cycle_gate"]["policy"], "advisory")
            for bad in ({"schema": "x"}, dict(doc, fetched_at_ms="1"), dict(doc, cycle_gate=verdict("x"))):
                with open(path, "w") as f:
                    json.dump(bad, f)
                self.assertIsNone(G.read_gate_file(path))
            with open(path, "w") as f:
                f.write("{half")
            self.assertIsNone(G.read_gate_file(path))


class Phases(unittest.TestCase):
    def test_every_machine_follows_each_policy(self):
        clock = Clock()
        site = FakeSite(clock, lag={"SIM-002": 30.0})
        ph = run_phases(site, clock)
        self.assertTrue(ph.done)
        self.assertEqual(site.sets, ["off", "advisory", "enforce"])
        t = ph.truth()
        self.assertEqual([p["policy"] for p in t["phases"]], ["off", "advisory", "enforce"])
        for p in t["phases"]:
            self.assertTrue(all(p["machines"].values()))
            self.assertEqual(p["machines"]["SIM-002"]["after_s"], 30.0)
            self.assertEqual(p["machines"]["SIM-001"]["reasons"], ["no_order", "nothing_bound"])
        self.assertEqual(G.compare(t), [])
        self.assertIn("advisory 3/3 (slowest 30 s)", G.summary(t))

    def test_a_machine_that_never_follows(self):
        clock = Clock()
        ph = run_phases(FakeSite(clock, lag={"SIM-003": 999.0}), clock)
        bad = G.compare(ph.truth())
        self.assertEqual(len(bad), 3)
        self.assertTrue(all("SIM-003 never showed it" in b for b in bad))

    def test_validated_without_an_order_is_wrong(self):
        clock = Clock()
        site = FakeSite(clock)
        site.validated_on.add("SIM-001")
        bad = G.compare(run_phases(site, clock, policies=["enforce"], seconds=100).truth())
        self.assertEqual(len(bad), 1)
        self.assertIn("SIM-001 says validated=True", bad[0])

    def test_setting_refused_and_run_too_short(self):
        clock = Clock()
        ph = run_phases(FakeSite(clock, broken={"advisory"}), clock, seconds=150)
        bad = G.compare(ph.truth())
        self.assertIn("setting cycle_gate advisory failed: HTTP 403 forbidden", bad)
        self.assertTrue(any("enforce: not reached" in b for b in bad))
        self.assertFalse(any("never showed" in b for b in bad))      # a phase not set is not blamed on machines

    def test_stale_verdict_is_reported(self):
        t = {"policies": ["off"], "wait_s": 60, "errors": [], "phases": [
            {"policy": "off", "set": True, "machines": {"SIM-001": {"after_s": 5, "validated": False,
                                                                    "reasons": ["no_order"], "order": None,
                                                                    "age_s": 300.0}}}]}
        self.assertEqual(len(G.compare(t)), 1)


class EmptyLedger:
    def unit(self, ns, ident):
        return {"attempts": [], "scans": [], "bindings": []}

    def scan(self, sid):
        return None

    def exceptions(self):
        return []


class CheckOutput(unittest.TestCase):
    def test_check_prints_the_gate_and_fails_on_a_mismatch(self):
        clock = Clock()
        good = run_phases(FakeSite(clock), clock).truth()
        lines = []
        rc = M.check({"mes": {"units": {}, "scans": {}, "exceptions": [], "cycle_gate": good}}, EmptyLedger(),
                     out=lines.append)
        self.assertEqual(rc, 0)
        self.assertTrue(any(ln.startswith("MES cycle gate: off 3/3") for ln in lines))
        clock = Clock()
        bad = run_phases(FakeSite(clock, lag={"SIM-001": 999.0}), clock).truth()
        lines = []
        rc = M.check({"mes": {"units": {}, "scans": {}, "exceptions": [], "cycle_gate": bad}}, EmptyLedger(),
                     out=lines.append)
        self.assertEqual(rc, 1)
        self.assertEqual(sum(ln.startswith("MISMATCH cycle gate") for ln in lines), 3)


class LinkWritesTheFile(unittest.TestCase):
    def test_link_gives_the_forwarder_a_gate_writer_only_when_asked(self):
        with tempfile.TemporaryDirectory() as d:
            mid = str(uuid.uuid4())
            link = M.Link(d, "SIM-001", mid, "https://127.0.0.1:1")
            link.open("boot", G.mw_data_dir(d, "SIM-001"))
            try:
                w = link.forwarder.on_feedback
                self.assertIsInstance(w, agent_mes_gate.GateWriter)
                self.assertEqual(w.mw_data, os.path.join(d, "SIM-001.mw_data"))
                self.assertTrue(os.path.isdir(w.mw_data))
            finally:
                link.close()
            link = M.Link(d, "SIM-002", str(uuid.uuid4()), "https://127.0.0.1:1")
            link.open("boot")
            try:
                self.assertIsNone(link.forwarder.on_feedback)
            finally:
                link.close()


class Cli(unittest.TestCase):
    def test_gate_options_listed(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with self.assertRaises(SystemExit):
                fleet_sim.main(["run", "--help"])
        finally:
            sys.stdout = old
        text = buf.getvalue()
        for opt in ("--mes-gate", "--mes-gate-hold", "--mes-gate-wait", "--mes-admin-login",
                    "--mes-admin-password-env"):
            self.assertIn(opt, text)


if __name__ == "__main__":
    unittest.main()
