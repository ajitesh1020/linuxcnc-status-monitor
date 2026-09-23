#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
cycle_time_calculator.py  —  v1.3.0
=====================================
Industrial-grade cycle time calculator for LinuxCNC CNC applications.

Tracks:
  - Cycle start / pause / resume / stop / abort
  - Completed cycle durations (milliseconds, rolling buffer)
  - Aborted cycle durations and abort count
  - Total parts produced — based on M2/M30 program-end detection
  - "Run From Here" mid-program start detection and safe handling
  - Thread-safe access to all state via RLock

Program Completion Detection (M2/M30 — No G-code Changes)
----------------------------------------------------------
program_tracker.py scans the loaded file and watches the executed line
numbers; when a cycle goes idle having run through to M2/M30, status.py
calls signal_cycle_complete() and then stop_cycle(). A cycle that stops
earlier is recorded as an abort.

Part accounting (one physical part = one pass through the program)
------------------------------------------------------------------
  * Top → M2/M30 in one go                         → 1 part.
  * Top → stopped midway → Run From Here → M2/M30  → 1 part (time is carried
    across the interruption, no second abort for the continuation).
  * Top → stopped midway → started again from top  → the unfinished part is
    recorded as a PARTIAL part with how far it got (e.g. 0.5 = half done),
    and the new run starts a fresh part.

Whether a cycle started from the top or mid-program is only known once the
first move executes, so start_cycle(None) opens the cycle "unclassified" and
status.py calls classify_start() when it knows.

Design principles:
  - Zero side-effects on LinuxCNC — read-only consumer of state signals
  - Thread-safe via RLock (reentrant)
  - DEV_MODE flag controls verbose logging; production is silent
  - All timestamps use time.perf_counter_ns() (monotonic, nanosecond)
  - Rolling buffer caps memory on long production runs
"""

import threading
import time
import logging
import collections
from dataclasses import dataclass
from typing import Optional, List, Deque

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MS_PER_NS: int          = 1_000_000   # ns → ms
MIN_VALID_CYCLE_MS: int = 1_000       # discard spurious cycles < 1 second
MAX_HISTORY: int        = 500         # rolling buffer depth for durations


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class CycleSnapshot:
    """Immutable point-in-time snapshot — safe to read from any thread."""
    is_running: bool
    is_paused: bool
    current_cycle_ms: int
    parts_produced: int
    abort_count: int
    run_from_here_count: int
    last_completed_ms: Optional[int]       # most recent good cycle duration
    average_cycle_ms: Optional[float]      # rolling average of good cycles
    total_completed_cycles: int
    cycle_complete_signalled: bool         # True if end-line was reached
    is_run_from_here: bool                 # True if cycle started mid-program
    last_aborted_ms: Optional[int] = None  # most recent aborted cycle duration
    partial_parts: int = 0                 # parts left unfinished (restarted from top)
    partial_part_equiv: float = 0.0        # sum of their completed fractions
    last_partial_pct: Optional[float] = None
    progress_pct: float = 0.0              # how far the current part has got
    part_in_progress: bool = False         # running, or awaiting Run From Here


@dataclass
class _CycleState:
    """Internal mutable state — always accessed under the RLock."""
    start_ns: Optional[int]          = None
    pause_start_ns: Optional[int]    = None
    total_paused_ns: int             = 0
    running: bool                    = False
    paused: bool                     = False
    cycle_complete_signalled: bool   = False   # end-line reached?
    is_run_from_here: bool           = False
    classified: bool                 = False   # start point (top / mid) known?
    progress: float                  = 0.0     # 0..1 furthest point this cycle


# ---------------------------------------------------------------------------
# CycleTimeCalculator
# ---------------------------------------------------------------------------
class CycleTimeCalculator:
    """
    Thread-safe cycle time and production counter for a single CNC machine.

    Caller (status.py) is responsible for calling:
        start_cycle(run_from_here)  — program execution begins
        pause_cycle()               — feed hold engaged
        resume_cycle()              — feed hold released
        signal_cycle_complete()     — motion_line reached M2/M30 end line
        stop_cycle()                — LinuxCNC transitions to IDLE
        abort_cycle()               — E-stop / SIGTERM while running
        reset_stats()               — operator-initiated counter reset
    """

    def __init__(self, dev_mode: bool = False) -> None:
        self._dev_mode = dev_mode
        self._lock     = threading.RLock()
        self._state    = _CycleState()

        self._completed_durations_ms: Deque[int] = collections.deque(maxlen=MAX_HISTORY)
        self._aborted_durations_ms:   Deque[int] = collections.deque(maxlen=MAX_HISTORY)

        self._parts_produced:      int = 0
        self._abort_count:         int = 0
        self._run_from_here_count: int = 0

        # Carried elapsed time (ms) from an incomplete cycle, so that a
        # subsequent "Run From Here" continues timing instead of restarting.
        # Reset to 0 on a fresh start-from-beginning and on a completed part.
        self._carry_ms:            int = 0

        # An unfinished part (fresh run stopped early, or an incomplete Run From
        # Here) that a later Run From Here may still complete.
        self._part_open:           bool  = False
        self._open_progress:       float = 0.0
        self._partial_parts:       int   = 0
        self._partial_equiv:       float = 0.0
        self._last_partial_pct:    Optional[float] = None

        self._log(logging.DEBUG,
                  "CycleTimeCalculator v1.3.0 initialised (dev_mode=%s)", dev_mode)

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    def start_cycle(self, run_from_here: Optional[bool] = False) -> None:
        """
        Start a new cycle.
        run_from_here=True  — continued mid-program via "Run From Here".
        run_from_here=False — started from the top.
        run_from_here=None  — not known yet; call classify_start() later.
        """
        with self._lock:
            if self._state.running:
                self._log(logging.WARNING,
                          "start_cycle() called but cycle already running — ignoring.")
                return
            self._state = _CycleState(
                start_ns=time.perf_counter_ns(),
                running=True,
                paused=False,
            )
            if run_from_here is None:
                self._log(logging.INFO, "Cycle STARTED — waiting for the first move "
                                        "to tell top vs Run From Here.")
            else:
                self._classify_unsafe(run_from_here)

    def classify_start(self, run_from_here: bool) -> None:
        """Record where the running cycle started. Only the first call counts."""
        with self._lock:
            if self._state.running and not self._state.classified:
                self._classify_unsafe(run_from_here)

    def set_progress(self, fraction: float) -> None:
        """Furthest point (0..1) the running cycle has reached in the program."""
        with self._lock:
            if self._state.running:
                f = min(1.0, max(0.0, float(fraction)))
                self._state.progress = max(self._state.progress, f)

    def pause_cycle(self) -> None:
        """Pause (feed hold). Ignored if not running or already paused."""
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "pause_cycle() — no cycle running, skip.")
                return
            if self._state.paused:
                self._log(logging.DEBUG, "pause_cycle() — already paused, skip.")
                return
            self._state.pause_start_ns = time.perf_counter_ns()
            self._state.paused         = True
            self._log(logging.INFO,
                      "Cycle PAUSED at %d ms.", self._elapsed_ms_unsafe())

    def resume_cycle(self) -> None:
        """Resume from feed hold. Ignored if not paused."""
        with self._lock:
            if not self._state.running or not self._state.paused:
                self._log(logging.DEBUG,
                          "resume_cycle() — not in paused state, skip.")
                return
            paused_ns = time.perf_counter_ns() - self._state.pause_start_ns
            self._state.total_paused_ns += paused_ns
            self._state.pause_start_ns   = None
            self._state.paused           = False
            self._log(logging.INFO,
                      "Cycle RESUMED. Paused for %d ms.", paused_ns // MS_PER_NS)

    def signal_cycle_complete(self) -> None:
        """
        Call when motion_line reaches the M2/M30 end line.
        This is the definitive signal that the program ran to completion.
        Has no effect if called outside an active cycle.
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG,
                          "signal_cycle_complete() — no cycle running, skip.")
                return
            if self._state.cycle_complete_signalled:
                return  # already signalled — guard against duplicate ticks
            self._state.cycle_complete_signalled = True
            self._log(logging.INFO,
                      "Program END LINE reached — cycle complete at %d ms.",
                      self._elapsed_ms_unsafe())

    def stop_cycle(self) -> None:
        """
        Call when LinuxCNC transitions to IDLE after a cycle.

        Decision tree:
          duration < MIN_VALID_CYCLE_MS     → discard (too short)
          cycle_complete_signalled          → part counted (incl. carried time)
          is_run_from_here                  → part still open, time carried
          else                              → abort recorded, part left open
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "stop_cycle() — no cycle running, skip.")
                return

            duration_ms = self._elapsed_ms_unsafe()
            complete    = self._state.cycle_complete_signalled
            rfh         = self._resolved_rfh_unsafe()
            progress    = self._progress_unsafe()

            self._log(logging.INFO,
                      "Cycle STOP. duration=%d ms  end_line_reached=%s  "
                      "run_from_here=%s  progress=%.0f%%",
                      duration_ms, complete, rfh, progress * 100)

            if duration_ms < MIN_VALID_CYCLE_MS and self._carry_ms == 0:
                self._log(logging.WARNING,
                          "Cycle %d ms < minimum %d ms — discarded.",
                          duration_ms, MIN_VALID_CYCLE_MS)

            elif complete:
                # Reached M2/M30 — a part, whether run from the top or continued
                # via Run From Here. Total time includes any carried segments.
                self._completed_durations_ms.append(duration_ms)
                self._parts_produced += 1
                self._close_part_unsafe()
                self._log(logging.INFO,
                          "Part COUNTED (#%d). Cycle time: %d ms%s.",
                          self._parts_produced, duration_ms,
                          " (incl. carried time)" if rfh else "")

            elif rfh:
                # A continuation that itself did not finish — keep the accumulated
                # time so a further Run From Here continues, without an abort.
                self._leave_part_open_unsafe(duration_ms, progress)
                self._log(logging.WARNING,
                          "Run-From-Here segment stopped at %d ms (%.0f%% done) — "
                          "carried for continuation (no abort).",
                          duration_ms, progress * 100)

            else:
                # Fresh run stopped before M2/M30 — abort. Carry the elapsed time so
                # the operator can resume it later with Run From Here.
                self._abort_count += 1
                if duration_ms >= MIN_VALID_CYCLE_MS:
                    self._aborted_durations_ms.append(duration_ms)
                self._leave_part_open_unsafe(duration_ms, progress)
                self._log(logging.WARNING,
                          "End line NOT reached — ABORT recorded (#%d). "
                          "Duration: %d ms, %.0f%% done (carried for Run From Here).",
                          self._abort_count, duration_ms, progress * 100)

            self._state = _CycleState()   # reset for next cycle

    def abort_cycle(self) -> None:
        """
        Explicit abort — called on E-stop, machine off, stop-while-paused, or
        process shutdown while running. Always recorded as abort; the part is
        left open so Run From Here can still finish it.
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "abort_cycle() — no cycle running, skip.")
                return
            duration_ms = self._elapsed_ms_unsafe()
            progress    = self._progress_unsafe()
            self._abort_count += 1
            if duration_ms >= MIN_VALID_CYCLE_MS:
                self._aborted_durations_ms.append(duration_ms)
            self._leave_part_open_unsafe(duration_ms, progress)
            self._log(logging.WARNING,
                      "Cycle ABORTED (explicit) at %d ms, %.0f%% done. Total aborts: %d.",
                      duration_ms, progress * 100, self._abort_count)
            self._state = _CycleState()

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    def snapshot(self) -> CycleSnapshot:
        """Non-blocking immutable snapshot. Safe to call from any thread."""
        with self._lock:
            current_ms = self._elapsed_ms_unsafe() if self._state.running else 0
            last_ms    = (self._completed_durations_ms[-1]
                          if self._completed_durations_ms else None)
            avg        = (sum(self._completed_durations_ms) /
                          len(self._completed_durations_ms)
                          if self._completed_durations_ms else None)
            last_abort = (self._aborted_durations_ms[-1]
                          if self._aborted_durations_ms else None)
            return CycleSnapshot(
                is_running=self._state.running,
                is_paused=self._state.paused,
                current_cycle_ms=current_ms,
                parts_produced=self._parts_produced,
                abort_count=self._abort_count,
                run_from_here_count=self._run_from_here_count,
                last_completed_ms=last_ms,
                average_cycle_ms=round(avg, 1) if avg is not None else None,
                total_completed_cycles=len(self._completed_durations_ms),
                cycle_complete_signalled=self._state.cycle_complete_signalled,
                is_run_from_here=self._state.is_run_from_here,
                last_aborted_ms=last_abort,
                partial_parts=self._partial_parts,
                partial_part_equiv=round(self._partial_equiv, 2),
                last_partial_pct=self._last_partial_pct,
                progress_pct=round(100 * (
                    self._progress_unsafe() if self._state.running
                    else self._open_progress if self._part_open else 0.0), 1),
                part_in_progress=self._state.running or self._part_open,
            )

    def get_completed_durations(self) -> List[int]:
        """Copy of completed cycle durations in ms."""
        with self._lock:
            return list(self._completed_durations_ms)

    def get_aborted_durations(self) -> List[int]:
        """Copy of aborted cycle durations in ms."""
        with self._lock:
            return list(self._aborted_durations_ms)

    def reset_stats(self) -> None:
        """Reset all counters and history. Refused while a cycle is active."""
        with self._lock:
            if self._state.running:
                logger.error("reset_stats() refused — cycle is running. "
                             "Stop or abort first.")
                return
            self._completed_durations_ms.clear()
            self._aborted_durations_ms.clear()
            self._parts_produced      = 0
            self._abort_count         = 0
            self._run_from_here_count = 0
            self._carry_ms            = 0
            self._part_open           = False
            self._open_progress       = 0.0
            self._partial_parts       = 0
            self._partial_equiv       = 0.0
            self._last_partial_pct    = None
            self._log(logging.INFO, "All stats RESET by operator.")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _classify_unsafe(self, run_from_here: bool) -> None:
        self._state.classified       = True
        self._state.is_run_from_here = run_from_here
        if run_from_here:
            self._run_from_here_count += 1
            self._log(logging.WARNING,
                      "Cycle CONTINUED mid-program (Run From Here #%d), carrying %d ms "
                      "(part %.0f%% done before).",
                      self._run_from_here_count, self._carry_ms,
                      self._open_progress * 100 if self._part_open else 0.0)
        else:
            if self._part_open:
                self._finalize_partial_unsafe()
            self._carry_ms = 0  # fresh run from the top starts the clock at 0
            self._log(logging.INFO, "Cycle STARTED from beginning.")

    def _resolved_rfh_unsafe(self) -> bool:
        """Start point of the running cycle. One that ends before any move was
        seen counts as a continuation of an open part, else as a fresh run."""
        if self._state.classified:
            return self._state.is_run_from_here
        return self._part_open

    def _progress_unsafe(self) -> float:
        p = self._state.progress
        if self._part_open and self._resolved_rfh_unsafe():
            p = max(p, self._open_progress)
        return p

    def _leave_part_open_unsafe(self, duration_ms: int, progress: float) -> None:
        self._carry_ms      = duration_ms
        self._part_open     = True
        self._open_progress = progress

    def _close_part_unsafe(self) -> None:
        self._carry_ms      = 0
        self._part_open     = False
        self._open_progress = 0.0

    def _finalize_partial_unsafe(self) -> None:
        """The open part was abandoned — program restarted from the top."""
        frac = self._open_progress
        self._partial_parts   += 1
        self._partial_equiv   += frac
        self._last_partial_pct = round(frac * 100, 1)
        self._log(logging.WARNING,
                  "Previous part abandoned at %.0f%% — recorded as partial part #%d.",
                  frac * 100, self._partial_parts)
        self._close_part_unsafe()

    def _elapsed_ms_unsafe(self) -> int:
        """Active elapsed ms. MUST be called while holding self._lock."""
        if self._state.start_ns is None:
            return 0
        now_ns = time.perf_counter_ns()
        if self._state.paused and self._state.pause_start_ns is not None:
            active_ns = (self._state.pause_start_ns
                         - self._state.start_ns
                         - self._state.total_paused_ns)
        else:
            active_ns = (now_ns
                         - self._state.start_ns
                         - self._state.total_paused_ns)
        # Include any carried time from an interrupted run being continued.
        return max(0, active_ns // MS_PER_NS) + self._carry_ms

    def _log(self, level: int, msg: str, *args) -> None:
        """Log only in DEV_MODE or for WARNING+ messages."""
        if self._dev_mode or level >= logging.WARNING:
            logger.log(level, msg, *args)
