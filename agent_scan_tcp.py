#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_scan_tcp.py  —  production reading from Keyence SR-X readers on TCP
==========================================================================
docs/SCANNER_KEYENCE_SRX_WORK.md, package 2. One ScanWorker thread per
enrolled scanner with `scanner_<n>_mode: "tcp"`; never the sampling loop.

Per read-data line from the reader (agent_srx.SrxClient):
  * the no-read string (scanner_<n>_no_read, "ERROR")
        -> scan.rejected_local reason "no_read" (contract 1.3), never a scan;
  * longer than scanner_<n>_max_len (<= 4096)  -> rejected "too_long";
  * control characters other than the GS1 / ISO 15434 separators GS, RS, EOT
        -> rejected "control_chars";
  * more than MAX_RATE scans in one second    -> dropped, counted, logged
        (the contract has no reason for it);
  * otherwise -> scan.received (new scan_id), framing = scanner_<n>_terminator,
        symbology = the AIM identifier the reader puts in front ("]d2"),
        raw as UTF-8 text, or base64 when it isn't clean UTF-8 or holds a
        separator. Stored in the spool (durable) before the status shows it.
  * spool full -> scan.rejected_local "spool_full" (stored over the budget).

The worker:
  * connects, reads model / version / MAC (info()), and reads only when the
    MAC equals scanner_<n>_mac; a different unit -> scanner.state "error",
    "scanner replaced: run Scanner setup" (re-enrol and calibrate);
  * reconnects with backoff 1 s -> 60 s; scanner.state on every change
    (connected / disconnected / error / setup);
  * setup handover: while ~/linuxcnc-monitor-agent/scanner/<n>.setup.lock
    exists it closes the socket within 1 s and reports "setup"; a lock older
    than 2 h is stale: logged, removed, reading resumes;
  * trigger "agent": on a new request in <mw_data>/scan_request.json for this
    scanner it sends LON, waits up to timeout_ms (default 2000), sends LOFF.
    A scan never changes what the machine does;
  * status (a dict replaced atomically, read by the mes_status writer): state,
    detail, last read, no-read rate over the last 100 triggers and
    retune_suggested (rate above scanner_<n>_no_read_warn, once at least
    MIN_TRIGGERS_FOR_RATE triggers were seen).

Contract 1.3 (no_read, setup, model/serial/firmware on scanner.state; vendored
from FloorWatch e57b74f) is used only when the vendored contract has it
(ContractFeatures), so an agent built against an older copy still runs: a
no-read is counted in the status but sends no event, and "setup" is sent as
"disconnected" with a detail.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import agent_srx
from agent_mes_spool import SpoolFull

logger = logging.getLogger(__name__)

MAX_LEN = 4096
MAX_RATE = 20                       # scans per second (plan P2.2)
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 60.0
POLL_S = 0.2                        # read timeout: lock and request checks this often
LOCK_STALE_S = 2 * 3600.0
WINDOW = 100                        # triggers for the no-read rate
MIN_TRIGGERS_FOR_RATE = 20          # no drift warning from a handful of triggers
REQUEST_TIMEOUT_MS = 2000
REQUEST_MAX_AGE_S = 30.0            # older requests (e.g. found at start) are ignored
AFTER_LOFF_S = 0.5                  # wait for the no-read string after LOFF
SEPARATORS = frozenset(b"\x1d\x1e\x04")   # GS, RS, EOT: GS1 / ISO 15434 data
AIM_ID = re.compile(rb"^\][A-Za-z][0-9A-Za-z]")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SCANNER_DIR = "scanner"
REQUEST_FILE = "scan_request.json"
MAX_REQUEST_FILE = 4096

STATES = ("connected", "disconnected", "error", "setup")
# scanner.state optional fields (contract 1.3) and their schema maxLength.
STATE_FIELD_MAX = {"model": 64, "serial": 128, "firmware": 64}


# ---------------------------------------------------------------------------
# Config (flat keys, docs/SCANNER_KEYENCE_SRX_WORK.md section 4.1)
# ---------------------------------------------------------------------------
class ScannerConfig:
    __slots__ = ("name", "mode", "host", "port", "mac", "serial", "model", "trigger",
                 "no_read", "terminator", "max_len", "no_read_warn", "pass_rate")

    def __init__(self, name: str, **kw: Any):
        self.name = name
        self.mode = kw.get("mode", "tcp")
        self.host = kw.get("host", "192.168.100.100")
        self.port = int(kw.get("port", agent_srx.DEFAULT_PORT))
        self.mac = kw.get("mac", "")
        self.serial = kw.get("serial", "")
        self.model = kw.get("model", "")
        self.trigger = kw.get("trigger", "reader")
        self.no_read = kw.get("no_read", "ERROR")
        self.terminator = kw.get("terminator", "cr")
        self.max_len = int(kw.get("max_len", MAX_LEN))
        self.no_read_warn = float(kw.get("no_read_warn", 5))
        self.pass_rate = float(kw.get("pass_rate", 98))


_KEYS = {"mode": str, "host": str, "port": int, "mac": str, "serial": str, "model": str,
         "trigger": str, "no_read": str, "terminator": str, "max_len": int,
         "no_read_warn": float, "pass_rate": float}


def normalize_mac(mac: Any) -> str:
    """'00:01:fc:00:00:01' / '0001FC000001' -> '0001FC000001'; '' when not a MAC."""
    s = re.sub(r"[^0-9A-Fa-f]", "", str(mac or "")).upper()
    return s if len(s) == 12 else ""


def scanner_configs(raw: Dict[str, Any]) -> Tuple[List[ScannerConfig], List[str]]:
    """The TCP scanners in a loaded config mapping, and one message per problem
    (a scanner with a problem is left out; the rest still run)."""
    names = [n.strip() for n in str(raw.get("scanners") or "").split(",") if n.strip()]
    out: List[ScannerConfig] = []
    problems: List[str] = []
    for name in names:
        if not NAME_RE.match(name):
            problems.append("scanner name %r: use letters, digits and _ only" % name)
            continue
        kw: Dict[str, Any] = {}
        bad = False
        for key, typ in _KEYS.items():
            v = raw.get("scanner_%s_%s" % (name, key))
            if v is None or v == "" and typ is not str:
                continue
            try:
                kw[key] = typ(v)
            except (TypeError, ValueError):
                problems.append("scanner %s: bad scanner_%s_%s %r" % (name, name, key, v))
                bad = True
        if bad:
            continue
        c = ScannerConfig(name, **kw)
        if c.mode != "tcp":
            continue                          # hid / serial: other adapters
        why = []
        if not c.host:
            why.append("no host")
        if not 0 < c.port < 65536:
            why.append("bad port")
        if c.trigger not in ("reader", "agent"):
            why.append("trigger must be reader or agent")
        if c.terminator not in agent_srx.TERMINATORS:
            why.append("terminator must be cr, lf or crlf")
        if not 1 <= c.max_len <= MAX_LEN:
            why.append("max_len must be 1-%d" % MAX_LEN)
        if why:
            problems.append("scanner %s: %s" % (name, ", ".join(why)))
            continue
        out.append(c)
    return out, problems


# ---------------------------------------------------------------------------
# Contract features (1.3 is additive; read from the vendored schemas)
# ---------------------------------------------------------------------------
class ContractFeatures:
    def __init__(self, contract_dir: str):
        def schema(name: str) -> Dict[str, Any]:
            try:
                with open(os.path.join(contract_dir, "events", name + ".schema.json"), encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {}
        rej = schema("scan.rejected_local").get("properties", {})
        st = schema("scanner.state").get("properties", {})
        self.no_read = "no_read" in rej.get("reason", {}).get("enum", [])
        self.setup = "setup" in st.get("state", {}).get("enum", [])
        self.state_fields = {k for k in ("model", "serial", "firmware") if k in st}

    @property
    def v13(self) -> bool:
        return self.no_read and self.setup


# ---------------------------------------------------------------------------
# One line -> one decision (pure, tested on its own)
# ---------------------------------------------------------------------------
def classify_read(line: bytes, cfg: ScannerConfig) -> Tuple[str, Dict[str, Any]]:
    """("no_read", {}), ("reject", {"reason", "partial_len"}) or
    ("scan", {"raw", "raw_encoding", "symbology"?, "framing"})."""
    if line == cfg.no_read.encode("utf-8", "replace") and cfg.no_read:
        return "no_read", {}
    if len(line) > cfg.max_len:
        return "reject", {"reason": "too_long", "partial_len": len(line)}
    payload: Dict[str, Any] = {"framing": cfg.terminator}
    data = line
    m = AIM_ID.match(line)
    if m and len(line) > 3:
        payload["symbology"] = line[:3].decode("ascii")
        data = line[3:]
    if any((b < 0x20 or b == 0x7f) and b not in SEPARATORS for b in data):
        return "reject", {"reason": "control_chars", "partial_len": len(line)}
    try:
        text = data.decode("utf-8")
        clean = not any(b < 0x20 for b in data)
    except UnicodeDecodeError:
        text, clean = "", False
    if clean:
        payload.update(raw=text, raw_encoding="utf8")
    else:
        payload.update(raw=base64.b64encode(data).decode("ascii"), raw_encoding="base64")
    return "scan", payload


def display_code(data: bytes, limit: int = 128) -> str:
    """For the operator readout only: separators as <GS> / <RS> / <EOT>."""
    names = {0x1d: "<GS>", 0x1e: "<RS>", 0x04: "<EOT>"}
    text = data.decode("utf-8", "replace")
    out = "".join(names.get(ord(c), c) if ord(c) < 0x20 else c for c in text)
    return out if len(out) <= limit else out[:limit - 1] + "…"


# ---------------------------------------------------------------------------
# Files: setup lock and scan request
# ---------------------------------------------------------------------------
def scanner_dir(config_dir: str) -> str:
    return os.path.join(config_dir, SCANNER_DIR)


def lock_path(config_dir: str, name: str) -> str:
    return os.path.join(scanner_dir(config_dir), name + ".setup.lock")


def lock_age(path: str, now: float) -> Optional[float]:
    """Seconds the lock has existed (its "since", else its mtime); None = no lock."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    since = st.st_mtime
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.loads(f.read(4096))
        s = doc.get("since") if isinstance(doc, dict) else None
        if isinstance(s, str):
            since = _parse_utc(s)
    except (OSError, ValueError):
        pass
    return max(0.0, now - since)


def _parse_utc(s: str) -> float:
    """'2026-12-01T10:15:00Z' (fraction optional) -> epoch seconds."""
    import calendar
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?Z$", s)
    if not m:
        raise ValueError(s)
    parts = [int(x) for x in m.groups()[:6]]
    return calendar.timegm(tuple(parts) + (0, 0, 0)) + float(m.group(7) or 0)


def read_request(path: str) -> Optional[Dict[str, Any]]:
    """scan_request.json when well formed, else None."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.loads(f.read(MAX_REQUEST_FILE))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("scanner"), str) \
            or not isinstance(doc.get("request_id"), str) or not doc["request_id"]:
        return None
    return doc


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class ScanWorker(threading.Thread):
    def __init__(self, cfg: ScannerConfig, spool: Any, config_dir: str,
                 mw_data: Optional[str], features: ContractFeatures,
                 client_factory: Optional[Callable[[ScannerConfig], agent_srx.SrxClient]] = None,
                 now: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        super().__init__(name="scan-%s" % cfg.name, daemon=True)
        self.cfg = cfg
        self.spool = spool
        self.config_dir = config_dir
        self.mw_data = mw_data
        self.features = features
        self.now, self.mono = now, mono
        self._factory = client_factory or (lambda c: agent_srx.SrxClient(
            c.host, c.port, timeout=3.0, terminator=c.terminator))
        self._halt = threading.Event()
        self._client: Optional[agent_srx.SrxClient] = None
        self._state: Optional[Tuple[str, str]] = None
        self._info: Dict[str, Optional[str]] = {}
        self._window: Deque[bool] = collections.deque(maxlen=WINDOW)   # True = no-read
        self._recent: Deque[float] = collections.deque()               # accepted scan times
        self._overlong_seen = 0
        self._last_request: Optional[str] = None
        self._request_pending: Optional[Dict[str, Any]] = None
        self.rate_limited = 0
        self.last_read: Optional[Dict[str, Any]] = None
        self.status: Dict[str, Any] = self._status_doc()
        self._lock_file = lock_path(config_dir, cfg.name)
        self._request_file = os.path.join(mw_data, REQUEST_FILE) if mw_data else None
        if self._request_file:                 # a request from before the start is not fired
            doc = read_request(self._request_file)
            self._last_request = doc["request_id"] if doc else None

    # -- public -------------------------------------------------------------
    def stop(self, timeout: float = 5.0) -> None:
        self._halt.set()
        self.join(timeout)

    def run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._halt.is_set():
            try:
                if self._setup_lock_held():
                    self._close()
                    self._set_state("setup", "a calibration tool owns the reader")
                    self._halt.wait(POLL_S)
                    continue
                if self._client is None:
                    if not self._open():
                        self._halt.wait(backoff)
                        backoff = min(BACKOFF_MAX_S, backoff * 2)
                        continue
                    backoff = BACKOFF_MIN_S
                self._serve_once()
            except agent_srx.SrxConnectionError as e:
                self._close()
                self._set_state("disconnected", str(e))
                self._halt.wait(backoff)             # a reader that keeps dropping: no tight loop
            except agent_srx.SrxError as e:
                self._close()
                self._set_state("error", str(e))
                self._halt.wait(backoff)
            except Exception as e:                   # never take the agent down
                logger.exception("scanner %s: %s", self.cfg.name, e)
                self._close()
                self._set_state("error", "internal error: %s" % e)
                self._halt.wait(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
        self._close()

    # -- connection ---------------------------------------------------------
    def _open(self) -> bool:
        c = self._factory(self.cfg)
        try:
            c.connect()
            info = c.info()
        except agent_srx.SrxError as e:
            c.close()
            self._set_state("disconnected", str(e))
            return False
        self._info = info
        want, got = normalize_mac(self.cfg.mac), normalize_mac(info.get("mac"))
        problem = None
        if not want:
            problem = "not enrolled: run lcnc-status-agent --scanner-enroll"
        elif not got:
            problem = "the reader did not report its MAC address"
        elif got != want:
            problem = "scanner replaced: run Scanner setup"
            logger.error("scanner %s: MAC %s, enrolled %s", self.cfg.name, got, want)
        if problem:
            c.close()
            self._set_state("error", problem)
            return False
        self._client = c
        self._overlong_seen = c.overlong
        self._set_state("connected", "")
        return True

    def _close(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            c.close()

    # -- reading ------------------------------------------------------------
    def _serve_once(self) -> None:
        c = self._client
        assert c is not None
        self._check_request()
        if self._request_pending is not None:
            self._run_request(c, self._request_pending)
            self._request_pending = None
            return
        line = c.read_line(POLL_S)
        self._check_overlong(c)
        if line is not None:
            self.handle_line(line)

    def _check_overlong(self, c: agent_srx.SrxClient) -> None:
        while self._overlong_seen < c.overlong:
            self._overlong_seen += 1
            self._window.append(False)
            self._reject("too_long", max(c.last_overlong_len, self.cfg.max_len + 1), b"")

    def handle_line(self, line: bytes) -> None:
        kind, info = classify_read(line, self.cfg)
        if kind == "no_read":
            self._window.append(True)
            self._no_read()
            return
        self._window.append(False)
        if kind == "reject":
            self._reject(info["reason"], info["partial_len"], line)
            return
        t = self.mono()
        while self._recent and t - self._recent[0] >= 1.0:
            self._recent.popleft()
        if len(self._recent) >= MAX_RATE:
            self.rate_limited += 1
            logger.warning("scanner %s: over %d scans/s, scan dropped (%d so far)",
                           self.cfg.name, MAX_RATE, self.rate_limited)
            self._refresh_status()
            return
        self._recent.append(t)
        payload = dict(info, scanner_id=self.cfg.name)
        scan_id = str(uuid.uuid4())
        try:
            self.spool.add("scan.received", payload, self.now(), {"scan_id": scan_id})
        except SpoolFull:
            self._reject("spool_full", len(line), line)
            return
        self.last_read = {"code": display_code(line[3:] if "symbology" in info else line),
                          "status": "received", "reason": None,
                          "at_ms": int(self.now() * 1000), "scan_id": scan_id}
        self._refresh_status()

    def _no_read(self) -> None:
        if self.features.no_read:
            self._add("scan.rejected_local", {"scanner_id": self.cfg.name, "reason": "no_read",
                                              "partial_len": 0})
        self.last_read = {"code": "", "status": "rejected", "reason": "no_read",
                          "at_ms": int(self.now() * 1000)}
        self._refresh_status()

    def _reject(self, reason: str, partial_len: int, line: bytes) -> None:
        logger.warning("scanner %s: input rejected (%s, %d bytes)", self.cfg.name, reason, partial_len)
        self._add("scan.rejected_local", {"scanner_id": self.cfg.name, "reason": reason,
                                          "partial_len": min(int(partial_len), 1048576)},
                  force=(reason == "spool_full"))
        self.last_read = {"code": "", "status": "rejected", "reason": reason,
                          "at_ms": int(self.now() * 1000)}
        self._refresh_status()

    def _add(self, type_: str, payload: Dict[str, Any], force: bool = False) -> None:
        """Store a rejection or state event. force: over a full spool's budget
        (spool_full and scanner.state: they report the problem; rare)."""
        try:
            self.spool.add(type_, payload, self.now(), force=force)
        except Exception as e:
            logger.error("scanner %s: %s not stored: %s", self.cfg.name, type_, e)

    # -- agent trigger ------------------------------------------------------
    def _check_request(self) -> None:
        if self.cfg.trigger != "agent" or not self._request_file:
            return
        doc = read_request(self._request_file)
        if doc is None or doc["request_id"] == self._last_request:
            return
        self._last_request = doc["request_id"]
        if doc["scanner"] != self.cfg.name:
            return
        at = doc.get("at")
        try:
            age = self.now() - _parse_utc(at) if isinstance(at, str) else None
        except ValueError:
            age = None
        if age is not None and age > REQUEST_MAX_AGE_S:
            logger.info("scanner %s: request %s is %.0f s old, ignored", self.cfg.name, doc["request_id"], age)
            return
        self._request_pending = doc

    def _run_request(self, c: agent_srx.SrxClient, doc: Dict[str, Any]) -> None:
        t = doc.get("timeout_ms")
        timeout_s = (t if isinstance(t, int) and not isinstance(t, bool) and 100 <= t <= 30000
                     else REQUEST_TIMEOUT_MS) / 1000.0
        c.command("LON")
        end = self.mono() + timeout_s
        got = False
        while not got and not self._halt.is_set():
            left = end - self.mono()
            if left <= 0:
                break
            line = c.read_line(min(left, POLL_S))
            self._check_overlong(c)
            if line is not None:
                self.handle_line(line)
                got = True
        c.command("LOFF")
        if not got:                              # the reader sends its no-read string on LOFF
            line = c.read_line(AFTER_LOFF_S)
            if line is not None:
                self.handle_line(line)
            else:
                logger.info("scanner %s: request %s: nothing read", self.cfg.name, doc["request_id"])

    # -- setup lock ---------------------------------------------------------
    def _setup_lock_held(self) -> bool:
        age = lock_age(self._lock_file, self.now())
        if age is None:
            return False
        if age > LOCK_STALE_S:
            logger.warning("scanner %s: setup lock %s is %.1f h old: stale, removed",
                           self.cfg.name, self._lock_file, age / 3600)
            try:
                os.remove(self._lock_file)
            except OSError as e:
                logger.error("scanner %s: cannot remove stale lock: %s", self.cfg.name, e)
                return True
            return False
        return True

    # -- state and status ---------------------------------------------------
    def _set_state(self, state: str, detail: str) -> None:
        if self._state == (state, detail):
            return
        prev = self._state
        self._state = (state, detail)
        if prev is not None or state != "disconnected":
            logger.info("scanner %s: %s%s", self.cfg.name, state, (" (%s)" % detail) if detail else "")
        sent = state
        if state == "setup" and not self.features.setup:
            sent, detail = "disconnected", "setup: " + detail
        payload: Dict[str, Any] = {"scanner_id": self.cfg.name, "state": sent}
        if detail:
            payload["detail"] = detail[:256]
        for k, v in (("model", self._info.get("model")), ("firmware", self._info.get("firmware")),
                     ("serial", self.cfg.serial or None)):
            if k in self.features.state_fields and v:
                payload[k] = str(v)[:STATE_FIELD_MAX[k]]
        self._add("scanner.state", payload, force=True)
        self._refresh_status()

    def _status_doc(self) -> Dict[str, Any]:
        n = len(self._window)
        rate = round(100.0 * sum(self._window) / n, 1) if n else None
        state, detail = self._state or ("disconnected", "")
        return {
            "id": self.cfg.name,
            "state": state,
            "detail": detail or None,
            "model": self._info.get("model"),
            "last_read": dict(self.last_read) if self.last_read else None,
            "no_read_rate": rate,
            "triggers": n,
            "retune_suggested": bool(n >= MIN_TRIGGERS_FOR_RATE and rate is not None
                                     and rate > self.cfg.no_read_warn),
            "rate_limited": self.rate_limited,
        }

    def _refresh_status(self) -> None:
        self.status = self._status_doc()


def last_scan(workers: List[ScanWorker]) -> Optional[Dict[str, Any]]:
    """The newest last read of all scanners, for mes_status.json "scan"."""
    best = None
    for w in workers:
        r = w.status.get("last_read")
        if r and (best is None or r["at_ms"] > best["at_ms"]):
            best = r
    if best is None:
        return None
    return {k: best[k] for k in ("code", "status", "reason", "at_ms")}


def start_workers(raw_config: Dict[str, Any], spool: Any, config_dir: str, mw_data: Optional[str],
                  contract_dir: str) -> List[ScanWorker]:
    cfgs, problems = scanner_configs(raw_config)
    for p in problems:
        logger.error("%s: not started", p)
    if not cfgs:
        return []
    features = ContractFeatures(contract_dir)
    if not features.v13:
        logger.warning("MES contract 1.3 not vendored yet: no-reads are not sent as events, "
                       "'setup' is sent as 'disconnected'")
    workers = []
    for c in cfgs:
        if c.trigger == "agent" and not mw_data:
            logger.warning("scanner %s: trigger agent needs mes_millwright_config; it only listens", c.name)
        w = ScanWorker(c, spool, config_dir, mw_data, features)
        w.start()
        workers.append(w)
    return workers
