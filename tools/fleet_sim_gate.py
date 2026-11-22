#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
fleet_sim_gate.py — cycle gate policies for tools/fleet_sim.py (MES plan P5.5)
=============================================================================
A test tool, not part of the installed agent. With `fleet_sim.py run --mes
--mes-gate off,advisory,enforce`, the simulator switches the site service's
cycle_gate setting (PUT /v1/admin/execution-settings, as an integration_admin
desktop user) through the listed policies, holding each for --mes-gate-hold
seconds. Every simulated machine runs the agent's own forwarder with
agent_mes_gate.GateWriter, so it polls the feedback endpoint and writes
<dir>/<name>.mw_data/cycle_gate.json exactly like a real LinuxCNC PC.

After each switch, every machine's file must show the new policy within
--mes-gate-wait seconds (default 60: the agent polls every 15 s). The
simulator dispatches no work orders, so every verdict must also say
validated = false with "no_order" among its reasons. The results go into
truth.json under mes.cycle_gate; `fleet_sim.py check` reports every machine
that never showed a policy, or showed a wrong verdict, as a MISMATCH. The
site's setting is put back as it was when the run ends.
"""
import json
import os
from typing import Any, Callable, Dict, List, Optional

POLICIES = ("off", "advisory", "enforce")
DEFAULT_HOLD_S = 90.0
DEFAULT_WAIT_S = 60.0


def mw_data_dir(directory: str, name: str) -> str:
    return os.path.join(directory, "%s.mw_data" % name)


def parse_policies(text: str) -> List[str]:
    out = [p.strip() for p in (text or "").split(",") if p.strip()]
    bad = [p for p in out if p not in POLICIES]
    if bad:
        raise ValueError("unknown cycle gate polic%s: %s (use %s)"
                         % ("ies" if len(bad) > 1 else "y", ", ".join(bad), ", ".join(POLICIES)))
    return out


def read_gate_file(path: str) -> Optional[Dict[str, Any]]:
    """The file as MillWright would take it (agent_mes_gate's own rules), or
    None when missing or not valid."""
    import agent_mes_gate
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != agent_mes_gate.SCHEMA:
        return None
    at = doc.get("fetched_at_ms")
    if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
        return None
    if agent_mes_gate.gate_of(doc) is None:
        return None
    return doc


class GatePhases:
    """Switches the site's cycle gate policy through `policies` and records,
    per phase, when each machine's cycle_gate.json showed it and with what
    verdict. `set_policy(p)` changes the service's setting; `read(name)`
    returns that machine's file (read_gate_file) or None. Times are in
    `clock()` seconds; `wall()` is the wall clock used for the file's age."""

    def __init__(self, names: List[str], policies: List[str], set_policy: Callable[[str], None],
                 read: Callable[[str], Optional[Dict[str, Any]]], clock: Callable[[], float],
                 wall: Callable[[], float], hold_s: float = DEFAULT_HOLD_S,
                 wait_s: float = DEFAULT_WAIT_S) -> None:
        self.names, self.policies = list(names), list(policies)
        self.set_policy, self.read, self.clock, self.wall = set_policy, read, clock, wall
        self.hold_s, self.wait_s = max(hold_s, wait_s), wait_s
        self.phases: List[Dict[str, Any]] = []
        self._i = -1
        self._t0 = 0.0
        self.errors: List[str] = []

    @property
    def done(self) -> bool:
        return self._i >= len(self.policies)

    def _start(self, i: int) -> None:
        self._i = i
        if self.done:
            return
        p = self.policies[i]
        self._t0 = self.clock()
        phase = {"policy": p, "set": True, "machines": {n: None for n in self.names}}
        try:
            self.set_policy(p)
        except Exception as exc:            # recorded: check reports it
            phase["set"] = False
            self.errors.append("setting cycle_gate %s failed: %s" % (p, exc))
        self.phases.append(phase)

    def step(self) -> None:
        if self.done:
            return
        if self._i < 0:
            self._start(0)
            return
        phase = self.phases[-1]
        now = self.clock()
        if phase["set"] and now - self._t0 <= self.wait_s:
            for name, seen in phase["machines"].items():
                if seen is not None:
                    continue
                doc = self.read(name)
                if doc is None or doc["cycle_gate"]["policy"] != phase["policy"]:
                    continue
                g = doc["cycle_gate"]
                phase["machines"][name] = {
                    "after_s": round(now - self._t0, 1), "validated": g["validated"],
                    "reasons": list(g["reasons"]), "order": g["order"],
                    "age_s": round(self.wall() - doc["fetched_at_ms"] / 1000.0, 1)}
        if now - self._t0 >= self.hold_s:
            self._start(self._i + 1)

    def truth(self) -> Dict[str, Any]:
        return {"policies": self.policies, "wait_s": self.wait_s, "hold_s": self.hold_s,
                "phases": self.phases, "errors": list(self.errors)}


def compare(gate: Dict[str, Any]) -> List[str]:
    """Every problem in truth's mes.cycle_gate, one line each; empty = all good."""
    bad = list(gate.get("errors", []))
    phases = gate.get("phases", [])
    for p in gate.get("policies", [])[len(phases):]:
        bad.append("cycle gate %s: not reached (the run ended first; run longer or hold shorter)" % p)
    for ph in phases:
        if not ph.get("set"):
            continue                        # already in errors
        for name, seen in sorted(ph.get("machines", {}).items()):
            if seen is None:
                bad.append("cycle gate %s: %s never showed it in cycle_gate.json within %g s"
                           % (ph["policy"], name, gate.get("wait_s", DEFAULT_WAIT_S)))
                continue
            if seen["validated"] or "no_order" not in seen["reasons"]:
                bad.append("cycle gate %s: %s says validated=%s reasons=%s; with no work order it must be "
                           "validated=false with no_order" % (ph["policy"], name, seen["validated"],
                                                              seen["reasons"]))
            if seen["age_s"] is not None and seen["age_s"] > gate.get("wait_s", DEFAULT_WAIT_S):
                bad.append("cycle gate %s: %s's verdict was %.0f s old when read" % (ph["policy"], name,
                                                                                    seen["age_s"]))
    return bad


def summary(gate: Dict[str, Any]) -> str:
    parts = []
    for ph in gate.get("phases", []):
        seen = [m["after_s"] for m in ph.get("machines", {}).values() if m]
        parts.append("%s %d/%d%s" % (ph["policy"], len(seen), len(ph.get("machines", {})),
                                     (" (slowest %.0f s)" % max(seen)) if seen else ""))
    return ", ".join(parts) or "no phase run"


class ServiceAdmin:
    """The site service as a desktop user with integration_admin (cycle gate setting)."""

    def __init__(self, url: str, login: str, password: str, ca_file: str) -> None:
        from fleet_sim_mes import HttpLedger
        self.http = HttpLedger(url, login, password, ca_file)       # logs in
        self.original: Optional[str] = None

    def get(self) -> str:
        status, doc = self.http._call("GET", "/v1/admin/execution-settings")
        if status != 200 or not isinstance(doc, dict) or doc.get("cycle_gate") not in POLICIES:
            raise RuntimeError("reading the cycle gate setting: HTTP %d %s" % (status, doc))
        return doc["cycle_gate"]

    def set(self, policy: str) -> None:
        if self.original is None:
            self.original = self.get()
        status, doc = self.http._call("PUT", "/v1/admin/execution-settings", {"cycle_gate": policy})
        if status != 200 or not isinstance(doc, dict) or doc.get("cycle_gate") != policy:
            raise RuntimeError("HTTP %d %s" % (status, doc))

    def restore(self) -> None:
        if self.original is not None:
            self.set(self.original)
