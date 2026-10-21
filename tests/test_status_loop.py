"""Runs the real status.main() loop against a fake LinuxCNC on a fake clock.

Checks, end to end through the agent's own loop:
  * the NML error channel is never opened by default (operator GUI keeps
    every message), and is only read with read_error_channel: true;
  * a short fault while idle reaches the dashboard and the journal at once,
    both the set and the clear edge, instead of waiting for the heartbeat.
Run: python -m unittest -v
"""
import importlib
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time as real_time
import types
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)


class FakeClock:
    """Stands in for the time module inside status.py."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += max(float(s), 0.001)

    def __getattr__(self, name):          # time_ns, time, ...
        return getattr(real_time, name)


class Scenario:
    """Machine ON, manual mode, idle. Faults as (start, end, field, value)
    windows in seconds since the clock started."""

    def __init__(self, clock, faults, stop_at, nml_messages=()):
        self.clock, self.start = clock, clock.now
        self.faults, self.stop_at = faults, stop_at
        self.nml = list(nml_messages)
        self.error_channels_opened = 0
        self.status = None                   # the status module, set later

    def t(self):
        return self.clock.now - self.start


def make_fake_linuxcnc(sc):
    mod = types.ModuleType("linuxcnc")
    mod.INTERP_IDLE, mod.INTERP_READING, mod.INTERP_PAUSED = 1, 2, 3
    mod.EXEC_ERROR = 1

    class stat:
        def poll(self):
            t = sc.t()
            if t >= sc.stop_at:
                sc.status._shutdown_requested = True
            self.task_state, self.task_mode, self.interp_state = 4, 1, 1
            self.exec_state, self.estop, self.paused = 2, False, False
            self.enabled = True
            self.joints = 2
            self.joint = [{"fault": False, "homed": True},
                          {"fault": False, "homed": True}]
            for start, end, field, value in sc.faults:
                if start <= t < end:
                    if field == "joint_fault":
                        self.joint[value] = dict(self.joint[value], fault=True)
                    else:
                        setattr(self, field, value)
            self.file = ""

    class error_channel:
        def __init__(self):
            sc.error_channels_opened += 1

        def poll(self):
            return sc.nml.pop(0) if sc.nml else None

    mod.stat, mod.error_channel = stat, error_channel
    return mod


class FakeSender:
    def __init__(self, *a, **k):
        self.packets = []
        FakeSender.last = self
        self.targets = ["test"]

    def send(self, data):
        self.packets.append(json.loads(data))
        return True

    def close(self):
        pass


class FakeGcode:
    def __init__(self, *a, **k):
        pass

    def reset(self):
        pass

    def check_and_send(self, *a, **k):
        pass


class StatusLoopTests(unittest.TestCase):
    def run_agent(self, faults, stop_at=20.0, nml=(), **cfg_over):
        clock = FakeClock()
        sc = Scenario(clock, faults, stop_at, nml)
        saved = sys.modules.get("linuxcnc")
        sys.modules["linuxcnc"] = make_fake_linuxcnc(sc)
        for sig in (signal.SIGINT, signal.SIGTERM):        # main() installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        try:
            sys.modules.pop("status", None)
            status = importlib.import_module("status")
            sc.status = status
            cfg = dict(status.DEFAULT_CONFIG, journal_port=0, **cfg_over)
            status.time = clock
            status.load_config = lambda path: cfg
            status.find_config = lambda arg: None
            status._configure_logging = lambda dev, c: None
            status._parse_args = lambda: types.SimpleNamespace(
                dev=False, config=None, check=False)
            status.single_instance = lambda: object()
            status.USER_CONFIG_DIR = tmp.name
            status.UdpSender = FakeSender
            status.GcodeFileSender = FakeGcode
            status.LinuxCNCWatch = lambda: types.SimpleNamespace(alive=lambda: True)
            status.main()
        finally:
            sys.modules.pop("status", None)
            if saved is None:
                sys.modules.pop("linuxcnc", None)
            else:
                sys.modules["linuxcnc"] = saved
        conn = sqlite3.connect(os.path.join(tmp.name, "journal.db"))
        journal = [json.loads(d) for (d,) in conn.execute("SELECT data FROM entries ORDER BY seq")]
        conn.close()
        # Seconds since start for each packet (packets carry wall time; use order).
        return sc, FakeSender.last.packets, journal

    def test_error_channel_is_not_opened_by_default(self):
        sc, packets, _ = self.run_agent([], nml=[(11, "Probe tripped")])
        self.assertEqual(sc.error_channels_opened, 0)
        self.assertTrue(packets)
        self.assertTrue(all(p["nml_errors"] == [] for p in packets))
        self.assertEqual(sc.nml, [(11, "Probe tripped")])   # still there for the GUI

    def test_error_channel_read_only_when_enabled(self):
        sc, packets, _ = self.run_agent([], nml=[(11, "Probe tripped")],
                                        read_error_channel=True)
        self.assertEqual(sc.error_channels_opened, 1)
        msgs = [e["msg"] for p in packets for e in p["nml_errors"]]
        self.assertEqual(msgs, ["Probe tripped"])

    def test_short_idle_exec_error_is_sent_and_journalled_both_edges(self):
        # EXEC_ERROR for 0.4 s at 8 s, long before the 30 s heartbeat.
        sc, packets, journal = self.run_agent([(8.0, 8.4, "exec_state", 1)])
        states = [p["exec_state"] for p in packets]
        self.assertIn(1, states)
        self.assertNotEqual(states[-1], 1)                 # the clear edge went out too
        self.assertEqual(len(packets), 3)                  # first idle packet, set, clear
        self.assertEqual([e["exec_state"] for e in journal][-2:], [1, 2])

    def test_idle_joint_fault_and_estop_edges(self):
        sc, packets, journal = self.run_agent(
            [(6.0, 6.5, "joint_fault", 1), (12.0, 13.0, "estop", True)])
        faults = [p["joints"][1]["fault"] for p in packets]
        estops = [p["estop"] for p in packets]
        self.assertEqual(faults, [False, True, False, False, False])
        self.assertEqual(estops, [False, False, False, True, False])
        self.assertEqual(len(journal), 5)

    def test_quiet_idle_machine_sends_only_heartbeats(self):
        sc, packets, _ = self.run_agent([], stop_at=65.0)
        self.assertEqual(len(packets), 3)                  # start, 30 s, 60 s


if __name__ == "__main__":
    unittest.main()
