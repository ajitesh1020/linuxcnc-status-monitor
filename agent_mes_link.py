#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_mes_link.py  —  the agent and MillWright (MES plan P1.6)
=============================================================
MillWright (the operator GUI inside AXIS) keeps, in <config>/mw_data/:

  current_execution.json   the run it saw start: execution_id, started_at,
                           program_path, run_file, mode, station_id,
                           operator_session_id (removed at run end)
  millwright.db mw_outbox  its MES events, seq 1, 2, 3... per outbox_epoch

and the agent writes there only mes_ack.json {"epoch", "seq"}: the last
outbox event stored in the agent's spool (MillWright prunes nothing above it).
The agent never writes millwright.db; it opens it read-only.

Sampling loop (CycleLink): at its own cycle start it reads
current_execution.json (<= 1 KB). Same run = program_path or run_file is the
file LinuxCNC runs, no ended_at, and started_at between 3 s before and 1 s
after the agent's start (both programs poll LinuxCNC, so either may see the
start first). Then the cycle carries MillWright's execution_id
(correlation_method "explicit") and operator_session_id; otherwise the agent
makes its own execution_id ("none") and the operator stays unknown (null). If
the file is not there yet, it is read again every 0.5 s during the first 3 s
of the cycle. At cycle end one fact goes to the worker with put_nowait — the
loop never waits on the MES side.

MES worker thread (MesWorker): turns facts into cycle.observed events in the
spool, copies new outbox events into the spool in one transaction with their
cursor, then writes mes_ack.json. SQLite waits, retries and pruning all
happen here.
"""

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from datetime import timezone
from typing import Any, Dict, List, Optional

from agent_identity import UUID4_RE
from agent_mes_spool import SpoolError, SpoolFull, contract, utc_text

logger = logging.getLogger(__name__)

CURRENT_FILE = "current_execution.json"
ACK_FILE = "mes_ack.json"
OUTBOX_SOURCE = "millwright"
OPERATOR_TYPES = ("operator.session_started", "operator.session_ended")
MATCH_BEFORE_S = 3.0          # MillWright's start may be this much before the agent's
MATCH_AFTER_S = 1.0           # ...or this much after (poll jitter of the two programs)
RETRY_EVERY_S = 0.5           # re-read current_execution.json this often...
RETRY_FOR_S = 3.0             # ...during the first seconds of a cycle
MAX_JSON_BYTES = 1024
QUEUE_SIZE = 256
POLL_S = 2.0                  # outbox read interval
BATCH = 200
PRUNE_EVERY_S = 3600.0


def mw_data_dir(config_dir: str) -> str:
    return os.path.join(os.path.expanduser(config_dir.strip()), "mw_data") if config_dir.strip() else ""


def _same_file(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return os.path.normpath(a) == os.path.normpath(b)


def read_current(mw_data: str) -> Optional[Dict[str, Any]]:
    """current_execution.json, or None (missing, too big, not valid)."""
    if not mw_data:
        return None
    try:
        with open(os.path.join(mw_data, CURRENT_FILE), "rb") as f:
            raw = f.read(MAX_JSON_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_JSON_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def match(doc: Optional[Dict[str, Any]], file: str, agent_start: float) -> bool:
    """Is `doc` the run the agent saw start at `agent_start` (wall time)?"""
    if not doc or doc.get("ended_at"):
        return False
    eid, sid = doc.get("execution_id"), doc.get("operator_session_id")
    if not isinstance(eid, str) or not UUID4_RE.match(eid):
        return False
    if sid is not None and (not isinstance(sid, str) or not UUID4_RE.match(sid)):
        return False
    started = contract().parse_timestamp(doc.get("started_at"))
    if started is None:
        return False
    delta = started.replace(tzinfo=timezone.utc).timestamp() - agent_start
    if not -MATCH_BEFORE_S <= delta <= MATCH_AFTER_S:
        return False
    return _same_file(doc.get("program_path"), file) or _same_file(doc.get("run_file"), file)


class CycleLink:
    """Used by the sampling loop only. Cheap: one small file read at cycle
    start (and a few during its first 3 s if MillWright is slower)."""

    def __init__(self, mw_data: str, out: "queue.Queue", wall=time.time) -> None:
        self.mw_data, self.out, self.wall = mw_data, out, wall
        self.cycle: Optional[Dict[str, Any]] = None
        self.dropped = 0

    def start(self, file: str) -> None:
        now = self.wall()
        self.cycle = {"started": now, "program_path": file or None, "execution_id": str(uuid.uuid4()),
                      "method": "none", "operator_session_id": None, "next_try": now + RETRY_EVERY_S}
        self._try()

    def _try(self) -> None:
        c = self.cycle
        doc = read_current(self.mw_data)
        if match(doc, c["program_path"], c["started"]):
            c.update(execution_id=doc["execution_id"], method="explicit",
                     operator_session_id=doc.get("operator_session_id"))

    def poll(self) -> None:
        """Every sample: re-read during the first seconds while not matched."""
        c = self.cycle
        if c is None or c["method"] != "none" or not self.mw_data:
            return
        now = self.wall()
        if now >= c["next_try"] and now - c["started"] <= RETRY_FOR_S + RETRY_EVERY_S:
            c["next_try"] = now + RETRY_EVERY_S
            self._try()

    def set_program(self, file: str) -> None:
        """The cycle's program turned out to be another file (the run started
        inside a subroutine): record it and look for MillWright's run again."""
        c = self.cycle
        if c is None or not file or file == c["program_path"]:
            return
        c["program_path"] = file
        if c["method"] == "none" and self.mw_data:
            self._try()

    def end(self, outcome: str) -> None:
        c, self.cycle = self.cycle, None
        if c is None:
            return
        fact = {"started": c["started"], "ended": max(self.wall(), c["started"]), "outcome": outcome,
                "program_path": c["program_path"], "execution_id": c["execution_id"],
                "method": c["method"], "operator_session_id": c["operator_session_id"]}
        try:
            self.out.put_nowait(fact)
        except queue.Full:
            self.dropped += 1
            logger.error("MES worker is not keeping up: cycle %s not recorded (%d so far)",
                         c["execution_id"], self.dropped)


def cycle_payload(fact: Dict[str, Any]) -> Dict[str, Any]:
    return {"started_at": utc_text(fact["started"]), "ended_at": utc_text(fact["ended"]),
            "outcome": fact["outcome"], "program_path": fact["program_path"],
            "correlation_method": fact["method"]}


class OutboxReader:
    """Copies MillWright's outbox into the spool (worker thread only)."""

    def __init__(self, mw_data: str, spool) -> None:
        self.mw_data, self.spool = mw_data, spool
        self.db_path = os.path.join(mw_data, "millwright.db")
        self._stuck_logged = None
        # Who is logged in on MillWright, for the operator readout (P2.6):
        # {"name", "role", "assurance", "operator_session_id", "since"} or None.
        # Replaced atomically; read by the mes-status thread.
        self.operator: Optional[Dict[str, Any]] = None
        self._op_epoch: Optional[str] = None
        self._op_seq = 0

    def _see_operator(self, seq: int, body: str) -> None:
        """Follow operator.session_started / _ended (newer seq only)."""
        if seq <= self._op_seq:
            return
        try:
            ev = json.loads(body)
            t = ev.get("type")
            if t not in OPERATOR_TYPES:
                return
            if t == "operator.session_started":
                p, c = ev.get("payload") or {}, ev.get("correlation") or {}
                op = {"name": p.get("operator_name"), "role": p.get("role"),
                      "assurance": p.get("assurance"),
                      "operator_session_id": c.get("operator_session_id"),
                      "since": ev.get("occurred_at")}
            else:
                op = None
        except (ValueError, AttributeError):
            return
        self._op_seq, self.operator = seq, op

    def _connect(self) -> sqlite3.Connection:
        # Read-only: the agent never writes millwright.db.
        return sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True, timeout=5.0)

    def write_ack(self, epoch: str, seq: int) -> None:
        path = os.path.join(self.mw_data, ACK_FILE)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"epoch": epoch, "seq": int(seq)}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def step(self) -> int:
        """Read one batch. Returns how many events were stored."""
        if not os.path.isfile(self.db_path):
            return 0
        db = self._connect()
        try:
            row = db.execute("SELECT value FROM state WHERE key='outbox_epoch'").fetchone()
            if row is None:
                return 0
            epoch = row[0]
            if epoch != self._op_epoch:               # start (or a new database): latest login state
                self._op_epoch, self._op_seq, self.operator = epoch, 0, None
                last = db.execute("SELECT seq, body FROM mw_outbox WHERE type IN (?, ?) "
                                  "ORDER BY seq DESC LIMIT 1", OPERATOR_TYPES).fetchone()
                if last:
                    self._see_operator(*last)
            cur = self.spool.cursor(OUTBOX_SOURCE)
            pos = cur[1] if cur and cur[0] == epoch else 0
            rows = db.execute("SELECT seq, body FROM mw_outbox WHERE seq > ? ORDER BY seq LIMIT ?",
                              (pos, BATCH)).fetchall()
        except sqlite3.OperationalError as exc:      # no table yet, locked for > 5 s...
            logger.debug("MillWright outbox not readable: %s", exc)
            return 0
        finally:
            db.close()
        if cur is not None and cur[0] != epoch:
            logger.warning("MillWright outbox has a new epoch %s (was %s): reading it from the start",
                           epoch, cur[0])
        for seq, body in rows:
            self._see_operator(seq, body)
        if not rows:
            if cur is not None and cur[0] == epoch:
                self.write_ack(epoch, pos)            # e.g. after an agent restart
            return 0
        try:
            self.spool.append([b.encode("utf-8") for _, b in rows], (OUTBOX_SOURCE, epoch, rows[-1][0]))
        except (SpoolFull, SpoolError) as exc:
            key = (epoch, rows[0][0], str(exc))
            if key != self._stuck_logged:            # stays in the outbox; retried, logged once
                logger.error("MillWright outbox events from seq %d not stored: %s", rows[0][0], exc)
                self._stuck_logged = key
            return 0
        self._stuck_logged = None
        self.write_ack(epoch, rows[-1][0])
        return len(rows)


class MesWorker(threading.Thread):
    """The MES thread: cycle facts -> cycle.observed; outbox -> spool; prune."""

    def __init__(self, spool, mw_data: str, facts: "queue.Queue", poll_s: float = POLL_S,
                 clock=time.monotonic) -> None:
        super().__init__(name="mes-worker", daemon=True)
        self.spool, self.facts, self.poll_s, self.clock = spool, facts, poll_s, clock
        self.reader = OutboxReader(mw_data, spool) if mw_data else None
        self._halt = threading.Event()
        self._waiting: List[Dict[str, Any]] = []      # facts the spool refused (full)
        self._last_prune = clock()

    def stop(self, timeout: float = 5.0) -> None:
        self._halt.set()
        self.join(timeout)

    def _store_facts(self) -> None:
        while True:
            try:
                self._waiting.append(self.facts.get_nowait())
            except queue.Empty:
                break
        while self._waiting:
            f = self._waiting[0]
            try:
                self.spool.add("cycle.observed", cycle_payload(f), f["ended"],
                               {"execution_id": f["execution_id"],
                                "operator_session_id": f["operator_session_id"]})
            except SpoolFull:
                if len(self._waiting) > QUEUE_SIZE:
                    lost = self._waiting.pop(0)
                    logger.error("MES spool full: cycle %s not recorded", lost["execution_id"])
                return
            except SpoolError as exc:
                logger.error("cycle.observed not stored: %s", exc)
            self._waiting.pop(0)

    def run_once(self) -> None:
        self._store_facts()
        if self.reader is not None:
            while self.reader.step() == BATCH:
                pass
        if self.clock() - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = self.clock()
            self.spool.prune()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.run_once()
            except Exception as exc:                 # never let the MES side die quietly
                logger.error("MES worker: %s", exc)
            self._halt.wait(self.poll_s)
        try:
            self._store_facts()                       # what is left at shutdown
        except Exception as exc:
            logger.error("MES worker at shutdown: %s", exc)
