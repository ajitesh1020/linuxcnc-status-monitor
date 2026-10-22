#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""Streams the loaded G-code program to the dashboard (PROTOCOL.md §5).

Why this is its own module, and not simple:

* Every packet must fit in ONE Ethernet frame. A datagram of 50 KB is cut into
  dozens of IP fragments and the dashboard gets nothing if any single one is
  lost (Wi-Fi, switches and firewalls drop fragments all the time). A chunk of
  that size, once JSON-escaped, could also pass the 65 507-byte UDP limit, and
  the send then failed for good.
* A program is marked "sent" only when EVERY chunk was handed to the network.
* UDP is fire-and-forget and a dashboard may start after the program was
  loaded, so the current program is sent again every ``resend_s`` seconds. The
  dashboard keeps what it already has, so this costs it nothing.
* A big program is read, split and sent from a background thread, paced and
  packet by packet (never all packets in memory), so the status loop is never
  held up and the dashboard's socket buffer is not flooded.
* The shared contract (contracts/gcode/v1.json) limits a program to
  ``MAX_PROGRAM_CHARS`` characters and ``MAX_CHUNKS`` packets. A program over
  either limit is not sent; one small ``gcode_file_refused`` packet goes instead
  (again on every resend, so a dashboard started later learns it too), and the
  agent logs it once, not on every resend.
"""
import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from agent_auth import SIG_RESERVE

logger = logging.getLogger("lcnc_status_agent")

# A packet stays below the usual 1500-byte Ethernet MTU minus IP/UDP headers.
MAX_PACKET_BYTES = 1400
RETRY_AFTER_FAILURE_S = 10.0
PACE_S = 0.001                  # between packets: about 1 MB/s at most
SEND_TRIES = 3                  # per packet (the socket never blocks)

# The dashboard contract (FloorWatch contracts/gcode/v1.json, vendored in
# contracts/gcode/v1.json; tests check these match): a program is sent only if
# it has at most MAX_PROGRAM_CHARS characters AND splits into at most MAX_CHUNKS
# packets; otherwise one gcode_file_refused packet tells the dashboard why.
MAX_PROGRAM_CHARS = 32 * 1024 * 1024
MAX_CHUNKS = 80_000

Fingerprint = Tuple[str, int, int]


def split_content(content: str, budget: int, max_chars: int) -> List[str]:
    """Cut ``content`` in pieces whose JSON-escaped UTF-8 form is at most ``budget`` bytes."""
    def fits(piece: str) -> bool:
        return len(json.dumps(piece, ensure_ascii=False).encode("utf-8")) <= budget

    pieces: List[str] = []
    pos, n = 0, len(content)
    while pos < n:
        take = min(max_chars, n - pos)
        if not fits(content[pos:pos + take]):
            lo, hi = 1, take - 1        # largest piece that fits, so chunks stay few (F05)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fits(content[pos:pos + mid]):
                    lo = mid
                else:
                    hi = mid - 1
            take = lo
        pieces.append(content[pos:pos + take])
        pos += take
    return pieces or [""]


class GcodeFileSender:
    """Sends the loaded program on load / change, and repeats it every ``resend_s``."""

    def __init__(self, chunk_size: int, machine_name: str, proto: int,
                 resend_s: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 pace_s: float = PACE_S, threaded: bool = True) -> None:
        self._chunk_size = max(1, int(chunk_size))
        self._machine_name = machine_name
        self._proto = proto
        self._resend_s = float(resend_s)
        self._clock = clock
        self._pace_s = pace_s
        self._threaded = threaded
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._sent_fingerprint: Optional[Fingerprint] = None
        self._sent_at = 0.0
        self._retry_at = 0.0
        self._generation = 0            # bumped by reset(): an old send must not mark success
        # (fingerprint, reason) of a program over a limit: logged once, and its
        # refusal resent without reading the file again.
        self._refused: Optional[Tuple[Fingerprint, str]] = None

    def reset(self) -> None:
        """Forget what was sent so the file goes out again after a LinuxCNC restart."""
        with self._lock:
            self._sent_fingerprint = None
            self._retry_at = 0.0
            self._refused = None
            self._generation += 1

    def _due(self, fp: Fingerprint, now: float) -> bool:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            if now < self._retry_at:
                return False
            if fp != self._sent_fingerprint:
                return True
            return self._resend_s > 0 and now - self._sent_at >= self._resend_s

    def check_and_send(self, file_path: str, meta: Dict[str, Any],
                       send: Callable[[bytes], bool]) -> None:
        """Called from the status loop: decides only whether a send is due. The file
        is read, split and sent by the worker, never on the calling thread."""
        fp: Fingerprint = (meta["file_name"], meta["file_size"], meta["file_modified_ms"])
        if not file_path or not meta["file_name"]:
            return
        now = self._clock()
        if not self._due(fp, now):
            return
        with self._lock:
            first = fp != self._sent_fingerprint and (self._refused is None
                                                      or self._refused[0] != fp)
            generation = self._generation
        if first:
            logger.info("G-code file changed → %s (%d bytes). Sending.",
                        meta["file_name"], meta["file_size"])
        if self._threaded:
            worker = threading.Thread(target=self._run, name="gcode-send", daemon=True,
                                      args=(meta, fp, file_path, send, generation))
            with self._lock:
                self._worker = worker
            worker.start()
        else:
            self._run(meta, fp, file_path, send, generation)

    def refused_packet(self, meta: Dict[str, Any], reason: str) -> bytes:
        """Sent instead of the chunks of a program over the contract's limits."""
        return json.dumps({
            "type": "gcode_file_refused", "proto": self._proto,
            "ts": int(time.time_ns() // 1_000_000), "machine_name": self._machine_name,
            "file_name": meta["file_name"], "file_size": meta["file_size"],
            "file_modified_ms": meta["file_modified_ms"], "reason": reason,
            "limit_chars": MAX_PROGRAM_CHARS, "limit_chunks": MAX_CHUNKS,
        }, ensure_ascii=False).encode("utf-8")

    def _pieces(self, meta: Dict[str, Any], content: str) -> Tuple[Dict[str, Any], List[str]]:
        head = {"type": "gcode_file", "proto": self._proto, "ts": 0,
                "machine_name": self._machine_name, "file_name": meta["file_name"],
                "file_size": meta["file_size"], "file_modified_ms": meta["file_modified_ms"],
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8", "surrogatepass")).hexdigest(),
                "chunk_index": 99999, "total_chunks": 99999, "content": ""}
        overhead = (len(json.dumps(head, ensure_ascii=False).encode("utf-8")) + 16
                    + SIG_RESERVE)              # room for a site-key signature
        return head, split_content(content, max(64, MAX_PACKET_BYTES - overhead),
                                   self._chunk_size)

    @staticmethod
    def _encode(head: Dict[str, Any], pieces: List[str]) -> Iterator[bytes]:
        for idx, piece in enumerate(pieces):
            head.update(ts=int(time.time_ns() // 1_000_000), chunk_index=idx,
                        total_chunks=len(pieces), content=piece)
            yield json.dumps(head, ensure_ascii=False).encode("utf-8")

    def _stream(self, meta: Dict[str, Any], content: str) -> Tuple[Optional[str], Iterable[bytes]]:
        """(refusal reason or None, the packets to send) for this program text."""
        if len(content) > MAX_PROGRAM_CHARS:
            return "too_large", [self.refused_packet(meta, "too_large")]
        head, pieces = self._pieces(meta, content)
        if len(pieces) > MAX_CHUNKS:
            return "too_many_chunks", [self.refused_packet(meta, "too_many_chunks")]
        return None, self._encode(head, pieces)

    def packets(self, meta: Dict[str, Any], content: str) -> List[bytes]:
        """Every packet for this program text (tests and tools; the sender streams)."""
        return list(self._stream(meta, content)[1])

    def _prepare(self, file_path: str, meta: Dict[str, Any],
                 fp: Fingerprint) -> Optional[Tuple[Optional[str], Iterable[bytes]]]:
        """(refusal reason or None, packets) for the program in file_path, or None
        when it cannot be read. A file too big to possibly fit (UTF-8 is at most
        4 bytes a character) is refused without being read, a refusal already
        found is resent without reading again, and at most one character past
        the limit is ever read."""
        with self._lock:
            known = self._refused[1] if self._refused and self._refused[0] == fp else None
        if known:
            return known, [self.refused_packet(meta, known)]
        if int(meta.get("file_size") or 0) > 4 * MAX_PROGRAM_CHARS:
            return "too_large", [self.refused_packet(meta, "too_large")]
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_PROGRAM_CHARS + 1)       # one more = "too large"
        except OSError as exc:
            logger.error("Cannot read G-code file for sending: %s", exc)
            return None
        return self._stream(meta, content)

    def _note_refusal(self, fp: Fingerprint, reason: str, generation: int) -> None:
        with self._lock:
            if generation != self._generation or (self._refused and self._refused[0] == fp):
                return
            self._refused = (fp, reason)
        limit = (f"{MAX_PROGRAM_CHARS} characters" if reason == "too_large"
                 else f"{MAX_CHUNKS} packets")
        logger.warning("G-code file %s not sent: over the limit of %s. The dashboard is "
                       "told and shows no program text for it.", fp[0], limit)

    def _run(self, meta: Dict[str, Any], fp: Fingerprint, file_path: str,
             send: Callable[[bytes], bool], generation: int) -> None:
        try:
            prepared = self._prepare(file_path, meta, fp)
        except Exception as exc:                   # never take the agent down
            logger.warning("G-code file could not be prepared: %s", exc)
            prepared = None
        if prepared is None:
            with self._lock:
                self._retry_at = self._clock() + RETRY_AFTER_FAILURE_S
            return
        reason, packets = prepared
        if reason:
            self._note_refusal(fp, reason, generation)
        try:
            ok = True
            for packet in packets:
                for attempt in range(SEND_TRIES):    # a full buffer drains in milliseconds
                    if send(packet):
                        break
                    if attempt + 1 < SEND_TRIES:
                        time.sleep(0.02)
                else:
                    ok = False
                if self._pace_s:
                    time.sleep(self._pace_s)
        except Exception as exc:               # never take the agent down
            logger.warning("G-code send failed: %s", exc)
            ok = False
        now = self._clock()
        with self._lock:
            if generation != self._generation:
                return                         # LinuxCNC restarted meanwhile: start over
            if ok:
                self._sent_fingerprint = fp
                self._sent_at = now
                self._retry_at = 0.0
            else:
                logger.warning("G-code file not fully sent; trying again in %d s",
                               RETRY_AFTER_FAILURE_S)
                self._retry_at = now + RETRY_AFTER_FAILURE_S
