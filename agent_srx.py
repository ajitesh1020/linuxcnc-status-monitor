#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_srx.py  —  client for a Keyence SR-X code reader on Ethernet (TCP)
=========================================================================
docs/SCANNER_KEYENCE_SRX_WORK.md, package 1. Standard library only, no
threads inside: the caller owns the thread (agent_scan_tcp.py, the CLI).

One TCP socket (port 9004) carries both commands and read data. The reader
answers a command with one line

    OK,<cmd>[,<field>...]       or      ER,<cmd>,<code>

where <cmd> is the command name just sent (the part before the first comma,
e.g. "TUNE" for "TUNE,01"). Every other line is read data (a code, the
no-read string, a tuning or test result). That rule lives in classify_line()
and nowhere else.

What is known (SR-X User's Manual, chapter 14 "Commands"; package 0, the
spike with a real reader, confirms it; work document section 7):
  * replies end in CR (commands may end in CR or CR LF); read data ends in the
    configured terminator, CR by default; no header by default.
  * `LON`, `LOFF`, `PRON`, `PROFF` get NO reply unless the reader's "Basic
    command response string" is set: command() returns at once for them
    (NO_REPLY_COMMANDS), and a stray "OK,LON" / "ER,LON,nn" line is dropped,
    never taken for read data.
  * `TUNE`, `TQUIT`, `RESET`, `BCLR` (and maybe `QUIT`) answer a bare "OK" or
    "ER" (BARE_REPLY_COMMANDS); "OK,<cmd>" is accepted for them too.
  * `KEYENCE` -> `OK,KEYENCE,<model>,<version>`; `EMAC` -> `OK,EMAC,<MAC>`.
  * the manual has no serial-number command. SERIAL_COMMAND stays None and
    info()["serial"] is None (never invented); the MAC address is returned
    as "mac".
  * no-read string "ERROR" by default (up to 32 characters, or none); the
    AIM symbology identifier, when enabled, comes before the data ("]d1...").

Use the comma forms of bank commands ("LON,01", "TUNE,01"), not "LON01".

Bounds: a line is at most MAX_LINE bytes plus its terminator (longer lines
are dropped and counted in .overlong); at most MAX_QUEUED read-data lines wait
for read_line() (older ones are dropped and counted in .dropped); every
socket call has a timeout; TCP keepalive is on.
"""
from __future__ import annotations

import collections
import ftplib
import logging
import os
import socket
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9004
MAX_LINE = 4096
MAX_QUEUED = 256
RECV_CHUNK = 4096
TERMINATORS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}

INFO_COMMAND = "KEYENCE"          # OK,KEYENCE,<model>,<version>
SERIAL_COMMAND: Optional[str] = None   # none in the manual
MAC_COMMAND = "EMAC"              # OK,EMAC,<12 hex digits>
NO_REPLY_COMMANDS = frozenset({"LON", "LOFF", "PRON", "PROFF"})
BARE_REPLY_COMMANDS = frozenset({"TUNE", "TQUIT", "RESET", "BCLR", "QUIT"})

ERROR_TEXT = {
    "00": "undefined command", "01": "wrong number of parameters",
    "02": "parameter out of range", "03": "parameter out of range",
    "20": "command not executable in the current state", "21": "buffer overflow",
    "22": "load/save error", "04": "parameter not in hex", "05": "hex parameter out of range",
    "10": "preset data incorrect", "11": "area data incorrect", "12": "file does not exist",
    "13": "bank out of range", "14": "communication check failed",
    "23": "not allowed while AutoID Network Navigator is connected",
    "99": "other error",
}


class SrxError(Exception):
    """Base for this module's errors."""


class SrxConnectionError(SrxError):
    """Not connected, connection refused or lost, or the peer closed it."""


class SrxTimeout(SrxError):
    """No reply to a command within its timeout (the connection stays open)."""


class SrxReply:
    """One reply line: ok, cmd, fields (list of str), error_code (str or None)."""
    __slots__ = ("ok", "cmd", "fields", "error_code")

    def __init__(self, ok: bool, cmd: str, fields: Optional[List[str]] = None,
                 error_code: Optional[str] = None):
        self.ok = ok
        self.cmd = cmd
        self.fields = list(fields or [])
        self.error_code = error_code

    @property
    def error_text(self) -> Optional[str]:
        if self.error_code is None:
            return None
        return ERROR_TEXT.get(self.error_code, "error " + self.error_code)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, SrxReply) and (self.ok, self.cmd, self.fields, self.error_code) == (
            other.ok, other.cmd, other.fields, other.error_code)

    def __repr__(self) -> str:
        return "SrxReply(ok=%r, cmd=%r, fields=%r, error_code=%r)" % (
            self.ok, self.cmd, self.fields, self.error_code)


def command_name(cmd: str) -> str:
    """'TUNE,01' -> 'TUNE'; 'LON' -> 'LON'."""
    return cmd.split(",", 1)[0].strip()


def classify_line(line: bytes, pending: Optional[str]) -> Tuple[str, Any]:
    """The one rule that tells replies from read data.

    line: one line without its terminator. pending: the name of the command
    waiting for its reply (command_name()), or None. Returns
      ("reply", SrxReply)  OK,<pending>[,...] or ER,<pending>,<code>; for a
                           BARE_REPLY_COMMANDS pending also a bare OK / ER;
      ("stray", SrxReply)  OK,<cmd> / ER,<cmd>,<code> for a NO_REPLY_COMMANDS
                           command (the reader's optional "basic command
                           response"): never read data, dropped by callers;
      ("data", line)       everything else.
    Spaces after commas are tolerated (the manuals print "OK, LON").
    """
    if pending in BARE_REPLY_COMMANDS and line in (b"OK", b"ER"):
        return "reply", SrxReply(line == b"OK", pending)
    if line[:3] in (b"OK,", b"ER,"):
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            return "data", line
        parts = [p.strip() for p in text.split(",")]
        name = parts[1]
        if name and (name == pending or name in NO_REPLY_COMMANDS):
            if parts[0] == "OK":
                r = SrxReply(True, name, parts[2:])
            else:
                code = parts[2] if len(parts) >= 3 and parts[2] else None
                r = SrxReply(False, name, parts[3:], code)
            return ("reply" if name == pending else "stray"), r
    return "data", line


def parse_endpoint(url: str) -> Tuple[str, int]:
    """'tcp://192.168.100.100:9004' (port optional) -> (host, port)."""
    rest = url[6:] if url.startswith("tcp://") else url
    rest = rest.rstrip("/")
    host, sep, port = rest.rpartition(":")
    if not sep:
        host, port = rest, str(DEFAULT_PORT)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("expected tcp://<host>:<port>, got %r" % url)
    return host, int(port)


class SrxClient:
    """One reader, one socket. Not thread-safe: one thread uses an instance."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 3.0,
                 terminator: str = "cr", max_line: int = MAX_LINE):
        if terminator not in TERMINATORS:
            raise ValueError("terminator must be one of %s" % ", ".join(sorted(TERMINATORS)))
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.max_line = int(max_line)
        self._term = TERMINATORS[terminator]
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._skipping = False           # inside an overlong line, up to its terminator
        self._data: Deque[bytes] = collections.deque()
        self._late: Deque[str] = collections.deque(maxlen=8)   # timed-out commands: their reply may still come
        self.overlong = 0                # lines dropped for length
        self.dropped = 0                 # read-data lines dropped because the queue was full
        self.partial = 0                 # half lines lost to a disconnect

    # -- connection -----------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise SrxConnectionError("cannot connect to %s:%d: %s" % (self.host, self.port, e)) from None
        _keepalive(sock)
        sock.settimeout(self.timeout)
        self._sock = sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if self._buf:
            self.partial += 1
        self._buf = bytearray()
        self._skipping = False
        self._late.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "SrxClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- commands and data ---------------------------------------------

    def command(self, cmd: str, timeout: Optional[float] = None) -> SrxReply:
        """Send cmd (CR is added) and wait for its OK/ER reply. Read data that
        arrives meanwhile is queued for read_line(). Raises SrxTimeout (no
        reply in time) or SrxConnectionError."""
        if not cmd or any(c in cmd for c in "\r\n") or not cmd.isascii():
            raise ValueError("bad command %r" % cmd)
        name = command_name(cmd)
        self._send(cmd.encode("ascii") + b"\r")
        if name in NO_REPLY_COMMANDS:
            return SrxReply(True, name)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            reply = None
            for line in self._take_lines():
                kind, val = classify_line(line, None if reply else name)
                if kind == "reply":
                    reply = val
                elif kind == "data" and not self._is_late_reply(line):
                    self._queue(val)      # incl. data that came in the same read after the reply
            if reply is not None:
                return reply
            left = deadline - time.monotonic()
            if left <= 0:
                self._late.append(name)
                raise SrxTimeout("no reply to %s within the timeout" % name)
            self._recv(left)

    def read_line(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """The next pushed read-data line (without terminator), or None when
        none arrives within timeout. Raises SrxConnectionError when the
        connection is lost (queued lines are still returned first)."""
        if self._data:
            return self._data.popleft()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            for line in self._take_lines():
                if classify_line(line, None)[0] == "data" and not self._is_late_reply(line):
                    self._queue(line)
            if self._data:
                return self._data.popleft()
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._recv(left)

    def info(self) -> Dict[str, Optional[str]]:
        """{"model", "firmware", "serial", "mac"}: what the reader reports,
        None where it reports nothing (serial until SERIAL_COMMAND is known)."""
        out: Dict[str, Optional[str]] = {"model": None, "firmware": None, "serial": None, "mac": None}
        r = self.command(INFO_COMMAND)
        if r.ok:
            out["model"] = r.fields[0] or None if r.fields else None
            out["firmware"] = r.fields[1] or None if len(r.fields) > 1 else None
        if SERIAL_COMMAND:
            r = self.command(SERIAL_COMMAND)
            if r.ok and r.fields and r.fields[0]:
                out["serial"] = r.fields[0]
        r = self.command(MAC_COMMAND)
        if r.ok and r.fields and r.fields[0]:
            out["mac"] = r.fields[0]
        return out

    # -- internals ------------------------------------------------------

    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise SrxConnectionError("not connected")
        try:
            self._sock.sendall(data)
        except socket.timeout:
            self.close()
            raise SrxConnectionError("send timed out") from None
        except OSError as e:
            self.close()
            raise SrxConnectionError("send failed: %s" % e) from None

    def _recv(self, timeout: float) -> None:
        if self._sock is None:
            raise SrxConnectionError("not connected")
        self._sock.settimeout(max(0.001, min(timeout, self.timeout)))
        try:
            chunk = self._sock.recv(RECV_CHUNK)
        except socket.timeout:
            return
        except OSError as e:
            self.close()
            raise SrxConnectionError("connection lost: %s" % e) from None
        if not chunk:
            self.close()
            raise SrxConnectionError("reader closed the connection")
        self._buf += chunk

    def _take_lines(self) -> List[bytes]:
        """Split complete lines off the buffer, enforcing the length bound."""
        lines: List[bytes] = []
        term, n = self._term, len(self._term)
        while True:
            i = self._buf.find(term)
            if i < 0:
                break
            line = bytes(self._buf[:i])
            del self._buf[:i + n]
            if self._skipping:
                self._skipping = False
                continue
            if len(line) > self.max_line:
                self.overlong += 1
                logger.warning("srx %s: dropped a %d-byte line (limit %d)", self.host, len(line), self.max_line)
                continue
            lines.append(line)
        # an unterminated line already past the limit: drop what we have and
        # skip the rest of it up to its terminator (keep n-1 bytes: a CRLF
        # terminator may be split across reads)
        if len(self._buf) > self.max_line + n:
            if not self._skipping:
                self.overlong += 1
                logger.warning("srx %s: dropped an overlong line (limit %d)", self.host, self.max_line)
            self._skipping = True
            keep = n - 1
            self._buf = self._buf[len(self._buf) - keep:] if keep else bytearray()
        return lines

    def _is_late_reply(self, line: bytes) -> bool:
        """A reply to a command that already timed out: dropped, never read data."""
        for i, name in enumerate(self._late):
            if classify_line(line, name)[0] == "reply":
                del self._late[i]
                logger.info("srx %s: late reply to %s dropped", self.host, name)
                return True
        return False

    def _queue(self, line: bytes) -> None:
        if len(self._data) >= MAX_QUEUED:
            self._data.popleft()
            self.dropped += 1
            logger.warning("srx %s: read-data queue full, dropped the oldest line", self.host)
        self._data.append(line)

    # -- FTP (images, history) -----------------------------------------

    def _ftp(self, user: str, password: str, timeout: Optional[float]) -> ftplib.FTP:
        ftp = ftplib.FTP(timeout=self.timeout if timeout is None else timeout)
        try:
            ftp.connect(self.host, 21)
            ftp.login(user, password)
        except (OSError, ftplib.Error):
            try:
                ftp.close()
            except OSError:
                pass
            raise
        return ftp

    def list_images(self, folder: str = "IMAGE", user: str = "", password: str = "",
                    timeout: Optional[float] = None) -> List[str]:
        """File names in the reader's image folder (folder and login per package 0)."""
        ftp = self._ftp(user, password, timeout)
        try:
            ftp.cwd(folder)
            names = ftp.nlst()
        except ftplib.error_perm as e:
            if str(e).startswith("550"):     # empty folder on some servers
                return []
            raise
        finally:
            _ftp_quit(ftp)
        return [os.path.basename(n) for n in names if n not in (".", "..")]

    def get_image(self, name: str, dest: str, folder: str = "IMAGE", user: str = "",
                  password: str = "", timeout: Optional[float] = None,
                  max_bytes: int = 64 * 1024 * 1024) -> str:
        """Download one image to dest (a file path, or a folder to put it in).
        Atomic: written to a temp file and renamed. Returns the path."""
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError("bad image name %r" % name)
        path = os.path.join(dest, name) if os.path.isdir(dest) else dest
        tmp = path + ".part"
        got = [0]
        ftp = self._ftp(user, password, timeout)
        try:
            ftp.cwd(folder)
            with open(tmp, "wb") as f:
                def write(block: bytes) -> None:
                    got[0] += len(block)
                    if got[0] > max_bytes:
                        raise SrxError("image larger than %d bytes" % max_bytes)
                    f.write(block)
                ftp.retrbinary("RETR " + name, write)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        finally:
            _ftp_quit(ftp)
        return path


def _ftp_quit(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except (OSError, ftplib.Error, EOFError):
        try:
            ftp.close()
        except OSError:
            pass


def _keepalive(sock: socket.socket) -> None:
    """Keepalive on: a pulled cable shows up as an error within ~1 min even
    when nothing is being sent."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, opt):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            except OSError:
                pass
