#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/udp_receiver.py  —  v1.1.0
=====================================
UDP receiver for the LinuxCNC Status Monitor.

Run this on your MONITORING PC (not the CNC machine) to receive and display
the JSON status packets broadcast by status.py.

Handles two packet types:
  "status"     — regular machine status (default)
  "gcode_file" — G-code file chunks sent on load/change

Usage
-----
    python3 udp_receiver.py                              # compact summary
    python3 udp_receiver.py --pretty                     # pretty JSON + log file
    python3 udp_receiver.py --pretty --log /tmp/cnc.log  # custom log path
    python3 udp_receiver.py --fields axis spindles cycle_time_ms
    python3 udp_receiver.py --port 5005                  # custom port
    python3 udp_receiver.py --save-gcode ./gcode_files/  # save received G-code files

When --pretty is used:
  - Full JSON of every packet is written to a rotating log file
  - Default log path: /tmp/udp_receiver_pretty.log
  - Rotates at 10 MB, keeps 3 backups
"""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
from datetime import datetime
from typing import Dict, Any, Optional

DEFAULT_PORT       = 5005
BUFFER_SIZE        = 65536
DEFAULT_PRETTY_LOG = "/tmp/udp_receiver_pretty.log"
PRETTY_LOG_MAX_BYTES   = 10 * 1024 * 1024   # 10 MB
PRETTY_LOG_BACKUP_COUNT = 3


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def _setup_pretty_logger(log_path: str) -> logging.Logger:
    """Set up a dedicated rotating logger for pretty-print output."""
    log = logging.getLogger("pretty_log")
    log.setLevel(logging.DEBUG)
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=PRETTY_LOG_MAX_BYTES,
            backupCount=PRETTY_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(fh)
        print(f"[Pretty log] Writing to: {log_path}")
    except OSError as exc:
        print(f"[WARNING] Cannot open pretty log file {log_path}: {exc}",
              file=sys.stderr)
    return log


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _ms_to_mmss(ms: int) -> str:
    if ms is None:
        return "--:--"
    minutes = ms // 60_000
    seconds = (ms % 60_000) // 1_000
    millis  = ms % 1_000
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _print_summary(data: Dict[str, Any], addr: tuple) -> None:
    """Compact single-line summary of a status packet."""
    ts       = data.get("ts", 0)
    dt       = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
    state    = data.get("cycle_state", "?")
    cycle_ms = data.get("cycle_time_ms", 0)
    parts    = data.get("parts_produced", 0)
    aborts   = data.get("abort_count", 0)
    rfh      = data.get("run_from_here_count", 0)
    estop    = "ESTOP" if data.get("estop") else "OK   "
    enabled  = "ON " if data.get("enabled") else "OFF"

    axes = data.get("axis", {})
    axis_str = "  ".join(
        f"{n.upper()}={v['pos']:+.3f}" for n, v in axes.items()
    ) or "no axis data"

    spindles  = data.get("spindles", [])
    rpm_str   = "  ".join(
        f"S{s['id']}:{s['speed']:.0f}rpm" for s in spindles
    ) or "no spindle"

    rfh_str   = f"  RFH:{rfh}" if rfh else ""
    errors    = data.get("nml_errors", [])
    err_str   = f"  ⚠ {len(errors)} err" if errors else ""

    print(
        f"[{dt}] {state:<8} {enabled} {estop} "
        f"| T:{_ms_to_mmss(cycle_ms)} "
        f"| P:{parts:>4} A:{aborts}{rfh_str} "
        f"| {axis_str} | {rpm_str}{err_str}"
    )
    for e in errors:
        print(f"          !! [{e.get('kind')}] {e.get('msg')}")


def _print_gcode_summary(data: Dict[str, Any], gcode_buffers: dict) -> None:
    """Track and display incoming G-code file chunks."""
    name    = data.get("file_name", "?")
    total   = data.get("total_chunks", 1)
    idx     = data.get("chunk_index", 0)
    content = data.get("content", "")
    size    = data.get("file_size", 0)

    if name not in gcode_buffers:
        gcode_buffers[name] = {"chunks": {}, "total": total, "size": size}
        print(f"\n[G-CODE FILE] Receiving: {name} ({size} bytes, {total} chunk(s))")

    gcode_buffers[name]["chunks"][idx] = content

    received = len(gcode_buffers[name]["chunks"])
    print(f"  Chunk {idx + 1}/{total} received ({received}/{total} total)")

    if received == total:
        # Reassemble
        full = "".join(
            gcode_buffers[name]["chunks"][i] for i in range(total)
        )
        lines = full.count("\n")
        print(f"  ✓ {name} complete — {lines} lines, {len(full)} chars")
        del gcode_buffers[name]


# ---------------------------------------------------------------------------
# G-code file saver
# ---------------------------------------------------------------------------
def _save_gcode(data: Dict[str, Any], gcode_buffers: dict, save_dir: str) -> None:
    """Save received G-code chunks to disk when complete."""
    name    = data.get("file_name", "unknown.ngc")
    total   = data.get("total_chunks", 1)
    idx     = data.get("chunk_index", 0)
    content = data.get("content", "")

    key = f"save_{name}"
    if key not in gcode_buffers:
        gcode_buffers[key] = {"chunks": {}, "total": total}

    gcode_buffers[key]["chunks"][idx] = content

    if len(gcode_buffers[key]["chunks"]) == total:
        full     = "".join(gcode_buffers[key]["chunks"][i] for i in range(total))
        out_path = os.path.join(save_dir, name)
        try:
            os.makedirs(save_dir, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(full)
            print(f"  [SAVED] G-code written to: {out_path}")
        except OSError as exc:
            print(f"  [ERROR] Cannot save G-code: {exc}", file=sys.stderr)
        del gcode_buffers[key]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LinuxCNC UDP status receiver — run on the monitoring PC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--port",   type=int, default=DEFAULT_PORT,
                   help=f"UDP port to listen on (default: {DEFAULT_PORT})")
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print full JSON and write to rotating log file")
    p.add_argument("--log",    default=DEFAULT_PRETTY_LOG,
                   help=f"Log file for --pretty mode (default: {DEFAULT_PRETTY_LOG})")
    p.add_argument("--fields", nargs="+", metavar="FIELD",
                   help="Print only these specific top-level fields per packet")
    p.add_argument("--save-gcode", metavar="DIR", default=None,
                   help="Directory to save received G-code files to disk")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = _parse_args()

    pretty_logger: Optional[logging.Logger] = None
    if args.pretty:
        pretty_logger = _setup_pretty_logger(args.log)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", args.port))
    except OSError as exc:
        print(f"[ERROR] Cannot bind UDP port {args.port}: {exc}", file=sys.stderr)
        print(f"  Try: sudo ufw allow {args.port}/udp", file=sys.stderr)
        sys.exit(1)

    print(f"Listening on UDP port {args.port}...")
    if args.pretty:
        print(f"  Pretty log → {args.log}  "
              f"(rotates at {PRETTY_LOG_MAX_BYTES // 1_048_576} MB)")
    if args.save_gcode:
        print(f"  G-code files → {args.save_gcode}")
    print("Press Ctrl+C to stop.\n")

    packet_count = 0
    gcode_buffers: dict = {}

    try:
        while True:
            raw, addr = sock.recvfrom(BUFFER_SIZE)
            packet_count += 1

            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(f"[WARN] Bad packet #{packet_count} from {addr}: {exc}")
                continue

            ptype = data.get("type", "status")

            # ── Pretty mode ──────────────────────────────────────────────
            if args.pretty:
                ts  = data.get("ts", 0)
                dt  = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                sep = "═" * 70
                entry = (
                    f"\n{sep}\n"
                    f"Packet #{packet_count}  from {addr[0]}:{addr[1]}  at {dt}\n"
                    f"{sep}\n"
                    f"{json.dumps(data, indent=2)}\n"
                )
                print(entry)
                if pretty_logger:
                    pretty_logger.info(entry)

            # ── G-code file packet ────────────────────────────────────────
            elif ptype == "gcode_file":
                _print_gcode_summary(data, gcode_buffers)
                if args.save_gcode:
                    _save_gcode(data, gcode_buffers, args.save_gcode)

            # ── Field-filter mode ─────────────────────────────────────────
            elif args.fields:
                ts  = data.get("ts", 0)
                dt  = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
                print(f"\n[{dt}] Packet #{packet_count}:")
                for field in args.fields:
                    print(f"  {field}: {data.get(field, '<not found>')}")

            # ── Default compact summary ───────────────────────────────────
            else:
                if ptype == "gcode_file":
                    _print_gcode_summary(data, gcode_buffers)
                else:
                    _print_summary(data, addr)

    except KeyboardInterrupt:
        print(f"\nStopped after {packet_count} packets.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
