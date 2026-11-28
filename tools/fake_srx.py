#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
tools/fake_srx.py  —  a fake Keyence SR-X reader on TCP, for tests
===================================================================
docs/SCANNER_KEYENCE_SRX_WORK.md, package 1. It behaves like the reader as
far as the SR-X User's Manual (chapter 14) says; package 0 (the spike with
the real reader) corrects it. Replies end in CR; read data in `terminator`
("cr" default, "lf", "crlf").

Commands:
  KEYENCE        OK,KEYENCE,<model>,<firmware>
  EMAC           OK,EMAC,<mac>
  <serial_command>  OK,<cmd>,<serial>  only when set (the manual has none)
  LON / LOFF     no reply (basic_response=True: OK,LON / OK,LOFF); LON then
                 sends one read-data line: the next scripted code, or the
                 no-read string when the script is empty
  TUNE,bb        OK, then after tune_s tune_result (bb 01-16, else ER)
  TQUIT          OK, cancels a pending tuning result
  FTUNE          OK,FTUNE then after tune_s ftune_result
  TEST1 / TEST2  OK,TEST1 then test_line every test_interval_s
                 (manual: "<data>:<bank>:<rate>%:<matching level>" per 10 decodes)
  QUIT           quit_reply ("OK,QUIT" per the manual's table)
  RP/WP, RB/WB, RC/WC   read / write settings; unknown number -> ER,RP,02
  SAVE / LOAD    OK,SAVE / OK,LOAD (saved settings kept over RESET)
  DFLT           OK,DFLT, settings back to factory defaults (not saved)
  RESET          OK, then drops every connection; unsaved settings lost
  BCLR           OK
  ERRSTAT        OK,ERRSTAT,none
  TMSET,t / TMGET  OK,TMSET / OK,TMGET,<YYYYMMDDhhmmss>
  anything else  ER,<cmd>,00

Fault modes (attributes; change them at any time):
  reply_delay_s         wait before every reply (slow replies)
  split_lines           send every line in two halves, split_delay_s apart
  disconnect_mid_line   the next line sent is cut in half and the connection closed
  second_client         "allow" | "refuse" (close at once) | "kick" (close the old one)
  silent                accept commands but never answer

From a test:  r = FakeSrx(codes=["ABC123"]); r.start(); ... r.port ...; r.stop()
              r.push(b"XYZ")   a read the reader made by itself (trigger: reader)
By hand:      python3 tools/fake_srx.py --port 9004 --codes ABC123,DEF456
"""
from __future__ import annotations

import argparse
import collections
import socket
import threading
import time
from typing import Deque, Dict, Iterable, List, Optional

TERMINATORS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}

DEFAULT_SETTINGS = {
    "RP": {"101": "1", "102": "0", "200": "ERROR", "300": "0"},
    "RB": {"01100": "0010"},
    "RC": {"0100": "1000"},
}
WRITE_TO_READ = {"WP": "RP", "WB": "RB", "WC": "RC"}


class FakeSrx:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, codes: Iterable[str] = (),
                 model: str = "SR-X100", firmware: str = "1.00", mac: str = "0001FC000001",
                 serial: str = "FAKE0001", serial_command: Optional[str] = None,
                 no_read: str = "ERROR", terminator: str = "cr"):
        self.host = host
        self.model, self.firmware, self.mac, self.serial = model, firmware, mac, serial
        self.serial_command = serial_command
        self.no_read = no_read
        self.term = TERMINATORS[terminator]
        self.codes: Deque[str] = collections.deque(codes)
        self.tune_s = 0.05
        self.tune_result = "Tuning SUCCEEDED,850ms,00000000"
        self.ftune_result = "Focus Tuning SUCCEEDED"
        self.test_interval_s = 0.05
        self.test_line = "ABC123:01:100%:92"
        self.quit_reply = "OK,QUIT"
        self.basic_response = False
        self.clock = "20261128120000"
        self.reply_delay_s = 0.0
        self.split_lines = False
        self.split_delay_s = 0.02
        self.disconnect_mid_line = False
        self.second_client = "allow"
        self.silent = False
        self.saved: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
        self.settings: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in self.saved.items()}
        self.received: List[str] = []          # every command, in order
        self.connections = 0                   # accepted so far
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()     # timers and the client thread share sockets
        self._clients: List[socket.socket] = []
        self._tune_gen = 0
        self._testing: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self.port = self._srv.getsockname()[1]
        self._threads: List[threading.Thread] = []

    # -- control --------------------------------------------------------

    def start(self) -> "FakeSrx":
        self._srv.listen(8)
        self._srv.settimeout(0.1)
        t = threading.Thread(target=self._accept_loop, name="fake-srx-accept", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self.drop_all()
        try:
            self._srv.close()
        except OSError:
            pass
        for t in self._threads:
            t.join(2)

    def __enter__(self) -> "FakeSrx":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def drop_all(self) -> None:
        """Close every connection (a reboot or a pulled cable, seen from the PC)."""
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            _close(c)

    def push(self, line: bytes) -> None:
        """A read the reader made by itself, sent to every client."""
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            self._send_line(c, line)

    def wait_clients(self, n: int, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.client_count == n:
                return True
            time.sleep(0.005)
        return self.client_count == n

    # -- server ---------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self.connections += 1
                if self._clients and self.second_client == "refuse":
                    _close(conn)
                    continue
                if self._clients and self.second_client == "kick":
                    old, self._clients = self._clients, []
                    for c in old:
                        _close(c)
                self._clients.append(conn)
            t = threading.Thread(target=self._serve, args=(conn,), name="fake-srx-client", daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        conn.settimeout(0.1)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    with self._lock:
                        if conn not in self._clients:
                            return
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\r" in buf:
                    raw, buf = buf.split(b"\r", 1)
                    cmd = raw.strip(b"\n").decode("ascii", "replace")
                    if cmd:
                        self.received.append(cmd)
                        if not self._handle(conn, cmd):
                            return
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
                if self._testing is conn:
                    self._testing = None
            _close(conn)

    def _reply(self, conn: socket.socket, text: str) -> None:
        if self.reply_delay_s:
            time.sleep(self.reply_delay_s)
        self._send_line(conn, text.encode("ascii"))

    def _send_line(self, conn: socket.socket, line: bytes) -> None:
        data = line + self.term
        with self._send_lock:
            self._send_locked(conn, data)

    def _send_locked(self, conn: socket.socket, data: bytes) -> None:
        try:
            if self.disconnect_mid_line:
                self.disconnect_mid_line = False
                conn.sendall(data[:max(1, len(data) // 2)])
                with self._lock:
                    if conn in self._clients:
                        self._clients.remove(conn)
                _close(conn)
                return
            if self.split_lines and len(data) > 1:
                half = len(data) // 2
                conn.sendall(data[:half])
                time.sleep(self.split_delay_s)
                conn.sendall(data[half:])
            else:
                conn.sendall(data)
        except OSError:
            pass

    def _later(self, delay: float, fn) -> None:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()

    def _handle(self, conn: socket.socket, cmd: str) -> bool:
        """Answer one command. False: the connection is gone."""
        if self.silent:
            return True
        parts = [p.strip() for p in cmd.split(",")]
        name, args = parts[0], parts[1:]
        if name == "KEYENCE":
            self._reply(conn, "OK,KEYENCE,%s,%s" % (self.model, self.firmware))
        elif name == "EMAC":
            self._reply(conn, "OK,EMAC,%s" % self.mac)
        elif self.serial_command and name == self.serial_command:
            self._reply(conn, "OK,%s,%s" % (name, self.serial))
        elif name == "LON":
            if self.basic_response:
                self._reply(conn, "OK,LON")
            code = self.codes.popleft() if self.codes else self.no_read
            self._send_line(conn, code.encode("utf-8"))
        elif name == "LOFF":
            if self.basic_response:
                self._reply(conn, "OK,LOFF")
        elif name == "TUNE":
            if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= 16:
                self._reply(conn, "ER,TUNE,02")
                return True
            self._reply(conn, "OK")
            self._delayed_result(conn, self.tune_result)
        elif name == "FTUNE":
            self._reply(conn, "OK,FTUNE")
            self._delayed_result(conn, self.ftune_result)
        elif name == "TQUIT":
            with self._lock:
                self._tune_gen += 1
            self._reply(conn, "OK")
        elif name in ("TEST1", "TEST2"):
            self._reply(conn, "OK,%s" % name)
            with self._lock:
                self._testing = conn
            self._later(self.test_interval_s, lambda: self._test_tick(conn))
        elif name == "QUIT":
            with self._lock:
                self._testing = None
            self._reply(conn, self.quit_reply)
        elif name in ("RP", "RB", "RC"):
            store = self.settings[name]
            if len(args) != 1 or args[0] not in store:
                self._reply(conn, "ER,%s,02" % name)
            else:
                self._reply(conn, "OK,%s,%s" % (name, store[args[0]]))
        elif name in WRITE_TO_READ:
            store = self.settings[WRITE_TO_READ[name]]
            if len(args) < 2 or args[0] not in store:
                self._reply(conn, "ER,%s,02" % name)
            else:
                store[args[0]] = ",".join(args[1:])
                self._reply(conn, "OK,%s" % name)
        elif name == "SAVE":
            self.saved = {k: dict(v) for k, v in self.settings.items()}
            self._reply(conn, "OK,SAVE")
        elif name == "LOAD":
            self.settings = {k: dict(v) for k, v in self.saved.items()}
            self._reply(conn, "OK,LOAD")
        elif name == "DFLT":
            self.settings = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
            self._reply(conn, "OK,DFLT")
        elif name == "ERRSTAT":
            self._reply(conn, "OK,ERRSTAT,none")
        elif name == "TMSET":
            if len(args) != 1 or len(args[0]) != 14 or not args[0].isdigit():
                self._reply(conn, "ER,TMSET,02")
            else:
                self.clock = args[0]
                self._reply(conn, "OK,TMSET")
        elif name == "TMGET":
            self._reply(conn, "OK,TMGET,%s" % self.clock)
        elif name == "RESET":
            self._reply(conn, "OK")
            self.settings = {k: dict(v) for k, v in self.saved.items()}
            self.drop_all()
            return False
        elif name == "BCLR":
            self._reply(conn, "OK")
        else:
            self._reply(conn, "ER,%s,00" % name)
        return True

    def _delayed_result(self, conn: socket.socket, text: str) -> None:
        with self._lock:
            self._tune_gen += 1
            gen = self._tune_gen

        def fire() -> None:
            with self._lock:
                if gen != self._tune_gen:
                    return              # TQUIT came first
            self._send_line(conn, text.encode("ascii"))
        self._later(self.tune_s, fire)

    def _test_tick(self, conn: socket.socket) -> None:
        with self._lock:
            if self._testing is not conn or self._stop.is_set():
                return
        self._send_line(conn, self.test_line.encode("ascii"))
        self._later(self.test_interval_s, lambda: self._test_tick(conn))


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake Keyence SR-X reader (tests, MillWright by hand)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9004)
    ap.add_argument("--codes", default="", help="comma list returned by LON, in order")
    ap.add_argument("--model", default="SR-X100")
    ap.add_argument("--second-client", choices=("allow", "refuse", "kick"), default="allow")
    a = ap.parse_args()
    r = FakeSrx(a.host, a.port, [c for c in a.codes.split(",") if c], model=a.model)
    r.second_client = a.second_client
    r.start()
    print("fake SR-X on %s:%d (Ctrl+C to stop)" % (a.host, r.port), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        r.stop()


if __name__ == "__main__":
    main()
