# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU GPL v3 or later; see LICENSE.
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
* A big program is sent from a background thread, paced, so the status loop is
  never held up and the dashboard's socket buffer is not flooded.
"""
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("lcnc_status_agent")

# A packet stays below the usual 1500-byte Ethernet MTU minus IP/UDP headers.
MAX_PACKET_BYTES = 1400
RETRY_AFTER_FAILURE_S = 10.0
PACE_S = 0.001                  # between packets: about 1 MB/s at most

Fingerprint = Tuple[str, int, int]


def split_content(content: str, budget: int, max_chars: int) -> List[str]:
    """Cut ``content`` in pieces whose JSON-escaped UTF-8 form is at most ``budget`` bytes."""
    pieces: List[str] = []
    pos, n = 0, len(content)
    while pos < n:
        take = min(max_chars, n - pos)
        while take > 1 and len(json.dumps(content[pos:pos + take],
                                          ensure_ascii=False).encode("utf-8")) > budget:
            take //= 2
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

    def reset(self) -> None:
        """Forget what was sent so the file goes out again after a LinuxCNC restart."""
        with self._lock:
            self._sent_fingerprint = None
            self._retry_at = 0.0
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
        fp: Fingerprint = (meta["file_name"], meta["file_size"], meta["file_modified_ms"])
        if not file_path or not meta["file_name"]:
            return
        now = self._clock()
        if not self._due(fp, now):
            return
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as exc:
            logger.error("Cannot read G-code file for sending: %s", exc)
            with self._lock:
                self._retry_at = now + RETRY_AFTER_FAILURE_S
            return
        with self._lock:
            first = fp != self._sent_fingerprint
            generation = self._generation
        if first:
            logger.info("G-code file changed → %s (%d bytes). Sending.",
                        meta["file_name"], meta["file_size"])
        if self._threaded:
            worker = threading.Thread(target=self._run, name="gcode-send", daemon=True,
                                      args=(meta, fp, content, send, generation))
            with self._lock:
                self._worker = worker
            worker.start()
        else:
            self._run(meta, fp, content, send, generation)

    def packets(self, meta: Dict[str, Any], content: str) -> List[bytes]:
        head = {"type": "gcode_file", "proto": self._proto, "ts": 0,
                "machine_name": self._machine_name, "file_name": meta["file_name"],
                "file_size": meta["file_size"], "file_modified_ms": meta["file_modified_ms"],
                "chunk_index": 99999, "total_chunks": 99999, "content": ""}
        overhead = len(json.dumps(head, ensure_ascii=False).encode("utf-8")) + 16
        pieces = split_content(content, max(64, MAX_PACKET_BYTES - overhead), self._chunk_size)
        out = []
        for idx, piece in enumerate(pieces):
            head.update(ts=int(time.time_ns() // 1_000_000), chunk_index=idx,
                        total_chunks=len(pieces), content=piece)
            out.append(json.dumps(head, ensure_ascii=False).encode("utf-8"))
        return out

    def _run(self, meta: Dict[str, Any], fp: Fingerprint, content: str,
             send: Callable[[bytes], bool], generation: int) -> None:
        try:
            ok = True
            for packet in self.packets(meta, content):
                if not send(packet):
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
