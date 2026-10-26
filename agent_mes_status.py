#!/usr/bin/env python3
"""Local MES readout for the operator (MES plan P2.6).

The agent writes ~/linuxcnc-monitor-agent/mes_status.json for MillWright's machine
panel (its MES strip) and for `lcnc-status-agent --mes-status`. Display only: nothing
reads it to decide anything about the machine.

  {"schema": "lcnc-agent.mes_status/1",
   "updated_at_ms": 1793000000000,       wall clock of this write
   "running": true,                      false = the agent stopped cleanly
   "machine_uuid": "...",
   "operator": {"name", "role", "assurance", "operator_session_id", "since"} | null,
   "queue": {"pending": n, "bytes": n, "state": "ok" | "full"},
   "link": {"state": "ok" | "offline" | "not_enrolled" | "cert_expired" | "rejected",
            "last_ok_ms": n (absent if never)},
   "scan": null,                         last scan, once scanners exist (P2.1-P2.3):
                                         {"code", "status": received | pending | bound | rejected,
                                          "reason", "at_ms"}
   "bound": []}                          items bound to this machine (P2.1-P2.3)

Written by its own thread (never the sampling loop), atomically (temp file + rename),
at most twice a second and only when something changed, but at least every REFRESH_S
so a reader can tell a stopped agent: a file older than STALE_S means "agent not
running". The file is removed at start when MES is off.

Colours (plan P2.6), the same in MillWright and --mes-status:
  scan    grey = received, amber = pending lookup, green = bound, red = rejected
  queue   red = full (new scans are refused: scan.rejected_local spool_full)
  link    green = ok, amber = offline, grey = not_enrolled, red = cert_expired / rejected
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = "lcnc-agent.mes_status/1"
FILE_NAME = "mes_status.json"
MIN_INTERVAL_S = 0.5        # at most 2 writes a second
REFRESH_S = 5.0             # rewritten at least this often while running
STALE_S = 15.0              # readers: older than this = the agent is not running

SCAN_COLOUR = {"received": "grey", "pending": "amber", "bound": "green", "rejected": "red"}
LINK_COLOUR = {"ok": "green", "offline": "amber", "not_enrolled": "grey",
               "cert_expired": "red", "rejected": "red"}


def status_path(config_dir: str) -> str:
    return os.path.join(config_dir, FILE_NAME)


def build(machine_uuid: Optional[str], queue: Optional[Dict[str, Any]],
          link: Optional[Dict[str, Any]], operator: Optional[Dict[str, Any]],
          scan: Optional[Dict[str, Any]] = None, bound: Optional[List[Any]] = None,
          running: bool = True) -> Dict[str, Any]:
    """The document without its timestamp (written by StatusWriter)."""
    q = queue or {}
    return {
        "schema": SCHEMA,
        "running": bool(running),
        "machine_uuid": machine_uuid,
        "operator": dict(operator) if operator else None,
        "queue": {"pending": int(q.get("pending", 0)), "bytes": int(q.get("bytes", 0)),
                  "state": q.get("state", "ok")},
        "link": dict(link) if link else {"state": "not_enrolled"},
        "scan": dict(scan) if scan else None,
        "bound": list(bound or []),
    }


def write_atomic(path: str, doc: Dict[str, Any]) -> None:
    # No fsync: display only, rewritten every few seconds; a reader treats a
    # damaged or missing file as "unknown".
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


def remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Cannot remove %s: %s", path, exc)


class StatusWriter(threading.Thread):
    """Writes mes_status.json from collect() (cheap, no I/O: it only copies
    status dicts the MES threads replace atomically)."""

    def __init__(self, path: str, collect: Callable[[], Dict[str, Any]],
                 min_interval: float = MIN_INTERVAL_S, refresh: float = REFRESH_S,
                 clock=time.monotonic, wall=time.time) -> None:
        super().__init__(name="mes-status", daemon=True)
        self.path, self.collect = path, collect
        self.min_interval, self.refresh = min_interval, refresh
        self.clock, self.wall = clock, wall
        self._halt = threading.Event()
        self._last: Optional[Dict[str, Any]] = None
        self._last_at: Optional[float] = None
        self._error_logged = False

    def step(self, force: bool = False, running: bool = True) -> bool:
        """Write if something changed or REFRESH_S passed. Returns True if written."""
        doc = self.collect()
        doc["running"] = running
        now = self.clock()
        if not force and doc == self._last and self._last_at is not None \
                and now - self._last_at < self.refresh:
            return False
        out = dict(doc, updated_at_ms=int(self.wall() * 1000))
        try:
            write_atomic(self.path, out)
        except OSError as exc:
            if not self._error_logged:
                logger.error("MES status file %s not written: %s", self.path, exc)
                self._error_logged = True
            return False
        self._error_logged = False
        self._last, self._last_at = doc, now
        return True

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.step()
            except Exception as exc:                 # display only: never take the agent down
                logger.error("MES status: %s", exc)
            self._halt.wait(self.min_interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._halt.set()
        self.join(timeout)
        try:
            self.step(force=True, running=False)
        except Exception as exc:
            logger.error("MES status at shutdown: %s", exc)


# -- reading (MillWright does the same in its own code) ----------------------
def read(path: str, wall=time.time) -> Tuple[Optional[Dict[str, Any]], Optional[float], str]:
    """(doc, age in seconds, problem). problem "" = fine; "missing", "unreadable",
    "stale" (agent not running) or "stopped" (agent stopped cleanly)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, None, "missing"
    except (OSError, ValueError):
        return None, None, "unreadable"
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None, None, "unreadable"
    age = max(0.0, wall() - float(doc.get("updated_at_ms", 0)) / 1000.0)
    if not doc.get("running", False):
        return doc, age, "stopped"
    if age > STALE_S:
        return doc, age, "stale"
    return doc, age, ""


def _ago(ms: Optional[int], wall=time.time) -> str:
    if not ms:
        return "never"
    s = max(0, int(wall() - ms / 1000.0))
    if s < 120:
        return "%d s ago" % s
    if s < 7200:
        return "%d min ago" % (s // 60)
    return "%d h ago" % (s // 3600)


def format_lines(doc: Optional[Dict[str, Any]], age: Optional[float], problem: str,
                 wall=time.time) -> List[str]:
    """Plain text for `lcnc-status-agent --mes-status`; colour names in brackets."""
    if problem == "missing":
        return ["MES status: none (MES is off, or the agent has not started yet)"]
    if problem == "unreadable":
        return ["MES status: file unreadable (being rewritten? try again)"]
    lines = []
    if problem == "stopped":
        lines.append("MES status: agent stopped %s — last known state:" % _ago(doc.get("updated_at_ms"), wall))
    elif problem == "stale":
        lines.append("MES status: agent NOT RUNNING (last update %s) — last known state:"
                     % _ago(doc.get("updated_at_ms"), wall))
    else:
        lines.append("MES status (updated %s):" % _ago(doc.get("updated_at_ms"), wall))
    op = doc.get("operator")
    lines.append("  operator : %s" % ("%s (%s, %s)" % (op.get("name"), op.get("role"), op.get("assurance"))
                                     if op else "nobody logged in"))
    scan = doc.get("scan")
    if scan:
        st = scan.get("status", "")
        reason = (" — %s" % scan["reason"]) if scan.get("reason") else ""
        lines.append("  last scan: %s  %s%s  [%s]" % (scan.get("code", ""), st, reason,
                                                     SCAN_COLOUR.get(st, "grey")))
    else:
        lines.append("  last scan: none")
    bound = doc.get("bound") or []
    if bound:
        lines.append("  bound    : %s" % ", ".join(str(b.get("code", b)) if isinstance(b, dict) else str(b)
                                                for b in bound))
    q = doc.get("queue") or {}
    full = q.get("state") == "full"
    lines.append("  queue    : %d waiting to send%s" % (q.get("pending", 0),
                                                         "  — SPOOL FULL, new scans refused  [red]" if full else ""))
    link = doc.get("link") or {}
    st = link.get("state", "not_enrolled")
    lines.append("  link     : %s, last OK %s  [%s]" % (st, _ago(link.get("last_ok_ms"), wall),
                                                       LINK_COLOUR.get(st, "grey")))
    return lines
