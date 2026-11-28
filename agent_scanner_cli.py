#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_scanner_cli.py  —  scanner commands of lcnc-status-agent (SR-X, TCP)
===========================================================================
docs/SCANNER_KEYENCE_SRX_WORK.md, package 3. Every command has --json (one
JSON object on stdout; --scanner-test prints one object per line) for
MillWright's Scanner setup wizard (package 4), which has no reader code of
its own; text output is for people.

  --scanner-enroll tcp://<ip>:9004 --name A [--force]
  --scanner-info A
  --scanner-test A [--count N]
  --scanner-calibrate A                     the text wizard (section 3)
  --scanner-backup A
  --scanner-restore A FILE [--with-network]
  --scanner-setup A <step> [args]           one wizard step (MillWright):
        lock | unlock | identify | network | network-set IP MASKBITS GW |
        time-sync [SNTP_IP] | focus | codes BANK | codes-set BANK TYPE,... |
        bank-name BANK NAME | tune BANK | tune-quit | rate-test [N] [BANK] |
        output-check | output-fix | trigger-test | save CALIB_JSON |
        image | factory-reset (with --confirm twice)

Setup lock (section 4.2): every command that talks to the reader holds
~/linuxcnc-monitor-agent/scanner/<name>.setup.lock while it runs, so the
agent hands the reader over (within 1 s) and takes it back afterwards.
MillWright keeps one lock for the whole wizard: `--scanner-setup A lock
--owner millwright --pid <its pid>`, then passes the same --owner/--pid with
every step (the step then runs under that lock), and `unlock` at the end. A
lock whose holder process is gone, or older than 2 h, is replaced.

Setting numbers are from the SR-X User's Manual, chapter 14-3 (package 0
confirms them on the real reader; work document section 7). Settings change
only inside a held lock and reach the reader's flash only with `save`
(SAVE). The reader's firmware is never touched; factory reset (DFLT) only
on an explicit, doubly confirmed request.
"""
from __future__ import annotations

import binascii
import glob
import json
import os
import re
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import agent_scan_tcp as st
import agent_srx as srx

# ---------------------------------------------------------------------------
# Reader settings used here (manual 14-3)
# ---------------------------------------------------------------------------
TERM_HEX = {"cr": "0D", "lf": "0A", "crlf": "0D0A"}
# What the agent expects (section 4.4); (kind, number) -> value, built per scanner
APPEND_OFF = ("300", "301", "303", "304", "305", "306", "308", "309", "310", "370")   # WP: time, code type, ...
WP_SYMBOL_ID = "302"
WP_NO_READ = "205"          # HEX, "FF" = not sent
WP_TIMING = "101"           # 0 level, 1 one-shot
WP_MATCH_THRESHOLD = "207"
WN_CHECKSUM, WN_SIZE, WN_HEADER, WN_TERMINATOR = "003", "004", "005", "006"
WN_IP, WN_MASK, WN_GATEWAY, WN_SNTP = "200", "201", "202", "520"
WB_CODES, WB_NAME, WB_EXPOSURE, WB_GAIN = "300", "625", "100", "101"
BANKS = range(1, 17)
CODE_BITS = {"qr": 1, "datamatrix": 2, "pdf417": 3, "maxicode": 4, "gs1databar": 5, "code39": 6,
             "itf": 7, "2of5": 8, "nw7": 9, "ean_upc": 10, "code128": 11, "coop2of5": 12,
             "code93": 13, "cc_databar": 14, "cc_ean_upc": 15, "cc_gs1_128": 16, "postal": 17,
             "pharmacode": 18, "dotcode": 20, "aztec": 21}
SRX_MODELS = re.compile(r"^SR-X")
TUNE_RE = re.compile(r"^Tuning (SUCCEEDED|FAILED),(\d+)ms,([0-9A-Fa-f]{8})$")
FOCUS_RE = re.compile(r"^Focus Tuning (SUCCEEDED|FAILED)$")
TEST_RE = re.compile(rb"^(.*):(\d{1,2}):(\d{1,3})%:(\d{1,3})$")
ADVICE = {"0": None, "1": "use an image filter", "2": "check installation, lighting and printing",
          "4": "not enough light"}
FAIL_REASON = {"1": "no code found", "2": "unstable reading"}
TUNE_TIMEOUT_S = 120.0
FOCUS_TIMEOUT_S = 60.0
HANDOVER_WAIT_S = 3.0
TEST_IDLE_S = 60.0          # --scanner-test (reader trigger): stop after this long without a read


class CliError(Exception):
    """A problem to report to the user (exit code 1)."""


def utc_now_text(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def utc_stamp(ts: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if ts is None else ts))


def to_hex(text: str) -> str:
    return binascii.hexlify(text.encode("ascii")).decode("ascii").upper()


def code_mask(types: List[str]) -> str:
    bad = [t for t in types if t not in CODE_BITS]
    if bad or not types:
        raise CliError("unknown code type(s) %s; known: %s" % (", ".join(bad) or "(none)",
                                                               ", ".join(sorted(CODE_BITS))))
    return "%X" % sum(1 << (CODE_BITS[t] - 1) for t in set(types))


def code_types(mask_hex: str) -> List[str]:
    v = int(mask_hex, 16)
    return [t for t, b in sorted(CODE_BITS.items(), key=lambda kv: kv[1]) if v & (1 << (b - 1))]


# ---------------------------------------------------------------------------
# Setup lock
# ---------------------------------------------------------------------------
def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.loads(f.read(4096))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


class SetupLock:
    """<name>.setup.lock, created atomically (temp file + link: never half
    written, never two holders)."""

    def __init__(self, config_dir: str, name: str, owner: str = "cli", pid: Optional[int] = None):
        if owner not in ("cli", "millwright"):
            raise CliError("lock owner must be cli or millwright")
        self.path = st.lock_path(config_dir, name)
        self.owner, self.pid = owner, pid or os.getpid()
        self.held = False

    def mine(self) -> bool:
        doc = read_lock(self.path)
        return bool(doc) and doc.get("owner") == self.owner and doc.get("pid") == self.pid

    def acquire(self) -> bool:
        """True when this call created the lock, False when we already held it."""
        if self.mine():
            return False
        doc = read_lock(self.path)
        if doc is not None:
            age = st.lock_age(self.path, time.time()) or 0.0
            if doc and _pid_alive(doc.get("pid")) and age <= st.LOCK_STALE_S:
                raise CliError("Scanner setup is already open (%s, pid %s, since %s)"
                               % (doc.get("owner"), doc.get("pid"), doc.get("since")))
            os.remove(self.path)              # holder gone, damaged or stale
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = "%s.tmp-%d" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"owner": self.owner, "pid": self.pid, "since": utc_now_text()}, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, self.path)
        except FileExistsError:
            raise CliError("Scanner setup was opened by someone else just now") from None
        finally:
            os.remove(tmp)
        self.held = True
        return True

    def release(self) -> None:
        if self.mine():
            os.remove(self.path)
        self.held = False


def wait_for_handover(config_dir: str, name: str, timeout: float = HANDOVER_WAIT_S,
                      sleep: Callable[[float], None] = time.sleep) -> None:
    """Wait until the running agent no longer holds the reader (its status
    shows the scanner not connected), or until the agent isn't running."""
    import agent_mes_status as mst
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        doc, _age, problem = mst.read(mst.status_path(config_dir))
        if problem or not doc:
            return
        sc = [s for s in doc.get("scanners") or [] if s.get("id") == name]
        if not sc or sc[0].get("state") != "connected":
            return
        sleep(0.1)


# ---------------------------------------------------------------------------
# Session: config + lock + client
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, ctx: "Context", name: str, owner: str = "cli", pid: Optional[int] = None,
                 need_enrolled: bool = True):
        self.ctx = ctx
        self.cfg = ctx.scanner(name)
        if need_enrolled and not st.normalize_mac(self.cfg.mac):
            raise CliError("scanner %s is not enrolled: lcnc-status-agent --scanner-enroll" % name)
        self.lock = SetupLock(ctx.config_dir, name, owner, pid)
        self.client: Optional[srx.SrxClient] = None
        self._created = False

    def __enter__(self) -> "Session":
        self._created = self.lock.acquire()
        try:
            if self._created:
                wait_for_handover(self.ctx.config_dir, self.cfg.name, sleep=self.ctx.sleep)
            self.client = self.ctx.client_factory(self.cfg)
            self.client.connect()
            info = self.client.info()
            want, got = st.normalize_mac(self.cfg.mac), st.normalize_mac(info.get("mac"))
            if want and got != want:
                raise CliError("scanner replaced: the reader at %s:%d has MAC %s, %s is enrolled with %s. "
                               "Enrol it again (--scanner-enroll ... --name %s)"
                               % (self.cfg.host, self.cfg.port, got or "?", self.cfg.name, want, self.cfg.name))
            self.info = info
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.client is not None:
            self.client.close()
        if self._created:
            self.lock.release()

    def cmd(self, command: str, timeout: Optional[float] = None) -> srx.SrxReply:
        assert self.client is not None
        r = self.client.command(command, timeout)
        if not r.ok:
            raise CliError("%s refused: %s" % (command, r.error_text or "error"))
        return r

    def get(self, kind: str, num: str) -> Optional[str]:
        """R<kind>,<num> -> value, or None when the reader refuses the number."""
        assert self.client is not None
        r = self.client.command("R%s,%s" % (kind, num))
        return ",".join(r.fields) if r.ok else None

    def put(self, kind: str, num: str, value: str) -> None:
        self.cmd("W%s,%s,%s" % (kind, num, value))

    def wait_line(self, timeout: float, match: Callable[[bytes], bool]) -> Optional[bytes]:
        assert self.client is not None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            line = self.client.read_line(min(0.5, max(0.01, end - time.monotonic())))
            if line is not None and match(line):
                return line
        return None


class Context:
    """Where things are, injected for tests."""

    def __init__(self, cfg_path: Optional[str], config_dir: str, raw: Dict[str, Any],
                 client_factory: Optional[Callable[[st.ScannerConfig], srx.SrxClient]] = None,
                 set_value: Optional[Callable[[str, str, str], None]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg_path = cfg_path
        self.config_dir = config_dir
        self.raw = raw
        self.client_factory = client_factory or (lambda c: srx.SrxClient(
            c.host, c.port, timeout=3.0, terminator=c.terminator))
        if set_value is None:
            import agent_mes_forward
            set_value = agent_mes_forward.set_config_value
        self.set_value = set_value
        self.sleep = sleep

    def scanner(self, name: str) -> st.ScannerConfig:
        cfgs, problems = st.scanner_configs(self.raw)
        for c in cfgs:
            if c.name == name:
                return c
        for p in problems:
            if p.startswith("scanner %s:" % name):
                raise CliError(p)
        raise CliError("no TCP scanner %r in %s (scanners: %s)"
                       % (name, self.cfg_path or "the config", self.raw.get("scanners") or "none"))

    def dir_of(self, name: str) -> str:
        d = os.path.join(st.scanner_dir(self.config_dir), name)
        os.makedirs(d, exist_ok=True)
        return d

    def write_config(self, key: str, value: Any) -> None:
        if not self.cfg_path:
            raise CliError("no config file to write to")
        self.set_value(self.cfg_path, key, str(value))
        self.raw[key] = str(value)


def _atomic_json(path: str, doc: Dict[str, Any]) -> None:
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Commands (each returns a JSON-able dict)
# ---------------------------------------------------------------------------
def enroll(ctx: Context, url: str, name: str, force: bool = False) -> Dict[str, Any]:
    if not st.NAME_RE.match(name):
        raise CliError("--name: letters, digits and _ only (it becomes the scanner_<name>_* config keys)")
    try:
        host, port = srx.parse_endpoint(url)
    except ValueError as e:
        raise CliError(str(e)) from None
    raw_old = dict(ctx.raw)
    ctx.raw.update({"scanner_%s_mode" % name: "tcp", "scanner_%s_host" % name: host,
                    "scanner_%s_port" % name: port})
    if name not in [n.strip() for n in str(ctx.raw.get("scanners") or "").split(",")]:
        ctx.raw["scanners"] = ",".join([n for n in str(ctx.raw.get("scanners") or "").split(",") if n.strip()]
                                       + [name])
    try:
        with Session(ctx, name, need_enrolled=False) as s:
            info = s.info
    except BaseException:
        ctx.raw.clear()
        ctx.raw.update(raw_old)
        raise
    model, mac = info.get("model"), st.normalize_mac(info.get("mac"))
    if not mac:
        raise CliError("the reader at %s:%d did not report its MAC address (EMAC)" % (host, port))
    if not (model and SRX_MODELS.match(model)) and not force:
        raise CliError("the reader at %s:%d reports model %r, not an SR-X; --force to enrol it anyway"
                       % (host, port, model))
    ctx.raw.clear()
    ctx.raw.update(raw_old)
    names = [n.strip() for n in str(ctx.raw.get("scanners") or "").split(",") if n.strip()]
    for key, value in (("mode", "tcp"), ("host", host), ("port", port), ("mac", mac),
                       ("model", model or "")):
        ctx.write_config("scanner_%s_%s" % (name, key), value)
    if name not in names:
        ctx.write_config("scanners", ",".join(names + [name]))
    return {"scanner": name, "host": host, "port": port, "model": model, "firmware": info.get("firmware"),
            "mac": mac, "serial": None, "config": ctx.cfg_path, "restart_agent": True}


def info_cmd(ctx: Context, name: str) -> Dict[str, Any]:
    import agent_mes_status as mst
    c = ctx.scanner(name)
    doc, _age, problem = mst.read(mst.status_path(ctx.config_dir))
    sc = [x for x in (doc or {}).get("scanners") or [] if x.get("id") == name]
    calib = None
    p = os.path.join(st.scanner_dir(ctx.config_dir), name, "calibration.json")
    try:
        with open(p, encoding="utf-8") as f:
            calib = json.load(f)
    except (OSError, ValueError):
        pass
    return {"scanner": name, "host": c.host, "port": c.port, "model": c.model or None,
            "mac": st.normalize_mac(c.mac) or None, "serial": c.serial or None, "trigger": c.trigger,
            "enrolled": bool(st.normalize_mac(c.mac)),
            "state": sc[0] if sc else None,
            "agent": "running" if not problem else ("stopped" if problem in ("stopped", "stale") else "unknown"),
            "setup_lock": read_lock(st.lock_path(ctx.config_dir, name)),
            "calibration": {k: calib.get(k) for k in ("at", "by", "passed", "test", "backup")} if calib else None}


def test_cmd(ctx: Context, name: str, count: int = 10, owner: str = "cli",
             pid: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """Reads without forwarding: one {"type": "read"} per trigger, then
    {"type": "summary"}. Each read goes through the agent's own parser."""
    if count < 1 or count > 1000:
        raise CliError("--count must be 1-1000")
    with Session(ctx, name, owner, pid) as s:
        assert s.client is not None
        reads = no_reads = rejected = 0
        for i in range(count):
            if s.cfg.trigger == "agent":
                s.client.command("LON")
                line = s.wait_line(2.0, lambda b: True)
                s.client.command("LOFF")
                if line is None:
                    line = s.wait_line(0.5, lambda b: True)
            else:
                line = s.wait_line(TEST_IDLE_S, lambda b: True)
            if line is None:
                yield {"type": "timeout", "n": i + 1}
                break
            kind, p = st.classify_read(line, s.cfg)
            out: Dict[str, Any] = {"type": "read", "n": i + 1, "result": kind}
            if kind == "scan":
                reads += 1
                out.update(code=st.display_code(line[3:] if "symbology" in p else line), stored_as=p)
            elif kind == "no_read":
                no_reads += 1
            else:
                rejected += 1
                out.update(reason=p["reason"], length=p["partial_len"])
            yield out
        total = reads + no_reads + rejected
        yield {"type": "summary", "reads": reads, "no_reads": no_reads, "rejected": rejected,
               "no_read_rate": round(100.0 * no_reads / total, 1) if total else None}


# -- backup / restore --------------------------------------------------------
def backup_keys() -> List[Tuple[str, str]]:
    keys = [("P", n) for n in (WP_TIMING, "102", WP_NO_READ, WP_MATCH_THRESHOLD, WP_SYMBOL_ID) + APPEND_OFF]
    keys += [("N", n) for n in (WN_CHECKSUM, WN_SIZE, WN_HEADER, WN_TERMINATOR, WN_IP, WN_MASK, WN_GATEWAY,
                                WN_SNTP)]
    for b in BANKS:
        keys += [("B", "%02d%s" % (b, n)) for n in (WB_EXPOSURE, WB_GAIN, WB_CODES, WB_NAME)]
    return keys


NETWORK_KEYS = {("N", WN_IP), ("N", WN_MASK), ("N", WN_GATEWAY)}


def backup(ctx: Context, name: str, owner: str = "cli", pid: Optional[int] = None,
           session: Optional[Session] = None) -> Dict[str, Any]:
    def run(s: Session) -> Dict[str, Any]:
        settings = {"%s,%s" % k: s.get(*k) for k in backup_keys()}
        stamp = utc_stamp()
        doc = {"schema": "lcnc-agent.scanner_settings/1", "scanner": name, "at": utc_now_text(),
               "model": s.info.get("model"), "firmware": s.info.get("firmware"),
               "mac": st.normalize_mac(s.info.get("mac")), "settings": settings,
               "note": "settings the agent reads (manual 14-3); null = the reader refused the number"}
        path = os.path.join(ctx.dir_of(name), "settings-%s.json" % stamp)
        _atomic_json(path, doc)
        return {"scanner": name, "file": path, "settings": len(settings),
                "unread": sorted(k for k, v in settings.items() if v is None)}
    if session is not None:
        return run(session)
    with Session(ctx, name, owner, pid) as s:
        return run(s)


def restore(ctx: Context, name: str, path: str, with_network: bool = False, owner: str = "cli",
            pid: Optional[int] = None) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise CliError("cannot read %s: %s" % (path, e)) from None
    if not isinstance(doc, dict) or doc.get("schema") != "lcnc-agent.scanner_settings/1" \
            or not isinstance(doc.get("settings"), dict):
        raise CliError("%s is not a scanner settings backup" % path)
    allowed = {"%s,%s" % k for k in backup_keys()}
    written, skipped = [], []
    with Session(ctx, name, owner, pid) as s:
        for key, value in sorted(doc["settings"].items()):
            kind, _, num = key.partition(",")
            if key not in allowed or value is None or not isinstance(value, str):
                skipped.append(key)
                continue
            if (kind, num) in NETWORK_KEYS and not with_network:
                skipped.append(key)
                continue
            s.put(kind, num, value)
            written.append(key)
        s.cmd("SAVE")
    return {"scanner": name, "file": path, "written": len(written), "skipped": skipped, "saved": True}


# -- wizard steps --------------------------------------------------------------
def _bank(b: Any) -> int:
    try:
        v = int(b)
    except (TypeError, ValueError):
        v = 0
    if v not in BANKS:
        raise CliError("bank must be 1-16")
    return v


def step(ctx: Context, name: str, action: str, args: List[str], owner: str = "cli",
         pid: Optional[int] = None, confirm: int = 0, wall: Callable[[], float] = time.time) -> Dict[str, Any]:
    out: Dict[str, Any] = {"scanner": name, "step": action}
    if action == "lock":
        lock = SetupLock(ctx.config_dir, name, owner, pid)
        lock.acquire()
        wait_for_handover(ctx.config_dir, name, sleep=ctx.sleep)
        return dict(out, lock=read_lock(lock.path))
    if action == "unlock":
        lock = SetupLock(ctx.config_dir, name, owner, pid)
        was = lock.mine()
        lock.release()
        return dict(out, released=was)
    _check_args(action, args, confirm)
    with Session(ctx, name, owner, pid) as s:
        if action == "identify":
            return dict(out, host=s.cfg.host, port=s.cfg.port, model=s.info.get("model"),
                        firmware=s.info.get("firmware"), mac=st.normalize_mac(s.info.get("mac")),
                        enrolled_mac=st.normalize_mac(s.cfg.mac), match=True)
        if action == "network":
            return dict(out, ip=s.get("N", WN_IP), mask_bits=s.get("N", WN_MASK),
                        gateway=s.get("N", WN_GATEWAY), sntp=s.get("N", WN_SNTP))
        if action == "network-set":
            ip, mask, gw = args
            s.put("N", WN_IP, ip)
            s.put("N", WN_MASK, mask)
            s.put("N", WN_GATEWAY, gw)
            s.cmd("SAVE")
            ctx.write_config("scanner_%s_host" % name, ip)
            return dict(out, ip=ip, mask_bits=int(mask), gateway=gw, saved=True,
                        note="the reader uses the new address after it restarts; config updated")
        if action == "time-sync":
            now = wall()
            s.cmd("TMSET,%s" % time.strftime("%Y%m%d%H%M%S", time.gmtime(now)))
            if args:
                s.put("N", WN_SNTP, args[0])
            return dict(out, set_to=utc_now_text(now), clock="UTC", sntp=args[0] if args else None)
        if action == "focus":
            s.cmd("FTUNE")
            line = s.wait_line(FOCUS_TIMEOUT_S, lambda b: bool(FOCUS_RE.match(b.decode("ascii", "replace"))))
            if line is None:
                raise CliError("no focus result within %d s" % FOCUS_TIMEOUT_S)
            ok = FOCUS_RE.match(line.decode("ascii")).group(1) == "SUCCEEDED"
            return dict(out, succeeded=ok, position=None,
                        note="the focus position is not reported over TCP (manual); it is kept in the reader")
        if action == "codes":
            b = _bank(args[0] if args else 1)
            v = s.get("B", "%02d%s" % (b, WB_CODES))
            return dict(out, bank=b, mask=v, types=code_types(v) if v else None)
        if action == "codes-set":
            b = _bank(args[0])
            types = [t.strip().lower() for t in args[1].split(",") if t.strip()]
            mask = code_mask(types)
            s.put("B", "%02d%s" % (b, WB_CODES), mask)
            return dict(out, bank=b, mask=mask, types=code_types(mask), saved=False)
        if action == "bank-name":
            b = _bank(args[0])
            s.put("B", "%02d%s" % (b, WB_NAME), args[1])
            return dict(out, bank=b, name=args[1], saved=False)
        if action == "tune":
            b = _bank(args[0] if args else 1)
            s.cmd("TUNE,%02d" % b)
            line = s.wait_line(TUNE_TIMEOUT_S, lambda x: bool(TUNE_RE.match(x.decode("ascii", "replace"))))
            if line is None:
                s.client.command("TQUIT")
                raise CliError("no tuning result within %d s (tuning stopped)" % TUNE_TIMEOUT_S)
            m = TUNE_RE.match(line.decode("ascii"))
            code = m.group(3)
            ok = m.group(1) == "SUCCEEDED"
            return dict(out, bank=b, succeeded=ok, ms=int(m.group(2)), code=code,
                        advice=ADVICE.get(code[5], "advice %s" % code[5]),
                        reason=None if ok else FAIL_REASON.get(code[7], "reason %s" % code[7]),
                        exposure=s.get("B", "%02d%s" % (b, WB_EXPOSURE)),
                        gain=s.get("B", "%02d%s" % (b, WB_GAIN)))
        if action == "tune-quit":
            s.cmd("TQUIT")
            return dict(out, stopped=True)
        if action == "rate-test":
            n = int(args[0]) if args else 100
            b = _bank(args[1]) if len(args) > 1 else None
            return dict(out, **rate_test(s, n, b))
        if action == "output-check":
            diffs = output_diffs(s)
            return dict(out, ok=not diffs, differences=diffs)
        if action == "output-fix":
            diffs = output_diffs(s)
            for d in diffs:
                s.put(d["kind"], d["number"], d["want"])
            return dict(out, changed=diffs, saved=False,
                        note="written; reaches the reader's flash with the save step")
        if action == "trigger-test":
            s.client.command("LON")
            line = s.wait_line(2.0, lambda x: True)
            s.client.command("LOFF")
            if line is None:
                line = s.wait_line(0.5, lambda x: True)
            if line is None:
                return dict(out, result="nothing")
            kind, p = st.classify_read(line, s.cfg)
            return dict(out, result=kind, stored_as=p if kind == "scan" else None,
                        reason=p.get("reason") if kind == "reject" else None)
        if action == "save":
            return dict(out, **save_calibration(ctx, s, args[0]))
        if action == "image":
            return dict(out, **latest_image(ctx, s))
        if action == "factory-reset":
            s.cmd("DFLT")
            s.cmd("SAVE")
            return dict(out, reset=True, note="factory settings saved: run a full calibration")
    raise CliError("unknown step %r" % action)          # not reached: _check_args


STEPS = ("identify", "network", "network-set", "time-sync", "focus", "codes", "codes-set", "bank-name",
         "tune", "tune-quit", "rate-test", "output-check", "output-fix", "trigger-test", "save", "image",
         "factory-reset")


def _check_args(action: str, args: List[str], confirm: int) -> None:
    """Everything that can be checked before the reader is touched."""
    if action not in STEPS:
        raise CliError("unknown step %r (steps: lock, unlock, %s)" % (action, ", ".join(STEPS)))
    if action == "network-set":
        if len(args) != 3:
            raise CliError("network-set IP MASKBITS GATEWAY")
        ip, mask, gw = args
        for a in (ip, gw):
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", a) or any(int(x) > 255 for x in a.split(".")):
                raise CliError("bad address %r" % a)
        if not mask.isdigit() or not 8 <= int(mask) <= 30:
            raise CliError("mask bits must be 8-30")
    elif action == "codes-set":
        if len(args) != 2:
            raise CliError("codes-set BANK TYPE,TYPE,...")
        _bank(args[0])
        code_mask([t.strip().lower() for t in args[1].split(",") if t.strip()])
    elif action == "bank-name":
        if len(args) != 2:
            raise CliError("bank-name BANK NAME")
        _bank(args[0])
        if not 0 < len(args[1]) <= 32 or not all(32 <= ord(c) < 127 for c in args[1]) or "," in args[1]:
            raise CliError("bank name: 1-32 printable ASCII characters, no comma")
    elif action in ("codes", "tune") and args:
        _bank(args[0])
    elif action == "rate-test":
        if args and (not args[0].isdigit() or not 10 <= int(args[0]) <= 1000):
            raise CliError("rate-test N: 10-1000 triggers")
        if len(args) > 1:
            _bank(args[1])
    elif action == "save" and len(args) != 1:
        raise CliError("save CALIBRATION_JSON (the wizard's answers: a file, or - for stdin)")
    elif action == "factory-reset" and confirm < 2:
        raise CliError("factory reset erases every setting on the reader: pass --confirm twice")


def rate_test(s: Session, n: int, bank: Optional[int]) -> Dict[str, Any]:
    """TEST1 for n triggers (the reader reports every 10 decodes), then QUIT."""
    assert s.client is not None
    threshold = s.get("P", WP_MATCH_THRESHOLD)
    s.cmd("TEST1" if bank is None else "TEST1,%02d" % bank)
    rows: List[Tuple[int, int]] = []
    try:
        want = max(1, n // 10)
        end = time.monotonic() + max(30.0, n * 1.0)
        while len(rows) < want and time.monotonic() < end:
            line = s.client.read_line(1.0)
            m = TEST_RE.match(line) if line is not None else None
            if m:
                rows.append((int(m.group(3)), int(m.group(4))))
    finally:
        s.client.command("QUIT")
    if not rows:
        raise CliError("no reading-rate results (is a label in front of the reader?)")
    rate = float(rows[-1][0])
    level = min(r[1] for r in rows)
    thr = int(threshold) if threshold and threshold.isdigit() else None
    passed = rate >= s.cfg.pass_rate and (thr is None or level >= thr)
    return {"triggers": len(rows) * 10, "reading_rate": rate, "matching_level": level,
            "matching_threshold": thr, "read_ms": None, "pass_rate": s.cfg.pass_rate, "passed": passed,
            "hints": [] if passed else ["glare: tilt the reader 10-15 degrees",
                                        "low contrast: tune again (exposure)",
                                        "out of focus: run focus again"]}


def expected_output(cfg: st.ScannerConfig) -> List[Tuple[str, str, str, str]]:
    """(kind, number, value, what) the agent expects (section 4.4)."""
    out = [("N", WN_HEADER, "FF", "no header"),
           ("N", WN_TERMINATOR, TERM_HEX[cfg.terminator], "terminator %s" % cfg.terminator.upper()),
           ("N", WN_CHECKSUM, "0", "no checksum"), ("N", WN_SIZE, "0", "no data size"),
           ("P", WP_SYMBOL_ID, "1", "symbology ID appended"),
           ("P", WP_NO_READ, to_hex(cfg.no_read) if cfg.no_read else "FF", "no-read string %r" % cfg.no_read)]
    out += [("P", n, "0", "nothing else appended") for n in APPEND_OFF]
    return out


def output_diffs(s: Session) -> List[Dict[str, Any]]:
    diffs = []
    for kind, num, want, what in expected_output(s.cfg):
        have = s.get(kind, num)
        if have is None or have.upper() != want.upper():
            diffs.append({"kind": kind, "number": num, "have": have, "want": want, "what": what})
    return diffs


CALIB_FIELDS = ("by", "distance_mm", "angle_deg", "focus", "symbologies", "banks", "test")


def save_calibration(ctx: Context, s: Session, source: str) -> Dict[str, Any]:
    try:
        text = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
        answers = json.loads(text)
    except (OSError, ValueError) as e:
        raise CliError("calibration answers: %s" % e) from None
    if not isinstance(answers, dict):
        raise CliError("calibration answers must be a JSON object")
    s.cmd("SAVE")
    b = backup(ctx, s.cfg.name, session=s)
    test = answers.get("test") if isinstance(answers.get("test"), dict) else None
    doc = {"scanner": s.cfg.name, "model": s.info.get("model"), "serial": s.cfg.serial or None,
           "firmware": s.info.get("firmware"), "at": utc_now_text()}
    for k in CALIB_FIELDS:
        doc[k] = answers.get(k)
    doc["test"] = test
    doc["passed"] = bool(test and test.get("passed") is True)
    doc["backup"] = os.path.basename(b["file"])
    d = ctx.dir_of(s.cfg.name)
    path = os.path.join(d, "calibration.json")
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        prev = os.path.join(d, "calibration-%s.json" % re.sub(r"[^0-9TZ]", "", str(old.get("at") or utc_stamp())))
        os.replace(path, prev)
    except (OSError, ValueError):
        pass
    _atomic_json(path, doc)
    return {"saved": True, "calibration": path, "backup": b["file"], "passed": doc["passed"]}


def calibration_history(ctx: Context, name: str) -> List[str]:
    return sorted(glob.glob(os.path.join(st.scanner_dir(ctx.config_dir), name, "calibration-*.json")))


def latest_image(ctx: Context, s: Session) -> Dict[str, Any]:
    assert s.client is not None
    try:
        names = s.client.list_images()
        if not names:
            return {"image": None, "note": "no images on the reader"}
        name = sorted(names)[-1]
        d = os.path.join(ctx.dir_of(s.cfg.name), "images")
        os.makedirs(d, exist_ok=True)
        return {"image": s.client.get_image(name, d)}
    except Exception as e:
        raise CliError("image over FTP failed: %s" % e) from None


# ---------------------------------------------------------------------------
# Text wizard (--scanner-calibrate): the same steps as MillWright's
# ---------------------------------------------------------------------------
def calibrate(ctx: Context, name: str, ask: Callable[[str], str] = input,
              say: Callable[[str], None] = print) -> Dict[str, Any]:
    def yes(q: str) -> bool:
        return ask(q + " [y/N] ").strip().lower() in ("y", "yes")

    pid = os.getpid()
    run = lambda action, *a: step(ctx, name, action, list(a), "cli", pid)   # noqa: E731
    lock = SetupLock(ctx.config_dir, name, "cli", pid)
    lock.acquire()
    answers: Dict[str, Any] = {"by": None, "distance_mm": None, "angle_deg": None,
                               "focus": {"mode": None, "position": None}, "symbologies": None,
                               "banks": [], "test": None}
    try:
        wait_for_handover(ctx.config_dir, name, sleep=ctx.sleep)
        r = run("identify")
        say("1  Reader %s at %s:%d, firmware %s, MAC %s (enrolled: match)"
            % (r["model"], r["host"], r["port"], r["firmware"], r["mac"]))
        net = run("network")
        say("2  Network: %s/%s gateway %s, SNTP %s" % (net["ip"], net["mask_bits"], net["gateway"], net["sntp"]))
        if yes("   Set the reader's clock to this PC's (UTC)?"):
            run("time-sync")
        say("3  Mounting: tilt the reader 10-15 degrees on glossy PCBs to avoid glare.")
        answers["distance_mm"] = _num(ask("   Distance to the label in mm (empty = skip): "))
        answers["angle_deg"] = _num(ask("   Angle in degrees (empty = skip): "))
        say("4  Live view: open http://%s in Chromium (SR Web Tool)." % net["ip"])
        if yes("   Run autofocus now?"):
            f = run("focus")
            say("   Focus %s" % ("succeeded" if f["succeeded"] else "FAILED"))
            answers["focus"] = {"mode": "auto" if f["succeeded"] else None, "position": None}
        codes = run("codes", "1")
        say("5  Code types (bank 1): %s" % ", ".join(codes["types"] or ["?"]))
        t = ask("   Code types to enable, comma list (e.g. datamatrix,qr; empty = keep): ").strip()
        if t:
            codes = run("codes-set", "1", t)
        answers["symbologies"] = codes["types"]
        while True:
            b = ask("6  Tune which bank (1-16, empty = done)? ").strip()
            if not b:
                break
            say("   Present the label and keep it still...")
            r = run("tune", b)
            say("   Tuning %s in %d ms%s%s" % ("succeeded" if r["succeeded"] else "FAILED", r["ms"],
                                             ("; " + r["advice"]) if r["advice"] else "",
                                             ("; " + r["reason"]) if r["reason"] else ""))
            label = ask("   Name for bank %s (empty = none): " % b).strip()
            if label:
                run("bank-name", b, label)
            answers["banks"].append({"bank": int(b), "name": label or None})
        n = ask("7  Reading-rate test: how many triggers (default 100)? ").strip() or "100"
        r = run("rate-test", n)
        say("   Reading rate %.1f%%, matching level %d (threshold %s): %s"
            % (r["reading_rate"], r["matching_level"], r["matching_threshold"],
               "PASS" if r["passed"] else "FAIL"))
        for h in r["hints"]:
            say("   - " + h)
        answers["test"] = {k: r[k] for k in ("triggers", "reading_rate", "matching_level", "read_ms", "passed")}
        o = run("output-check")
        if o["differences"]:
            say("8  Output format differs from what the agent expects:")
            for d in o["differences"]:
                say("   %s %s: %s -> %s (%s)" % (d["kind"], d["number"], d["have"], d["want"], d["what"]))
            if yes("   Fix it?"):
                run("output-fix")
        else:
            say("8  Output format: as the agent expects.")
        r = run("trigger-test")
        say("9  Test trigger: %s" % r["result"])
        answers["by"] = ask("10 Your name (empty = not recorded): ").strip() or None
        if not yes("   Save to the reader's flash and write calibration.json?"):
            say("   Not saved (unsaved changes are lost when the reader restarts).")
            return {"scanner": name, "saved": False}
        tmp = os.path.join(ctx.dir_of(name), ".answers-%d.json" % pid)
        _atomic_json(tmp, answers)
        try:
            r = run("save", tmp)
        finally:
            os.remove(tmp)
        say("   Saved. Calibration %s; backup %s" % ("PASSED" if r["passed"] else "not passed", r["backup"]))
        return {"scanner": name, **r}
    finally:
        lock.release()


def _num(text: str) -> Optional[float]:
    text = text.strip()
    if not text:
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return int(v) if v == int(v) else v


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def emit(doc: Dict[str, Any], as_json: bool, out=None) -> None:
    out = out or sys.stdout
    if as_json:
        out.write(json.dumps(doc, sort_keys=True) + "\n")
        out.flush()
        return
    for k, v in doc.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, sort_keys=True)
        out.write("  %-14s: %s\n" % (k, "-" if v is None else v))


def emit_test_line(doc: Dict[str, Any], as_json: bool, out=None) -> None:
    out = out or sys.stdout
    if as_json:
        out.write(json.dumps(doc, sort_keys=True) + "\n")
    elif doc["type"] == "read":
        extra = {"scan": doc.get("code"), "no_read": "NO READ",
                 "reject": "rejected (%s)" % doc.get("reason")}[doc["result"]]
        out.write("%4d  %s\n" % (doc["n"], extra))
    elif doc["type"] == "timeout":
        out.write("%4d  nothing read within the time limit; stopped\n" % doc["n"])
    else:
        out.write("reads %d, no-reads %d, rejected %d%s\n" % (
            doc["reads"], doc["no_reads"], doc["rejected"],
            "" if doc["no_read_rate"] is None else " (no-read rate %.1f%%)" % doc["no_read_rate"]))
    out.flush()
