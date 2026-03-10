#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cycle_time_calculator.py  —  v1.2.0
=====================================
Industrial-grade cycle time calculator for LinuxCNC CNC applications.

Tracks:
  - Cycle start / pause / resume / stop / abort
  - Completed cycle durations (milliseconds, rolling buffer)
  - Aborted cycle durations and abort count
  - Total parts produced — based on M2/M30 program-end detection
  - "Run From Here" mid-program start detection and safe handling
  - Thread-safe access to all state via RLock

Program Completion Detection (M2/M30 Line Scan — No G-code Changes)
--------------------------------------------------------------------
status.py scans the loaded G-code file to find:
  - first_executable_line : first non-blank, non-comment, non-% line
  - end_line              : last line containing M2, M30, or a trailing %

When motion_line reaches end_line during execution, status.py calls
signal_cycle_complete() — the definitive signal that the program ran
to the end normally.

If the program stops before reaching end_line (E-stop, operator cancel,
feed-hold-then-stop), stop_cycle() is called without a prior
signal_cycle_complete() and the cycle is recorded as an abort.

No changes to G-code files are required.

Run-From-Here Handling
----------------------
If a cycle starts with motion_line > first_executable_line, it is flagged
as is_run_from_here = True. These cycles are tracked but not counted as
completed parts.

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

        self._log(logging.DEBUG,
                  "CycleTimeCalculator v1.2.0 initialised (dev_mode=%s)", dev_mode)

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    def start_cycle(self, run_from_here: bool = False) -> None:
        """
        Start a new cycle.
        run_from_here=True — cycle started mid-program via "Run From Here".
        These cycles are timed but not counted as completed parts.
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
                is_run_from_here=run_from_here,
            )
            if run_from_here:
                self._run_from_here_count += 1
                self._log(logging.WARNING,
                          "Cycle STARTED mid-program (Run From Here #%d). "
                          "Will NOT count as a part.",
                          self._run_from_here_count)
            else:
                self._log(logging.INFO, "Cycle STARTED from beginning.")

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
          is_run_from_here                  → run_from_here record only (no part)
          cycle_complete_signalled          → part counted
          else                              → abort recorded
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "stop_cycle() — no cycle running, skip.")
                return

            duration_ms = self._elapsed_ms_unsafe()
            complete    = self._state.cycle_complete_signalled
            rfh         = self._state.is_run_from_here

            self._log(logging.INFO,
                      "Cycle STOP. duration=%d ms  end_line_reached=%s  "
                      "run_from_here=%s",
                      duration_ms, complete, rfh)

            if duration_ms < MIN_VALID_CYCLE_MS:
                self._log(logging.WARNING,
                          "Cycle %d ms < minimum %d ms — discarded.",
                          duration_ms, MIN_VALID_CYCLE_MS)

            elif rfh:
                self._log(logging.WARNING,
                          "Run-From-Here cycle ended at %d ms — "
                          "not counted as part.", duration_ms)

            elif complete:
                self._completed_durations_ms.append(duration_ms)
                self._parts_produced += 1
                self._log(logging.INFO,
                          "Part COUNTED (#%d). Cycle time: %d ms.",
                          self._parts_produced, duration_ms)

            else:
                # Program did not reach M2/M30 — abort
                self._abort_count += 1
                if duration_ms >= MIN_VALID_CYCLE_MS:
                    self._aborted_durations_ms.append(duration_ms)
                self._log(logging.WARNING,
                          "End line NOT reached — ABORT recorded (#%d). "
                          "Duration: %d ms.",
                          self._abort_count, duration_ms)

            self._state = _CycleState()   # reset for next cycle

    def abort_cycle(self) -> None:
        """
        Explicit abort — called on E-stop or process shutdown while running.
        Always recorded as abort regardless of end-line state.
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "abort_cycle() — no cycle running, skip.")
                return
            duration_ms = self._elapsed_ms_unsafe()
            self._abort_count += 1
            if duration_ms >= MIN_VALID_CYCLE_MS:
                self._aborted_durations_ms.append(duration_ms)
            self._log(logging.WARNING,
                      "Cycle ABORTED (explicit) at %d ms. Total aborts: %d.",
                      duration_ms, self._abort_count)
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
            self._log(logging.INFO, "All stats RESET by operator.")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

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
        return max(0, active_ns // MS_PER_NS)

    def _log(self, level: int, msg: str, *args) -> None:
        """Log only in DEV_MODE or for WARNING+ messages."""
        if self._dev_mode or level >= logging.WARNING:
            logger.log(level, msg, *args)
