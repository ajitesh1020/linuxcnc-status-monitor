#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
fleet_sim.py — many simulated agents, for testing a dashboard end to end
=========================================================================
A test tool, not part of the installed agent. Each simulated machine uses the
agent's own modules, so the dashboard sees what real agents send:

  * status packets with session_id / packet_seq / cmd_state / cmd_serial, timed
    by agent_packets.PacketGate (1 s while active, 30 s heartbeat while idle,
    fault edges at once);
  * a real history journal (agent_journal.Journal) and history server
    (JournalServer) per machine, each on its own UDP port;
  * G-code programs streamed by agent_gcode.GcodeFileSender (--gcode).

Machines run cycles, finish parts, abort some, press E-stop, get short joint
faults, fail commands (G-code errors, sometimes two in a row) and restart
(a new session, counters back to 0). Every one of those is counted as it happens
and written to a ground-truth file; `check` then compares the dashboard's
history database with it — exact part counts and events, the review's gate 3.

    # 99 machines for 30 min, then 2 min of quiet so the dashboard can catch up
    python3 tools/fleet_sim.py run --machines 99 --minutes 30 --dir /tmp/fleet \\
        --gcode ~/linuxcnc/nc_files/*.ngc
    python3 tools/fleet_sim.py check --truth /tmp/fleet/truth.json --db <history.db>

Loss / reordering: use Linux netem on the PC (docs/FLEET_TEST.md). For a quick
look without it, --drop / --reorder lose or swap status packets in the sender.
"""
import argparse
import glob
import json
import os
import random
import socket
import sqlite3
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent_gcode import GcodeFileSender          # noqa: E402
from agent_journal import Journal, JournalServer  # noqa: E402
from agent_packets import PacketGate             # noqa: E402

PROTOCOL_VERSION = 1
RCS_DONE, RCS_EXEC, RCS_ERROR = 1, 2, 3
EXEC_ERROR, EXEC_DONE = 1, 2
STATE_ESTOP, STATE_ON = 1, 4
JOINTS = 3

# Dashboard event texts (FloorWatch machine_state.derive_events) that `check` counts.
EV_ESTOP = ("E-stop activated", "E-stop active")
EV_CMD = "Command failed (G-code error)"
EV_ABORT = "Program stopped before M2/M30"
EV_JOINT = "Joint {} amplifier fault"


def _span(text: str) -> Tuple[float, float]:
    lo, _, hi = text.partition(",")
    lo_f = float(lo)
    return lo_f, float(hi) if hi else lo_f


class Sender:
    """One UDP socket for every machine, with optional loss / reordering."""

    def __init__(self, target: Tuple[str, int], drop: float, reorder: float,
                 rng: random.Random) -> None:
        self.target = target
        self.drop, self.reorder, self.rng = drop, reorder, rng
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if target[0].endswith(".255"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.held: Dict[str, bytes] = {}
        self.sent = self.dropped = self.swapped = 0

    def raw(self, data: bytes) -> bool:
        try:
            self.sock.sendto(data, self.target)
            return True
        except OSError:
            return False

    def status(self, name: str, data: bytes) -> None:
        """A status packet, maybe lost or held back to go out after the next one."""
        if self.drop and self.rng.random() < self.drop:
            self.dropped += 1
            return
        if name in self.held:
            self.raw(data)
            self.raw(self.held.pop(name))     # the older one arrives second
            self.sent += 2
            self.swapped += 1
            return
        if self.reorder and self.rng.random() < self.reorder:
            self.held[name] = data
            return
        self.raw(data)
        self.sent += 1


class SimMachine:
    def __init__(self, index: int, args: argparse.Namespace, rng: random.Random,
                 sender: Sender, program: Optional[str]) -> None:
        self.name = f"{args.prefix}{index + 1:03d}"
        self.args, self.rng, self.sender, self.program = args, rng, sender, program
        self.journal = Journal(os.path.join(args.dir, f"{self.name}.journal.db"),
                               args.journal_interval, 14.0)
        self.port = args.journal_base_port + index if args.journal_base_port else 0
        self.server = JournalServer(self.journal.path, self.port, self.name,
                                    self.journal.agent_id, bind_host=args.journal_host)
        if self.port:
            self.server.start()
        self.gate = PacketGate(args.poll, args.heartbeat)
        self.gcode = (GcodeFileSender(1000, self.name, PROTOCOL_VERSION, args.gcode_resend)
                      if program else None)
        self.truth: Dict[str, Any] = {"parts": 0, "aborts": 0, "estops": 0,
                                      "cmd_failures": 0, "joint_faults": [0] * JOINTS,
                                      "sessions": 0, "agent_id": self.journal.agent_id,
                                      "journal_port": self.port}
        self.cmd_serial = rng.randint(1, 50)
        self._new_session(time.monotonic())

    # -- state ---------------------------------------------------------------
    def _new_session(self, now: float) -> None:
        self.session_id = uuid.uuid4().hex[:16]
        self.packet_seq = 0
        self.parts = self.aborts = 0
        self.state = "IDLE"
        self.cycle_start = self.cycle_end = 0.0
        self.abort_at: Optional[float] = None
        self.last_cycle_ms = 0
        self.estop_until = 0.0
        self.joint_until = [0.0] * JOINTS
        # Fault flags as of the last step. A new E-stop / joint fault may only
        # start once the end of the previous one has been sampled; otherwise two
        # faults merge into one that no packet can tell apart, and truth over-counts.
        self.estop_was = False
        self.joint_was = [False] * JOINTS
        self.cmd_state = RCS_DONE
        self.cmd_clear_at = 0.0
        self.again_at: Optional[float] = None
        self.next_start = now + self.rng.uniform(*_span(self.args.idle))
        self.gate = PacketGate(self.args.poll, self.args.heartbeat)
        self.truth["sessions"] += 1

    def _chance(self, per_hour: float, dt: float) -> bool:
        return per_hour > 0 and self.rng.random() < per_hour * dt / 3600.0

    def _abort(self) -> None:
        self.aborts += 1
        self.truth["aborts"] += 1
        self.state = "IDLE"
        self.cmd_state = RCS_DONE
        self.abort_at = None

    def _fail_command(self, now: float) -> None:
        self.cmd_serial += 1
        self.cmd_state = RCS_ERROR
        self.truth["cmd_failures"] += 1
        self.cmd_clear_at = now + self.rng.uniform(2.0, 20.0)

    def step(self, now: float, dt: float, quiet: bool) -> None:
        """Advance the machine. quiet = settling: no new cycles, faults or restarts."""
        a, rng = self.args, self.rng
        estop = now < self.estop_until
        if not quiet and self.state == "IDLE" and not estop and self._chance(a.restart_rate, dt):
            self._new_session(now)            # agent restarted: new session, counters at 0
            return
        if not quiet and not estop and not self.estop_was and self._chance(a.estop_rate, dt):
            self.estop_until = now + rng.uniform(0.3, 3.0)
            self.truth["estops"] += 1
            estop = True
            if self.state == "RUNNING":
                self._abort()                 # E-stop mid-cycle: never a finished part
        idle_ok = self.state == "IDLE" and not estop
        if not quiet and idle_ok and self.cmd_state != RCS_ERROR:
            if self._chance(a.joint_fault_rate, dt):
                j = rng.randrange(JOINTS)
                if now >= self.joint_until[j] and not self.joint_was[j]:
                    self.joint_until[j] = now + rng.uniform(0.2, 2.0)
                    self.truth["joint_faults"][j] += 1
            if self._chance(a.cmd_error_rate, dt):
                self._fail_command(now)       # e.g. a G-code error in MDI
                if rng.random() < 0.3:
                    self.again_at = now + 0.5     # and the operator tries again
        if self.again_at is not None and now >= self.again_at:
            self.again_at = None
            if self.cmd_state == RCS_ERROR and not estop:
                self._fail_command(now)
        if self.cmd_state == RCS_ERROR and now >= self.cmd_clear_at:
            self.cmd_serial += 1              # the next command succeeds
            self.cmd_state = RCS_DONE

        faulted = estop or any(now < t for t in self.joint_until)
        if self.state == "IDLE":
            if (not quiet and not faulted and self.cmd_state != RCS_ERROR
                    and now >= self.next_start):
                self.cmd_serial += 1          # cycle start is a command
                self.cmd_state = RCS_EXEC
                self.state = "RUNNING"
                self.cycle_start = now
                self.cycle_end = now + rng.uniform(*_span(a.cycle))
                self.abort_at = (now + (self.cycle_end - now) * rng.uniform(0.2, 0.9)
                                 if rng.random() < a.abort_prob else None)
        elif self.state == "RUNNING":
            if self.abort_at is not None and now >= self.abort_at:
                self._abort()
                self.next_start = now + rng.uniform(*_span(a.idle))
            elif now >= self.cycle_end:
                self.parts += 1
                self.truth["parts"] += 1
                self.last_cycle_ms = int((now - self.cycle_start) * 1000)
                self.state = "IDLE"
                self.cmd_state = RCS_DONE
                self.next_start = now + rng.uniform(*_span(a.idle))
        self.estop_was = now < self.estop_until
        self.joint_was = [now < t for t in self.joint_until]

    # -- packets -------------------------------------------------------------
    def _signature(self, now: float) -> Tuple:
        estop = now < self.estop_until
        return (estop, STATE_ESTOP if estop else STATE_ON, False,
                tuple((now < t, False) for t in self.joint_until),
                self.cmd_serial if self.cmd_state == RCS_ERROR else None)

    def _file_meta(self) -> Dict[str, Any]:
        if not self.program:
            return {"file_name": "", "file_size": 0, "file_modified_ms": 0}
        try:
            st = os.stat(self.program)
            return {"file_name": os.path.basename(self.program), "file_size": st.st_size,
                    "file_modified_ms": int(st.st_mtime * 1000)}
        except OSError:
            return {"file_name": os.path.basename(self.program), "file_size": 0,
                    "file_modified_ms": 0}

    def _payload(self, now: float) -> Dict[str, Any]:
        estop = now < self.estop_until
        running = self.state == "RUNNING"
        length = max(0.001, self.cycle_end - self.cycle_start)
        pct = round(min(99.9, 100.0 * (now - self.cycle_start) / length), 1) if running else 0.0
        line = int(pct * 10) + 1 if running else 0
        x = round(50.0 * pct / 100.0, 4)
        return {
            "type": "status", "proto": PROTOCOL_VERSION, "ts": int(time.time() * 1000),
            "machine_name": self.name, "session_id": self.session_id,
            "packet_seq": self.packet_seq,
            "cycle_state": self.state,
            "cycle_time_ms": int((now - self.cycle_start) * 1000) if running else 0,
            "parts_produced": self.parts, "abort_count": self.aborts,
            "run_from_here_count": 0, "last_cycle_ms": self.last_cycle_ms,
            "last_abort_ms": 0, "avg_cycle_ms": self.last_cycle_ms or None,
            "total_completed_cycles": self.parts, "cycle_complete_signalled": False,
            "is_run_from_here": False, "partial_parts": 0, "partial_part_equiv": 0.0,
            "last_partial_pct": None, "program_progress_pct": pct, "part_in_progress": running,
            "gcode_end_line": 1002, "gcode_first_exec_line": 1, "gcode_tail_line": 1001,
            "task_state": STATE_ESTOP if estop else STATE_ON,
            "task_mode": 2 if running else 1, "interp_state": 2 if running else 1,
            "exec_state": EXEC_DONE, "estop": estop, "enabled": not estop, "paused": False,
            "cmd_state": self.cmd_state, "cmd_serial": self.cmd_serial,
            "tool_in_spindle": 1, "g5x_index": 1, "wcs": "G54",
            "g5x_offset": [0.0] * 9, "g92_offset": [0.0] * 9, "tool_offset": [0.0] * 9,
            "gcodes": [], "mcodes": [], "settings": [],
            "current_vel": 25.0 if running else 0.0, "distance_to_go": 0.0,
            "motion_type": 2 if running else 0, "motion_line": line, "current_line": line,
            "delay_left": 0.0, "feedrate": 1.0, "rapidrate": 1.0,
            "axis": {n: {"pos": x, "work": x, "vel": 0.0, "min_pos_limit": -500.0,
                         "max_pos_limit": 500.0} for n in "xyz"},
            "joints": [{"id": j, "pos": x, "vel": 0.0, "homed": True,
                        "fault": now < self.joint_until[j], "ferror": 0.0,
                        "hard_limit": False} for j in range(JOINTS)],
            "spindles": [{"id": 0, "speed": 12000.0 if running else 0.0, "direction": 1 if running else 0,
                          "override": 1.0, "at_speed": running, "enabled": running}],
            **self._file_meta(),
            "nml_errors": [],
        }

    def tick(self, now: float) -> None:
        """Send a status packet when the agent would (agent_packets.PacketGate)."""
        if not self.gate.tick(now, self.state, self._signature(now)):
            return
        if self.gcode:
            self.gcode.check_and_send(self.program, self._file_meta(), self.sender.raw)
        if not self.gate.should_send(now, self.state):
            return
        self.packet_seq += 1
        payload = self._payload(now)
        self.journal.record(payload, now)
        if self.journal.enabled:
            payload["agent_id"] = self.journal.agent_id
            payload["journal_seq"] = self.journal.last_seq
            payload["journal_port"] = self.port if self.server.alive else 0
        self.sender.status(self.name, json.dumps(payload, separators=(",", ":")).encode())
        self.gate.sent(now, self.state)

    def close(self) -> None:
        self.server.stop()
        self.journal.close()


def _write_truth(path: str, machines: List[SimMachine], args: argparse.Namespace,
                 sender: Sender, final: bool) -> None:
    data = {"final": final, "written_at": int(time.time() * 1000),
            "args": {k: v for k, v in vars(args).items() if k != "func"},
            "sent": sender.sent, "dropped": sender.dropped, "swapped": sender.swapped,
            "machines": {m.name: m.truth for m in machines}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def cmd_run(args: argparse.Namespace) -> int:
    os.makedirs(args.dir, exist_ok=True)
    host, _, port = args.target.rpartition(":")
    target = (host or "127.0.0.1", int(port or 5005))
    if args.journal_host is None:
        args.journal_host = "127.0.0.1" if target[0].startswith("127.") else ""
    rng = random.Random(args.seed)
    programs = sorted({p for pat in args.gcode for p in glob.glob(os.path.expanduser(pat))})
    sender = Sender(target, args.drop, args.reorder, rng)
    machines = [SimMachine(i, args, rng, sender, programs[i % len(programs)] if programs else None)
                for i in range(args.machines)]
    truth = args.truth or os.path.join(args.dir, "truth.json")
    start = time.monotonic()
    busy_until = start + args.minutes * 60.0
    end = busy_until + args.settle
    print(f"fleet_sim: {len(machines)} machines -> {target[0]}:{target[1]}, history ports "
          f"{args.journal_base_port}..{args.journal_base_port + len(machines) - 1} "
          f"on {args.journal_host or 'all interfaces'}, {len(programs)} program(s); "
          f"{args.minutes} min + {args.settle:.0f} s settle. Truth: {truth}", flush=True)
    last, last_report, last_truth = start, start, start
    try:
        while True:
            now = time.monotonic()
            if now >= end:
                break
            dt, last = now - last, now
            quiet = now >= busy_until
            for m in machines:
                m.step(now, dt, quiet)
                m.tick(now)
            if now - last_truth >= 30:
                last_truth = now
                _write_truth(truth, machines, args, sender, final=False)
            if now - last_report >= args.report_s:
                last_report = now
                tot = {k: sum(m.truth[k] for m in machines)
                       for k in ("parts", "aborts", "estops", "cmd_failures", "sessions")}
                print(f"  {now - start:6.0f} s {'(settling) ' if quiet else ''}"
                      f"sent {sender.sent} dropped {sender.dropped} swapped {sender.swapped} "
                      + " ".join(f"{k}={v}" for k, v in tot.items()), flush=True)
            time.sleep(max(0.0, args.tick - (time.monotonic() - now)))
    except KeyboardInterrupt:
        print("interrupted: the truth file says final=false", flush=True)
        _write_truth(truth, machines, args, sender, final=False)
        for m in machines:
            m.close()
        return 130
    _write_truth(truth, machines, args, sender, final=True)
    for m in machines:
        m.close()
    print(f"done. Check with: python3 tools/fleet_sim.py check --truth {truth} --db <history.db>")
    return 0


def _counts(conn: sqlite3.Connection, key: str) -> Dict[str, Any]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    parts = conn.execute("SELECT COUNT(*) FROM completed_cycles WHERE machine_key=?",
                         (key,)).fetchone()[0]
    ev = dict(conn.execute("SELECT text, COUNT(*) FROM machine_events WHERE machine_key=? "
                           "GROUP BY text", (key,)).fetchall())
    gaps = (conn.execute("SELECT COUNT(*) FROM history_gaps WHERE machine_key=?",
                         (key,)).fetchone()[0] if "history_gaps" in tables else None)
    return {"parts": parts,
            "aborts": sum(n for t, n in ev.items() if t.startswith(EV_ABORT)),
            "estops": sum(ev.get(t, 0) for t in EV_ESTOP),
            "cmd_failures": ev.get(EV_CMD, 0),
            "joint_faults": [ev.get(EV_JOINT.format(j), 0) for j in range(JOINTS)],
            "gaps": gaps}


def cmd_check(args: argparse.Namespace) -> int:
    with open(args.truth, encoding="utf-8") as f:
        truth = json.load(f)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    fields = ("parts", "aborts", "estops", "cmd_failures", "joint_faults")
    bad, rows = 0, []
    for name, t in sorted(truth["machines"].items()):
        got = _counts(conn, name)
        diff = [f for f in fields if got[f] != t[f]]
        bad += bool(diff)
        rows.append((name, t, got, diff))
    conn.close()
    if not truth.get("final"):
        print("WARNING: the truth file is not final (the run was interrupted or is still going)")
    print(f"{'machine':<10} {'parts':>11} {'aborts':>9} {'E-stops':>9} {'cmd fail':>9} "
          f"{'joint faults':>18} gaps  result")
    for name, t, got, diff in rows:
        if diff or args.verbose:
            def pair(f):
                return f"{got[f]}/{t[f]}"
            print(f"{name:<10} {pair('parts'):>11} {pair('aborts'):>9} {pair('estops'):>9} "
                  f"{pair('cmd_failures'):>9} {pair('joint_faults'):>18} "
                  f"{got['gaps'] if got['gaps'] is not None else '-':>4}  "
                  f"{'MISMATCH ' + ','.join(diff) if diff else 'ok'}")
    total = {f: (sum(sum(g[f]) if f == "joint_faults" else g[f] for _, _, g, _ in rows),
                 sum(sum(t[f]) if f == "joint_faults" else t[f] for _, t, _, _ in rows))
             for f in fields}
    print("totals (dashboard/truth): " + "  ".join(f"{f} {a}/{b}" for f, (a, b) in total.items()))
    print(f"sender: sent {truth.get('sent')} dropped {truth.get('dropped')} "
          f"swapped {truth.get('swapped')}")
    print(f"{len(rows) - bad} of {len(rows)} machines match exactly"
          + ("" if not bad else f"; {bad} do not"))
    return 1 if bad else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="simulate a fleet")
    r.add_argument("--machines", type=int, default=99)
    r.add_argument("--minutes", type=float, default=30.0, help="busy time")
    r.add_argument("--settle", type=float, default=120.0,
                   help="seconds of quiet at the end (heartbeats only) so the dashboard "
                        "can fetch any history it missed")
    r.add_argument("--target", default="127.0.0.1:5005", help="dashboard host:port")
    r.add_argument("--dir", default="fleet-sim", help="journals and truth.json go here")
    r.add_argument("--truth", default="", help="ground-truth file (default <dir>/truth.json)")
    r.add_argument("--prefix", default="SIM-", help="machine names: SIM-001, SIM-002…")
    r.add_argument("--journal-base-port", type=int, default=6100,
                   help="machine n's history server is on this port + n - 1 (0 = none)")
    r.add_argument("--journal-host", default=None,
                   help="address the history servers listen on (default 127.0.0.1 when "
                        "the target is local, else all interfaces)")
    r.add_argument("--journal-interval", type=float, default=10.0)
    r.add_argument("--gcode", nargs="*", default=[], help="program files / globs")
    r.add_argument("--gcode-resend", type=float, default=60.0)
    r.add_argument("--cycle", default="20,60", help="cycle length range, seconds")
    r.add_argument("--idle", default="3,15", help="time between cycles, seconds")
    r.add_argument("--abort-prob", type=float, default=0.05, help="per cycle")
    r.add_argument("--estop-rate", type=float, default=2.0, help="per machine-hour")
    r.add_argument("--joint-fault-rate", type=float, default=2.0, help="per machine-hour")
    r.add_argument("--cmd-error-rate", type=float, default=4.0, help="per machine-hour")
    r.add_argument("--restart-rate", type=float, default=0.3, help="per machine-hour")
    r.add_argument("--poll", type=float, default=1.0)
    r.add_argument("--heartbeat", type=float, default=30.0)
    r.add_argument("--tick", type=float, default=0.1, help="sample interval, seconds")
    r.add_argument("--drop", type=float, default=0.0,
                   help="lose this share of status packets in the sender (0-1)")
    r.add_argument("--reorder", type=float, default=0.0,
                   help="swap this share of status packets with the next one (0-1)")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--report-s", type=float, default=60.0)
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("check", help="compare a dashboard history database with truth.json")
    c.add_argument("--truth", required=True)
    c.add_argument("--db", required=True, help="the dashboard's history.db")
    c.add_argument("-v", "--verbose", action="store_true", help="list every machine")
    c.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
