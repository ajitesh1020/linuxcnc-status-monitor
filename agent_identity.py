#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_identity.py  —  stable machine identity (MES plan P0.6)
=============================================================
`machine_uuid` names this machine for good: the dashboard keys a machine by it,
so two PCs with the same `machine_name` stay two machines, and a renamed PC
keeps its history. It is also the MES `machine_id`.

File — ~/linuxcnc-monitor-agent/identity.json (config key `machine_uuid_file`),
mode 0600, written once:

    {"machine_uuid": "8d7c6b5a-4f3e-4d2c-8b1a-0f9e8d7c6b5a",
     "created_at": "2026-10-22T09:15:02Z"}

* Made on the first start when the file is missing.
* A file that cannot be read or does not hold a valid id is kept as
  `identity.json.bad-<UTC time>` and a new one is made (with a warning).
* Never made again otherwise. Installing, upgrading or removing the package
  does not touch it.
"""

import json
import logging
import os
import re
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# uuid4, lowercase, with hyphens — the form str(uuid.uuid4()) gives.
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def default_path(user_dir: str) -> str:
    return os.path.join(user_dir, "identity.json")


def resolve_path(configured: str, user_dir: str) -> str:
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path(user_dir)


def _parse(raw: bytes) -> Optional[str]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    value = doc.get("machine_uuid") if isinstance(doc, dict) else None
    return value if isinstance(value, str) and UUID4_RE.match(value) else None


def read(path: str) -> Optional[str]:
    """The machine_uuid in `path`, or None if missing, unreadable or invalid.
    Never changes anything (used by --check)."""
    try:
        with open(path, "rb") as f:
            return _parse(f.read(4096))
    except OSError:
        return None


def _fsync_dir(directory: str) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_new(path: str) -> str:
    """Write a new identity file atomically. If another process made one in the
    meantime, that one wins and is returned."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    machine_uuid = str(uuid.uuid4())
    body = json.dumps({"machine_uuid": machine_uuid,
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (body + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(tmp, path)          # never replaces an existing file
        except FileExistsError:
            existing = read(path)
            if existing:
                return existing
            raise
        except OSError:
            os.replace(tmp, path)       # filesystem without hard links
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _fsync_dir(directory)
    return machine_uuid


def load_or_create(path: str) -> Optional[str]:
    """The machine_uuid, creating identity.json on first use. Returns None only
    if no identity file can be written (logged); packets then go without it."""
    if os.path.lexists(path):
        existing = read(path)
        if existing:
            return existing
        bad = "%s.bad-%s" % (path, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        try:
            os.replace(path, bad)
        except OSError as exc:
            logger.error("Machine identity %s is damaged and cannot be moved aside "
                         "(%s); packets are sent without machine_uuid.", path, exc)
            return None
        logger.warning("Machine identity %s was unreadable or damaged; kept as %s "
                       "and a new machine_uuid was made. The dashboard will see "
                       "this PC as a new machine.", path, bad)
    try:
        machine_uuid = _write_new(path)
    except OSError as exc:
        logger.error("Cannot write machine identity %s (%s); packets are sent "
                     "without machine_uuid.", path, exc)
        return None
    logger.info("Machine identity %s: %s", path, machine_uuid)
    return machine_uuid
