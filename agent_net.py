#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_net.py  —  where status packets are sent
===============================================
``monitor_pc_ip`` in config.yaml accepts a comma-separated list of:

  auto            LAN broadcast (default). Every dashboard on the same network
                  receives the packets whatever its IP is — no static IP and no
                  reconfiguring when the monitoring PC's address changes.
  192.168.0.50    a fixed IP (unicast), as before.
  office-pc.local a host name, re-resolved every 30 s (DNS or mDNS).

Broadcast safety: the Mesa hm2_eth link must carry nothing but real-time
traffic, so any interface with a Mesa board on it (MAC prefix 00:60:1b in the
ARP table — hm2_eth adds that entry) is skipped automatically, as are
loopback, point-to-point links and anything in ``exclude_interfaces``.

Targets are refreshed on a background thread so a slow DNS lookup can never
stall the sampling loop.
"""

import ipaddress
import json
import logging
import socket
import subprocess
import threading
from typing import Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

MESA_OUI = "00:60:1b"            # Mesa Electronics MAC prefix
LIMITED_BROADCAST = "255.255.255.255"
REFRESH_S = 30.0
AUTO_WORDS = {"", "auto", "broadcast"}


# ---------------------------------------------------------------------------
# Interface discovery (Linux: `ip -j addr`, /proc/net/arp)
# ---------------------------------------------------------------------------
def mesa_interfaces(arp_text: str) -> Set[str]:
    """Interfaces that have a Mesa board as an ARP neighbour."""
    found: Set[str] = set()
    for line in arp_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[3].lower().startswith(MESA_OUI):
            found.add(parts[5])
    return found


def broadcast_addresses(ip_addr_json: list, exclude: Iterable[str]) -> List[Tuple[str, str]]:
    """[(interface, broadcast address)] for usable IPv4 LAN interfaces."""
    skip = set(exclude)
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for iface in ip_addr_json or []:
        name = iface.get("ifname", "")
        flags = set(iface.get("flags") or [])
        if (not name or name == "lo" or name in skip or "LOOPBACK" in flags
                or "BROADCAST" not in flags or "UP" not in flags):
            continue
        for a in iface.get("addr_info") or []:
            if a.get("family") != "inet":
                continue
            try:
                prefix = int(a.get("prefixlen", 32))
                if prefix >= 31:
                    continue
                bcast = a.get("broadcast") or str(ipaddress.IPv4Network(
                    f"{a['local']}/{prefix}", strict=False).broadcast_address)
            except (KeyError, ValueError):
                continue
            if bcast not in seen:
                seen.add(bcast)
                out.append((name, bcast))
    return out


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def discover_broadcast_targets(exclude: Iterable[str]) -> List[str]:
    skip = set(exclude) | mesa_interfaces(_read("/proc/net/arp"))
    try:
        res = subprocess.run(["ip", "-j", "-4", "addr", "show", "up"],
                             capture_output=True, text=True, timeout=3)
        data = json.loads(res.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("Cannot list interfaces (%s) — using %s.", exc, LIMITED_BROADCAST)
        return [LIMITED_BROADCAST]
    pairs = broadcast_addresses(data, skip)
    if not pairs:
        return [LIMITED_BROADCAST]
    logger.debug("Broadcast targets: %s (skipped: %s)",
                 ", ".join(f"{b} via {n}" for n, b in pairs), ", ".join(sorted(skip)) or "-")
    return [b for _, b in pairs]


def parse_targets(setting: str) -> List[str]:
    items = [s.strip() for s in str(setting or "").split(",")]
    return [s for s in items if s] or ["auto"]


def resolve_targets(setting: str, exclude: Iterable[str]) -> List[str]:
    """Turn the monitor_pc_ip setting into a list of destination IPs."""
    out: List[str] = []
    for item in parse_targets(setting):
        if item.lower() in AUTO_WORDS:
            out.extend(discover_broadcast_targets(exclude))
            continue
        try:
            out.append(str(ipaddress.IPv4Address(item)))
            continue
        except ValueError:
            pass
        try:
            out.append(socket.gethostbyname(item))
        except OSError as exc:
            logger.warning("Cannot resolve monitor host %r: %s", item, exc)
    # keep order, drop duplicates
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------
class UdpSender:
    """Sends each payload to every current target. Never blocks on DNS."""

    def __init__(self, setting: str, port: int, exclude_interfaces: str = "") -> None:
        self._setting = setting
        self._port = int(port)
        self._exclude = [s.strip() for s in str(exclude_interfaces or "").split(",") if s.strip()]
        self._targets: List[str] = []
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._create_socket()
        self.refresh()                       # first resolution inline
        self._thread = threading.Thread(target=self._refresh_loop,
                                        name="target-refresh", daemon=True)
        self._thread.start()

    @property
    def targets(self) -> List[str]:
        return list(self._targets)

    def refresh(self) -> None:
        try:
            new = resolve_targets(self._setting, self._exclude)
        except Exception as exc:   # never let discovery kill the agent
            logger.warning("Target refresh failed: %s", exc)
            return
        if new and new != self._targets:
            logger.info("Sending status to %s (port %d)", ", ".join(new), self._port)
        if new:
            self._targets = new

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_S):
            self.refresh()

    def _create_socket(self) -> None:
        try:
            if self._sock:
                self._sock.close()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as exc:
            logger.error("Failed to create UDP socket: %s", exc)
            self._sock = None

    def send(self, payload: bytes) -> bool:
        if self._sock is None:
            self._create_socket()
        if self._sock is None:
            return False
        ok = False
        for ip in self._targets:
            try:
                self._sock.sendto(payload, (ip, self._port))
                ok = True
            except OSError as exc:
                logger.warning("UDP send to %s failed: %s", ip, exc)
        if not ok:
            self._create_socket()
        return ok

    def close(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
