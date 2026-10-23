#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_journal.py  —  history kept on the machine PC
====================================================
UDP status packets are fire-and-forget: while the dashboard is closed, the
office PC is off, or the network is down, they are simply lost. The journal
keeps a compact copy on the machine so the dashboard can fetch what it missed
when it comes back.

Storage — ~/linuxcnc-monitor-agent/journal.db (SQLite, WAL)
  meta(key, value)                  agent_id: random id created once; changes
                                    only if the journal file is deleted
  entries(seq, ts_ms, data)         seq increases forever; data = compact JSON

What is written — the fields in JOURNAL_FIELDS of each status packet,
  * immediately when something that matters changes (cycle state, part /
    abort / partial counts, errors, E-stop, machine on/off, program), else
  * at most every `journal_interval_s` (default 10 s) while active, and on each
    idle heartbeat.
  Roughly 1 MB per day of production; entries older than `journal_days` are
  pruned hourly.

Serving — JournalServer answers on UDP `journal_port` (default 5006):
  request   {"type": "journal_request", "proto": 1, "from_seq": N, "to_seq": M}
  response  {"type": "journal", "proto": 1, "machine_name", "agent_id",
             "first_seq", "last_seq", "from_seq", "entries": [[seq, ts_ms, {...}]],
             "more": true|false}
  Entries with from_seq < seq <= to_seq (to_seq optional), oldest first, up to
  ~16 KB per reply; the dashboard asks again from the last seq it got while
  "more" is true. Read-only: a request can only read history.
  from_seq / to_seq must be whole numbers 0..2^63-1; anything else (1e309,
  NaN, fractions, booleans) gets no reply. One bad request never stops the
  server; `JournalServer.alive` says whether it is serving.

Every status packet carries `agent_id`, `journal_seq` (newest seq) and
`journal_port`, so a dashboard can tell exactly what it has missed.
"""

import collections
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agent_auth import sign, verify

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_REPLY_BYTES = 16_000
MAX_ROWS_PER_QUERY = 500
PRUNE_EVERY_S = 3600.0

# Copied from each status packet into the journal.
JOURNAL_FIELDS = (
    "cycle_state", "cycle_time_ms", "parts_produced", "abort_count",
    "run_from_here_count", "partial_parts", "partial_part_equiv",
    "last_partial_pct", "last_cycle_ms", "last_abort_ms", "avg_cycle_ms",
    "program_progress_pct", "part_in_progress", "is_run_from_here",
    "exec_state", "cmd_state", "cmd_serial",
    "estop", "task_state", "task_mode", "motion_line",
    "file_name", "wcs", "tool_in_spindle", "feedrate",
    "session_id",      # agent start: part counters restart at 0 with a new one
    "machine_uuid",    # stable machine identity (agent_identity.py)
)
# A change in any of these is written immediately.
CHANGE_FIELDS = (
    "cycle_state", "parts_produced", "abort_count", "partial_parts",
    "exec_state", "estop", "task_state", "file_name",
)
RCS_ERROR = 3   # cmd_state of a failed command

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entries (
    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    data  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_ts ON entries (ts_ms);
"""


def compact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The journalled subset of a status packet. Joints are kept only when
    faulted or on a limit; NML messages only when present."""
    out = {k: payload[k] for k in JOURNAL_FIELDS if k in payload}
    bad = [{"id": j.get("id"), "fault": bool(j.get("fault")),
            "hard_limit": bool(j.get("hard_limit"))}
           for j in payload.get("joints") or []
           if isinstance(j, dict) and (j.get("fault") or j.get("hard_limit"))]
    if bad:
        out["joints"] = bad
    if payload.get("nml_errors"):
        out["nml_errors"] = payload["nml_errors"]
    return out


def _change_key(c: Dict[str, Any]) -> Tuple:
    # cmd_state / cmd_serial change with every command (jogs, override
    # moves...); only a failed command, and its clearing, is written at once.
    failed = c.get("cmd_serial") if c.get("cmd_state") == RCS_ERROR else None
    return (tuple(c.get(k) for k in CHANGE_FIELDS)
            + (json.dumps(c.get("joints"), sort_keys=True), failed))


def _open(path: str) -> sqlite3.Connection:
    # A short busy wait: the writer runs in the sampling loop, and WAL readers
    # (the history server) never block it anyway.
    conn = sqlite3.connect(path, timeout=1.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


RETRY_FIRST_S = 5.0             # reopen a failed journal after this long, doubling...
RETRY_MAX_S = 300.0             # ...up to this
DISK_CHECK_S = 60.0             # how often free disk space is checked


def _free_bytes(path: str) -> Optional[int]:
    try:
        st = os.statvfs(os.path.dirname(os.path.abspath(path)) or ".")
        return st.f_bavail * st.f_frsize
    except (OSError, AttributeError):   # no statvfs on Windows
        return None


class Journal:
    """Writer side — used from the agent's main loop only. Never raises.

    ``state`` is reported to the dashboard (``journal_state``):
      "ok"        writing normally
      "failing"   the database cannot be opened or written; reopened after
                  5 s, 10 s, ... up to every 5 min, so a passing problem (a
                  locked file, a remounted disk) does not end the history
      "disk_low"  less than ``min_free_mb`` free: writes pause, so the journal
                  never fills the disk LinuxCNC needs; they resume with 10 % spare
    Packets not journalled meanwhile are missing from the history.
    """

    def __init__(self, path: str, interval_s: float = 10.0, retain_days: float = 14.0,
                 min_free_mb: float = 100.0, free_bytes=_free_bytes) -> None:
        self.path = path
        self._interval = max(1.0, float(interval_s))
        self._retain_ms = int(max(1.0, float(retain_days)) * 86_400_000)
        self._min_free = int(max(0.0, float(min_free_mb)) * 1_000_000)
        self._free_bytes = free_bytes
        self._last_key: Optional[Tuple] = None
        self._last_write = 0.0
        self._last_prune = 0.0
        self._last_disk_check = -1e18
        self._retry_at = 0.0
        self._backoff = RETRY_FIRST_S
        self.last_seq = 0
        self.agent_id = ""
        self.state = "ok"
        self.error = ""
        self._conn: Optional[sqlite3.Connection] = None
        self._open_db(time.monotonic())

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def _open_db(self, now: float) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            conn = _open(self.path)
            row = conn.execute("SELECT value FROM meta WHERE key='agent_id'").fetchone()
            if row:
                agent_id = row[0]
            else:
                agent_id = uuid.uuid4().hex
                conn.execute("INSERT INTO meta VALUES ('agent_id', ?)", (agent_id,))
                conn.commit()
            last_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM entries").fetchone()[0]
        except (OSError, sqlite3.Error) as exc:
            self._fail(now, f"cannot open {self.path}: {exc}")
            return False
        self._conn, self.agent_id, self.last_seq = conn, agent_id, last_seq
        return True

    def _fail(self, now: float, message: str) -> None:
        if self.state != "failing":
            logger.warning("Journal failing (history not kept until it recovers): %s", message)
        self.close()
        self.state, self.error = "failing", message[:200]
        self._retry_at = now + self._backoff
        self._backoff = min(self._backoff * 2, RETRY_MAX_S)

    def _disk_ok(self, now: float) -> bool:
        if now - self._last_disk_check < DISK_CHECK_S:
            return self.state != "disk_low"
        self._last_disk_check = now
        free = self._free_bytes(self.path) if self._min_free else None
        if free is None:
            return self.state != "disk_low"
        if self.state == "disk_low":
            if free < self._min_free * 1.1:
                return False
            logger.warning("Journal: disk space back (%d MB free); writing again.",
                           free // 1_000_000)
            self.state, self.error = ("ok" if self._conn else "failing"), ""
            return True
        if free < self._min_free:
            logger.warning("Journal paused: only %d MB free on the disk (needs %d MB).",
                           free // 1_000_000, self._min_free // 1_000_000)
            self.state, self.error = "disk_low", f"{free // 1_000_000} MB free"
            return False
        return True

    def record(self, payload: Dict[str, Any], now: Optional[float] = None) -> Optional[int]:
        """Journal this packet if it changed something that matters or the
        interval has passed. Returns the new seq, or None if not written."""
        now = time.monotonic() if now is None else now
        if not self._disk_ok(now):
            return None
        if self._conn is None:
            if now < self._retry_at or not self._open_db(now):
                return None
        c = compact(payload)
        key = _change_key(c)
        due = (key != self._last_key or bool(c.get("nml_errors"))
               or now - self._last_write >= self._interval)
        if not due:
            return None
        ts = int(payload.get("ts") or time.time() * 1000)
        try:
            cur = self._conn.execute(
                "INSERT INTO entries (ts_ms, data) VALUES (?, ?)",
                (ts, json.dumps(c, separators=(",", ":"), default=str)))
            self._conn.commit()
        except sqlite3.Error as exc:
            self._fail(now, f"write failed: {exc}")
            return None
        if self.state == "failing":
            logger.warning("Journal recovered; history is kept again.")
        self.state, self.error, self._backoff = "ok", "", RETRY_FIRST_S
        self.last_seq = cur.lastrowid
        self._last_key = key
        self._last_write = now
        if now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            self.prune(ts)
        return self.last_seq

    def prune(self, now_ms: Optional[int] = None) -> int:
        if self._conn is None:
            return 0
        cutoff = int(now_ms if now_ms is not None else time.time() * 1000) - self._retain_ms
        try:
            n = self._conn.execute("DELETE FROM entries WHERE ts_ms < ?", (cutoff,)).rowcount
            self._conn.commit()
            return n
        except sqlite3.Error as exc:
            logger.warning("Journal prune failed: %s", exc)
            return 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None


MAX_SEQ = 2 ** 63 - 1   # largest SQLite INTEGER


class _BadRequest(ValueError):
    pass


def _seq_arg(value: Any) -> int:
    """A sequence number from a request: a whole number 0..MAX_SEQ.

    Accepts a JSON integer, a whole float (5.0) or a short digit string.
    Rejects booleans, fractions, NaN / infinity (JSON 1e309 parses as inf and
    int(inf) raises OverflowError) and numbers too big for SQLite.
    """
    if isinstance(value, bool):
        raise _BadRequest("boolean")
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or not value.is_integer():
            raise _BadRequest("not a whole finite number")
        n = int(value)
    elif isinstance(value, str) and 0 < len(value.strip()) <= 20 and value.strip().isdigit():
        n = int(value.strip())
    else:
        raise _BadRequest("not a number")
    if not 0 <= n <= MAX_SEQ:
        raise _BadRequest("out of range")
    return n


def build_reply(conn: sqlite3.Connection, request: Any, machine_name: str,
                agent_id: str, max_bytes: int = MAX_REPLY_BYTES) -> Optional[Dict[str, Any]]:
    """Answer one journal_request (pure; testable). None if the request is invalid."""
    if not isinstance(request, dict) or request.get("type") != "journal_request":
        return None
    try:
        from_seq = _seq_arg(request.get("from_seq", 0))
        to_seq = request.get("to_seq")
        to_seq = _seq_arg(to_seq) if to_seq is not None else None
    except _BadRequest:
        return None
    first_seq, last_seq = conn.execute(
        "SELECT COALESCE(MIN(seq), 0), COALESCE(MAX(seq), 0) FROM entries").fetchone()
    upper = last_seq if to_seq is None else min(to_seq, last_seq)
    rows = conn.execute(
        "SELECT seq, ts_ms, data FROM entries WHERE seq > ? AND seq <= ? "
        "ORDER BY seq LIMIT ?", (from_seq, upper, MAX_ROWS_PER_QUERY)).fetchall()
    entries: List[list] = []
    size = 400   # envelope
    for seq, ts_ms, data in rows:
        item_len = len(data) + 40
        if entries and size + item_len > max_bytes:
            break
        try:
            entries.append([seq, ts_ms, json.loads(data)])
        except ValueError:
            continue
        size += item_len
    last_sent = entries[-1][0] if entries else upper
    return {
        "type": "journal",
        "proto": PROTOCOL_VERSION,
        "ts": int(time.time() * 1000),
        "machine_name": machine_name,
        "agent_id": agent_id,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "from_seq": from_seq,
        "entries": entries,
        "more": last_sent < upper,
    }


class JournalServer:
    """Background thread answering journal requests over UDP.

    One bad request can never stop it: every request is handled inside its
    own catch-all. `alive` says whether it is serving; the agent reports
    journal_port 0 while it is not, and calls start() again to restart it.
    """

    def __init__(self, path: str, port: int, machine_name: str, agent_id: str,
                 max_replies_per_s: int = 50, bind_host: str = "",
                 key: Optional[bytes] = None, allow: Tuple[str, ...] = ()) -> None:
        self._path = path
        self._port = int(port)
        self._host = bind_host
        self._name = machine_name
        self.agent_id = agent_id         # updated by the agent if the journal is recreated
        self._max_rate = max_replies_per_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self.bad_requests = 0   # requests rejected or that failed, for diagnostics
        # With a site key a request must be signed and carry a fresh nonce
        # (a replayed request is ignored); replies are signed.
        self._key = key
        self._allow = tuple(a for a in allow if a)   # only these IPs may ask (empty = any)
        self._nonces: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self.refused = 0        # unsigned / bad signature / replayed / not allowed

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start serving (again, after it stopped). True when serving."""
        if self.alive:
            return True
        if self._port <= 0:
            return False      # port 0 = off in the config
        self._close_socket()
        self._stop.clear()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.settimeout(0.5)
        except OSError as exc:
            logger.warning("Journal server not started on UDP %d: %s", self._port, exc)
            return False
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, args=(sock,),
                                        name="journal-server", daemon=True)
        self._thread.start()
        logger.info("Journal server listening on UDP %d", self._port)
        return True

    def _loop(self, sock: socket.socket) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _open(self._path)
            window_start, sent_in_window = time.monotonic(), 0
            while not self._stop.is_set():
                try:
                    raw, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    time.sleep(0.1)
                    continue
                now = time.monotonic()
                if now - window_start >= 1.0:
                    window_start, sent_in_window = now, 0
                if sent_in_window >= self._max_rate:
                    continue
                if self._answer(conn, sock, raw, addr):
                    sent_in_window += 1
        except Exception as exc:   # opening the database, or anything unforeseen
            logger.warning("Journal server stopped: %s", exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            try:
                sock.close()
            except OSError:
                pass

    def _answer(self, conn: sqlite3.Connection, sock: socket.socket,
                raw: bytes, addr: Any) -> bool:
        """Handle one request. Never raises. True if a reply was sent."""
        if self._allow and (not addr or addr[0] not in self._allow):
            self.refused += 1
            return False
        if self._key is not None and verify(raw, self._key) != "ok":
            self.refused += 1
            return False
        try:
            request = json.loads(raw.decode("utf-8"))
            if self._key is not None and not self._fresh_nonce(request):
                self.refused += 1
                return False
            reply = build_reply(conn, request, self._name, self.agent_id)
        except Exception as exc:   # bad JSON, deep nesting, overflow, database error...
            self.bad_requests += 1
            logger.debug("Bad journal request from %s: %s: %s",
                         addr[0] if addr else "?", type(exc).__name__, exc)
            return False
        if reply is None:
            self.bad_requests += 1
            return False
        try:
            sock.sendto(sign(json.dumps(reply, separators=(",", ":"),
                                        default=str).encode("utf-8"), self._key), addr)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("Journal reply to %s failed: %s", addr[0] if addr else "?", exc)
            return False

    def _fresh_nonce(self, request: Any) -> bool:
        nonce = request.get("nonce") if isinstance(request, dict) else None
        if not isinstance(nonce, str) or not 8 <= len(nonce) <= 64 or nonce in self._nonces:
            return False
        self._nonces[nonce] = None
        while len(self._nonces) > 4096:
            self._nonces.popitem(last=False)
        return True

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._close_socket()
