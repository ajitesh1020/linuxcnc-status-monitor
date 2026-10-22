#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_mes_spool.py  —  durable MES event spool (MES plan P2.4)
==============================================================
Every MES event the agent produces or collects (its own scans and cycles,
MillWright's outbox) is stored here first; the forwarder sends the stored
bytes to the site service later. Durable before acknowledged: an event is
"taken" only once its row is committed.

Storage — ~/linuxcnc-monitor-agent/mes_spool.db (SQLite, WAL, synchronous=FULL)

  meta(key, value)        producer_id, epoch, machine_id, site_id (no row
                          until enrolment), producer_seq (last agent seq)
  spool(seq, event_id, type, occurred_at, body, state, attempts, acked_at)
                          body = validate.encode(event), sent unchanged;
                          state pending | acked; occurred_at / acked_at in ms
  cursors(source, epoch, position)
                          how far another producer's store was read (e.g.
                          MillWright's outbox); advanced in the SAME
                          transaction as the events read (append(cursor=...))

Epochs: `producer.seq` of the agent's own events counts 1, 2, 3... within an
epoch. A new epoch starts when the database is created again or restored from
a backup, so a sequence number is never used twice. A restore is recognised by
the sidecar file mes_spool.producer.json (producer id, epoch, highest seq),
written after every commit: a database whose epoch differs from it, or whose
seq is lower, is an older copy. The producer id survives a re-created
database (it is kept in the sidecar too).

Budget: `mes_spool_mb` (default 1024) of disk for the database files. Over
90 % a warning; at 100 % append() raises SpoolFull and stores nothing — the
caller keeps the events where they are (MillWright's outbox is not advanced)
or rejects the scan visibly. Nothing is ever dropped silently. Acked rows are
pruned after 7 days; pending rows never.

Only MES worker threads use this; the sampling loop reads `stats` (a dict
replaced atomically) and nothing else.
"""

import ctypes
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta    (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spool   (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                                    type TEXT NOT NULL, occurred_at INTEGER NOT NULL, body TEXT NOT NULL,
                                    state TEXT NOT NULL DEFAULT 'pending',
                                    attempts INTEGER NOT NULL DEFAULT 0, acked_at INTEGER);
CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, epoch TEXT, position INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_spool_state ON spool (state, seq);
"""

APP = "linuxcnc-status-agent"
WARN_AT = 0.9
ACKED_KEEP_S = 7 * 86400
_HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT_DIR = os.path.join(_HERE, "contracts", "mes", "v1")


class SpoolFull(Exception):
    """The disk budget is used up: nothing was stored."""


class SpoolError(Exception):
    """The event cannot be stored (e.g. the contract rejects it)."""


# ---------------------------------------------------------------------------
# Contract and clock
# ---------------------------------------------------------------------------
_validate = None


def contract():
    """The vendored contract's validate module (cached)."""
    global _validate
    if _validate is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_mes_contract_validate",
                                                      os.path.join(CONTRACT_DIR, "validate.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.default_contract()
        _validate = mod
    return _validate


def utc_text(ts: float) -> str:
    """2026-10-22T09:15:02.123Z"""
    ms = int(round(ts * 1000))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)


def clock_synced() -> bool:
    """True only when the kernel says the clock is NTP-synchronised; False when
    unknown (adjtimex state 5 = TIME_ERROR)."""
    try:
        buf = ctypes.create_string_buffer(512)      # struct timex, modes = 0: read only
        state = ctypes.CDLL(None, use_errno=True).adjtimex(buf)
        return 0 <= state < 5
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Spool
# ---------------------------------------------------------------------------
class Spool:
    def __init__(self, path: str, machine_id: str, boot_id: str, version: str,
                 budget_mb: float = 1024.0, now=time.time) -> None:
        self.path = path
        self.sidecar = os.path.splitext(path)[0] + ".producer.json"
        self.budget = max(1, int(float(budget_mb) * 1024 * 1024))
        self.boot_id, self.version, self.now = boot_id, version, now
        self._lock = threading.Lock()
        self._warned = False
        self.validate = contract()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30.0, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._open_epoch(machine_id)
        self.stats: Dict[str, Any] = {}
        self._refresh()

    # -- identity / epochs ---------------------------------------------------
    def _meta(self, key: str) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value) -> None:
        self._db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _read_sidecar(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.sidecar, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        if (isinstance(doc, dict) and isinstance(doc.get("producer_id"), str)
                and isinstance(doc.get("epoch"), str) and isinstance(doc.get("seq"), int)
                and not isinstance(doc.get("seq"), bool)):
            return doc
        return None

    def _write_sidecar(self) -> None:
        doc = {"producer_id": self.producer_id, "epoch": self.epoch, "seq": self.producer_seq}
        tmp = "%s.tmp-%d" % (self.sidecar, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(doc).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.sidecar)

    def _open_epoch(self, machine_id: str) -> None:
        side = self._read_sidecar()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            epoch = self._meta("epoch")
            seq = int(self._meta("producer_seq") or 0)
            producer = self._meta("producer_id") or (side or {}).get("producer_id") or str(uuid.uuid4())
            why = None
            if epoch is None:
                why = "new spool database"
            elif side is not None and side["producer_id"] == producer and (
                    side["epoch"] != epoch or side["seq"] > seq):
                why = "spool database restored from an older copy"
            if why:
                epoch, seq = str(uuid.uuid4()), 0
                logger.warning("MES spool %s: %s, new epoch %s", self.path, why, epoch)
            self._set_meta("producer_id", producer)
            self._set_meta("epoch", epoch)
            self._set_meta("producer_seq", seq)
            self._set_meta("machine_id", machine_id)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self.producer_id, self.epoch, self.producer_seq = producer, epoch, seq
        self.machine_id = machine_id
        self._write_sidecar()

    @property
    def site_id(self) -> Optional[str]:
        with self._lock:
            return self._meta("site_id")

    # -- size ----------------------------------------------------------------
    def _bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def _refresh(self) -> None:
        pending = self._db.execute("SELECT COUNT(*) FROM spool WHERE state='pending'").fetchone()[0]
        size = self._bytes()
        full = size >= self.budget
        if size >= WARN_AT * self.budget and not self._warned:
            logger.warning("MES spool %s uses %d of %d bytes%s", self.path, size, self.budget,
                           " — FULL: new events are refused" if full else "")
            self._warned = True
        elif size < WARN_AT * self.budget:
            self._warned = False
        self.stats = {"pending": pending, "bytes": size, "state": "full" if full else "ok"}

    @property
    def full(self) -> bool:
        return self.stats.get("state") == "full"

    # -- writing -------------------------------------------------------------
    def make_event(self, type_: str, payload: Dict[str, Any], occurred: float,
                   correlation: Optional[Dict[str, Any]] = None, seq: int = 0,
                   station_id: Optional[str] = None) -> Dict[str, Any]:
        corr = {"execution_id": None, "scan_id": None, "operator_session_id": None}
        corr.update(correlation or {})
        event = {
            "schema": "fw.mes.event/1",
            "event_id": str(uuid.uuid4()),
            "type": type_,
            "producer": {"kind": "agent", "id": self.producer_id, "epoch": self.epoch, "seq": seq},
            "site_id": self._meta("site_id"),
            "machine_id": self.machine_id,
            "occurred_at": utc_text(occurred),
            "clock": {"synced": clock_synced(), "boot_id": self.boot_id,
                      "mono_ms": int(time.monotonic() * 1000)},
            "correlation": corr,
            "source": {"app": APP, "version": self.version},
            "payload": payload,
        }
        if station_id:
            event["station_id"] = station_id
        return event

    def add(self, type_: str, payload: Dict[str, Any], occurred: float,
            correlation: Optional[Dict[str, Any]] = None, force: bool = False) -> int:
        """Store one event of the agent's own. Returns its spool seq.
        force=True may go over the budget (for the few events that report a
        full spool); anything else raises SpoolFull."""
        with self._lock:
            return self._append_locked([("own", type_, payload, occurred, correlation)], None, force)[0]

    def append(self, bodies: Iterable[bytes], cursor: Optional[Tuple[str, str, int]] = None) -> List[int]:
        """Store events from another producer exactly as encoded (MillWright's
        outbox), and advance `cursor` = (source, epoch, position) in the same
        transaction. Raises SpoolFull / SpoolError and then stores nothing."""
        with self._lock:
            return self._append_locked([("raw", b) for b in bodies], cursor, False)

    def _append_locked(self, items, cursor, force: bool) -> List[int]:
        if not force:
            self._refresh()
            if self.full:
                raise SpoolFull("MES spool full (%d of %d bytes)" % (self.stats["bytes"], self.budget))
        v = self.validate
        seqs: List[int] = []
        self._db.execute("BEGIN IMMEDIATE")
        try:
            producer_seq = self.producer_seq
            for item in items:
                row = self._db.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='spool'").fetchone()
                rowseq = (row[0] if row else 0) + 1
                if item[0] == "own":
                    _, type_, payload, occurred, corr = item
                    producer_seq += 1
                    event = self.make_event(type_, payload, occurred, corr, producer_seq)
                    event["source"]["record"] = "spool:%d" % rowseq
                    errors = v.validate_event(event)
                    if errors:
                        raise SpoolError("%s rejected by the contract: %s" % (type_, errors))
                    body = v.encode(event)
                else:
                    body = item[1]
                    event, errors = v.decode(body)
                    if not errors:
                        errors = v.validate_event(event)
                    if errors:
                        raise SpoolError("event rejected by the contract: %s" % errors)
                    type_ = event["type"]
                    occurred = (v.parse_timestamp(event["occurred_at"])   # naive UTC
                                .replace(tzinfo=timezone.utc).timestamp())
                self._db.execute(
                    "INSERT INTO spool(seq, event_id, type, occurred_at, body) VALUES(?, ?, ?, ?, ?)",
                    (rowseq, event["event_id"], type_, int(round(occurred * 1000)), body.decode("utf-8")))
                seqs.append(rowseq)
            if cursor is not None:
                source, epoch, position = cursor
                self._db.execute("INSERT INTO cursors(source, epoch, position) VALUES(?, ?, ?) "
                                 "ON CONFLICT(source) DO UPDATE SET epoch=excluded.epoch, "
                                 "position=excluded.position", (source, epoch, int(position)))
            if producer_seq != self.producer_seq:
                self._set_meta("producer_seq", producer_seq)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        if producer_seq != self.producer_seq:
            self.producer_seq = producer_seq
            try:
                self._write_sidecar()
            except OSError as exc:
                logger.warning("MES spool sidecar %s not written: %s", self.sidecar, exc)
        self._refresh()
        return seqs

    def cursor(self, source: str) -> Optional[Tuple[Optional[str], int]]:
        with self._lock:
            row = self._db.execute("SELECT epoch, position FROM cursors WHERE source=?",
                                   (source,)).fetchone()
        return (row[0], row[1]) if row else None

    # -- forwarding (used by the forwarder, package J) -----------------------
    def pending(self, limit: int = 200) -> List[Tuple[int, bytes]]:
        with self._lock:
            return [(s, b.encode("utf-8")) for s, b in self._db.execute(
                "SELECT seq, body FROM spool WHERE state='pending' ORDER BY seq LIMIT ?", (limit,))]

    def mark_acked(self, seqs: Iterable[int]) -> None:
        now_ms = int(self.now() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany("UPDATE spool SET state='acked', acked_at=? WHERE seq=? AND state='pending'",
                                     [(now_ms, s) for s in seqs])
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._refresh()

    def prune(self) -> int:
        """Delete acked rows older than 7 days. Pending rows are never deleted."""
        cutoff = int((self.now() - ACKED_KEEP_S) * 1000)
        with self._lock:
            n = self._db.execute("DELETE FROM spool WHERE state='acked' AND acked_at < ?",
                                 (cutoff,)).rowcount
            self._refresh()
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()


def status_off() -> Dict[str, Any]:
    return {"pending": 0, "bytes": 0, "state": "off"}
