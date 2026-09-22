#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
# It links the GPL-licensed LinuxCNC Python module and is therefore GPL.
"""
status.py  —  v1.3.0
=====================
Industrial-grade LinuxCNC status monitor for OFC_PC.

Responsibilities
----------------
  1. Poll LinuxCNC status channel every second (non-blocking).
  2. Drive cycle state machine: IDLE → RUNNING → PAUSED → IDLE/ABORTED.
  3. Auto-detect program completion by scanning the loaded G-code file for
     M2 / M30 / trailing-% lines — NO changes to G-code files required.
  4. Detect "Run From Here" mid-program starts via motion_line comparison.
  5. Suppress UDP packets while idle; send one heartbeat every 30 s.
  6. Stream the full G-code file once on load; re-send on file change.
  7. Passively poll the NML error channel — AXIS always wins the queue race
     and displays errors to the operator; status.py captures only what
     AXIS misses (best-effort, not guaranteed).
  8. DEV_MODE via --dev flag OR CNC_DEV_MODE=1 env var.

NML Error Design Decision
--------------------------
The LinuxCNC error channel is a NML *queue*. The official documentation
states:

  "The first consumer of an error message DELETES that message from the
   queue. Whether another error message consumer (e.g. AXIS) will see the
   message is dependent on timing. It is recommended to have just one
   error channel reader task in a setup."

This means: whoever calls error_channel.poll() first gets the message,
and it is gone for everyone else.

Design choice: AXIS operator visibility takes priority.
  - status.py polls the error channel ONCE per second in the main loop
  - AXIS polls much faster and will win the race in most cases
  - The operator at the machine always sees error notifications
  - The monitoring PC receives any errors status.py happens to catch
    (typically during startup before AXIS is polling, or on rare timing wins)
  - nml_errors in the UDP packet is best-effort, not guaranteed
  - exec_state in every packet reliably signals an error condition without
    consuming the queue: exec_state == 1 means EXEC_ERROR

Logging (v1.3.0)
-----------------
  Production (no --dev):
    - Root logger level = WARNING
    - NullHandler only — no file, no console, zero output
    - All logger.debug() / logger.info() calls are zero-overhead
    - Log file is never created or written to

  Dev mode (--dev or CNC_DEV_MODE=1):
    - Root logger level = DEBUG
    - Console StreamHandler (stdout)
    - Rotating file handler → /tmp/cnc_status.log

Program End Detection (No G-code changes required)
---------------------------------------------------
_GcodeEndDetector scans the loaded .ngc file on every file change:
  first_exec_line — first non-blank, non-comment, non-%, non-O-word line
  end_line        — LAST line containing M2, M30, or a standalone %

When motion_line >= end_line → signal_cycle_complete() → part counted.
If program stops before end_line → abort recorded.

Run-From-Here Detection
-----------------------
If motion_line > first_exec_line + 2 at cycle start → run_from_here flag.

Idle Suppression
----------------
  - One packet on IDLE transition edge
  - Silence for idle_heartbeat_interval_s from config.yaml (default 30 s)
  - Keep-alive heartbeat every 30 s
  - Full stream resumes immediately when machine becomes active

Safety guarantees
-----------------
  - All LinuxCNC calls wrapped in try/except.
  - UDP send failures logged but never crash the loop.
  - SIGTERM/SIGINT → clean shutdown; in-flight cycle marked as abort.
"""

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

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
# Protocol
# ---------------------------------------------------------------------------
PROTOCOL_VERSION: int = 1   # wire-format version — see PROTOCOL.md

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# All tunables live in config.yaml (see config.example.yaml). The values below
# are the built-in fallbacks used when a key is missing or no config file is
# found, so the agent always starts even with zero configuration.
DEFAULT_CONFIG: Dict[str, Any] = {
    "monitor_pc_ip":             "193.168.0.3",   # monitoring PC static IP
    "monitor_pc_port":           5005,            # UDP port on the monitoring PC
    "machine_name":              "",              # identifies this machine to the dashboard
    "poll_interval_s":           1.0,             # seconds between active status packets
    "idle_heartbeat_interval_s": 30.0,            # keep-alive interval while idle
    "gcode_chunk_size":          50_000,          # bytes per G-code file chunk
    "log_file":                  "/tmp/cnc_status.log",
    "log_max_bytes":             5 * 1024 * 1024,
    "log_backup_count":          3,
}

# Searched when --config is not given: config.yaml next to this script.
DEFAULT_CONFIG_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.yaml"
)


def _coerce(value: Any, template: Any) -> Any:
    """Coerce a loaded value to the type of its default (best-effort)."""
    if isinstance(template, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(template, int):
        return int(value)
    if isinstance(template, float):
        return float(value)
    return str(value)


def _parse_flat_yaml(text: str) -> Dict[str, Any]:
    """
    Minimal zero-dependency parser for a FLAT 'key: value' YAML file.

    Supports comments (#), blank lines, quoted or bare string scalars, ints,
    floats and booleans. Nested structures are NOT supported — this config is
    intentionally flat. Used only when PyYAML is not installed.
    """
    out: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            out[key] = val[1:-1]
            continue
        low = val.lower()
        if low in ("true", "false"):
            out[key] = (low == "true")
        elif low in ("null", "~", ""):
            out[key] = ""
        else:
            try:
                out[key] = int(val)
            except ValueError:
                try:
                    out[key] = float(val)
                except ValueError:
                    out[key] = val
    return out


def load_config(path: Optional[str]) -> Dict[str, Any]:
    """
    Load configuration, layering config.yaml over DEFAULT_CONFIG.

    Prefers PyYAML if installed; otherwise uses the built-in flat parser so the
    zero-dependency guarantee holds. A missing file, unreadable file, or any
    parse error logs a warning and falls back to defaults — the agent must
    always start.
    """
    cfg: Dict[str, Any] = dict(DEFAULT_CONFIG)
    cfg_path = path or DEFAULT_CONFIG_PATH

    if not os.path.isfile(cfg_path):
        if path:  # user explicitly pointed at a file that isn't there
            logger.warning("Config file not found: %s — using defaults.", cfg_path)
        else:
            logger.info("No config.yaml at %s — using built-in defaults.", cfg_path)
        return cfg

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        logger.warning("Cannot read config %s: %s — using defaults.", cfg_path, exc)
        return cfg

    try:
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(text) or {}
            if not isinstance(loaded, dict):
                raise ValueError("top-level YAML is not a mapping")
        except ImportError:
            loaded = _parse_flat_yaml(text)
    except Exception as exc:
        logger.warning("Config parse error in %s: %s — using defaults.", cfg_path, exc)
        return cfg

    for key, default in DEFAULT_CONFIG.items():
        if key in loaded and loaded[key] is not None:
            try:
                cfg[key] = _coerce(loaded[key], default)
            except (TypeError, ValueError):
                logger.warning("Bad value for '%s' in config — keeping default %r.",
                               key, default)

    logger.info("Loaded config from %s", cfg_path)
    return cfg

# LinuxCNC task states
STATE_ESTOP:       int = 1
STATE_ESTOP_RESET: int = 2
STATE_OFF:         int = 3
STATE_ON:          int = 4

# Task modes
MODE_MANUAL: int = 1
MODE_AUTO:   int = 2
MODE_MDI:    int = 3

# G-code end-line patterns
_GCODE_END_RE  = re.compile(r"^(m0*2\b|m0*30\b|%\s*$)", re.IGNORECASE)
_GCODE_SKIP_RE = re.compile(r"^(\s*$|;|%|\(|o\s*\d)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_shutdown_requested: bool = False
logger: logging.Logger    = logging.getLogger("cnc_status")


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------
def _handle_signal(signum: int, _frame) -> None:
    global _shutdown_requested
    logger.warning("Signal %d received — initiating clean shutdown.", signum)
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _configure_logging(dev_mode: bool, cfg: Dict[str, Any]) -> None:
    """
    Production (no --dev):
        root = WARNING + NullHandler only.
        No file created. No console output. Zero overhead on all log calls.

    Dev mode (--dev or CNC_DEV_MODE=1):
        root = DEBUG + console StreamHandler + rotating file handler.
    """
    root = logging.getLogger()

    if not dev_mode:
        root.setLevel(logging.WARNING)
        root.addHandler(logging.NullHandler())
        return

    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    try:
        fh = logging.handlers.RotatingFileHandler(
            cfg["log_file"], maxBytes=cfg["log_max_bytes"],
            backupCount=cfg["log_backup_count"], encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        print(f"[WARNING] Cannot open log file {cfg['log_file']}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# LinuxCNC state helpers
# ---------------------------------------------------------------------------
def _safe_get(stat: linuxcnc.stat, attr: str, default: Any = None) -> Any:
    try:
        return getattr(stat, attr)
    except AttributeError:
        return default
    except Exception:
        return default


def _is_program_running(stat: linuxcnc.stat) -> bool:
    return (
        stat.task_state == STATE_ON
        and stat.task_mode == MODE_AUTO
        and stat.interp_state not in (
            linuxcnc.INTERP_IDLE,    # type: ignore[attr-defined]
            linuxcnc.INTERP_PAUSED,  # type: ignore[attr-defined]
        )
        and not stat.paused
    )


def _is_program_paused(stat: linuxcnc.stat) -> bool:
    return (
        stat.task_state == STATE_ON
        and stat.task_mode == MODE_AUTO
        and stat.paused
    )


# ---------------------------------------------------------------------------
# NML error drain — passive, best-effort
# ---------------------------------------------------------------------------
def _drain_nml_errors(error_channel: linuxcnc.error_channel) -> List[Dict]:
    """
    Drain whatever NML errors status.py happens to catch this tick.

    AXIS will win the queue race in most cases and display errors to the
    operator. This function captures only what AXIS misses. The result is
    best-effort: nml_errors in the UDP packet may be empty even when an
    error occurred.

    Use exec_state == 1 (EXEC_ERROR) in the status packet for a reliable
    error-condition indicator that does not consume the queue.
    """
    errors: List[Dict] = []
    try:
        while True:
            err = error_channel.poll()
            if err is None:
                break
            kind, msg = err
            errors.append({"kind": kind, "msg": msg.strip()})
            logger.debug("NML caught [kind=%d]: %s", kind, msg.strip())
    except Exception as exc:
        logger.debug("NML drain error: %s", exc)
    return errors


# ---------------------------------------------------------------------------
# G-code end-line detector
# ---------------------------------------------------------------------------
class _GcodeEndDetector:
    """
    Scans the loaded G-code file to find:
      first_exec_line — first non-blank, non-comment, non-%, non-O-word line
      end_line        — LAST line matching M2 / M30 / standalone %

    No changes to G-code files are required.
    """

    def __init__(self) -> None:
        self._file_path:          str  = ""
        self.first_exec_line:     int  = 1
        self.end_line:            int  = -1
        self._complete_signalled: bool = False

    def load(self, file_path: str) -> None:
        if file_path == self._file_path:
            return
        self._file_path          = file_path
        self.first_exec_line     = 1
        self.end_line            = -1
        self._complete_signalled = False

        if not file_path:
            return
        try:
            first_found = False
            last_end    = -1
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, raw in enumerate(f, start=1):
                    stripped  = raw.strip()
                    if not first_found and not _GCODE_SKIP_RE.match(stripped):
                        self.first_exec_line = lineno
                        first_found = True
                    code_part = re.sub(r"\(.*?\)", "", stripped).strip()
                    if _GCODE_END_RE.match(code_part):
                        last_end = lineno
            self.end_line = last_end
            logger.debug(
                "End-line scan: file=%s  first_exec=%d  end_line=%d",
                os.path.basename(file_path), self.first_exec_line, self.end_line,
            )
            if self.end_line == -1:
                logger.warning(
                    "No M2/M30/%% in '%s' — all cycles will be aborts.",
                    os.path.basename(file_path),
                )
        except OSError as exc:
            logger.error("Cannot read G-code file '%s': %s", file_path, exc)

    def reset_cycle(self) -> None:
        self._complete_signalled = False

    def check_motion_line(
        self, motion_line: int, calculator: CycleTimeCalculator
    ) -> None:
        if (
            not self._complete_signalled
            and self.end_line != -1
            and motion_line >= self.end_line
        ):
            self._complete_signalled = True
            calculator.signal_cycle_complete()

    def is_run_from_here(self, motion_line: int) -> bool:
        return motion_line > (self.first_exec_line + 2)


# ---------------------------------------------------------------------------
# Cycle state machine
# ---------------------------------------------------------------------------
class _CycleStateMachine:
    """Edge-triggered: calls calculator methods exactly ONCE per transition."""

    IDLE    = "IDLE"
    RUNNING = "RUNNING"
    PAUSED  = "PAUSED"

    def __init__(
        self,
        calculator: CycleTimeCalculator,
        detector: _GcodeEndDetector,
    ) -> None:
        self._calc     = calculator
        self._detector = detector
        self._state    = self.IDLE

    def update(self, stat: linuxcnc.stat) -> str:
        is_running  = _is_program_running(stat)
        is_paused   = _is_program_paused(stat)
        is_idle     = not is_running and not is_paused
        motion_line = _safe_get(stat, "motion_line", 0)

        if self._state == self.IDLE:
            if is_running:
                rfh = self._detector.is_run_from_here(motion_line)
                self._detector.reset_cycle()
                self._calc.start_cycle(run_from_here=rfh)
                self._state = self.RUNNING

        elif self._state == self.RUNNING:
            self._detector.check_motion_line(motion_line, self._calc)
            if is_paused:
                self._calc.pause_cycle()
                self._state = self.PAUSED
            elif is_idle:
                self._calc.stop_cycle()
                self._state = self.IDLE

        elif self._state == self.PAUSED:
            if is_running:
                self._calc.resume_cycle()
                self._state = self.RUNNING
            elif is_idle:
                self._calc.abort_cycle()
                self._state = self.IDLE

        return self._state


# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------
# Active work coordinate system name from g5x_index (1=G54 … 9=G59.3).
_WCS_NAMES = {1: "G54", 2: "G55", 3: "G56", 4: "G57", 5: "G58",
              6: "G59", 7: "G59.1", 8: "G59.2", 9: "G59.3"}


def _wcs_name(g5x_index: Any) -> str:
    try:
        return _WCS_NAMES.get(int(g5x_index), "G54")
    except (TypeError, ValueError):
        return "G54"


def _collect_axis_data(stat: linuxcnc.stat) -> Dict[str, Any]:
    axis_data: Dict[str, Any] = {}
    axis_mask = _safe_get(stat, "axis_mask", 0)
    raw_axes  = _safe_get(stat, "axis", [])
    # Machine (absolute) position lives in stat.actual_position (a 9-tuple ordered
    # x,y,z,a,b,c,u,v,w). In LinuxCNC 2.8+ stat.axis[n] holds only velocity and
    # position limits — NOT the position — so reading it there yields 0.0.
    positions = (_safe_get(stat, "actual_position", None)
                 or _safe_get(stat, "position", None) or [])
    # Work (relative) position = machine - g5x_offset - g92_offset - tool_offset.
    # This matches the axis readout LinuxCNC shows in the active WCS (e.g. G54).
    g5x  = _safe_get(stat, "g5x_offset", []) or []
    g92  = _safe_get(stat, "g92_offset", []) or []
    tool = _safe_get(stat, "tool_offset", []) or []

    def _offset(i: int) -> float:
        return ((g5x[i]  if i < len(g5x)  else 0.0)
                + (g92[i]  if i < len(g92)  else 0.0)
                + (tool[i] if i < len(tool) else 0.0))

    for idx, name in enumerate(["x","y","z","a","b","c","u","v","w"]):
        if axis_mask & (1 << idx) and idx < len(raw_axes):
            a = raw_axes[idx]
            pos = positions[idx] if idx < len(positions) else 0.0
            axis_data[name] = {
                "pos":           round(pos, 6),               # machine (absolute)
                "work":          round(pos - _offset(idx), 6),  # active WCS (relative)
                "vel":           round(a.get("velocity", 0.0), 6),
                # Limit keys are camelCase in LinuxCNC 2.8+; keep a snake_case fallback.
                "min_pos_limit": round(a.get("minPositionLimit",
                                             a.get("min_position_limit", 0.0)), 4),
                "max_pos_limit": round(a.get("maxPositionLimit",
                                             a.get("max_position_limit", 0.0)), 4),
            }
    return axis_data


def _collect_joint_data(stat: linuxcnc.stat) -> List[Dict[str, Any]]:
    joints     = []
    num_joints = int(_safe_get(stat, "joints", 0))
    raw_joints = _safe_get(stat, "joint", [])
    for idx in range(num_joints):
        if idx < len(raw_joints):
            j = raw_joints[idx]
            joints.append({
                "id":     idx,
                "pos":    round(j.get("input",          0.0), 6),
                "vel":    round(j.get("velocity",        0.0), 6),
                "homed":  bool(j.get("homed",           False)),
                "fault":  bool(j.get("fault",           False)),
                "ferror": round(j.get("ferror_current", 0.0), 6),
            })
    return joints


def _collect_spindle_data(stat: linuxcnc.stat) -> List[Dict[str, Any]]:
    spindles     = []
    num_spindles = int(_safe_get(stat, "spindles", 1))
    raw_spindles = _safe_get(stat, "spindle", [])
    for idx in range(num_spindles):
        if idx < len(raw_spindles):
            s = raw_spindles[idx]
            spindles.append({
                "id":        idx,
                "speed":     round(s.get("speed",    0.0), 2),
                "direction":       s.get("direction",  0),
                "override":  round(s.get("override", 1.0), 4),
                "at_speed":  bool(s.get("at_speed",  False)),
                "enabled":   bool(s.get("enabled",   False)),
            })
    return spindles


def _collect_file_meta(stat: linuxcnc.stat) -> Dict[str, Any]:
    file_path = _safe_get(stat, "file", "") or ""
    if not file_path:
        return {"file_name": "", "file_size": 0, "file_modified_ms": 0}
    try:
        st = os.stat(file_path)
        return {
            "file_name":        os.path.basename(file_path),
            "file_size":        st.st_size,
            "file_modified_ms": int(st.st_mtime * 1000),
        }
    except OSError:
        return {
            "file_name":        os.path.basename(file_path),
            "file_size":        0,
            "file_modified_ms": 0,
        }


def _collect_motion_data(stat: linuxcnc.stat) -> Dict[str, Any]:
    return {
        "current_vel":    round(_safe_get(stat, "current_vel",    0.0), 6),
        "distance_to_go": round(_safe_get(stat, "distance_to_go", 0.0), 6),
        "motion_type":    _safe_get(stat, "motion_type",    0),
        "motion_line":    _safe_get(stat, "motion_line",    0),
        "current_line":   _safe_get(stat, "current_line",   0),
        "delay_left":     round(_safe_get(stat, "delay_left", 0.0), 3),
        "feedrate":       round(_safe_get(stat, "feedrate",   0.0), 4),
        "rapidrate":      round(_safe_get(stat, "rapidrate",  0.0), 4),
    }


def _collect_machine_status(stat: linuxcnc.stat) -> Dict[str, Any]:
    return {
        "task_state":      _safe_get(stat, "task_state",    0),
        "task_mode":       _safe_get(stat, "task_mode",     0),
        "interp_state":    _safe_get(stat, "interp_state",  0),
        "exec_state":      _safe_get(stat, "exec_state",    0),
        "estop":           bool(_safe_get(stat, "estop",    True)),
        "enabled":         bool(_safe_get(stat, "enabled",  False)),
        "paused":          bool(_safe_get(stat, "paused",   False)),
        "tool_in_spindle": _safe_get(stat, "tool_in_spindle", 0),
        "g5x_index":       _safe_get(stat, "g5x_index",    0),
        "wcs":             _wcs_name(_safe_get(stat, "g5x_index", 1)),
        "g5x_offset":      list(_safe_get(stat, "g5x_offset", [])),
        "g92_offset":      list(_safe_get(stat, "g92_offset", [])),
        "tool_offset":     list(_safe_get(stat, "tool_offset", [])),
        "gcodes":          list(_safe_get(stat, "gcodes",   [])),
        "mcodes":          list(_safe_get(stat, "mcodes",   [])),
        "settings":        list(_safe_get(stat, "settings", [])),
    }


# ---------------------------------------------------------------------------
# G-code file sender
# ---------------------------------------------------------------------------
class _GcodeFileSender:
    """Sends full G-code file on load; re-sends only when file changes."""

    def __init__(self, chunk_size: int, machine_name: str) -> None:
        self._chunk_size:       int   = chunk_size
        self._machine_name:     str   = machine_name
        self._sent_fingerprint: Tuple = ("", 0, 0)

    def check_and_send(self, stat: linuxcnc.stat, sender: "_UdpSender") -> None:
        meta      = _collect_file_meta(stat)
        fp        = (meta["file_name"], meta["file_size"], meta["file_modified_ms"])
        file_path = _safe_get(stat, "file", "") or ""

        if fp == self._sent_fingerprint or not file_path or not meta["file_name"]:
            return

        logger.info("G-code file changed → %s (%d bytes). Sending.",
                    meta["file_name"], meta["file_size"])
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            total_chunks = max(1, (len(content) + self._chunk_size - 1) // self._chunk_size)
            for idx in range(total_chunks):
                chunk  = content[idx * self._chunk_size:(idx + 1) * self._chunk_size]
                packet = json.dumps({
                    "type":             "gcode_file",
                    "proto":            PROTOCOL_VERSION,
                    "ts":               int(time.time_ns() // 1_000_000),
                    "machine_name":     self._machine_name,
                    "file_name":        meta["file_name"],
                    "file_size":        meta["file_size"],
                    "file_modified_ms": meta["file_modified_ms"],
                    "chunk_index":      idx,
                    "total_chunks":     total_chunks,
                    "content":          chunk,
                }).encode("utf-8")
                sender.send(packet)
                logger.debug("Sent gcode chunk %d/%d (%d bytes)",
                             idx + 1, total_chunks, len(packet))
            self._sent_fingerprint = fp

        except OSError as exc:
            logger.error("Cannot read G-code file for sending: %s", exc)


# ---------------------------------------------------------------------------
# UDP sender
# ---------------------------------------------------------------------------
class _UdpSender:
    def __init__(self, ip: str, port: int) -> None:
        self._ip   = ip
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._create_socket()

    def _create_socket(self) -> None:
        try:
            if self._sock:
                self._sock.close()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        except OSError as exc:
            logger.error("Failed to create UDP socket: %s", exc)
            self._sock = None

    def send(self, payload: bytes) -> bool:
        if self._sock is None:
            self._create_socket()
        if self._sock is None:
            return False
        try:
            self._sock.sendto(payload, (self._ip, self._port))
            return True
        except OSError as exc:
            logger.warning("UDP send failed: %s", exc)
            self._create_socket()
            return False

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# Argument parsing / dev mode
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CNC Status Monitor — broadcasts LinuxCNC state via UDP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "DEV MODE can also be enabled without --dev by setting:\n"
            "  CNC_DEV_MODE=1\n\n"
            "Examples:\n"
            "  python3 status.py --dev\n"
            "  CNC_DEV_MODE=1 bash launch_ofc.sh"
        ),
    )
    parser.add_argument(
        "--dev", action="store_true", default=False,
        help="Enable verbose DEBUG logging to console and file.",
    )
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="Path to config.yaml (default: config.yaml next to this script).",
    )
    return parser.parse_args()


def _resolve_dev_mode(args: argparse.Namespace) -> bool:
    env = os.environ.get("CNC_DEV_MODE", "").strip().lower()
    return args.dev or env in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global _shutdown_requested

    args     = _parse_args()
    dev_mode = _resolve_dev_mode(args)

    if dev_mode:
        src = "--dev flag" if args.dev else "CNC_DEV_MODE env var"
        print(f"[DEV MODE ACTIVE — enabled via {src}]", flush=True)

    cfg = load_config(args.config)
    _configure_logging(dev_mode, cfg)

    poll_interval_s           = cfg["poll_interval_s"]
    idle_heartbeat_interval_s = cfg["idle_heartbeat_interval_s"]
    machine_name              = cfg["machine_name"]

    logger.info(
        "CNC Status Monitor v1.3.0 starting. dev_mode=%s machine=%r target=%s:%d",
        dev_mode, machine_name, cfg["monitor_pc_ip"], cfg["monitor_pc_port"],
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    calculator    = CycleTimeCalculator(dev_mode=dev_mode)
    detector      = _GcodeEndDetector()
    state_machine = _CycleStateMachine(calculator, detector)
    sender        = _UdpSender(cfg["monitor_pc_ip"], cfg["monitor_pc_port"])
    gcode_sender  = _GcodeFileSender(cfg["gcode_chunk_size"], machine_name)

    stat_channel:  Optional[linuxcnc.stat]          = None
    error_channel: Optional[linuxcnc.error_channel] = None

    def _connect_linuxcnc() -> bool:
        nonlocal stat_channel, error_channel
        try:
            stat_channel  = linuxcnc.stat()
            error_channel = linuxcnc.error_channel()
            logger.info("Connected to LinuxCNC channels.")
            return True
        except Exception as exc:
            logger.error("Cannot connect to LinuxCNC: %s. Retrying in 5 s.", exc)
            stat_channel  = None
            error_channel = None
            return False

    last_poll_time:          float = 0.0
    last_idle_heartbeat:     float = 0.0
    pending_nml_errors:      list  = []
    consecutive_poll_errors: int   = 0
    MAX_CONSECUTIVE_ERRORS:  int   = 10

    prev_cycle_state: str  = ""
    idle_packet_sent: bool = False

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    while not _shutdown_requested:
        now = time.monotonic()

        if now - last_poll_time < poll_interval_s:
            time.sleep(0.05)
            continue
        last_poll_time = now

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
        except Exception as exc:
            consecutive_poll_errors += 1
            logger.error("Poll error (%d/%d): %s",
                         consecutive_poll_errors, MAX_CONSECUTIVE_ERRORS, exc)
            if consecutive_poll_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.critical("Too many poll errors — reconnecting.")
                stat_channel  = None
                error_channel = None
                consecutive_poll_errors = 0
            time.sleep(1.0)
            continue

        # ------------------------------------------------------------------
        # Load G-code file into end-line detector (no-op if unchanged)
        # ------------------------------------------------------------------
        try:
            file_path = _safe_get(stat_channel, "file", "") or ""
            detector.load(file_path)
        except Exception as exc:
            logger.debug("Detector load error: %s", exc)

        # ------------------------------------------------------------------
        # Drive cycle state machine
        # ------------------------------------------------------------------
        try:
            current_cycle_state = state_machine.update(stat_channel)
        except Exception as exc:
            logger.error("State machine error: %s", exc)
            current_cycle_state = "UNKNOWN"

        # ------------------------------------------------------------------
        # Passively drain NML errors — AXIS keeps priority on the queue.
        # Any errors captured here are a bonus; nml_errors=[] is normal.
        # Use exec_state==1 in the packet for a reliable error indicator.
        # ------------------------------------------------------------------
        if error_channel is not None:
            try:
                pending_nml_errors.extend(_drain_nml_errors(error_channel))
            except Exception as exc:
                logger.debug("NML drain outer error: %s", exc)

        # ------------------------------------------------------------------
        # Send G-code file if new/changed
        # ------------------------------------------------------------------
        try:
            gcode_sender.check_and_send(stat_channel, sender)
        except Exception as exc:
            logger.debug("G-code file send error: %s", exc)

        # ------------------------------------------------------------------
        # Idle suppression
        # ------------------------------------------------------------------
        state_changed = current_cycle_state != prev_cycle_state

        if current_cycle_state == "IDLE":
            if state_changed:
                idle_packet_sent    = False
                last_idle_heartbeat = now
            if idle_packet_sent and (now - last_idle_heartbeat) < idle_heartbeat_interval_s:
                prev_cycle_state = current_cycle_state
                pending_nml_errors.clear()
                logger.debug("IDLE — packet suppressed.")
                continue
        else:
            idle_packet_sent = False

        prev_cycle_state = current_cycle_state

        # ------------------------------------------------------------------
        # Build payload
        # ------------------------------------------------------------------
        try:
            snap: CycleSnapshot = calculator.snapshot()

            payload: Dict[str, Any] = {
                "type":         "status",
                "proto":        PROTOCOL_VERSION,
                "ts":           int(time.time_ns() // 1_000_000),
                "machine_name": machine_name,

                # Cycle & production
                "cycle_state":              current_cycle_state,
                "cycle_time_ms":            snap.current_cycle_ms,
                "parts_produced":           snap.parts_produced,
                "abort_count":              snap.abort_count,
                "run_from_here_count":      snap.run_from_here_count,
                "last_cycle_ms":            snap.last_completed_ms,
                "last_abort_ms":            snap.last_aborted_ms,
                "avg_cycle_ms":             snap.average_cycle_ms,
                "total_completed_cycles":   snap.total_completed_cycles,
                "cycle_complete_signalled": snap.cycle_complete_signalled,
                "is_run_from_here":         snap.is_run_from_here,

                # End-line info
                "gcode_end_line":           detector.end_line,
                "gcode_first_exec_line":    detector.first_exec_line,

                # Machine
                **_collect_machine_status(stat_channel),

                # Motion
                **_collect_motion_data(stat_channel),

                # Axes / joints / spindles
                "axis":     _collect_axis_data(stat_channel),
                "joints":   _collect_joint_data(stat_channel),
                "spindles": _collect_spindle_data(stat_channel),

                # File
                **_collect_file_meta(stat_channel),

                # NML errors — best-effort only; [] is normal and expected.
                # AXIS displays errors to the operator; we only catch extras.
                # For reliable error detection use: exec_state == 1 (EXEC_ERROR)
                "nml_errors": pending_nml_errors.copy(),
            }
            pending_nml_errors.clear()

        except Exception as exc:
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
            logger.warning("Payload %d bytes near UDP limit.", len(json_bytes))

        sent = sender.send(json_bytes)

        if current_cycle_state == "IDLE":
            idle_packet_sent    = True
            last_idle_heartbeat = now

        logger.debug(
            "Packet %s (%d bytes) | state=%s parts=%d aborts=%d",
            "SENT" if sent else "FAILED", len(json_bytes),
            current_cycle_state, snap.parts_produced, snap.abort_count,
        )

    # -----------------------------------------------------------------------
    # Clean shutdown
    # -----------------------------------------------------------------------
    logger.info("Shutting down.")
    snap = calculator.snapshot()
    if snap.is_running:
        logger.warning("Shutdown with active cycle — recording as abort.")
        calculator.abort_cycle()
    sender.close()
    logger.info("CNC Status Monitor stopped cleanly.")


if __name__ == "__main__":
    main()
