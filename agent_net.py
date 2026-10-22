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

``monitor_interface`` (e.g. "enp2s0") sends only on that interface: the socket
is bound to the interface's IPv4 address, "auto" becomes that interface's
broadcast address only, and a fixed IP or host name is used only if the route
to it leaves through that interface (``ip route get <ip> from <address>``).
Binding the source address alone would not be enough: Linux may still route a
packet out of another card. SO_BINDTODEVICE would need root. The interface is
refused (nothing is sent) if it has a Mesa board on it or is listed in
``exclude_interfaces``; if it is missing, down or has no IPv4 address, nothing
is sent until it comes back (checked every 30 s).

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

from agent_auth import sign

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


def interface_ipv4(ip_addr_json: list, name: str) -> Optional[Tuple[str, Optional[str]]]:
    """(address, broadcast) of interface `name` if it is up and has an IPv4
    address; broadcast is None on a point-to-point (/31, /32) address."""
    for iface in ip_addr_json or []:
        if iface.get("ifname") != name or "UP" not in set(iface.get("flags") or []):
            continue
        for a in iface.get("addr_info") or []:
            if a.get("family") != "inet" or not a.get("local"):
                continue
            try:
                prefix = int(a.get("prefixlen", 32))
                bcast = None
                if prefix < 31:
                    bcast = a.get("broadcast") or str(ipaddress.IPv4Network(
                        f"{a['local']}/{prefix}", strict=False).broadcast_address)
                return str(ipaddress.IPv4Address(a["local"])), bcast
            except ValueError:
                continue
    return None


def _ip_json(args: List[str]) -> list:
    res = subprocess.run(["ip", "-j"] + args, capture_output=True, text=True, timeout=3)
    return json.loads(res.stdout or "[]")


def route_device(dest: str, source: str) -> Optional[str]:
    """The interface the kernel would send `dest` out of, from `source`."""
    try:
        routes = _ip_json(["route", "get", dest, "from", source])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return routes[0].get("dev") if routes and isinstance(routes[0], dict) else None


class InterfacePlan:
    """What monitor_interface allows right now."""

    def __init__(self, interface: str, address: Optional[str] = None,
                 broadcast: Optional[str] = None, problem: str = "") -> None:
        self.interface, self.address, self.broadcast = interface, address, broadcast
        self.problem = problem              # "" = usable

    def __eq__(self, other) -> bool:
        return isinstance(other, InterfacePlan) and vars(self) == vars(other)

    def describe(self) -> str:
        if self.problem:
            return f"{self.interface} — {self.problem}; nothing is sent"
        return (f"{self.interface}, address {self.address}, broadcast "
                f"{self.broadcast or '(none: point-to-point)'}")


def plan_interface(interface: str, exclude: Iterable[str]) -> InterfacePlan:
    if interface in set(exclude):
        return InterfacePlan(interface, problem="listed in exclude_interfaces")
    if interface in mesa_interfaces(_read("/proc/net/arp")):
        return InterfacePlan(interface, problem="a Mesa board is on it (control network)")
    try:
        data = _ip_json(["-4", "addr", "show", "dev", interface])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return InterfacePlan(interface, problem=f"cannot list interfaces ({exc})")
    found = interface_ipv4(data, interface)
    if found is None:
        return InterfacePlan(interface, problem="missing, down or without an IPv4 address")
    return InterfacePlan(interface, found[0], found[1])


def parse_targets(setting: str) -> List[str]:
    items = [s.strip() for s in str(setting or "").split(",")]
    return [s for s in items if s] or ["auto"]


def resolve_targets(setting: str, exclude: Iterable[str],
                    plan: Optional[InterfacePlan] = None) -> List[str]:
    """Turn the monitor_pc_ip setting into a list of destination IPs. With an
    interface plan, only destinations that leave through that interface."""
    if plan is not None and plan.problem:
        return []
    out: List[str] = []
    for item in parse_targets(setting):
        if item.lower() in AUTO_WORDS:
            if plan is None:
                out.extend(discover_broadcast_targets(exclude))
            elif plan.broadcast:
                out.append(plan.broadcast)
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
    out = list(dict.fromkeys(out))          # keep order, drop duplicates
    if plan is None:
        return out
    kept = []
    for ip in out:
        if ip == plan.broadcast or route_device(ip, plan.address) == plan.interface:
            kept.append(ip)
        else:
            logger.warning("Not sending to %s: it is not reached through "
                           "monitor_interface %s.", ip, plan.interface)
    return kept


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------
class UdpSender:
    """Sends each payload to every current target. Never blocks on DNS."""

    def __init__(self, setting: str, port: int, exclude_interfaces: str = "",
                 key: Optional[bytes] = None, interface: str = "") -> None:
        self._setting = setting
        self._interface = str(interface or "").strip()
        self._plan: Optional[InterfacePlan] = None
        self._source: Optional[str] = None   # bound address (monitor_interface)
        self._sock_lock = threading.Lock()
        self._key = key                      # site key: every packet is signed
        self._port = int(port)
        self._exclude = [s.strip() for s in str(exclude_interfaces or "").split(",") if s.strip()]
        self._targets: List[str] = []
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self.would_block = 0                 # packets dropped because the buffer was full
        self._create_socket()
        self.refresh()                       # first resolution inline
        self._thread = threading.Thread(target=self._refresh_loop,
                                        name="target-refresh", daemon=True)
        self._thread.start()

    @property
    def targets(self) -> List[str]:
        return list(self._targets)

    @property
    def interface_plan(self) -> Optional[InterfacePlan]:
        """None when monitor_interface is not set."""
        return self._plan

    def refresh(self) -> None:
        try:
            plan = plan_interface(self._interface, self._exclude) if self._interface else None
            new = resolve_targets(self._setting, self._exclude, plan)
        except Exception as exc:   # never let discovery kill the agent
            logger.warning("Target refresh failed: %s", exc)
            return
        if plan is not None:
            if plan != self._plan:
                (logger.warning if plan.problem else logger.info)(
                    "monitor_interface %s", plan.describe())
            self._plan = plan
            if plan.address != self._source:
                self._source = plan.address
                self._create_socket()        # bind to the new address (or none)
            if new != self._targets:
                logger.info("Sending status to %s (port %d) via %s",
                            ", ".join(new) or "nothing", self._port, self._interface)
            self._targets = new              # may be empty: send nothing
            return
        if new and new != self._targets:
            logger.info("Sending status to %s (port %d)", ", ".join(new), self._port)
        if new:
            self._targets = new

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_S):
            self.refresh()

    def _create_socket(self) -> None:
        with self._sock_lock:
            old, self._sock = self._sock, None
            if old:
                old.close()
            if self._interface and not self._source:
                return                       # interface unusable: no socket, nothing sent
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if self._source:
                    sock.bind((self._source, 0))
                # Never wait in the sampling loop: a full send buffer drops the packet.
                sock.setblocking(False)
                self._sock = sock
            except OSError as exc:
                logger.error("Failed to create UDP socket%s: %s",
                             f" on {self._source}" if self._source else "", exc)
                if sock:
                    sock.close()

    def send(self, payload: bytes) -> bool:
        payload = sign(payload, self._key)
        if self._sock is None and not (self._interface and not self._source):
            self._create_socket()
        sock, targets = self._sock, self._targets
        if sock is None or not targets:
            return False
        ok = busy = False
        for ip in targets:
            try:
                sock.sendto(payload, (ip, self._port))
                ok = True
            except BlockingIOError:
                busy = True                  # send buffer full: this packet is dropped
                self.would_block += 1
            except OSError as exc:
                logger.warning("UDP send to %s failed: %s", ip, exc)
        if not ok and not busy:
            self._create_socket()
        return ok

    def close(self) -> None:
        self._stop.set()
        with self._sock_lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
