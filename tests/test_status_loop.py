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

    def perf_counter_ns(self):            # the cycle calculator's clock
        return int(self.now * 1e9)

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
    mod.RCS_ERROR = 3

    class stat:
        def poll(self):
            t = sc.t()
            if t >= sc.stop_at:
                sc.status._shutdown_requested = True
            self.task_state, self.task_mode, self.interp_state = 4, 1, 1
            self.exec_state, self.estop, self.paused = 2, False, False
            self.state, self.echo_serial_number = 1, 10    # RCS_DONE
            self.enabled = True
            self.joints = 2
            self.joint = [{"fault": False, "homed": True},
                          {"fault": False, "homed": True}]
            self.file = ""
            for start, end, field, value in sc.faults:
                if start <= t < end:
                    if field == "joint_fault":
                        self.joint[value] = dict(self.joint[value], fault=True)
                    else:
                        setattr(self, field, value)

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


class RealClock:
    """Real time with FakeClock's interface (for timing tests)."""

    @property
    def now(self):
        return real_time.monotonic()

    def __getattr__(self, name):
        return getattr(real_time, name)


class StatusLoopTests(unittest.TestCase):
    def run_agent(self, faults, stop_at=20.0, nml=(), clock=None, **cfg_over):
        clock = clock or FakeClock()
        sc = Scenario(clock, faults, stop_at, nml)
        saved = sys.modules.get("linuxcnc")
        sys.modules["linuxcnc"] = make_fake_linuxcnc(sc)
        for sig in (signal.SIGINT, signal.SIGTERM):        # main() installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.agent_dir = tmp.name
        try:
            sys.modules.pop("status", None)
            status = importlib.import_module("status")
            sc.status = status
            cfg = dict(status.DEFAULT_CONFIG, journal_port=0, **cfg_over)
            status.time = clock
            import cycle_time_calculator
            self.addCleanup(setattr, cycle_time_calculator, "time", cycle_time_calculator.time)
            cycle_time_calculator.time = clock
            status.load_config = lambda path: cfg
            status.find_config = lambda arg: None
            status._configure_logging = lambda dev, c: None
            status._parse_args = lambda: types.SimpleNamespace(
                dev=False, config=None, check=False, mes_enroll=None, mes_status=False)
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

    def test_every_packet_says_the_journal_is_ok(self):
        _sc, packets, _ = self.run_agent([])
        self.assertTrue(packets)
        self.assertTrue(all(p["journal_state"] == "ok" for p in packets))

    def test_agent_restarted_mid_part_still_counts_the_part(self):
        # The agent (re)starts while a program is half way through (service
        # restart, package upgrade). It cannot know the part's start, so it
        # treats it as Run From Here: the part is counted when the program
        # reaches M30, with no abort; its cycle time covers only what it saw.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prog = os.path.join(tmp.name, "part.ngc")
        with open(prog, "w") as f:
            f.write("%\nG21 G90\n" + "".join(f"G1 X{i} F100\n" for i in range(20)) + "M30\n%\n")
        running = [(0.0, 15.0, "task_mode", 2), (0.0, 15.0, "interp_state", 2),
                   (0.0, 99.0, "file", prog)]
        lines = [(0.0 + i * 0.5, 0.5 + i * 0.5, "motion_line", 12 + i) for i in range(11)]
        lines.append((5.5, 99.0, "motion_line", 22))                  # last move
        _sc, packets, _ = self.run_agent(running + lines, stop_at=25.0)
        last = packets[-1]
        self.assertEqual((last["parts_produced"], last["abort_count"]), (1, 0))
        self.assertEqual(last["run_from_here_count"], 1)
        self.assertFalse(last["part_in_progress"])

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

    def test_idle_failed_mdi_commands_each_sent_and_journalled(self):
        # Two G-code errors in a row while idle (EXEC_ERROR is never set; only
        # stat.state goes to RCS_ERROR), then a good command clears it.
        sc, packets, journal = self.run_agent([
            (8.0, 9.0, "state", 3),
            (8.0, 8.5, "echo_serial_number", 11),
            (8.5, 9.0, "echo_serial_number", 12),
            (9.0, 99.0, "echo_serial_number", 13),
        ])
        self.assertEqual([(p["cmd_state"], p["cmd_serial"]) for p in packets],
                         [(1, 10), (3, 11), (3, 12), (1, 13)])
        self.assertEqual([(e["cmd_state"], e["cmd_serial"]) for e in journal][-3:],
                         [(3, 11), (3, 12), (1, 13)])

    def test_successful_commands_while_idle_send_nothing_extra(self):
        sc, packets, _ = self.run_agent([(5.0, 99.0, "echo_serial_number", 11),
                                         (5.0, 5.2, "state", 2),
                                         (9.0, 99.0, "echo_serial_number", 12)])
        self.assertEqual(len(packets), 1)                  # just the first idle packet

    def test_idle_joint_fault_and_estop_edges(self):
        sc, packets, journal = self.run_agent(
            [(6.0, 6.5, "joint_fault", 1), (12.0, 13.0, "estop", True)])
        faults = [p["joints"][1]["fault"] for p in packets]
        estops = [p["estop"] for p in packets]
        self.assertEqual(faults, [False, True, False, False, False])
        self.assertEqual(estops, [False, False, False, True, False])
        self.assertEqual(len(journal), 5)

    def test_packets_carry_session_and_increasing_packet_seq(self):
        _, first, journal = self.run_agent([(6.0, 6.5, "exec_state", 1)])
        sessions = {p["session_id"] for p in first}
        self.assertEqual(len(sessions), 1)
        self.assertEqual([p["packet_seq"] for p in first], list(range(1, len(first) + 1)))
        self.assertEqual({e["session_id"] for e in journal}, sessions)
        # A restarted agent is a new session (its part counters start again at 0).
        _, second, _ = self.run_agent([])
        self.assertNotEqual({p["session_id"] for p in second}, sessions)
        self.assertEqual(second[0]["packet_seq"], 1)

    def test_machine_uuid_in_every_packet_and_entry_and_kept_across_restarts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "identity.json")
        _, first, journal = self.run_agent([(6.0, 6.5, "exec_state", 1)],
                                           machine_uuid_file=path)
        with open(path) as f:
            uid = json.load(f)["machine_uuid"]
        self.assertEqual({p["machine_uuid"] for p in first}, {uid})
        self.assertEqual({e["machine_uuid"] for e in journal}, {uid})
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        _, second, _ = self.run_agent([], machine_uuid_file=path)
        self.assertEqual({p["machine_uuid"] for p in second}, {uid})
        self.assertNotEqual(first[0]["session_id"], second[0]["session_id"])

    def test_default_identity_file_is_in_the_agent_folder(self):
        _, packets, _ = self.run_agent([])
        self.assertRegex(packets[0]["machine_uuid"],
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

    def test_mes_off_by_default(self):
        _, packets, _ = self.run_agent([])
        self.assertTrue(all(p["mes_spool"] == {"pending": 0, "bytes": 0, "state": "off"} for p in packets))
        self.assertTrue(all("features" not in p for p in packets))
        self.assertTrue(all("mes_link" not in p for p in packets))

    def test_mes_spool_state_and_features_when_enabled(self):
        _, packets, _ = self.run_agent([], mes_enabled=True)
        for p in packets:
            self.assertEqual(p["features"], ["mes1"])
            self.assertEqual((p["mes_spool"]["state"], p["mes_spool"]["pending"]), ("ok", 0))
            self.assertGreater(p["mes_spool"]["bytes"], 0)
            self.assertEqual(p["mes_link"], {"state": "not_enrolled"})   # mes_service_url not set

    def _program(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prog = os.path.join(tmp.name, "part.ngc")
        with open(prog, "w") as f:
            f.write("%\nG21 G90\n" + "".join(f"G1 X{i} F100\n" for i in range(20)) + "M30\n%\n")
        run = [(2.0, 8.0, "task_mode", 2), (2.0, 8.0, "interp_state", 2), (0.0, 99.0, "file", prog)]
        run += [(2.0 + i * 0.5, 2.5 + i * 0.5, "motion_line", 3 + i) for i in range(11)]
        run.append((7.5, 99.0, "motion_line", 22))
        return tmp.name, prog, run

    def _cycles(self):
        import sqlite3 as sq
        with sq.connect(os.path.join(self.agent_dir, "mes_spool.db")) as db:
            return [json.loads(b) for (b,) in db.execute(
                "SELECT body FROM spool WHERE type='cycle.observed' ORDER BY seq")]

    def test_cycle_observed_with_millwrights_execution_id(self):
        import uuid as u
        cfg_dir, prog, run = self._program()
        os.makedirs(os.path.join(cfg_dir, "mw_data"))
        eid, sid = str(u.uuid4()), str(u.uuid4())
        with open(os.path.join(cfg_dir, "mw_data", "current_execution.json"), "w") as f:
            json.dump({"execution_id": eid, "started_at": real_time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", real_time.gmtime()), "program_path": prog, "run_file": prog,
                "mode": "normal", "station_id": str(u.uuid4()), "operator_session_id": sid}, f)
        _, packets, _ = self.run_agent(run, stop_at=12.0, mes_enabled=True, mes_millwright_config=cfg_dir)
        self.assertEqual(packets[-1]["parts_produced"], 1)
        (c,) = self._cycles()
        self.assertEqual(c["correlation"]["execution_id"], eid)
        self.assertEqual(c["correlation"]["operator_session_id"], sid)
        self.assertEqual((c["payload"]["outcome"], c["payload"]["correlation_method"],
                          c["payload"]["program_path"]), ("completed", "explicit", prog))

    def test_cycle_observed_without_millwright(self):
        _cfg, prog, run = self._program()
        self.run_agent(run, stop_at=12.0, mes_enabled=True)
        (c,) = self._cycles()
        self.assertEqual((c["payload"]["correlation_method"], c["correlation"]["operator_session_id"]),
                         ("none", None))
        self.assertRegex(c["correlation"]["execution_id"], r"^[0-9a-f-]{36}$")

    def test_packets_report_loop_lag(self):
        _, packets, _ = self.run_agent([], stop_at=40.0)
        lag = packets[-1]["loop_lag_ms"]
        self.assertEqual(sorted(lag), ["max", "p50", "p99"])
        self.assertTrue(all(isinstance(v, int) and 0 <= v <= 20 for v in lag.values()), lag)

    def test_a_stalled_spool_does_not_slow_the_loop(self):
        # Real time: cycles run while the MES worker is stuck inside the spool
        # (a hung disk). The sampling loop must keep its timing.
        import threading
        import agent_mes_link
        import agent_mes_spool
        stuck = threading.Event()
        entered = threading.Event()
        real_add, real_stop = agent_mes_spool.Spool.add, agent_mes_link.MesWorker.stop

        def hung_add(spool, *a, **k):
            entered.set()
            stuck.wait(30)
            return real_add(spool, *a, **k)

        def stop(worker, timeout=5.0):
            stuck.set()
            return real_stop(worker, timeout)
        agent_mes_spool.Spool.add = hung_add
        agent_mes_link.MesWorker.stop = stop
        self.addCleanup(setattr, agent_mes_spool.Spool, "add", real_add)
        self.addCleanup(setattr, agent_mes_link.MesWorker, "stop", real_stop)
        self.addCleanup(stuck.set)
        cycles = []
        for k in range(3):                                  # three short cycles
            t0 = 0.5 + k * 1.2
            cycles += [(t0, t0 + 0.6, "task_mode", 2), (t0, t0 + 0.6, "interp_state", 2)]
        _, packets, _ = self.run_agent(cycles, stop_at=4.5, clock=RealClock(),
                                       mes_enabled=True)
        self.assertTrue(entered.is_set(), "the worker never reached the spool")
        lag = packets[-1]["loop_lag_ms"]
        self.assertLess(lag["p99"], 60, lag)
        self.assertLess(lag["max"], 150, lag)

    def test_no_machine_uuid_field_if_identity_cannot_be_written(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        blocker = os.path.join(tmp.name, "a-file")
        open(blocker, "w").close()
        _, packets, journal = self.run_agent(
            [], machine_uuid_file=os.path.join(blocker, "identity.json"))
        self.assertTrue(packets)
        self.assertTrue(all("machine_uuid" not in p for p in packets))
        self.assertTrue(all("machine_uuid" not in e for e in journal))

    def test_quiet_idle_machine_sends_only_heartbeats(self):
        sc, packets, _ = self.run_agent([], stop_at=65.0)
        self.assertEqual(len(packets), 3)                  # start, 30 s, 60 s


    # --- programs that call subroutines (pick/drop check, 27 Oct 2026) -------------------------------------
    def _pickdrop(self):
        """A MillWright pick/drop check: line 2 and 4 are M6, remapped to macros/rack_change.ngc (in the
        INI's SUBROUTINE_PATH). While LinuxCNC is in the remap, stat.file is rack_change.ngc, call_level 1
        and motion_line counts its lines (90..95)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "macros"))
        ini = os.path.join(tmp.name, "m.ini")
        with open(ini, "w") as f:
            f.write("[RS274NGC]\nSUBROUTINE_PATH = macros\nUSER_M_PATH = macros\n")
        sub = os.path.join(tmp.name, "macros", "rack_change.ngc")
        with open(sub, "w") as f:
            f.write("o<rack_change> sub\n" + "G0 X1\n" * 100 + "o<rack_change> endsub\nM2\n")
        prog = os.path.join(tmp.name, "atc_pickdrop_1.ngc")
        with open(prog, "w") as f:
            f.write("G21 G90 G94\nM6 T1 G43 H1\nG4 P2\nM6 T2 G43 H2\nG4 P2\nG53 G0 Z50\nM30\n")
        # The agent's first sample is at about 2 s: the run starts at 4 s.
        run = [(0.0, 99.0, "ini_filename", ini), (0.0, 99.0, "file", prog), (0.0, 99.0, "call_level", 0),
               (4.0, 11.0, "task_mode", 2), (4.0, 11.0, "interp_state", 2)]
        for a, b in ((4.0, 6.0), (6.5, 8.5)):                     # the two tool changes
            run += [(a, b, "file", sub), (a, b, "call_level", 1)]
            run += [(a + i * 0.3, a + (i + 1) * 0.3, "motion_line", 90 + i) for i in range(6)]
        run += [(6.0, 6.5, "motion_line", 95), (8.5, 99.0, "motion_line", 6)]   # back in the program
        return prog, sub, run

    def test_subroutine_file_is_not_the_program(self):
        prog, _sub, run = self._pickdrop()
        _, packets, journal = self.run_agent(run, stop_at=16.0, mes_enabled=True)
        last = packets[-1]
        self.assertEqual((last["parts_produced"], last["abort_count"], last["run_from_here_count"]), (1, 0, 0))
        self.assertTrue(all(p["file_name"] == "atc_pickdrop_1.ngc" for p in packets))
        self.assertTrue(all(e["file_name"] == "atc_pickdrop_1.ngc" for e in journal))
        (c,) = self._cycles()
        self.assertEqual((c["payload"]["program_path"], c["payload"]["outcome"]), (prog, "completed"))

    def test_agent_started_inside_a_subroutine_knows_the_program_later(self):
        # The agent (re)starts during the first tool change: it has never seen the program file.
        _prog, sub, run = self._pickdrop()
        run = [r for r in run if r[2] not in ("task_mode", "interp_state")]
        run += [(0.0, 11.0, "task_mode", 2), (0.0, 11.0, "interp_state", 2),
                (0.0, 6.0, "file", sub), (0.0, 6.0, "call_level", 1)]
        _, packets, _ = self.run_agent(run, stop_at=16.0)
        self.assertEqual(packets[0]["file_name"], "")              # not rack_change.ngc
        self.assertEqual(packets[-1]["file_name"], "atc_pickdrop_1.ngc")
        self.assertNotIn("rack_change.ngc", {p["file_name"] for p in packets})

    def _remap_run(self, stop):
        """AXIS traces 27 Oct 14:48 / 15:00: axis.ngc loaded; MillWright opens the pick/drop program and starts
        it at once, so the agent first sees the run inside rack_change.ngc (all three line numbers are its
        lines). After the last M6 LinuxCNC is back in the program with read_line on its M30 while motion
        still runs the remap's moves (98, 103, 110); the run goes idle at 110. `stop`: Stop pressed during
        the last M6, so LinuxCNC is never back in the program."""
        import uuid as u
        prog, sub, run = self._pickdrop()
        cfg_dir = os.path.dirname(prog)
        other = os.path.join(cfg_dir, "axis.ngc")
        with open(other, "w") as f:
            f.write("G21\n" + "G1 X1 Y1 F100\n" * 150 + "M2\n")
        os.makedirs(os.path.join(cfg_dir, "mw_data"))
        eid = str(u.uuid4())
        with open(os.path.join(cfg_dir, "mw_data", "current_execution.json"), "w") as f:
            json.dump({"execution_id": eid, "started_at": real_time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", real_time.gmtime()), "program_path": prog, "run_file": prog,
                "mode": "atc_check", "station_id": str(u.uuid4()), "operator_session_id": None}, f)
        run = [r for r in run if r[2] not in ("file", "call_level", "motion_line")]
        in_sub_until = 11.0 if stop else 9.0
        run += [(0.0, 99.0, "file", other), (0.0, 99.0, "call_level", 0), (0.0, 99.0, "read_line", 0),
                (4.0, in_sub_until, "file", sub), (4.0, in_sub_until, "call_level", 1),
                (4.0, in_sub_until, "read_line", 100)]
        run += [(4.0 + i * 0.5, 4.5 + i * 0.5, "motion_line", ln)
                for i, ln in enumerate((92, 98, 103, 110, 73, 77, 82, 92, 98, 98))]
        if not stop:
            run += [(9.0, 11.0, "file", prog), (9.0, 11.0, "read_line", 7),       # 7 = its M30
                    (9.0, 9.5, "motion_line", 98), (9.5, 10.0, "motion_line", 103), (10.0, 99.0, "motion_line", 110)]
        else:
            run += [(9.0, 99.0, "motion_line", 98)]
        run += [(11.0, 12.0, "file", prog), (12.0, 99.0, "file", other)]       # idle; MillWright reopens axis.ngc
        _, packets, _ = self.run_agent(run, stop_at=16.0, mes_enabled=True, mes_millwright_config=cfg_dir)
        (c,) = self._cycles()
        self.assertEqual(c["correlation"]["execution_id"], eid)
        # 15:24 run: the dashboard showed axis.ngc for the whole run. Matched at the first sample, so
        # every packet after it names the program.
        running = [p["file_name"] for p in packets if p["cycle_state"] == "RUNNING"]
        self.assertTrue(running)
        self.assertEqual(set(running[1:]), {"atc_pickdrop_1.ngc"})
        return prog, packets[-1], c

    def test_program_opened_and_started_inside_the_remap(self):
        prog, last, c = self._remap_run(stop=False)
        self.assertEqual((last["parts_produced"], last["abort_count"], last["run_from_here_count"]), (1, 0, 0))
        # matched by start time (the agent did not know its program), MillWright named the file
        self.assertEqual((c["payload"]["program_path"], c["payload"]["outcome"], c["payload"]["correlation_method"]),
                         (prog, "completed", "time_window"))

    def test_stop_in_the_last_tool_change_is_an_abort(self):
        prog, last, c = self._remap_run(stop=True)
        self.assertEqual((last["parts_produced"], last["abort_count"]), (0, 1))
        self.assertEqual((c["payload"]["program_path"], c["payload"]["outcome"]), (prog, "aborted"))

    def _atc_drill(self, stop):
        """LX052_drill_ATC.ngc from the top (agent journal, 29 Oct 2026 11:51 and 12:06): first move line 27,
        M6 on 30, holes from 37, tail 633, M30 634. While LinuxCNC is in rack_change.ngc motion shows 27 (the
        program's G0, still running), 73, 92, 98; back in the program it still runs the remap's 103 and 110,
        never seen inside it. `stop`: Stop pressed at the first hole (12:08:30 case)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "macros"))
        ini = os.path.join(tmp.name, "m.ini")
        with open(ini, "w") as f:
            f.write("[RS274NGC]\nSUBROUTINE_PATH = macros\nUSER_M_PATH = macros\n")
        sub = os.path.join(tmp.name, "macros", "rack_change.ngc")
        moves = (72, 73, 74, 75, 77, 82, 91, 92, 93, 94, 98, 103, 109, 110)
        with open(sub, "w") as f:
            f.write("".join("G53 G0 Z0\n" if n in moves else "(c)\n" for n in range(1, 115)))
        prog = os.path.join(tmp.name, "LX052_drill_ATC.ngc")
        with open(prog, "w") as f:
            f.write("(h)\n" * 26 + "G0 Z20\nG28 G91 Z0\nG90\nM6 T01\nG43 H01\nM3 S40000\nG4 P2\nG00 Z20\n\n(T01)\n"
                    + "".join("X%d Y%d\n" % (i, i) for i in range(596)) + "G0 Z20\nM30\n")
        end = 9.0 if stop else 12.0
        run = [(0.0, 99.0, "ini_filename", ini), (0.0, 99.0, "file", prog), (0.0, 99.0, "call_level", 0),
               (4.0, end, "task_mode", 2), (4.0, end, "interp_state", 2),
               (4.1, 6.5, "file", sub), (4.1, 6.5, "call_level", 1),     # LinuxCNC reads ahead into the M6
               (4.2, 4.5, "motion_line", 27),
               (4.5, 5.0, "motion_line", 73), (5.0, 5.5, "motion_line", 92), (5.5, 6.8, "motion_line", 98),
               (6.8, 7.2, "motion_line", 103), (7.2, 7.8, "motion_line", 110), (7.8, 8.4, "motion_line", 34),
               (8.4, 99.0, "motion_line", 37)]
        if not stop:
            run += [(9.0, 10.0, "motion_line", 300), (10.0, 99.0, "motion_line", 633)]
        _, packets, _ = self.run_agent(run, stop_at=16.0, mes_enabled=True)
        (c,) = self._cycles()
        return packets[-1], c

    def test_atc_program_from_the_top_is_not_run_from_here(self):
        last, c = self._atc_drill(stop=False)
        self.assertEqual((last["parts_produced"], last["abort_count"], last["run_from_here_count"]), (1, 0, 0))
        self.assertEqual(c["payload"]["outcome"], "completed")

    def test_atc_program_stopped_after_the_first_m6_is_an_abort(self):
        last, c = self._atc_drill(stop=True)
        self.assertEqual((last["parts_produced"], last["abort_count"], last["run_from_here_count"]), (0, 1, 0))
        self.assertEqual(c["payload"]["outcome"], "aborted")             # was "unknown" on 29 Oct

    def test_agent_stopped_mid_run_is_unknown_not_an_abort(self):
        _cfg, prog, run = self._program()
        run = [(r[0], 99.0 if r[2] in ("task_mode", "interp_state") else r[1], r[2], r[3]) for r in run]
        _, packets, journal = self.run_agent(run[:-1], stop_at=5.0, mes_enabled=True)  # still running at 5 s
        self.assertEqual((packets[-1]["abort_count"], packets[-1]["parts_produced"]), (0, 0))
        self.assertTrue(all(e["abort_count"] == 0 for e in journal))
        (c,) = self._cycles()
        self.assertEqual((c["payload"]["outcome"], c["payload"]["program_path"]), ("unknown", prog))

if __name__ == "__main__":
    unittest.main()
