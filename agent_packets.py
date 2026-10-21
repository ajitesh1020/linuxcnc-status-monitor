#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_packets.py  —  when to send a status packet
=================================================
The main loop samples LinuxCNC every sample_interval_s (0.1 s) and sends a
status packet every poll_interval_s (1 s) while active. While idle it stays
quiet and sends a heartbeat every idle_heartbeat_interval_s (30 s).

A packet goes out at once, whatever the cadence, when:
  * the cycle state changes (IDLE / RUNNING / PAUSED), or
  * the fault picture changes: E-stop, machine on/off, EXEC_ERROR, a failed
    command, a joint fault or a hard limit (see fault_signature()). This is
    checked on every sample, so a fault that sets and clears between two status packets, or
    while idle, still reaches the dashboard and the journal, both edges.

All of this reads only the stat channel. Nothing here touches the NML error
channel, so the operator's GUI keeps every error message.

Pure logic with no LinuxCNC import, so it is unit-tested on any PC.
"""

from typing import Any, Optional, Tuple

EXEC_ERROR = 1   # linuxcnc.EXEC_ERROR
RCS_ERROR = 3    # linuxcnc.RCS_ERROR: stat.state after a command failed


def _get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return default


def fault_signature(stat: Any, exec_error: int = EXEC_ERROR,
                    rcs_error: int = RCS_ERROR) -> Tuple:
    """The fault-relevant part of a polled linuxcnc.stat, as a comparable tuple.

    Read-only: uses status flags only and never consumes anything.

    A G-code error in MDI does not set EXEC_ERROR (LinuxCNC 2.9 goes straight
    back to EXEC_DONE), but stat.state stays RCS_ERROR until the next command
    succeeds. The command serial is included while in error, so each failed
    command is its own edge, even two in a row.
    """
    joints = []
    try:
        n = int(_get(stat, "joints", 0) or 0)
        raw = _get(stat, "joint", None) or []
        for i in range(min(n, len(raw))):
            j = raw[i]
            joints.append((bool(j.get("fault", False)),
                           bool(j.get("min_hard_limit", False)
                                or j.get("max_hard_limit", False))))
    except Exception:
        joints = []
    return (
        bool(_get(stat, "estop", False)),
        _get(stat, "task_state", 0),
        _get(stat, "exec_state", 0) == exec_error,
        tuple(joints),
        _get(stat, "echo_serial_number", None)
        if _get(stat, "state", 0) == rcs_error else None,
    )


class PacketGate:
    """Decides, sample by sample, whether the loop does packet work and
    whether a packet is sent or suppressed.

    Per sample:   if gate.tick(now, state, fault_sig): ...packet-cadence work...
                  if gate.should_send(now, state, has_errors): build + send
                                                               gate.sent(now, state)
    """

    def __init__(self, poll_interval_s: float, idle_heartbeat_interval_s: float) -> None:
        self._poll = float(poll_interval_s)
        self._heartbeat = float(idle_heartbeat_interval_s)
        self._last_sampled_state = ""
        self._last_poll = float("-inf")
        self._prev_state = ""
        self._idle_sent = False
        self._last_heartbeat = 0.0
        self._last_fault: Optional[Tuple] = None
        # A fault edge stays pending until a packet carrying it has gone out.
        self.fault_pending = False

    def tick(self, now: float, state: str, fault_sig: Optional[Tuple] = None) -> bool:
        """Record one sample. True = do packet work now (cadence is due, the
        cycle state changed, or the fault picture changed)."""
        state_change = state != self._last_sampled_state
        self._last_sampled_state = state
        if fault_sig is not None and fault_sig != self._last_fault:
            if self._last_fault is not None:
                self.fault_pending = True
            self._last_fault = fault_sig
        if not (state_change or self.fault_pending) and now - self._last_poll < self._poll:
            return False
        self._last_poll = now
        return True

    def should_send(self, now: float, state: str, has_errors: bool = False) -> bool:
        """Apply idle suppression. Never suppresses a fault edge or a packet
        carrying a caught error message."""
        changed = state != self._prev_state
        self._prev_state = state
        if state != "IDLE":
            self._idle_sent = False
            return True
        if changed:
            self._idle_sent = False
            self._last_heartbeat = now
        if (self._idle_sent and not has_errors and not self.fault_pending
                and now - self._last_heartbeat < self._heartbeat):
            return False
        return True

    def sent(self, now: float, state: str) -> None:
        """A packet went out (or was handed to the sender)."""
        self.fault_pending = False
        if state == "IDLE":
            self._idle_sent = True
            self._last_heartbeat = now
