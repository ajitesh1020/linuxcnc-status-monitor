#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_mes_gate.py  —  the cycle gate verdict for MillWright (MES plan P5.5)
==========================================================================
The site service's feedback response (GET /v1/agents/{machine_id}/feedback)
carries "cycle_gate": {"policy", "validated", "reasons", "order"}: is there a
validated job at this machine? MillWright never talks to the service; it
applies its own optional "no validated job, no Cycle Start" rule at Cycle
Start using the last verdict the agent hands over in

  <config>/mw_data/cycle_gate.json
  {"schema": "lcnc-agent.cycle_gate/1",
   "fetched_at_ms": 1763790000123,   when the agent RECEIVED it (wall clock)
   "written_at_ms": 1763790000150,   when this file was written
   "machine_id": "...",
   "cycle_gate": {...}}              the response's object, unchanged

Contract: MillWright docs/MES_CYCLE_GATE.md. Rules on this side:
  * written after every successful feedback response that has a cycle_gate,
    even when nothing changed (fetched_at_ms must move: MillWright measures
    staleness from it, default 120 s);
  * never touched after a failed response, or one without cycle_gate, or one
    whose cycle_gate MillWright would reject (it then sees a stale verdict,
    which never blocks unless the machine chose so);
  * atomic: temp file in the same folder, fsync, os.replace;
  * written by the forwarder thread, never the sampling loop.
The agent only carries the verdict; it never decides anything about the
machine.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA = "lcnc-agent.cycle_gate/1"
FILE_NAME = "cycle_gate.json"
POLICIES = ("off", "advisory", "enforce")
MAX_REASONS = 32
MAX_REASON_LEN = 64
MAX_ORDER_LEN = 128
MAX_FILE = 65536


def gate_path(mw_data: str) -> str:
    return os.path.join(mw_data, FILE_NAME)


def gate_of(feedback: Any) -> Optional[Dict[str, Any]]:
    """The response's cycle_gate when it is exactly as the contract says, else
    None (then nothing is written)."""
    if not isinstance(feedback, dict):
        return None
    g = feedback.get("cycle_gate")
    if not isinstance(g, dict):
        return None
    policy, validated, reasons, order = (g.get("policy"), g.get("validated"),
                                         g.get("reasons"), g.get("order"))
    if policy not in POLICIES or not isinstance(validated, bool) or not isinstance(reasons, list):
        return None
    if len(reasons) > MAX_REASONS or not all(
            isinstance(r, str) and 0 < len(r) <= MAX_REASON_LEN for r in reasons):
        return None
    if validated == bool(reasons):            # the service: validated = no reasons
        return None
    if not (order is None or (isinstance(order, str) and len(order) <= MAX_ORDER_LEN)
            or (isinstance(order, int) and not isinstance(order, bool))):
        return None
    return g


def build(cycle_gate: Dict[str, Any], fetched_at_ms: int, written_at_ms: int,
          machine_id: Optional[str]) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"schema": SCHEMA, "fetched_at_ms": int(fetched_at_ms),
                           "written_at_ms": int(written_at_ms), "cycle_gate": cycle_gate}
    if machine_id:
        doc["machine_id"] = machine_id
    return doc


def write_atomic(path: str, doc: Dict[str, Any]) -> None:
    data = json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FILE:
        raise ValueError("cycle_gate.json would be %d bytes" % len(data))
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class GateWriter:
    """Callback for the forwarder: Forwarder(on_feedback=GateWriter(...))."""

    def __init__(self, mw_data: str, machine_id: Optional[str], wall=None) -> None:
        import time
        self.mw_data, self.machine_id = mw_data, machine_id
        self.wall = wall or time.time
        self.written = 0
        self._problem: Optional[str] = None      # logged once until it changes

    def _log_once(self, problem: Optional[str], level=logging.WARNING) -> None:
        if problem and problem != self._problem:
            logger.log(level, "MES cycle gate: %s", problem)
        self._problem = problem

    def __call__(self, feedback: Any, fetched_at_ms: int) -> bool:
        """Write the verdict from one successful feedback response. True = written."""
        if not self.mw_data:
            return False
        if not isinstance(feedback, dict) or "cycle_gate" not in feedback:
            self._log_once("the site service sends no cycle_gate (older than MES Phase 5): "
                           "%s not written" % FILE_NAME, logging.INFO)
            return False
        g = gate_of(feedback)
        if g is None:
            self._log_once("cycle_gate in the feedback is not as the contract says (%r): "
                           "%s not written" % (str(feedback.get("cycle_gate"))[:200], FILE_NAME))
            return False
        try:
            write_atomic(gate_path(self.mw_data),
                         build(g, fetched_at_ms, int(self.wall() * 1000), self.machine_id))
        except (OSError, ValueError) as exc:
            self._log_once("%s not written: %s" % (gate_path(self.mw_data), exc), logging.ERROR)
            return False
        self._log_once(None)
        self.written += 1
        return True
