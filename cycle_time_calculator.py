#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cycle_time_calculator.py
========================
Industrial-grade cycle time calculator for LinuxCNC CNC applications.

Tracks:
  - Cycle start / pause / resume / stop
  - Completed cycle durations (milliseconds)
  - Aborted cycle durations and abort count
  - Total parts produced (based on completed cycles)
  - Thread-safe access to all state

Design principles:
  - Zero side-effects on LinuxCNC — read-only consumer of state signals
  - Thread-safe via RLock (reentrant so same thread can call multiple methods)
  - DEV_MODE flag controls verbose logging; production stays silent
  - All timestamps use time.perf_counter_ns() for monotonic, high-resolution timing
  - Completed cycles stored as rolling buffer (last MAX_HISTORY entries) to
    avoid unbounded memory growth on long production runs
"""

import threading
import time
import logging
import collections
from dataclasses import dataclass, field
from typing import Optional, List, Deque

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MS_PER_NS: int = 1_000_000          # nanoseconds → milliseconds divisor
MIN_VALID_CYCLE_MS: int = 1_000      # ignore sub-1-second spurious cycles
MAX_HISTORY: int = 500               # rolling buffer size for cycle durations
# A cycle is counted as a "good part" only if its duration is ≥ 50 % of the
# most-recent completed cycle (guards against partial re-runs being counted).
PART_COUNT_THRESHOLD: float = 0.50


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class CycleSnapshot:
    """Immutable point-in-time snapshot returned to callers."""
    is_running: bool
    is_paused: bool
    current_cycle_ms: int               # elapsed ms of the active cycle
    parts_produced: int
    abort_count: int
    last_completed_ms: Optional[int]    # duration of most-recent good cycle
    average_cycle_ms: Optional[float]   # rolling average of completed cycles
    total_completed_cycles: int


@dataclass
class _CycleState:
    """Internal mutable state — always accessed under the lock."""
    start_ns: Optional[int] = None
    pause_start_ns: Optional[int] = None
    total_paused_ns: int = 0
    running: bool = False
    paused: bool = False


# ---------------------------------------------------------------------------
# CycleTimeCalculator
# ---------------------------------------------------------------------------
class CycleTimeCalculator:
    """
    Thread-safe cycle time calculator for a single CNC program slot.

    Public API
    ----------
    start_cycle()   — call when LinuxCNC state transitions to RUNNING
    pause_cycle()   — call when LinuxCNC state transitions to PAUSED
    resume_cycle()  — call when LinuxCNC state transitions back to RUNNING
    stop_cycle()    — call when program completes normally
    abort_cycle()   — call when program is aborted / E-stopped mid-run
    snapshot()      — returns a CycleSnapshot (non-blocking, safe to poll)
    reset_stats()   — clears counters (operator-initiated reset only)
    """

    def __init__(self, dev_mode: bool = False) -> None:
        self._dev_mode = dev_mode
        self._lock = threading.RLock()
        self._state = _CycleState()

        # Completed-cycle storage
        self._completed_durations_ms: Deque[int] = collections.deque(
            maxlen=MAX_HISTORY
        )
        self._aborted_durations_ms: Deque[int] = collections.deque(
            maxlen=MAX_HISTORY
        )
        self._parts_produced: int = 0
        self._abort_count: int = 0

        self._log(logging.DEBUG, "CycleTimeCalculator initialised (dev_mode=%s)", dev_mode)

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    def start_cycle(self) -> None:
        """Start a new cycle.  Ignored if a cycle is already running."""
        with self._lock:
            if self._state.running:
                self._log(
                    logging.WARNING,
                    "start_cycle() called but cycle already running — ignoring.",
                )
                return
            self._state = _CycleState(
                start_ns=time.perf_counter_ns(),
                running=True,
                paused=False,
            )
            self._log(logging.INFO, "Cycle STARTED.")

    def pause_cycle(self) -> None:
        """Pause the running cycle.  Ignored if not running or already paused."""
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "pause_cycle() — no cycle running, skip.")
                return
            if self._state.paused:
                self._log(logging.DEBUG, "pause_cycle() — already paused, skip.")
                return
            self._state.pause_start_ns = time.perf_counter_ns()
            self._state.paused = True
            elapsed_ms = self._elapsed_ms_unsafe()
            self._log(logging.INFO, "Cycle PAUSED at %d ms.", elapsed_ms)

    def resume_cycle(self) -> None:
        """Resume a paused cycle.  Ignored if not paused."""
        with self._lock:
            if not self._state.running or not self._state.paused:
                self._log(logging.DEBUG, "resume_cycle() — not in paused state, skip.")
                return
            paused_duration_ns = time.perf_counter_ns() - self._state.pause_start_ns
            self._state.total_paused_ns += paused_duration_ns
            self._state.pause_start_ns = None
            self._state.paused = False
            self._log(
                logging.INFO,
                "Cycle RESUMED. Paused for %d ms.",
                paused_duration_ns // MS_PER_NS,
            )

    def stop_cycle(self) -> None:
        """
        Stop the current cycle (program completed normally).
        Records duration and increments parts_produced if cycle meets threshold.
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "stop_cycle() — no cycle running, skip.")
                return
            duration_ms = self._elapsed_ms_unsafe()
            self._log(
                logging.INFO,
                "Cycle STOPPED. Duration: %d ms.",
                duration_ms,
            )
            if duration_ms >= MIN_VALID_CYCLE_MS:
                self._completed_durations_ms.append(duration_ms)
                # Part count: accept if duration ≥ threshold × last good cycle
                if self._is_good_part(duration_ms):
                    self._parts_produced += 1
                    self._log(
                        logging.INFO,
                        "Part counted. Total parts: %d.",
                        self._parts_produced,
                    )
                else:
                    self._log(
                        logging.WARNING,
                        "Cycle too short vs previous (%d ms) — part NOT counted.",
                        duration_ms,
                    )
            else:
                self._log(
                    logging.WARNING,
                    "Cycle duration %d ms below minimum %d ms — discarded.",
                    duration_ms,
                    MIN_VALID_CYCLE_MS,
                )
            self._state = _CycleState()   # reset

    def abort_cycle(self) -> None:
        """
        Abort the current cycle (E-stop, operator cancel, error).
        Records duration separately; does NOT increment parts_produced.
        """
        with self._lock:
            if not self._state.running:
                self._log(logging.DEBUG, "abort_cycle() — no cycle running, skip.")
                return
            duration_ms = self._elapsed_ms_unsafe()
            self._abort_count += 1
            if duration_ms >= MIN_VALID_CYCLE_MS:
                self._aborted_durations_ms.append(duration_ms)
            self._log(
                logging.WARNING,
                "Cycle ABORTED at %d ms. Total aborts: %d.",
                duration_ms,
                self._abort_count,
            )
            self._state = _CycleState()   # reset

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    def snapshot(self) -> CycleSnapshot:
        """Return a non-blocking, immutable snapshot of current state."""
        with self._lock:
            current_ms = self._elapsed_ms_unsafe() if self._state.running else 0
            last_completed = (
                self._completed_durations_ms[-1]
                if self._completed_durations_ms else None
            )
            avg = (
                sum(self._completed_durations_ms) / len(self._completed_durations_ms)
                if self._completed_durations_ms else None
            )
            return CycleSnapshot(
                is_running=self._state.running,
                is_paused=self._state.paused,
                current_cycle_ms=current_ms,
                parts_produced=self._parts_produced,
                abort_count=self._abort_count,
                last_completed_ms=last_completed,
                average_cycle_ms=round(avg, 1) if avg is not None else None,
                total_completed_cycles=len(self._completed_durations_ms),
            )

    def get_completed_durations(self) -> List[int]:
        """Return a copy of completed cycle durations (ms)."""
        with self._lock:
            return list(self._completed_durations_ms)

    def get_aborted_durations(self) -> List[int]:
        """Return a copy of aborted cycle durations (ms)."""
        with self._lock:
            return list(self._aborted_durations_ms)

    def reset_stats(self) -> None:
        """
        Reset all counters and history.  Must be called only when no cycle
        is running (enforced).  Use for operator-initiated shift/job resets.
        """
        with self._lock:
            if self._state.running:
                logger.error(
                    "reset_stats() refused — cycle is currently running. "
                    "Stop or abort the cycle first."
                )
                return
            self._completed_durations_ms.clear()
            self._aborted_durations_ms.clear()
            self._parts_produced = 0
            self._abort_count = 0
            self._log(logging.INFO, "Stats RESET by operator.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _elapsed_ms_unsafe(self) -> int:
        """
        Compute elapsed active cycle time in ms.
        MUST be called while holding self._lock.
        """
        if self._state.start_ns is None:
            return 0
        now_ns = time.perf_counter_ns()
        if self._state.paused and self._state.pause_start_ns is not None:
            # Time frozen at pause point
            active_ns = (
                self._state.pause_start_ns
                - self._state.start_ns
                - self._state.total_paused_ns
            )
        else:
            active_ns = (
                now_ns
                - self._state.start_ns
                - self._state.total_paused_ns
            )
        return max(0, active_ns // MS_PER_NS)

    def _is_good_part(self, duration_ms: int) -> bool:
        """
        A part is 'good' (counts toward production) if:
          - It is the first cycle ever, OR
          - Its duration ≥ PART_COUNT_THRESHOLD × most-recent completed cycle
        """
        if len(self._completed_durations_ms) <= 1:
            return True
        # Compare against the *previous* completed cycle (index -2 because
        # the current one was just appended at index -1)
        previous_ms = self._completed_durations_ms[-2]
        return duration_ms >= previous_ms * PART_COUNT_THRESHOLD

    def _log(self, level: int, msg: str, *args) -> None:
        """Emit log only when DEV_MODE is active (or level >= WARNING)."""
        if self._dev_mode or level >= logging.WARNING:
            logger.log(level, msg, *args)
