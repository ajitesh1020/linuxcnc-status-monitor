#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
status.py
=========
Industrial-grade LinuxCNC status monitor for OFC_PC.

Responsibilities
----------------
  1. Poll LinuxCNC status channel every second (non-blocking, catches all
     exceptions so LinuxCNC is NEVER affected by this process).
  2. Track machine cycle state (running / paused / stopped / aborted) and
     delegate timing to CycleTimeCalculator.
  3. Collect axis positions, velocities, accelerations, spindle RPM, active
     G-code line, loaded file details, and LinuxCNC error/NML messages.
  4. Serialise collected data as JSON and transmit via UDP to the monitoring PC.
  5. Expose DEV_MODE flag: when True → verbose DEBUG logging to console + file;
     when False → only WARNING/ERROR logged (silent in production).

Usage
-----
    # Terminal (manual testing):
    python3 status.py --dev

    # Via launch_ofc.sh without modifying it (export before running):
    CNC_DEV_MODE=1 bash launch_ofc.sh

    # Or export persistently in your shell session:
    export CNC_DEV_MODE=1

DEV_MODE activates when EITHER --dev flag OR CNC_DEV_MODE=1 env var is set.
This lets you enable debug logging without touching the launcher script.

Safety guarantees
-----------------
  - All LinuxCNC calls are wrapped in try/except.  A failure in this script
    will NEVER raise an exception into the LinuxCNC process — it runs as a
    completely separate OS process.
  - UDP is fire-and-forget; a missing/unreachable monitoring PC causes only
    a logged warning, never a crash.
  - The poll loop catches ALL exceptions and continues; only a clean
    KeyboardInterrupt or SIGTERM will stop the process.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Guard: linuxcnc module is only available inside the LinuxCNC environment.
# Provide a clear error instead of a cryptic ImportError.
# ---------------------------------------------------------------------------
try:
    import linuxcnc
except ImportError:
    print(
        "[FATAL] Could not import 'linuxcnc'. "
        "This script must run inside a LinuxCNC environment.",
        file=sys.stderr,
    )
    sys.exit(1)

from cycle_time_calculator import CycleTimeCalculator, CycleSnapshot

# ---------------------------------------------------------------------------
# Configuration — edit these constants for your installation
# ---------------------------------------------------------------------------
MONITOR_PC_IP: str = "193.168.0.3"
MONITOR_PC_PORT: int = 5005

POLL_INTERVAL_S: float = 1.0          # seconds between status broadcasts
NML_ERROR_POLL_INTERVAL_S: float = 0.25  # how often to drain the NML error channel

LOG_FILE: str = "/tmp/cnc_status.log"
LOG_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB per log file
LOG_BACKUP_COUNT: int = 3             # keep 3 rotated files

# LinuxCNC task states (from linuxcnc.h)
STATE_ESTOP: int = 1
STATE_ESTOP_RESET: int = 2
STATE_OFF: int = 3
STATE_ON: int = 4

# LinuxCNC exec states
EXEC_DONE: int = 1
EXEC_ERROR: int = 2
EXEC_WAITING_FOR_MOTION: int = 3
EXEC_WAITING_FOR_MOTION_QUEUE: int = 4
EXEC_WAITING_FOR_IO: int = 5
EXEC_WAITING_FOR_PAUSE: int = 6
EXEC_WAITING_FOR_MOTION_AND_IO: int = 7
EXEC_WAITING_FOR_DELAY: int = 8
EXEC_WAITING_FOR_SYSTEM_CMD: int = 9
EXEC_WAITING_FOR_SPINDLE_ORIENTED: int = 10

# Task modes
MODE_MANUAL: int = 1
MODE_AUTO: int = 2
MODE_MDI: int = 3

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_shutdown_requested: bool = False
logger: logging.Logger = logging.getLogger("cnc_status")


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------
def _handle_signal(signum: int, _frame) -> None:
    global _shutdown_requested
    logger.warning("Received signal %d — initiating clean shutdown.", signum)
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def _configure_logging(dev_mode: bool) -> None:
    """
    Configure logging:
      DEV_MODE=True  → DEBUG level, console + rotating file
      DEV_MODE=False → WARNING level, rotating file only (silent CLI)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # capture everything at root

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — always active
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG if dev_mode else logging.WARNING)
        fh.setFormatter(fmt)
        root_logger.addHandler(fh)
    except OSError as exc:
        print(f"[WARNING] Cannot open log file {LOG_FILE}: {exc}", file=sys.stderr)

    # Console handler — only in DEV_MODE
    if dev_mode:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(fmt)
        root_logger.addHandler(ch)


# ---------------------------------------------------------------------------
# LinuxCNC state helpers
# ---------------------------------------------------------------------------
def _is_program_running(stat: linuxcnc.stat) -> bool:
    """True when a G-code program is actively executing (not paused)."""
    return (
        stat.task_state == STATE_ON
        and stat.task_mode == MODE_AUTO
        and stat.interp_state not in (
            linuxcnc.INTERP_IDLE,   # type: ignore[attr-defined]
            linuxcnc.INTERP_PAUSED, # type: ignore[attr-defined]
        )
        and not stat.paused
    )


def _is_program_paused(stat: linuxcnc.stat) -> bool:
    """True when a G-code program is paused (feed hold)."""
    return (
        stat.task_state == STATE_ON
        and stat.task_mode == MODE_AUTO
        and stat.paused
    )


def _is_program_idle(stat: linuxcnc.stat) -> bool:
    """True when no program is running (idle, MDI, manual, e-stop)."""
    return not _is_program_running(stat) and not _is_program_paused(stat)


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------
def _safe_get(stat: linuxcnc.stat, attr: str, default: Any = None) -> Any:
    """Safely retrieve a LinuxCNC stat attribute; return default on failure."""
    try:
        return getattr(stat, attr)
    except AttributeError:
        logger.debug("Attribute '%s' not available on linuxcnc.stat.", attr)
        return default
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Error reading stat.%s: %s", attr, exc)
        return default


def _collect_axis_data(stat: linuxcnc.stat) -> Dict[str, Any]:
    """Collect per-axis position, velocity, and acceleration data."""
    axis_data: Dict[str, Any] = {}
    axis_mask: int = _safe_get(stat, "axis_mask", 0)
    axis_names = ["x", "y", "z", "a", "b", "c", "u", "v", "w"]

    raw_axes = _safe_get(stat, "axis", [])
    for idx, name in enumerate(axis_names):
        if axis_mask & (1 << idx):
            if idx < len(raw_axes):
                a = raw_axes[idx]
                axis_data[name] = {
                    "pos": round(a.get("input", 0.0), 6),
                    "vel": round(a.get("velocity", 0.0), 6),
                    "min_pos_limit": round(a.get("min_position_limit", 0.0), 4),
                    "max_pos_limit": round(a.get("max_position_limit", 0.0), 4),
                }
    return axis_data


def _collect_joint_data(stat: linuxcnc.stat) -> list:
    """Collect per-joint position, velocity, and fault data."""
    joint_data = []
    num_joints: int = _safe_get(stat, "joints", 0)
    raw_joints = _safe_get(stat, "joint", [])
    for idx in range(int(num_joints)):
        if idx < len(raw_joints):
            j = raw_joints[idx]
            joint_data.append(
                {
                    "id": idx,
                    "pos": round(j.get("input", 0.0), 6),
                    "vel": round(j.get("velocity", 0.0), 6),
                    "homed": bool(j.get("homed", False)),
                    "fault": bool(j.get("fault", False)),
                    "ferror": round(j.get("ferror_current", 0.0), 6),
                }
            )
    return joint_data


def _collect_spindle_data(stat: linuxcnc.stat) -> list:
    """Collect per-spindle speed, direction, and override data."""
    spindle_data = []
    num_spindles: int = _safe_get(stat, "spindles", 1)
    raw_spindles = _safe_get(stat, "spindle", [])
    for idx in range(int(num_spindles)):
        if idx < len(raw_spindles):
            s = raw_spindles[idx]
            spindle_data.append(
                {
                    "id": idx,
                    "speed": round(s.get("speed", 0.0), 2),
                    "direction": s.get("direction", 0),
                    "override": round(s.get("override", 1.0), 4),
                    "at_speed": bool(s.get("at_speed", False)),
                    "enabled": bool(s.get("enabled", False)),
                }
            )
    return spindle_data


def _collect_file_data(stat: linuxcnc.stat) -> Dict[str, Any]:
    """Collect loaded G-code file metadata safely."""
    file_path: str = _safe_get(stat, "file", "") or ""
    if not file_path:
        return {"file_name": "", "file_size": 0, "file_modified_ms": 0}
    try:
        stat_result = os.stat(file_path)
        return {
            "file_name": os.path.basename(file_path),
            "file_size": stat_result.st_size,
            "file_modified_ms": int(stat_result.st_mtime * 1000),
        }
    except OSError as exc:
        logger.debug("Cannot stat file '%s': %s", file_path, exc)
        return {
            "file_name": os.path.basename(file_path),
            "file_size": 0,
            "file_modified_ms": 0,
        }


def _collect_motion_data(stat: linuxcnc.stat) -> Dict[str, Any]:
    """Collect current velocity, distance to go, and motion state."""
    return {
        "current_vel": round(_safe_get(stat, "current_vel", 0.0), 6),
        "distance_to_go": round(_safe_get(stat, "distance_to_go", 0.0), 6),
        "motion_type": _safe_get(stat, "motion_type", 0),
        "motion_line": _safe_get(stat, "motion_line", 0),
        "current_line": _safe_get(stat, "current_line", 0),
        "delay_left": round(_safe_get(stat, "delay_left", 0.0), 3),
        "feedrate": round(_safe_get(stat, "feedrate", 0.0), 4),
        "rapidrate": round(_safe_get(stat, "rapidrate", 0.0), 4),
    }


def _collect_program_status(stat: linuxcnc.stat) -> Dict[str, Any]:
    """Collect high-level machine / program state flags."""
    return {
        "task_state": _safe_get(stat, "task_state", 0),
        "task_mode": _safe_get(stat, "task_mode", 0),
        "interp_state": _safe_get(stat, "interp_state", 0),
        "exec_state": _safe_get(stat, "exec_state", 0),
        "estop": bool(_safe_get(stat, "estop", True)),
        "enabled": bool(_safe_get(stat, "enabled", False)),
        "paused": bool(_safe_get(stat, "paused", False)),
        "tool_in_spindle": _safe_get(stat, "tool_in_spindle", 0),
        "g5x_index": _safe_get(stat, "g5x_index", 0),
        "g5x_offset": list(_safe_get(stat, "g5x_offset", [])),
        "gcodes": list(_safe_get(stat, "gcodes", [])),
        "mcodes": list(_safe_get(stat, "mcodes", [])),
        "settings": list(_safe_get(stat, "settings", [])),
    }


def _drain_nml_errors(error_channel: linuxcnc.error_channel) -> list:
    """
    Drain all pending NML error messages without blocking.
    Returns list of {"kind": int, "msg": str} dicts.
    """
    errors = []
    try:
        while True:
            error = error_channel.poll()
            if error is None:
                break
            kind, msg = error
            errors.append({"kind": kind, "msg": msg.strip()})
            logger.warning("LinuxCNC NML error [kind=%d]: %s", kind, msg.strip())
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("NML error poll exception: %s", exc)
    return errors


# ---------------------------------------------------------------------------
# Cycle state machine
# ---------------------------------------------------------------------------
class _CycleStateMachine:
    """
    Tracks transitions between IDLE → RUNNING → PAUSED → IDLE/ABORTED and
    calls the appropriate CycleTimeCalculator methods exactly once per
    transition edge (not on every poll tick).
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"

    def __init__(self, calculator: CycleTimeCalculator) -> None:
        self._calc = calculator
        self._state = self.IDLE

    def update(self, stat: linuxcnc.stat) -> str:
        """
        Evaluate LinuxCNC stat and drive state transitions.
        Returns the current logical state string.
        """
        is_running = _is_program_running(stat)
        is_paused = _is_program_paused(stat)
        is_idle = not is_running and not is_paused

        if self._state == self.IDLE:
            if is_running:
                self._calc.start_cycle()
                self._state = self.RUNNING
            # paused from idle is ignored (e.g., feed-hold with no program)

        elif self._state == self.RUNNING:
            if is_paused:
                self._calc.pause_cycle()
                self._state = self.PAUSED
            elif is_idle:
                # Distinguish normal completion from abort/error
                exec_state = _safe_get(stat, "exec_state", EXEC_DONE)
                if exec_state in (EXEC_DONE,):
                    self._calc.stop_cycle()
                else:
                    self._calc.abort_cycle()
                self._state = self.IDLE

        elif self._state == self.PAUSED:
            if is_running:
                self._calc.resume_cycle()
                self._state = self.RUNNING
            elif is_idle:
                # Aborted while paused
                self._calc.abort_cycle()
                self._state = self.IDLE

        return self._state


# ---------------------------------------------------------------------------
# UDP sender
# ---------------------------------------------------------------------------
class _UdpSender:
    """
    Thin wrapper around a UDP socket with lazy re-creation on error.
    UDP is connectionless — send failures are logged but never crash the app.
    """

    def __init__(self, ip: str, port: int) -> None:
        self._ip = ip
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._create_socket()

    def _create_socket(self) -> None:
        try:
            if self._sock:
                self._sock.close()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            logger.debug("UDP socket created → %s:%d", self._ip, self._port)
        except OSError as exc:
            logger.error("Failed to create UDP socket: %s", exc)
            self._sock = None

    def send(self, payload: bytes) -> bool:
        """Send bytes; returns True on success, False on failure."""
        if self._sock is None:
            self._create_socket()
        if self._sock is None:
            return False
        try:
            self._sock.sendto(payload, (self._ip, self._port))
            return True
        except OSError as exc:
            logger.warning("UDP send failed (%s:%d): %s", self._ip, self._port, exc)
            self._create_socket()  # recreate for next attempt
            return False

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CNC Status Monitor — broadcasts LinuxCNC state via UDP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "DEV MODE can also be enabled without the --dev flag by setting\n"
            "the environment variable:  CNC_DEV_MODE=1\n\n"
            "Examples:\n"
            "  python3 status.py --dev              # terminal testing\n"
            "  CNC_DEV_MODE=1 bash launch_ofc.sh   # via launcher, no script edit needed\n"
            "  export CNC_DEV_MODE=1                # persist for current shell session"
        ),
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        default=False,
        help=(
            "Enable development mode: verbose DEBUG logging to console and file. "
            "Equivalent to setting env var CNC_DEV_MODE=1."
        ),
    )
    return parser.parse_args()


def _resolve_dev_mode(args: argparse.Namespace) -> bool:
    """
    DEV_MODE is active when EITHER:
      - --dev flag passed on the command line, OR
      - Environment variable CNC_DEV_MODE is set to '1' or 'true' (case-insensitive)

    This allows enabling debug logging via the launcher without modifying
    any script — just prefix the launch command:
        CNC_DEV_MODE=1 bash launch_ofc.sh
    """
    env_value = os.environ.get("CNC_DEV_MODE", "").strip().lower()
    env_dev = env_value in ("1", "true", "yes")
    return args.dev or env_dev


def main() -> None:
    global _shutdown_requested

    args = _parse_args()
    dev_mode: bool = _resolve_dev_mode(args)

    # Print the active dev mode source so it's visible in terminal
    if dev_mode:
        source = "--dev flag" if args.dev else "CNC_DEV_MODE env var"
        print(f"[DEV MODE ACTIVE — enabled via {source}]", flush=True)

    _configure_logging(dev_mode)

    logger.info(
        "CNC Status Monitor starting. dev_mode=%s, target=%s:%d",
        dev_mode,
        MONITOR_PC_IP,
        MONITOR_PC_PORT,
    )

    # Register graceful shutdown signals
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Initialise subsystems
    calculator = CycleTimeCalculator(dev_mode=dev_mode)
    state_machine = _CycleStateMachine(calculator)
    sender = _UdpSender(MONITOR_PC_IP, MONITOR_PC_PORT)

    # LinuxCNC channels
    stat_channel: Optional[linuxcnc.stat] = None
    error_channel: Optional[linuxcnc.error_channel] = None

    def _connect_linuxcnc() -> bool:
        nonlocal stat_channel, error_channel
        try:
            stat_channel = linuxcnc.stat()
            error_channel = linuxcnc.error_channel()
            logger.info("Connected to LinuxCNC status/error channels.")
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Cannot connect to LinuxCNC: %s. Retrying in 5 s.", exc)
            stat_channel = None
            error_channel = None
            return False

    last_poll_time: float = 0.0
    pending_nml_errors: list = []
    consecutive_poll_errors: int = 0
    MAX_CONSECUTIVE_ERRORS: int = 10

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    while not _shutdown_requested:
        now = time.monotonic()

        # Throttle to POLL_INTERVAL_S
        if now - last_poll_time < POLL_INTERVAL_S:
            time.sleep(0.05)
            continue
        last_poll_time = now

        # Ensure LinuxCNC connection
        if stat_channel is None:
            if not _connect_linuxcnc():
                time.sleep(5.0)
                continue

        # ------------------------------------------------------------------
        # Poll LinuxCNC stat channel
        # ------------------------------------------------------------------
        try:
            stat_channel.poll()
            consecutive_poll_errors = 0
        except Exception as exc:  # pylint: disable=broad-except
            consecutive_poll_errors += 1
            logger.error(
                "LinuxCNC stat poll error (%d/%d): %s",
                consecutive_poll_errors,
                MAX_CONSECUTIVE_ERRORS,
                exc,
            )
            if consecutive_poll_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.critical(
                    "Too many consecutive poll errors — reconnecting to LinuxCNC."
                )
                stat_channel = None
                error_channel = None
                consecutive_poll_errors = 0
            time.sleep(1.0)
            continue

        # ------------------------------------------------------------------
        # Drive cycle state machine
        # ------------------------------------------------------------------
        try:
            current_cycle_state = state_machine.update(stat_channel)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Cycle state machine error: %s", exc)
            current_cycle_state = "UNKNOWN"

        # ------------------------------------------------------------------
        # Drain NML errors (non-blocking)
        # ------------------------------------------------------------------
        if error_channel is not None:
            try:
                new_errors = _drain_nml_errors(error_channel)
                pending_nml_errors.extend(new_errors)
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("NML error drain failed: %s", exc)

        # ------------------------------------------------------------------
        # Collect all status data
        # ------------------------------------------------------------------
        try:
            snap: CycleSnapshot = calculator.snapshot()

            payload: Dict[str, Any] = {
                # Timestamp (epoch ms)
                "ts": int(time.time_ns() // 1_000_000),

                # Cycle / production data
                "cycle_state": current_cycle_state,
                "cycle_time_ms": snap.current_cycle_ms,
                "parts_produced": snap.parts_produced,
                "abort_count": snap.abort_count,
                "last_cycle_ms": snap.last_completed_ms,
                "avg_cycle_ms": snap.average_cycle_ms,
                "total_completed_cycles": snap.total_completed_cycles,

                # Machine status
                **_collect_program_status(stat_channel),

                # Motion / velocity
                **_collect_motion_data(stat_channel),

                # Axes (X, Y, Z …)
                "axis": _collect_axis_data(stat_channel),

                # Joints
                "joints": _collect_joint_data(stat_channel),

                # Spindle(s)
                "spindles": _collect_spindle_data(stat_channel),

                # Loaded file
                **_collect_file_data(stat_channel),

                # NML errors since last packet (drain-and-clear)
                "nml_errors": pending_nml_errors.copy(),
            }
            pending_nml_errors.clear()

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Data collection error: %s", exc)
            continue

        # ------------------------------------------------------------------
        # Serialise and send
        # ------------------------------------------------------------------
        try:
            json_bytes = json.dumps(payload, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.error("JSON serialisation error: %s", exc)
            continue

        if len(json_bytes) > 65000:
            logger.warning(
                "Payload size %d bytes approaches UDP limit — consider reducing data.",
                len(json_bytes),
            )

        sent = sender.send(json_bytes)
        logger.debug(
            "Packet %s (%d bytes) → %s:%d | cycle=%s parts=%d aborts=%d",
            "sent" if sent else "FAILED",
            len(json_bytes),
            MONITOR_PC_IP,
            MONITOR_PC_PORT,
            current_cycle_state,
            snap.parts_produced,
            snap.abort_count,
        )

    # -----------------------------------------------------------------------
    # Clean shutdown
    # -----------------------------------------------------------------------
    logger.info("Shutting down CNC Status Monitor.")

    # If a cycle was running, mark it as aborted (power-off scenario)
    snap = calculator.snapshot()
    if snap.is_running:
        logger.warning("Shutdown with active cycle — recording as abort.")
        calculator.abort_cycle()

    sender.close()
    logger.info("CNC Status Monitor stopped cleanly.")


if __name__ == "__main__":
    main()
