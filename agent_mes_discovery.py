#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_mes_discovery.py  —  finding the site service at a new address (agent package K)
======================================================================================
Interface: FloorWatch docs/MES_SERVICE_DISCOVERY.md. The site service
announces itself on UDP 5007 every 5 s and answers probes:

  service -> agent  {"type": "fw.service_beacon", "v": 1, "site_id", "port",
                     "ca_sha256", "service_version", "ts_ms"}
  agent -> service  {"type": "fw.service_probe", "v": 1, "site_id"}

The message carries no address: the address to try is the datagram's SOURCE.
A message counts only when its site_id is this machine's site and its
ca_sha256 is the SHA-256 of the pinned mes/ca.crt; everything else is ignored
silently. The forwarder then tries the address with its normal TLS check
(pinned CA, certificate naming that address), so a fake message can only make
it try a host that refuses it.

Finder.look() runs on the forwarder's thread (never the sampling loop): it
sends one probe per interface the agent broadcasts on (monitor_interface
respected, Mesa interfaces skipped, like the status packets), to that
interface's broadcast address from its own address, and collects answers for
up to 3 s. Meanwhile it also listens on UDP 5007 for announcements
(SO_REUSEADDR / SO_REUSEPORT, so several agents on one PC can listen). The
listening socket is open only while looking: a site service on the same PC
binds 5007 without sharing, and must never find it taken.
"""

import ipaddress
import json
import logging
import select
import socket
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import agent_net

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 5007
LOOK_S = 3.0                  # collect answers this long at most...
SETTLE_S = 0.3                # ...or this long after the first one
MAX_MESSAGE = 512
BEACON, PROBE = "fw.service_beacon", "fw.service_probe"

# (source address to bind, "" = any; destination) for each probe
Targets = Callable[[], List[Tuple[str, str]]]
Found = List[Tuple[str, int, str]]          # [(ip, https port, "probe" | "announcement")]


def probe_message(site_id: str) -> bytes:
    return json.dumps({"type": PROBE, "v": 1, "site_id": site_id}, separators=(",", ":")).encode("utf-8")


def accept(data: bytes, site_id: str, ca_sha256: str) -> Optional[int]:
    """The HTTPS port of a beacon for this site and CA, else None (never raises)."""
    if len(data) > MAX_MESSAGE:
        return None
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != BEACON or msg.get("v") != 1:
        return None
    if msg.get("site_id") != site_id or not isinstance(msg.get("ca_sha256"), str) \
            or msg["ca_sha256"].lower() != ca_sha256.lower():
        return None
    port = msg.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    return port


def _usable_source(ip: str) -> bool:
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (a.is_unspecified or a.is_multicast or a.is_link_local or ip == agent_net.LIMITED_BROADCAST)


def lan_targets(interface: str = "", exclude: Iterable[str] = ()) -> Targets:
    """Probe targets like the status packets: with monitor_interface only that
    interface (nothing while it is unusable), else every broadcast interface
    except Mesa / excluded ones; 255.255.255.255 when none can be listed."""
    interface = str(interface or "").strip()
    skip = [s.strip() for s in (exclude.split(",") if isinstance(exclude, str) else exclude) if s.strip()]

    def targets() -> List[Tuple[str, str]]:
        if interface:
            plan = agent_net.plan_interface(interface, skip)
            if plan.problem or not plan.broadcast:
                return []
            return [(plan.address or "", plan.broadcast)]
        found = [(local, bcast) for _, local, bcast in agent_net.list_broadcast_sources(skip)]
        return list(dict.fromkeys(found)) or [("", agent_net.LIMITED_BROADCAST)]
    return targets


class Finder:
    """Callable as finder(site_id, ca_sha256) -> [(ip, port, via)], best first."""

    def __init__(self, port: int = DISCOVERY_PORT, targets: Optional[Targets] = None,
                 look_s: float = LOOK_S, listen: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.port, self.look_s, self.listen, self.clock = int(port), look_s, listen, clock
        self.targets = targets or lan_targets()
        self._listen_logged = False

    def _listener(self) -> Optional[socket.socket]:
        if not self.listen:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", self.port))
            return s
        except OSError as exc:            # e.g. a site service on this PC holds it: probes only
            s.close()
            if not self._listen_logged:
                logger.debug("MES discovery: cannot listen on UDP %d (%s); using probes only",
                             self.port, exc)
                self._listen_logged = True
            return None

    def _probes(self, site_id: str) -> List[socket.socket]:
        out: List[socket.socket] = []
        msg = probe_message(site_id)
        try:
            targets = self.targets()
        except Exception as exc:          # never let interface listing stop the forwarder
            logger.debug("MES discovery: no probe targets (%s)", exc)
            targets = []
        for source, dest in targets:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((source, 0))
                s.sendto(msg, (dest, self.port))
                out.append(s)
            except OSError as exc:        # interface gone, no route: next time
                logger.debug("MES discovery: probe from %s to %s failed: %s", source or "any", dest, exc)
                s.close()
        return out

    def __call__(self, site_id: str, ca_sha256: str) -> Found:
        listener = self._listener()       # first, so no announcement is missed
        probes = self._probes(site_id)
        socks = probes + ([listener] if listener else [])
        found: Dict[Tuple[str, int], str] = {}
        try:
            end = self.clock() + self.look_s
            while socks:
                left = end - self.clock()
                if left <= 0:
                    break
                ready, _, _ = select.select(socks, [], [], left)
                for s in ready:
                    try:
                        data, sender = s.recvfrom(MAX_MESSAGE + 1)
                    except OSError:
                        continue
                    port = accept(data, site_id, ca_sha256)
                    if port is None or not _usable_source(sender[0]):
                        continue
                    if not found:
                        end = min(end, self.clock() + SETTLE_S)
                    found.setdefault((sender[0], port), "announcement" if s is listener else "probe")
        finally:
            for s in socks:
                s.close()
        # Probe answers first: they came from a service that heard this machine.
        items = sorted(found.items(), key=lambda kv: kv[1] != "probe")
        return [(ip, port, via) for (ip, port), via in items]


def describe_found(doc: Optional[Dict[str, Any]]) -> str:
    """'probe, 2026-10-28 12:30' for --check."""
    if not doc:
        return ""
    ms = doc.get("found_at_ms")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if isinstance(ms, int) else "?"
    return "%s, %s" % (doc.get("via", "?"), when)
