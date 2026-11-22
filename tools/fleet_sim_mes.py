#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
fleet_sim_mes.py — MES traffic for tools/fleet_sim.py (MES plan P4.5)
====================================================================
A test tool, not part of the installed agent. With `fleet_sim.py run --mes`,
every simulated machine also has a scanner, an operator and a MillWright-like
panel, and sends contract-valid MES v1 events (contracts/mes/v1) to the site
service the way the real agent does: each event is stored in the agent's own
spool (agent_mes_spool.Spool) and sent by the agent's own forwarder
(agent_mes_forward.Forwarder: mTLS POST /v1/events, batches of at most 200,
the same bytes again after any failure).

Two producers per machine, each with a stable id (kept in
<dir>/<name>.mes_producers.json), a new epoch per run and its own rising seq:

  agent      scan.received, binding.requested, binding.cleared
  millwright operator.session_started / _ended, execution.started /
             execution.ended, tool.snapshot (site_id and machine_id null:
             MillWright cannot know them; the service takes them from the
             forwarding agent's certificate, contract rule 7)

Scenarios (each recorded in truth.json under "mes"):

  unit             one unit label, position A, a normal run
  panel            2-4 panel labels in positions P1..Pn, one run
  lot              a GS1 lot label (base64, GS separated), no position
  duplicate_read   the same label read twice; the second read re-uses position A
                   (the first binding is superseded): one attempt
  wrong_revision   a label of revision R2 (no profile matches: unresolved);
                   the operator clears it and runs a good unit instead
  setup            a dry run or probe first (setup attempt, not production),
                   then the real run
  run_from_here    a run aborted, then run-from-here (a continuation)
  aborted          a run aborted
  interrupted      a run ending interrupted_unknown (interrupted_run exception)
  two_heads        tool.snapshot with two heads: still one attempt
  operator_change  the operator is replaced mid-run (a participant interval)
  late_scan        a run with nothing bound; the label is scanned after the end
                   (no attempt: a supervisor would attach it, never by time)
  conflict         the same unit scanned and running on two machines at once:
                   one of the two attempts held, one unit_on_two_machines exception

The operator waits for the readout to show the scans bound (green) before
starting a run that uses them, as the work instruction says: the simulator asks
the service (GET /v1/agents/{id}/feedback, the readout's own source). The
service resolves scans in the background, after a batch is stored; a run started
before that would record no binding and no fixture position, and the two-machine
check would not see the unit (docs/MES_BINDING.md "Identity found after the
start"). The two runs of a conflict both keep running until both starts are
acknowledged, so the service sees them overlap whatever the network does.

`fleet_sim.py check --mes-db <ledger.db>` (or --mes-url, as a desktop user)
then compares the service's record with truth: every attempt of every unit
(no missing, extra or duplicated attempts; state, mode, production, held,
continuation, fixture position, binding, operator, participants, heads),
bindings, scan states and exceptions.
"""
import base64
import hashlib
import http.client
import json
import logging
import os
import random
import re
import shlex
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONTRACT_DIR = os.path.join(ROOT, "contracts", "mes", "v1")
PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_sim_profiles.json")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger("fleet_sim.mes")

SCHEMA = "fw.mes.event/1"
SCENARIOS = ("unit", "panel", "lot", "duplicate_read", "wrong_revision", "setup", "run_from_here",
             "aborted", "interrupted", "two_heads", "operator_change", "late_scan", "conflict")
WEIGHTS = {"unit": 30, "panel": 10, "lot": 8, "duplicate_read": 6, "wrong_revision": 4, "setup": 8,
           "run_from_here": 5, "aborted": 5, "interrupted": 3, "two_heads": 8, "operator_change": 4,
           "late_scan": 4, "conflict": 5}
SETUP_MODES = ("dryrun", "probe")
END_STATE = {"completed": "completed_pending_inspection", "aborted": "aborted",
             "interrupted_unknown": "interrupted_unknown"}
GS = "\x1d"
QUARANTINE_KINDS = ("event_id_conflict", "seq_conflict", "newer_contract")
MILLWRIGHT_VERSION = "0.9.0-sim"


def contract():
    """The vendored contract's own checker (contracts/mes/v1/validate.py)."""
    if CONTRACT_DIR not in sys.path:
        sys.path.insert(0, CONTRACT_DIR)
    import validate  # noqa: E402
    return validate


def agent_version() -> str:
    with open(os.path.join(ROOT, "status.py"), encoding="utf-8") as f:
        m = re.search(r'^AGENT_VERSION: str = "([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0"


def new_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def utc(ts: float) -> str:
    ms = int(round(ts * 1000))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)


def gtin14(body13: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = gtin14("0950110153000")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _span(text) -> Tuple[float, float]:
    if isinstance(text, (tuple, list)):
        return float(text[0]), float(text[1])
    lo, _, hi = str(text).partition(",")
    return float(lo), float(hi) if hi else float(lo)


# ---------------------------------------------------------------------------
# Producers and events
# ---------------------------------------------------------------------------
class Producer:
    """One event producer (the agent or MillWright) of one machine."""

    def __init__(self, kind: str, pid: str, epoch: str, app: str, version: str, boot_id: str,
                 record: str) -> None:
        self.kind, self.id, self.epoch = kind, pid, epoch
        self.app, self.version, self.boot_id, self.record = app, version, boot_id, record
        self.seq = 0

    def event(self, rng: random.Random, type_: str, payload: Dict[str, Any], at: float, mono_ms: int,
              correlation: Dict[str, Any], site_id: Optional[str],
              machine_id: Optional[str]) -> Dict[str, Any]:
        self.seq += 1
        corr = {"execution_id": None, "scan_id": None, "operator_session_id": None}
        corr.update(correlation)
        return {
            "schema": SCHEMA, "event_id": new_uuid(rng), "type": type_,
            "producer": {"kind": self.kind, "id": self.id, "epoch": self.epoch, "seq": self.seq},
            "site_id": site_id, "machine_id": machine_id, "occurred_at": utc(at),
            "clock": {"synced": True, "boot_id": self.boot_id, "mono_ms": max(0, int(mono_ms))},
            "correlation": corr,
            "source": {"app": self.app, "version": self.version, "record": "%s:%d" % (self.record, self.seq)},
            "payload": payload,
        }


# -- waits a job yields ------------------------------------------------------
class Sleep:
    def __init__(self, until: float) -> None:
        self.until = until


class Bound:
    """The operator waits until the readout shows these scans bound."""

    def __init__(self, scan_ids: List[str]) -> None:
        self.scan_ids = list(scan_ids)


class Acked:
    """Until the service has acknowledged these events: [(machine, event_id)]."""

    def __init__(self, items: List[Tuple[Any, str]]) -> None:
        self.items = list(items)


class Until:
    def __init__(self, test: Callable[[], bool]) -> None:
        self.test = test


class InstantOracle:
    """No service (tests, dry runs): every scan is bound and every event acknowledged at once."""

    def bound(self, machine: "MesMachine", scan_ids: List[str]) -> bool:
        return True

    def acked(self, machine: "MesMachine", event_ids: List[str]) -> bool:
        return True


# ---------------------------------------------------------------------------
# Truth
# ---------------------------------------------------------------------------
class Truth:
    """What the service must end up with, built as the events are made."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.units: Dict[str, Dict[str, Any]] = {}
        self.scans: Dict[str, Dict[str, Any]] = {}       # scans outside any unit (unresolved)
        self.exceptions: List[Dict[str, Any]] = []
        self.executions: List[str] = []
        self.scenarios: Dict[str, int] = {}

    def unit(self, namespace: str, ident: str, kind: str, scenario: str) -> Dict[str, Any]:
        key = "%s/%s" % (namespace, ident)
        if key not in self.units:
            self.units[key] = {"namespace": namespace, "id": ident, "entity_kind": kind, "scenario": scenario,
                               "attempts": [], "scans": [], "bindings": []}
        return self.units[key]

    def as_dict(self) -> Dict[str, Any]:
        return {"tag": self.tag, "scenarios": dict(sorted(self.scenarios.items())),
                "units": self.units, "scans": self.scans, "exceptions": self.exceptions,
                "executions": self.executions}


class Label:
    """A label on a board, panel or lot: what the scanner reads and what it identifies."""

    def __init__(self, raw: str, namespace: Optional[str], ident: Optional[str], kind: str,
                 encoding: str = "utf8", symbology: Optional[str] = None) -> None:
        self.raw, self.namespace, self.ident, self.kind = raw, namespace, ident, kind
        self.encoding, self.symbology = encoding, symbology


# ---------------------------------------------------------------------------
# One machine
# ---------------------------------------------------------------------------
class MesMachine:
    def __init__(self, fleet: "MesFleet", index: int, name: str, machine_id: str,
                 producer_ids: Dict[str, str], site_id: Optional[str]) -> None:
        self.fleet, self.index, self.name, self.machine_id = fleet, index, name, machine_id
        self.site_id = site_id
        rng = fleet.ids
        boot = rng.getrandbits(64).to_bytes(8, "big").hex()
        self.agent = Producer("agent", producer_ids["agent"], new_uuid(rng), "linuxcnc-status-agent",
                              fleet.agent_version, boot, "spool")
        self.mw = Producer("millwright", producer_ids["millwright"], new_uuid(rng), "millwright",
                           MILLWRIGHT_VERSION, boot, "outbox")
        self.now = 0.0
        self.job = None
        self.wait: Any = None
        self.next_job_at = 0.0
        self.jobs_done = 0
        self.operator: Optional[Tuple[str, str]] = None      # (session id, operator name)
        self.operators = 0
        self.running: List[Dict[str, Any]] = []              # truth attempts of the current run
        self.closed = False
        self.events = 0
        self.scenarios: Dict[str, int] = {}

    # -- emitting --------------------------------------------------------------
    def _emit(self, producer: Producer, type_: str, payload: Dict[str, Any],
              corr: Dict[str, Any]) -> Dict[str, Any]:
        f = self.fleet
        mill = producer.kind == "millwright"
        e = producer.event(f.ids, type_, payload, f.wall(self.now), int(self.now * 1000) + 1000,
                           corr, None if mill else self.site_id, None if mill else self.machine_id)
        self.events += 1
        f.emit(self, e)
        return e

    def scan(self, label: Label) -> str:
        sid = new_uuid(self.fleet.ids)
        payload = {"scanner_id": "%s-scanner" % self.name.lower(), "raw": label.raw,
                   "raw_encoding": label.encoding, "framing": "enter"}
        if label.symbology:
            payload["symbology"] = label.symbology
        self._emit(self.agent, "scan.received", payload, {"scan_id": sid})
        return sid

    def bind(self, scan_ids: List[str], positions: Dict[str, str], reason: str = "next run") -> None:
        self._emit(self.agent, "binding.requested",
                   {"scan_ids": list(scan_ids), "fixture_positions": dict(positions), "reason": reason}, {})

    def clear(self, scan_ids: List[str], reason: str) -> None:
        self._emit(self.agent, "binding.cleared", {"scan_ids": list(scan_ids), "reason": reason}, {})

    def session_start(self, reason: str = "login") -> None:
        self.operators += 1
        sid = new_uuid(self.fleet.ids)
        self.operator = (sid, "op-%s-%d" % (self.name.lower(), self.operators))
        self._emit(self.mw, "operator.session_started",
                   {"operator_name": self.operator[1], "role": "operator", "assurance": "selected_name",
                    "reason": reason}, {"operator_session_id": sid})

    def session_end(self, reason: str) -> None:
        if self.operator is None:
            return
        sid, name = self.operator
        self._emit(self.mw, "operator.session_ended",
                   {"operator_name": name, "role": "operator", "assurance": "selected_name", "reason": reason},
                   {"operator_session_id": sid})
        self.operator = None

    def start(self, scan_ids: List[str], mode: str, program: str) -> Tuple[str, Dict[str, Any]]:
        eid = new_uuid(self.fleet.ids)
        path = "/home/cnc/linuxcnc/nc_files/sim/%s.ngc" % program
        run_file = _sha(path + ":tail" if mode == "run_from_here" else path)
        e = self._emit(self.mw, "execution.started",
                       {"program_path": path, "program_sha256": _sha(path), "run_file_sha256": run_file,
                        "mode": mode, "wcs": "G54", "offsets": {"x": 120.5, "y": -40.25, "z": 0.0},
                        "bound_scan_ids": list(scan_ids)},
                       {"execution_id": eid, "operator_session_id": self.operator[0] if self.operator else None})
        self.fleet.truth.executions.append(eid)
        return eid, e

    def tools(self, eid: str, heads: int) -> None:
        h = [{"head": 0, "tool": 3, "type": "drill", "diameter": 0.8, "usage": {"holes": 120}}]
        if heads > 1:
            h.append({"head": 1, "tool": 7, "type": "router", "diameter": 2.0, "usage": {"cuts": 4}})
        self._emit(self.mw, "tool.snapshot", {"heads": h},
                   {"execution_id": eid, "operator_session_id": self.operator[0] if self.operator else None})

    def end(self, eid: str, outcome: str, seconds: float) -> None:
        self._emit(self.mw, "execution.ended",
                   {"outcome": outcome, "seconds": round(max(0.0, seconds), 3), "last_line": 1001,
                    "S": [12000.0], "F": [600.0], "overrides": {"feed": 1.0, "rapid": 1.0, "spindle": 1.0}},
                   {"execution_id": eid, "operator_session_id": self.operator[0] if self.operator else None})

    # -- truth helpers -----------------------------------------------------------
    def _scan_truth(self, label: Label, sid: str, scenario: str, status: Optional[str]) -> Dict[str, Any]:
        u = self.fleet.truth.unit(label.namespace, label.ident, label.kind, scenario)
        rec = {"scan_id": sid, "machine": self.name, "machine_id": self.machine_id, "status": status}
        u["scans"].append(rec)
        return rec

    def _binding_truth(self, label: Label, sid: str, position: Optional[str], scenario: str) -> Dict[str, Any]:
        u = self.fleet.truth.unit(label.namespace, label.ident, label.kind, scenario)
        rec = {"scan_id": sid, "machine_id": self.machine_id, "fixture_position": position, "state": "bound"}
        u["bindings"].append(rec)
        return rec

    def _attempt_truth(self, label: Label, sid: str, eid: str, mode: str, position: Optional[str],
                       bound: bool, scenario: str, continuation_of: Optional[Dict[str, str]] = None,
                       held: Any = False, heads: int = 1) -> Dict[str, Any]:
        u = self.fleet.truth.unit(label.namespace, label.ident, label.kind, scenario)
        setup = mode in SETUP_MODES
        rec = {"machine": self.name, "machine_id": self.machine_id, "execution_id": eid, "scan_id": sid,
               "mode": mode, "state": "setup" if setup else "running",
               "counts_as_production": not setup and continuation_of is None,
               "continuation_of": continuation_of, "fixture_position": position, "bound": bound,
               "held": held, "operator_session_id": self.operator[0] if self.operator else None,
               "participants": [], "heads": heads}
        u["attempts"].append(rec)
        self.running.append(rec)
        return rec

    def _ended(self, outcome: str, scans: List[Dict[str, Any]]) -> None:
        for a in self.running:
            if a["state"] != "setup":
                a["state"] = END_STATE[outcome]
        for s in scans:
            s["status"] = "done"
        self.running = []

    # -- scheduling ----------------------------------------------------------------
    def sleep(self, seconds: float) -> Sleep:
        return Sleep(self.now + seconds)

    def _ready(self) -> bool:
        w = self.wait
        if w is None:
            return True
        if isinstance(w, Sleep):
            return self.now >= w.until
        if isinstance(w, Bound):
            return self.fleet.oracle.bound(self, w.scan_ids)
        if isinstance(w, Acked):
            by: Dict[Any, List[str]] = {}
            for m, eid in w.items:
                by.setdefault(m, []).append(eid)
            return all(self.fleet.oracle.acked(m, ids) for m, ids in by.items())
        if isinstance(w, Until):
            return bool(w.test())
        raise TypeError(w)

    def step(self, now: float, quiet: bool) -> None:
        self.now = now
        if self.closed:
            return
        if self.operator is None and self.job is None and not quiet and self.operators == 0:
            self.session_start()
        for _ in range(20):                       # a job may get past several waits in one step
            if self.job is None:
                if quiet or now < self.next_job_at:
                    return
                self.job = self.fleet.next_job(self)
                self.wait = None
            if not self._ready():
                return
            try:
                self.wait = self.job.send(None)
            except StopIteration:
                self.job, self.wait = None, None
                self.jobs_done += 1
                self.next_job_at = now + self.fleet.rng.uniform(*self.fleet.idle)
                return

    @property
    def idle(self) -> bool:
        return self.job is None

    def close(self) -> None:
        """End of the simulation: the operator logs out."""
        if not self.closed and self.job is None:
            self.session_end("logout")
            self.closed = True

    # -- job building blocks ---------------------------------------------------------
    def _run(self, eid: str, start_at: float, outcome: str, length: float, mid=None):
        """Wait out the run (calling `mid` halfway), then end it."""
        if mid is not None:
            yield self.sleep(length / 2)
            mid()
        yield Until(lambda: self.now >= start_at + length)

    def _cycle(self) -> float:
        return self.fleet.rng.uniform(*self.fleet.cycle)

    def _bound_run(self, scenario: str, labels: List[Label], positions: List[Optional[str]],
                   mode: str = "normal", outcome: str = "completed", heads: int = 1, mid=None,
                   program: str = "board"):
        """Scan every label, bind them, wait for green, run once."""
        sids = [self.scan(lb) for lb in labels]
        scans = [self._scan_truth(lb, sid, scenario, "bound") for lb, sid in zip(labels, sids)]
        pos_map = {p: sid for p, sid in zip(positions, sids) if p is not None}
        self.bind(sids, pos_map)
        binds = [self._binding_truth(lb, sid, p, scenario) for lb, sid, p in zip(labels, sids, positions)]
        yield Bound(sids)
        result = yield from self._run_listed(scenario, labels, sids, positions, scans, binds, mode,
                                             outcome, heads, mid, program)
        return result


    def _run_listed(self, scenario, labels, sids, positions, scans, binds, mode="normal",
                    outcome="completed", heads=1, mid=None, program="board", bound=True,
                    continuation=None, held: Any = False, on_start=None, before_end=None):
        """Start a run listing `sids`, wait out its length, end it; truth as it goes."""
        eid, start = self.start(sids, mode, program)
        self.tools(eid, heads)
        setup = mode in SETUP_MODES
        atts = [self._attempt_truth(lb, sid, eid, mode, p, bound and not setup, scenario,
                                    continuation_of=(continuation[i] if continuation else None),
                                    held=held, heads=heads)
                for i, (lb, sid, p) in enumerate(zip(labels, sids, positions))]
        if not setup:
            for s in scans:
                s["status"] = "running"
            for b in binds:
                b["state"] = "consumed"
        if on_start is not None:
            on_start(eid, start)
        t0, length = self.now, self._cycle()
        yield from self._run(eid, t0, outcome, length, mid)
        if before_end is not None:
            yield before_end
        self.end(eid, outcome, self.now - t0)
        self._ended(outcome, [] if setup else scans)
        return eid, atts

    # -- jobs ------------------------------------------------------------------------
    def make_job(self, scenario: str):
        self.scenarios[scenario] = self.scenarios.get(scenario, 0) + 1
        f = self.fleet
        f.truth.scenarios[scenario] = f.truth.scenarios.get(scenario, 0) + 1
        return getattr(self, "_job_" + scenario)(scenario)

    def _job_unit(self, scenario):
        outcome = "aborted" if self.fleet.rng.random() < 0.1 else "completed"
        yield from self._bound_run(scenario, [self.fleet.unit_label()], ["A"], outcome=outcome)

    def _job_aborted(self, scenario):
        yield from self._bound_run(scenario, [self.fleet.unit_label()], ["A"], outcome="aborted")

    def _job_interrupted(self, scenario):
        eid, atts = yield from self._bound_run(scenario, [self.fleet.unit_label()], ["A"],
                                               outcome="interrupted_unknown")
        self.fleet.truth.exceptions.append({"kind": "interrupted_run", "ref": eid, "attempts": len(atts),
                                            "machine": self.name})

    def _job_two_heads(self, scenario):
        yield from self._bound_run(scenario, [self.fleet.unit_label()], ["A"], heads=2)

    def _job_panel(self, scenario):
        n = self.fleet.rng.randint(2, 4)
        labels = [self.fleet.panel_label() for _ in range(n)]
        yield from self._bound_run(scenario, labels, ["P%d" % (i + 1) for i in range(n)], program="panel")

    def _job_lot(self, scenario):
        yield from self._bound_run(scenario, [self.fleet.lot_label()], [None], program="lot")

    def _job_operator_change(self, scenario):
        def hand_over():
            self.session_end("replaced")
            self.session_start("shift change")
            for a in self.running:                    # a participant from now to the run's end
                a["participants"].append(self.operator[0])
        yield from self._bound_run(scenario, [self.fleet.unit_label()], ["A"], mid=hand_over)

    def _job_duplicate_read(self, scenario):
        label = self.fleet.unit_label()
        first = self.scan(label)
        self._scan_truth(label, first, scenario, None)          # validated or superseded: timing
        self.bind([first], {"A": first})
        b1 = self._binding_truth(label, first, "A", scenario)
        yield self.sleep(0.3)
        second = self.scan(label)                               # the same label read again
        s2 = self._scan_truth(label, second, scenario, "bound")
        self.bind([second], {"A": second})
        b1["state"] = "superseded"                              # position A re-used
        b2 = self._binding_truth(label, second, "A", scenario)
        yield Bound([second])
        yield from self._run_listed(scenario, [label], [second], ["A"], [s2], [b2])

    def _job_wrong_revision(self, scenario):
        wrong = self.fleet.unit_label(rev="R2")
        sid = self.scan(wrong)
        self.bind([sid], {"A": sid})
        self.fleet.truth.scans[sid] = {"machine": self.name, "machine_id": self.machine_id, "raw": wrong.raw,
                                       "status": "unresolved", "binding": "superseded", "scenario": scenario}
        yield self.sleep(self.fleet.rng.uniform(1.0, 3.0))      # amber on the readout: the operator looks
        self.clear([sid], "wrong revision R2")
        yield from self._bound_run(scenario, [self.fleet.unit_label()], ["A"])

    def _job_setup(self, scenario):
        label = self.fleet.unit_label()
        sid = self.scan(label)
        s = self._scan_truth(label, sid, scenario, "bound")
        self.bind([sid], {"A": sid})
        b = self._binding_truth(label, sid, "A", scenario)
        yield Bound([sid])
        mode = self.fleet.rng.choice(SETUP_MODES)
        yield from self._run_listed(scenario, [label], [sid], ["A"], [s], [b], mode=mode)
        yield self.sleep(self.fleet.rng.uniform(0.5, 2.0))
        yield from self._run_listed(scenario, [label], [sid], ["A"], [s], [b])

    def _job_run_from_here(self, scenario):
        label = self.fleet.unit_label()
        eid, atts = yield from self._bound_run(scenario, [label], ["A"], outcome="aborted")
        sid = atts[0]["scan_id"]
        unit = self.fleet.truth.unit(label.namespace, label.ident, label.kind, scenario)
        scans = [r for r in unit["scans"] if r["scan_id"] == sid]
        yield self.sleep(self.fleet.rng.uniform(0.5, 2.0))
        yield from self._run_listed(scenario, [label], [sid], [None], scans, [], mode="run_from_here",
                                    bound=False, continuation=[{"execution_id": eid, "scan_id": sid}])

    def _job_late_scan(self, scenario):
        label = self.fleet.unit_label()
        eid, _ = self.start([], "normal", "board")              # the operator forgot to scan
        self.tools(eid, 1)
        t0, length = self.now, self._cycle()
        yield Until(lambda: self.now >= t0 + length)
        self.end(eid, "completed", self.now - t0)
        yield self.sleep(self.fleet.rng.uniform(0.5, 2.0))
        sid = self.scan(label)                                  # after the run: never bound
        self._scan_truth(label, sid, scenario, "validated")

    def _job_conflict(self, scenario):
        """Lead side: wait for a partner machine, then run the same unit as it."""
        pair = self.fleet.want_partner(self)
        yield Until(lambda: pair["partner"] is not None or pair["cancelled"])
        if pair["cancelled"]:
            return
        yield from self._conflict_side(pair, scenario)

    def _conflict_side(self, pair, scenario):
        label = pair["label"]
        sid = self.scan(label)
        s = self._scan_truth(label, sid, scenario, "bound")
        self.bind([sid], {"A": sid})
        b = self._binding_truth(label, sid, "A", scenario)
        yield Bound([sid])
        f = self.fleet

        def started(eid, start_event):
            pair["starts"][self.name] = (self, eid, start_event["event_id"])
            if len(pair["starts"]) == 2:
                f.truth.exceptions.append({"kind": "unit_on_two_machines",
                                           "ref_one_of": sorted(e for _m, e, _s in pair["starts"].values()),
                                           "unit": "%s/%s" % (label.namespace, label.ident),
                                           "pair": pair["id"]})

        def both_known():
            # keep running until the service has both starts: then it saw the two runs overlap
            if len(pair["starts"]) < 2:
                return False
            return all(f.oracle.acked(m, [se]) for m, _e, se in pair["starts"].values())

        yield from self._run_listed(scenario, [label], [sid], ["A"], [s], [b], held="pair:" + pair["id"],
                                    on_start=started, before_end=Until(both_known))


# ---------------------------------------------------------------------------
# The fleet
# ---------------------------------------------------------------------------
class MesFleet:
    """Every machine's MES side. `emit(machine, event)` receives each event as
    it is made; `oracle` answers the operator's waits (InstantOracle without a
    service). Deterministic for a seed: the same machines, seed, clock and
    oracle answers give the same events, byte for byte (`nonce` mixes a run id
    into the identifiers so two live runs never reuse an event id)."""

    def __init__(self, machines: List[Tuple[str, str]], seed: Any = None, nonce: str = "",
                 cycle=(20.0, 60.0), idle=(5.0, 20.0), scenarios: Iterable[str] = SCENARIOS,
                 emit: Optional[Callable[[MesMachine, Dict[str, Any]], None]] = None,
                 oracle: Any = None, wall: Optional[Callable[[float], float]] = None,
                 producer_ids: Optional[Dict[str, Dict[str, str]]] = None,
                 site_id: Optional[str] = None) -> None:
        scenarios = list(scenarios)
        unknown = set(scenarios) - set(SCENARIOS)
        if unknown:
            raise ValueError("unknown MES scenario(s): %s" % ", ".join(sorted(unknown)))
        self.rng = random.Random("mes-plan:%s" % (seed,))
        self.ids = random.Random("mes-ids:%s:%s" % (seed, nonce))
        self.cycle, self.idle = _span(cycle), _span(idle)
        self.scenarios = [s for s in SCENARIOS if s in set(scenarios)]
        if len(machines) < 2 and "conflict" in self.scenarios:
            self.scenarios.remove("conflict")             # needs two machines
        if not self.scenarios:
            raise ValueError("no MES scenarios left to run")
        self.sink = emit
        self.oracle = oracle or InstantOracle()
        base = 1798761600.0                                # 2027-01-01T00:00:00Z, deterministic default
        self.wall = wall or (lambda now: base + now)
        self.agent_version = agent_version()
        self.tag = "%04X" % self.ids.getrandbits(16)
        self.truth = Truth(self.tag)
        self.serial = 0
        self.waiting: List[Dict[str, Any]] = []            # conflict leads without a partner
        self.pairs = 0
        self.machines: List[MesMachine] = []
        for i, (name, mid) in enumerate(machines):
            pids = (producer_ids or {}).get(name) or {"agent": new_uuid(self.ids),
                                                      "millwright": new_uuid(self.ids)}
            self.machines.append(MesMachine(self, i, name, mid, pids, site_id))

    # -- labels --------------------------------------------------------------------
    def _next(self) -> int:
        self.serial += 1
        return self.serial

    def unit_label(self, rev: str = "R3") -> Label:
        ident = "%s-%06d" % (self.tag, self._next())
        if rev != "R3":
            return Label("PCB-%s-%s" % (ident, rev), None, None, "unit")
        return Label("PCB-%s-%s" % (ident, rev), "pcb", ident, "unit")

    def panel_label(self) -> Label:
        ident = "%s-P%05d" % (self.tag, self._next())
        return Label("%s|BRD-A|R3" % ident, "panel", ident, "panel")

    def lot_label(self) -> Label:
        ident = "L%s%05d" % (self.tag, self._next())
        text = "]d201%s10%s%s17271231" % (GTIN, ident, GS)
        return Label(base64.b64encode(text.encode("ascii")).decode("ascii"), "lot", ident, "lot",
                     encoding="base64", symbology="]d2")

    # -- jobs ------------------------------------------------------------------------
    def want_partner(self, lead: MesMachine) -> Dict[str, Any]:
        self.pairs += 1
        pair = {"id": "%s-%d" % (self.tag, self.pairs), "lead": lead, "partner": None, "cancelled": False,
                "label": self.unit_label(), "starts": {}}
        self.waiting.append(pair)
        return pair

    def next_job(self, m: MesMachine):
        for pair in self.waiting:                          # a lead waits for a second machine: join it
            if pair["lead"] is not m and pair["partner"] is None:
                pair["partner"] = m
                self.waiting.remove(pair)
                m.scenarios["conflict"] = m.scenarios.get("conflict", 0) + 1
                return m._conflict_side(pair, "conflict")
        k = m.jobs_done
        if k < len(self.scenarios):                        # first, every scenario on every machine
            scenario = self.scenarios[(m.index + k) % len(self.scenarios)]
        else:
            scenario = self.rng.choices(self.scenarios, [WEIGHTS[s] for s in self.scenarios])[0]
        return m.make_job(scenario)

    def emit(self, m: MesMachine, event: Dict[str, Any]) -> None:
        if self.sink is not None:
            self.sink(m, event)

    def step(self, now: float, quiet: bool) -> None:
        if quiet:
            for pair in list(self.waiting):                # no partner will come now
                pair["cancelled"] = True
                self.waiting.remove(pair)
        for m in self.machines:
            m.step(now, quiet)

    @property
    def idle_all(self) -> bool:
        return all(m.idle for m in self.machines)

    def close(self) -> None:
        for m in self.machines:
            m.close()

    def truth_dict(self) -> Dict[str, Any]:
        d = self.truth.as_dict()
        d["machines"] = {m.name: {"machine_id": m.machine_id,
                                  "agent": {"id": m.agent.id, "epoch": m.agent.epoch, "seq": m.agent.seq},
                                  "millwright": {"id": m.mw.id, "epoch": m.mw.epoch, "seq": m.mw.seq},
                                  "events": m.events, "jobs_done": m.jobs_done, "busy": not m.idle,
                                  "scenarios": dict(sorted(m.scenarios.items()))}
                         for m in self.machines}
        return d


def simulate(fleet: MesFleet, seconds: float, settle: float = 600.0, dt: float = 0.25) -> float:
    """Drive a fleet on a virtual clock (no service): busy for `seconds`, then
    let running jobs finish and close the sessions. Returns the end time."""
    now = 0.0
    while now < seconds:
        fleet.step(now, False)
        now += dt
    end = now + settle
    while now < end and not fleet.idle_all:
        fleet.step(now, True)
        now += dt
    fleet.step(now, True)
    fleet.close()
    return now


# ---------------------------------------------------------------------------
# Live: enrolment, the agent's spool and forwarder, the operator's readout
# ---------------------------------------------------------------------------
_CODE_LINE = re.compile(r"^[A-Za-z0-9]{12}[0-9a-fA-F]{16}$")


class CodeSource:
    """One-time enrolment codes: from a file (one per line) or a command that
    prints one per call (e.g. `python -m floorwatch_service --data D enroll-code`;
    the machine name is added as `--note NAME`)."""

    def __init__(self, path: str = "", command: str = "") -> None:
        self.codes: List[str] = []
        if path:
            with open(path, encoding="utf-8") as f:
                self.codes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        self.command = command

    def take(self, name: str) -> str:
        if self.codes:
            return self.codes.pop(0)
        if self.command:
            out = subprocess.run(shlex.split(self.command, posix=os.name != "nt") + ["--note", name],
                                 capture_output=True, text=True, timeout=120)
            for line in out.stdout.splitlines():
                if _CODE_LINE.match(re.sub(r"[\s-]", "", line)):
                    return line.strip()
            raise RuntimeError("the enrolment code command printed no code: %s %s"
                               % (out.stdout.strip(), out.stderr.strip()))
        raise RuntimeError("no enrolment code left for %s: give --mes-codes FILE or --mes-code-cmd CMD" % name)


class Link:
    """One machine's way to the service: enrolment files, spool, forwarder
    (all the agent's own code)."""

    def __init__(self, directory: str, name: str, machine_id: str, url: str) -> None:
        self.name, self.machine_id, self.url = name, machine_id, url
        self.mes_dir = os.path.join(directory, "%s.mes" % name)
        self.spool_path = os.path.join(directory, "%s.mes_spool.db" % name)
        self.ids_path = os.path.join(directory, "%s.mes_producers.json" % name)
        self.spool = None
        self.forwarder = None
        self._reader: Optional[sqlite3.Connection] = None

    def producer_ids(self) -> Dict[str, str]:
        """Stable producer ids for this machine (a new epoch every run)."""
        try:
            with open(self.ids_path, encoding="utf-8") as f:
                doc = json.load(f)
            if all(isinstance(doc.get(k), str) for k in ("agent", "millwright")):
                return {"agent": doc["agent"], "millwright": doc["millwright"]}
        except (OSError, ValueError, AttributeError):
            pass
        doc = {"agent": str(uuid.uuid4()), "millwright": str(uuid.uuid4())}
        with open(self.ids_path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return doc

    def enrolled(self) -> Optional[Dict[str, Any]]:
        from agent_mes_forward import load_enrolment
        doc = load_enrolment(self.mes_dir)
        if doc and doc.get("machine_id") == self.machine_id and doc.get("service_url") == self.url.rstrip("/"):
            return doc
        return None

    def enrol(self, codes: CodeSource) -> Dict[str, Any]:
        from agent_mes_forward import enroll
        return enroll(self.url, codes.take(self.name), self.machine_id, self.name, self.mes_dir)

    def open(self, boot_id: str) -> None:
        from agent_mes_forward import Forwarder
        from agent_mes_spool import Spool
        self.spool = Spool(self.spool_path, self.machine_id, boot_id, agent_version())
        self.forwarder = Forwarder(self.spool, self.url, self.mes_dir, self.machine_id)
        self.forwarder.start()

    def store(self, event: Dict[str, Any], body: bytes) -> None:
        if event["producer"]["kind"] == "millwright":   # as read from MillWright's outbox
            p = event["producer"]
            self.spool.append([body], cursor=("millwright", p["epoch"], p["seq"]))
        else:
            self.spool.append([body])

    def pending(self) -> int:
        """Events not yet acknowledged (own read-only connection: the spool's
        is the forwarder thread's)."""
        if not self.spool:
            return 0
        if self._reader is None:
            self._reader = sqlite3.connect("file:%s?mode=ro" % self.spool_path, uri=True, timeout=10.0,
                                           check_same_thread=False)
        try:
            return int(self._reader.execute("SELECT COUNT(*) FROM spool WHERE state='pending'").fetchone()[0])
        except sqlite3.Error:
            return -1

    def close(self) -> None:
        if self.forwarder:
            self.forwarder.stop()
        if self._reader is not None:
            self._reader.close()
        if self.spool:
            self.spool.close()


class LiveOracle(threading.Thread):
    """Answers the operator's waits without blocking the simulation loop: one
    thread asks the service which scans are bound (the readout's feedback,
    GET /v1/agents/{id}/feedback) and reads each spool for acknowledged events,
    only for the machines that wait."""

    def __init__(self, links: Dict[str, Link], poll_s: float = 1.0) -> None:
        super().__init__(name="mes-oracle", daemon=True)
        self.links, self.poll_s = links, poll_s
        self.lock = threading.Lock()
        self.want_bound: Dict[str, set] = {}
        self.seen_bound: Dict[str, set] = {}
        self.want_acked: Dict[str, set] = {}
        self.seen_acked: Dict[str, set] = {}
        self.halt = threading.Event()
        self.errors = 0

    def bound(self, m: MesMachine, scan_ids: List[str]) -> bool:
        with self.lock:
            seen = self.seen_bound.setdefault(m.name, set())
            if set(scan_ids) <= seen:
                return True
            self.want_bound.setdefault(m.name, set()).update(set(scan_ids) - seen)
            return False

    def acked(self, m: MesMachine, event_ids: List[str]) -> bool:
        with self.lock:
            seen = self.seen_acked.setdefault(m.name, set())
            if set(event_ids) <= seen:
                return True
            self.want_acked.setdefault(m.name, set()).update(set(event_ids) - seen)
            return False

    def _feedback(self, link: Link, transports: Dict[str, Any]) -> set:
        from agent_mes_forward import Transport
        t = transports.get(link.name)
        if t is None:
            t = transports[link.name] = Transport(link.url, link.mes_dir, timeout=10.0)
        status, raw = t.request("GET", "/v1/agents/%s/feedback?since=0" % link.machine_id)
        if status != 200:
            raise OSError("feedback HTTP %d" % status)
        doc = json.loads(raw.decode("utf-8"))
        return {b["scan_id"] for b in doc.get("bound", []) if b.get("state") == "bound"}

    def run(self) -> None:
        transports: Dict[str, Any] = {}
        readers: Dict[str, sqlite3.Connection] = {}
        while not self.halt.is_set():
            with self.lock:
                bound = {n: set(v) for n, v in self.want_bound.items() if v}
                acked = {n: set(v) for n, v in self.want_acked.items() if v}
            for name, ids in bound.items():
                try:
                    got = self._feedback(self.links[name], transports) & ids
                except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
                    self.errors += 1
                    continue
                with self.lock:
                    self.seen_bound[name].update(got)
                    self.want_bound[name] -= got
            for name, ids in acked.items():
                try:
                    db = readers.get(name)
                    if db is None:
                        db = readers[name] = sqlite3.connect(
                            "file:%s?mode=ro" % self.links[name].spool_path, uri=True, timeout=5.0)
                    got = {r[0] for r in db.execute(
                        "SELECT event_id FROM spool WHERE state='acked' AND event_id IN (%s)"
                        % ",".join("?" * len(ids)), sorted(ids))}
                except sqlite3.Error:
                    self.errors += 1
                    continue
                with self.lock:
                    self.seen_acked[name].update(got)
                    self.want_acked[name] -= got
            self.halt.wait(self.poll_s)
        for db in readers.values():
            db.close()

    def stop(self) -> None:
        self.halt.set()
        self.join(5.0)


class LiveMes:
    """The MES side of a live `fleet_sim.py run --mes`."""

    def __init__(self, args, names_uuids: List[Tuple[str, str]], seed: Any) -> None:
        self.validate = contract()
        url = args.mes_service.strip().rstrip("/")
        self.url = url
        self.links = {n: Link(args.dir, n, mid, url) for n, mid in names_uuids}
        codes = CodeSource(args.mes_codes, args.mes_code_cmd)
        site = None
        for n, link in self.links.items():
            doc = link.enrolled()
            if doc is None:
                print("  enrolling %s with %s" % (n, url), flush=True)
                doc = link.enrol(codes)
            if site is not None and doc["site_id"] != site:
                raise RuntimeError("%s is enrolled in another site (%s, not %s)" % (n, doc["site_id"], site))
            site = doc["site_id"]
        self.site_id = site
        self.oracle = LiveOracle(self.links)
        self.bad = 0
        self.fleet = MesFleet(names_uuids, seed=seed, nonce=uuid.uuid4().hex, cycle=args.mes_cycle,
                              idle=args.mes_idle,
                              scenarios=[s.strip() for s in args.mes_scenarios.split(",") if s.strip()],
                              emit=self._store, oracle=self.oracle, wall=lambda now: time.time(),
                              producer_ids={n: lk.producer_ids() for n, lk in self.links.items()},
                              site_id=site)
        for m in self.fleet.machines:
            self.links[m.name].open(m.agent.boot_id)
        self.oracle.start()

    def _store(self, m: MesMachine, event: Dict[str, Any]) -> None:
        errors = self.validate.validate_event(event)
        if errors:                                         # a simulator bug: never send it
            self.bad += 1
            logger.error("fleet_sim made an invalid %s: %s", event["type"], errors)
            return
        self.links[m.name].store(event, self.validate.encode(event))

    def step(self, now: float, quiet: bool) -> None:
        self.fleet.step(now, quiet)

    def pending(self) -> int:
        return sum(lk.pending() for lk in self.links.values())

    def receipts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for lk in self.links.values():
            for k, v in (lk.forwarder.sent if lk.forwarder else {}).items():
                out[k] = out.get(k, 0) + v
        return out

    def finish(self, timeout: float, clock: Callable[[], float] = time.monotonic) -> bool:
        """Quiet: let running jobs end, log the operators out, wait until every
        spool is sent. True when everything was delivered in time."""
        deadline = clock() + timeout
        while clock() < deadline:
            self.fleet.step(clock(), True)
            if self.fleet.idle_all:
                self.fleet.close()
                if self.pending() == 0:
                    return True
            time.sleep(0.2)
        return False

    def truth_dict(self) -> Dict[str, Any]:
        d = self.fleet.truth_dict()
        d.update({"service": self.url, "site_id": self.site_id, "pending": self.pending(),
                  "receipts": self.receipts(), "invalid_events_not_sent": self.bad,
                  "readout_errors": self.oracle.errors,
                  "profiles": os.path.relpath(PROFILES_FILE, ROOT).replace(os.sep, "/")})
        return d

    def close(self) -> None:
        self.oracle.stop()
        for lk in self.links.values():
            lk.close()


# ---------------------------------------------------------------------------
# Check: the service's record against truth
# ---------------------------------------------------------------------------
_ATT_COLS = ("attempt_id", "execution_id", "machine_id", "scan_id", "binding_id", "fixture_position", "state",
             "mode", "held", "continuation_of", "identified_after_start", "operator_session_id")


class DbLedger:
    """The service's SQLite ledger, read-only (tables attempts, bindings,
    scans, exceptions, execution_heads, attempt_participants), in the shape of
    GET /v1/units/{namespace}/{id}/history."""

    def __init__(self, path: str) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30.0)

    def unit(self, namespace: str, ident: str) -> Dict[str, Any]:
        c = self.conn
        attempts = [dict(zip(_ATT_COLS, r)) for r in c.execute(
            "SELECT %s FROM attempts WHERE namespace = ? AND norm_id = ? ORDER BY started_at, rowid"
            % ", ".join(_ATT_COLS), (namespace, ident))]
        for a in attempts:
            a["held"] = bool(a["held"])
            a["identified_after_start"] = bool(a["identified_after_start"])
            a["counts_as_production"] = a["state"] != "setup" and a["continuation_of"] is None
            a["heads"] = [{"head": r[0]} for r in c.execute(
                "SELECT head FROM execution_heads WHERE execution_id = ? ORDER BY head", (a["execution_id"],))]
            a["participants"] = [dict(zip(("operator_session_id", "from_at", "to_at"), r)) for r in c.execute(
                "SELECT operator_session_id, from_at, to_at FROM attempt_participants WHERE attempt_id = ? "
                "ORDER BY from_at", (a["attempt_id"],))]
        scans = [dict(zip(("scan_id", "machine_id", "status"), r)) for r in c.execute(
            "SELECT scan_id, machine_id, status FROM scans WHERE namespace = ? AND normalized = ? "
            "ORDER BY received_at", (namespace, ident))]
        ids = [s["scan_id"] for s in scans]
        bindings: List[Dict[str, Any]] = []
        if ids:
            bindings = [dict(zip(("binding_id", "scan_id", "machine_id", "fixture_position", "state"), r))
                        for r in c.execute(
                            "SELECT binding_id, scan_id, machine_id, fixture_position, state FROM bindings "
                            "WHERE scan_id IN (%s) ORDER BY created_at" % ",".join("?" * len(ids)), ids)]
        return {"attempts": attempts, "scans": scans, "bindings": bindings}

    def scan(self, scan_id: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT status FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
        if r is None:
            return None
        return {"status": r[0], "bindings": [x[0] for x in self.conn.execute(
            "SELECT state FROM bindings WHERE scan_id = ? ORDER BY created_at", (scan_id,))]}

    def exceptions(self) -> List[Dict[str, Any]]:
        out = []
        for xid, kind, ref, detail in self.conn.execute("SELECT exception_id, kind, ref, detail FROM exceptions"):
            try:
                d = json.loads(detail) if detail else {}
            except ValueError:
                d = {}
            out.append({"exception_id": xid, "kind": kind, "ref": ref, "detail": d})
        return out

    def close(self) -> None:
        self.conn.close()


class HttpLedger:
    """The service over HTTPS as a logged-in desktop user (POST /v1/session,
    then GET /v1/units/{namespace}/{id}/history, /v1/scans/{id}, /v1/exceptions)."""

    def __init__(self, url: str, login: str, password: str, ca_file: str, timeout: float = 30.0) -> None:
        from urllib.parse import urlsplit
        u = urlsplit(url.strip())
        if u.scheme != "https" or not u.hostname:
            raise ValueError("--mes-url must look like https://<host>:8443")
        self.host, self.port, self.timeout = u.hostname, u.port or 8443, timeout
        self.ctx = ssl.create_default_context(cafile=ca_file)
        self.token: Optional[str] = None
        status, doc = self._call("POST", "/v1/session", {"login": login, "password": password})
        if status != 200 or not isinstance(doc, dict) or not doc.get("token"):
            raise RuntimeError("login refused (HTTP %d): %s" % (status, doc))
        self.token = doc["token"]

    def _call(self, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ctx)
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            conn.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None,
                         headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read()
        finally:
            conn.close()
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw[:200]

    def unit(self, namespace: str, ident: str) -> Dict[str, Any]:
        status, doc = self._call("GET", "/v1/units/%s/%s/history" % (quote(namespace, safe=""),
                                                                     quote(ident, safe="")))
        if status != 200:
            raise RuntimeError("unit history %s/%s: HTTP %d %s" % (namespace, ident, status, doc))
        return doc

    def scan(self, scan_id: str) -> Optional[Dict[str, Any]]:
        status, doc = self._call("GET", "/v1/scans/%s" % scan_id)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError("scan %s: HTTP %d %s" % (scan_id, status, doc))
        return {"status": doc["scan"]["status"], "bindings": None}    # bindings are not in this read

    def exceptions(self) -> List[Dict[str, Any]]:
        seen: Dict[str, Dict[str, Any]] = {}
        before: Optional[int] = None
        while True:
            path = "/v1/exceptions?state=all&limit=500" + ("" if before is None else "&before=%d" % before)
            status, doc = self._call("GET", path)
            if status != 200:
                raise RuntimeError("exceptions: HTTP %d %s" % (status, doc))
            new = [e for e in doc["exceptions"] if e["exception_id"] not in seen]
            for e in new:
                seen[e["exception_id"]] = {"exception_id": e["exception_id"], "kind": e["kind"],
                                           "ref": e["ref"], "detail": e.get("detail") or {}}
            nxt = doc.get("next_before")
            if nxt is None or not new:
                return list(seen.values())
            before = nxt + 1             # rows sharing that millisecond are read again, kept once

    def close(self) -> None:
        pass


def _attempt_key(a: Dict[str, Any]) -> Tuple[str, str]:
    return a["execution_id"], a["scan_id"]


def _compare_attempt(key: str, t: Dict[str, Any], g: Dict[str, Any], ids: Dict[str, Tuple[str, str]],
                     held_got: Dict[str, List[Tuple[str, bool]]]) -> List[str]:
    diffs = []
    for field in ("machine_id", "state", "mode", "counts_as_production", "fixture_position", "operator_session_id"):
        if g.get(field) != t[field]:
            diffs.append("%s %r, truth %r" % (field, g.get(field), t[field]))
    if bool(g.get("binding_id")) != t["bound"]:
        diffs.append("binding %s, truth %s" % ("yes" if g.get("binding_id") else "none",
                                               "yes" if t["bound"] else "none"))
    if isinstance(t["held"], str) and t["held"].startswith("pair:"):
        held_got.setdefault(t["held"][5:], []).append((t["execution_id"], bool(g.get("held"))))
    elif bool(g.get("held")) != bool(t["held"]):
        diffs.append("held %s, truth %s" % (bool(g.get("held")), bool(t["held"])))
    cont = ids.get(g["continuation_of"], ("?", g["continuation_of"])) if g.get("continuation_of") else None
    want = (t["continuation_of"]["execution_id"], t["continuation_of"]["scan_id"]) if t["continuation_of"] else None
    if cont != want:
        diffs.append("continuation_of %s, truth %s" % (cont, want))
    if g.get("identified_after_start"):
        diffs.append("identified_after_start (the scan was not identified when the run started)")
    heads = len(g.get("heads") or [])
    if heads != t["heads"]:
        diffs.append("heads %d, truth %d" % (heads, t["heads"]))
    parts = g.get("participants") or []
    got_p = sorted(p["operator_session_id"] for p in parts)
    if got_p != sorted(t["participants"]):
        diffs.append("participants %s, truth %s" % (got_p, sorted(t["participants"])))
    elif t["state"] != "running" and any(p.get("to_at") is None for p in parts):
        diffs.append("a participant interval is still open after the run ended")
    return diffs


def compare(truth: Dict[str, Any], ledger: Any) -> Tuple[List[str], Dict[str, int]]:
    """Every difference between truth["mes"] and the service's record, one line
    each; empty = exact. Also returns what was checked, for the summary."""
    bad: List[str] = []
    counts = {"units": 0, "attempts": 0, "exceptions": 0, "scans": 0}
    held_got: Dict[str, List[Tuple[str, bool]]] = {}       # conflict pair -> [(run, held)]
    for key, u in sorted(truth.get("units", {}).items()):
        counts["units"] += 1
        got = ledger.unit(u["namespace"], u["id"])
        by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for a in got.get("attempts", []):
            by_key.setdefault(_attempt_key(a), []).append(a)
        ids = {a["attempt_id"]: _attempt_key(a) for a in got.get("attempts", [])}
        want = {_attempt_key(a): a for a in u["attempts"]}
        for k, rows in by_key.items():
            if len(rows) > 1:
                bad.append("%s: attempt run %s scan %s recorded %d times" % (key, k[0], k[1], len(rows)))
            if k not in want:
                bad.append("%s: extra attempt run %s scan %s (state %s)" % (key, k[0], k[1], rows[0].get("state")))
        for k, t in want.items():
            counts["attempts"] += 1
            if k not in by_key:
                bad.append("%s: missing attempt on %s, run %s scan %s (%s, %s)"
                           % (key, t["machine"], k[0], k[1], t["mode"], t["state"]))
                continue
            diffs = _compare_attempt(key, t, by_key[k][0], ids, held_got)
            if diffs:
                bad.append("%s, run %s on %s: %s" % (key, k[0], t["machine"], "; ".join(diffs)))
        got_scans = {s["scan_id"]: s for s in got.get("scans", [])}
        for s in u["scans"]:
            counts["scans"] += 1
            g = got_scans.pop(s["scan_id"], None)
            if g is None:
                bad.append("%s: missing scan %s from %s" % (key, s["scan_id"], s["machine"]))
            elif s["status"] is not None and g.get("status") != s["status"]:
                bad.append("%s: scan %s status %r, truth %r" % (key, s["scan_id"], g.get("status"), s["status"]))
        for sid in got_scans:
            bad.append("%s: extra scan %s" % (key, sid))
        got_b: Dict[str, List[Dict[str, Any]]] = {}
        for b in got.get("bindings", []):
            got_b.setdefault(b["scan_id"], []).append(b)
        for b in u["bindings"]:
            rows = got_b.pop(b["scan_id"], [])
            if len(rows) != 1:
                bad.append("%s: scan %s has %d bindings, truth 1 (%s)" % (key, b["scan_id"], len(rows), b["state"]))
            elif rows[0].get("state") != b["state"] or rows[0].get("fixture_position") != b["fixture_position"]:
                bad.append("%s: binding of scan %s is %s at %r, truth %s at %r"
                           % (key, b["scan_id"], rows[0].get("state"), rows[0].get("fixture_position"),
                              b["state"], b["fixture_position"]))
        for sid, rows in got_b.items():
            bad.append("%s: extra binding(s) of scan %s: %s" % (key, sid, [r.get("state") for r in rows]))
    for sid, t in sorted(truth.get("scans", {}).items()):          # scans no profile identifies
        counts["scans"] += 1
        g = ledger.scan(sid)
        if g is None:
            bad.append("scan %s (%s, %s): missing" % (sid, t["machine"], t["scenario"]))
            continue
        if g["status"] != t["status"]:
            bad.append("scan %s (%s, %s): status %r, truth %r" % (sid, t["machine"], t["scenario"],
                                                                  g["status"], t["status"]))
        if g.get("bindings") is not None and g["bindings"] != [t["binding"]]:
            bad.append("scan %s (%s, %s): bindings %s, truth [%r]" % (sid, t["machine"], t["scenario"],
                                                                      g["bindings"], t["binding"]))
    ours = set(truth.get("executions", []))
    machine_ids = {m["machine_id"] for m in truth.get("machines", {}).values()}
    got_exc = [e for e in ledger.exceptions()
               if e["ref"] in ours
               or (e["kind"] in QUARANTINE_KINDS and (e.get("detail") or {}).get("via") in machine_ids)
               or (e["kind"] == "binding_other_machine" and (e.get("detail") or {}).get("machine") in machine_ids)]
    pairs_held = {p: [run for run, h in rows if h] for p, rows in held_got.items()}
    for t in truth.get("exceptions", []):
        counts["exceptions"] += 1
        if t["kind"] == "interrupted_run":
            rows = [e for e in got_exc if e["kind"] == "interrupted_run" and e["ref"] == t["ref"]]
            if len(rows) != 1:
                bad.append("interrupted_run for run %s on %s: %d exception(s), truth 1"
                           % (t["ref"], t["machine"], len(rows)))
            elif len((rows[0].get("detail") or {}).get("attempts") or []) != t["attempts"]:
                bad.append("interrupted_run for run %s: %d attempts listed, truth %d"
                           % (t["ref"], len(rows[0]["detail"].get("attempts") or []), t["attempts"]))
        elif t["kind"] == "unit_on_two_machines":
            rows = [e for e in got_exc if e["kind"] == "unit_on_two_machines" and e["ref"] in t["ref_one_of"]]
            held = pairs_held.pop(t["pair"], [])
            if len(rows) != 1:
                bad.append("%s (pair %s): %d unit_on_two_machines exception(s), truth 1"
                           % (t["unit"], t["pair"], len(rows)))
            if len(held) != 1:
                bad.append("%s (pair %s): %d of its two attempts held, truth exactly 1"
                           % (t["unit"], t["pair"], len(held)))
            elif len(rows) == 1 and rows[0]["ref"] != held[0]:
                bad.append("%s (pair %s): exception on run %s but run %s is held"
                           % (t["unit"], t["pair"], rows[0]["ref"], held[0]))
        else:
            bad.append("truth has an exception of unknown kind %r" % t["kind"])
            rows = []
        for r in rows:
            got_exc.remove(r)
    for p, held in sorted(pairs_held.items()):
        bad.append("conflict pair %s: only one side started in the simulation, %d held" % (p, len(held)))
    for e in got_exc:
        bad.append("extra exception %s ref %s: %s"
                   % (e["kind"], e["ref"], json.dumps(e.get("detail"), sort_keys=True)[:200]))
    return bad, counts


def check(truth_doc: Dict[str, Any], ledger: Any, out: Callable[[str], None] = print) -> int:
    """Print the MES comparison; 0 = exact, 1 = any mismatch."""
    mes = truth_doc.get("mes")
    if not mes:
        out("MES: the truth file has no MES part (run with --mes)")
        return 1
    if mes.get("pending"):
        out("WARNING: %d MES events were still in the spools when the run ended (not delivered)"
            % mes["pending"])
    busy = [n for n, m in sorted(mes.get("machines", {}).items()) if m.get("busy")]
    if busy:
        out("WARNING: %d machine(s) were still in a job at the end: %s" % (len(busy), ", ".join(busy[:10])))
    bad, counts = compare(mes, ledger)
    for line in bad:
        out("MISMATCH " + line)
    out("MES scenarios: " + ", ".join("%s %d" % kv for kv in sorted(mes.get("scenarios", {}).items())))
    if mes.get("receipts"):
        out("MES receipts: " + ", ".join("%s %d" % kv for kv in sorted(mes["receipts"].items())))
    out("MES: %d units, %d attempts, %d scans, %d expected exceptions checked: %s"
        % (counts["units"], counts["attempts"], counts["scans"], counts["exceptions"],
           "exact" if not bad else "%d mismatch(es)" % len(bad)))
    return 1 if bad else 0
