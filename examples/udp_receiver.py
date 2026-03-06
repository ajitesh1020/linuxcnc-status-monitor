#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/udp_receiver.py
========================
Example UDP receiver for the LinuxCNC Status Monitor.

Run this on your MONITORING PC (not the CNC machine) to receive
and display the JSON status packets broadcast by status.py.

Usage:
    python3 udp_receiver.py                  # listen on default port 5005
    python3 udp_receiver.py --port 5005      # specify port explicitly
    python3 udp_receiver.py --pretty         # pretty-print JSON
    python3 udp_receiver.py --fields axis spindles cycle_time_ms
"""

import argparse
import json
import socket
import sys
from datetime import datetime

DEFAULT_PORT = 5005
BUFFER_SIZE  = 65536   # large enough for any UDP packet from status.py


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Receive and display LinuxCNC status packets over UDP."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"UDP port to listen on (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print the full JSON packet"
    )
    parser.add_argument(
        "--fields", nargs="+", metavar="FIELD",
        help="Print only these specific fields from each packet"
    )
    return parser.parse_args()


def format_cycle_time(ms: int) -> str:
    """Convert milliseconds to MM:SS.mmm string."""
    minutes = ms // 60_000
    seconds = (ms % 60_000) // 1_000
    millis  = ms % 1_000
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def print_summary(data: dict) -> None:
    """Print a compact one-line summary of the packet."""
    ts        = data.get("ts", 0)
    dt        = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
    state     = data.get("cycle_state", "?")
    cycle_ms  = data.get("cycle_time_ms", 0)
    parts     = data.get("parts_produced", 0)
    aborts    = data.get("abort_count", 0)
    estop     = "ESTOP" if data.get("estop") else "OK"
    enabled   = "ON" if data.get("enabled") else "OFF"

    # Axis positions
    axes = data.get("axis", {})
    axis_str = "  ".join(
        f"{name.upper()}={info['pos']:+.3f}"
        for name, info in axes.items()
    )

    # Spindle RPM
    spindles = data.get("spindles", [])
    rpm_str = " ".join(
        f"S{s['id']}:{s['speed']:.0f}rpm" for s in spindles
    ) if spindles else "no spindle"

    # NML errors
    errors = data.get("nml_errors", [])
    error_str = f"  ⚠ {len(errors)} error(s)" if errors else ""

    print(
        f"[{dt}] {state:<8} {enabled:<4} {estop:<6} "
        f"| Cycle: {format_cycle_time(cycle_ms)} "
        f"| Parts: {parts:>4}  Aborts: {aborts} "
        f"| {axis_str} "
        f"| {rpm_str}"
        f"{error_str}"
    )

    # Print NML errors if any
    for err in errors:
        print(f"          !! LinuxCNC error [{err.get('kind')}]: {err.get('msg')}")


def main() -> None:
    args = parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", args.port))
    except OSError as e:
        print(f"[ERROR] Cannot bind to UDP port {args.port}: {e}", file=sys.stderr)
        print("  Try: sudo ufw allow {}/udp".format(args.port), file=sys.stderr)
        sys.exit(1)

    print(f"Listening for LinuxCNC status packets on UDP port {args.port}...")
    print("Press Ctrl+C to stop.\n")

    packet_count = 0
    try:
        while True:
            raw, addr = sock.recvfrom(BUFFER_SIZE)
            packet_count += 1

            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"[WARN] Bad packet from {addr}: {e}")
                continue

            if args.pretty:
                print(f"\n{'─' * 60}")
                print(f"Packet #{packet_count} from {addr[0]}:{addr[1]}")
                print(json.dumps(data, indent=2))

            elif args.fields:
                print(f"\nPacket #{packet_count}:")
                for field in args.fields:
                    value = data.get(field, "<not found>")
                    print(f"  {field}: {value}")

            else:
                print_summary(data)

    except KeyboardInterrupt:
        print(f"\nStopped. Received {packet_count} packets.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
