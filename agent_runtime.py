#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_runtime.py  —  running alongside LinuxCNC
================================================
The agent runs as a per-user service from login and simply waits for
LinuxCNC: it attaches when LinuxCNC starts — from the desktop icon, a
terminal, or the configuration picker — and lets go when it exits. Nothing
in LinuxCNC's own files is modified, so LinuxCNC updates cannot break it.

  LinuxCNCWatch      — is LinuxCNC running right now? (looks for its server
                       processes in /proc; cheap enough to call every tick)
  find_config()      — where config.yaml lives; creates the per-user copy
  single_instance()  — stop a second agent (e.g. a manual run while the
                       service is active) from double-counting parts
"""

import errno
import os
import shutil
import time
from typing import Iterable, Optional

# LinuxCNC's task server processes (/proc/<pid>/comm, max 15 chars).
LINUXCNC_PROCS = ("linuxcncsvr", "milltask")

USER_CONFIG_DIR = os.path.join(os.path.expanduser("~"), "linuxcnc-monitor-agent")
USER_CONFIG = os.path.join(USER_CONFIG_DIR, "config.yaml")
SYSTEM_CONFIG = "/etc/linuxcnc-status-agent/config.yaml"
_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = (
    "/usr/share/linuxcnc-status-agent/config.example.yaml",   # .deb install
    os.path.join(_HERE, "config.example.yaml"),               # git checkout
)


def _comm(pid: str, proc_root: str = "/proc") -> str:
    try:
        with open(os.path.join(proc_root, pid, "comm"), "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def find_linuxcnc_pid(proc_root: str = "/proc",
                      names: Iterable[str] = LINUXCNC_PROCS) -> Optional[int]:
    wanted = set(names)
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for pid in entries:
        if pid.isdigit() and _comm(pid, proc_root) in wanted:
            return int(pid)
    return None


class LinuxCNCWatch:
    """alive() is O(1) while the known PID lives; rescans /proc at most once
    per `rescan_s` otherwise."""

    def __init__(self, proc_root: str = "/proc", rescan_s: float = 1.0) -> None:
        self._root = proc_root
        self._rescan_s = rescan_s
        self._pid: Optional[int] = None
        self._last_scan = 0.0
        self.started_at: Optional[float] = None   # monotonic time it was first seen

    def alive(self) -> bool:
        if self._pid is not None and _comm(str(self._pid), self._root) in LINUXCNC_PROCS:
            return True
        self._pid = None
        now = time.monotonic()
        if now - self._last_scan >= self._rescan_s:
            self._last_scan = now
            self._pid = find_linuxcnc_pid(self._root)
            if self._pid is not None:
                self.started_at = now
        return self._pid is not None


def find_config(explicit: Optional[str] = None, create: bool = True) -> Optional[str]:
    """--config path, else ~/linuxcnc-monitor-agent/config.yaml, else
    /etc/linuxcnc-status-agent/config.yaml, else config.yaml beside the script.
    With create=True and nothing found, the per-user file is created from the
    template so there is always one obvious file to edit."""
    if explicit:
        return explicit
    for path in (USER_CONFIG, SYSTEM_CONFIG, os.path.join(_HERE, "config.yaml")):
        if os.path.isfile(path):
            return path
    if create:
        for tpl in TEMPLATES:
            if os.path.isfile(tpl):
                try:
                    os.makedirs(USER_CONFIG_DIR, exist_ok=True)
                    shutil.copyfile(tpl, USER_CONFIG)
                    return USER_CONFIG
                except OSError:
                    break
    return None


def lock_path() -> str:
    run_dir = os.environ.get("XDG_RUNTIME_DIR")
    if run_dir and os.path.isdir(run_dir):
        return os.path.join(run_dir, "lcnc-status-agent.lock")
    return f"/tmp/lcnc-status-agent-{os.getuid()}.lock"


def single_instance(path: Optional[str] = None):
    """Return an open, locked file object (keep a reference!) or None if
    another agent already holds the lock."""
    import fcntl
    f = open(path or lock_path(), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        f.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f
