#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""Packet signing with a site key (PROTOCOL.md §8.1, review F07).

One shared secret per plant (``site_key`` in config.yaml, the same text in
FloorWatch). Every datagram stays a plain JSON object; a signed one ends with
a ``"sig"`` member holding HMAC-SHA256 over the same JSON without it:

    unsigned  {"type":"status",...,"packet_seq":7}
    signed    {"type":"status",...,"packet_seq":7,"sig":"<64 hex digits>"}
    sig       = HMAC-SHA256(key = site_key as UTF-8,
                            msg = b"lcnc-sig-v1\\n" + <the unsigned bytes>)

The receiver cuts the ``,"sig":"..."`` text off the end and checks the HMAC
over exactly what was sent, so no JSON re-encoding is involved. A dashboard
without a key ignores the extra member.

This proves a packet came from something that knows the site key and was not
changed. It does not hide anything (packets are not encrypted), and it does not
tell one machine from another: anyone with the key can claim any name.
Replays are stopped by the stream order (session_id / packet_seq) for status
packets and by a nonce for history requests.
"""
import hashlib
import hmac
import re
from typing import Optional

DOMAIN = b"lcnc-sig-v1\n"
# Bytes a signature adds: ,"sig":"<64 hex>"  — G-code chunks leave room for it.
SIG_RESERVE = 80
MIN_KEY_CHARS = 16

_SUFFIX = re.compile(rb'(,?)"sig":"([0-9a-f]{64})"\}\s*\Z')


def key_bytes(site_key: Optional[str]) -> Optional[bytes]:
    """The HMAC key for a configured ``site_key``; None when signing is off."""
    key = (site_key or "").strip()
    return key.encode("utf-8") if key else None


def _mac(key: bytes, payload: bytes) -> str:
    return hmac.new(key, DOMAIN + payload, hashlib.sha256).hexdigest()


def sign(payload: bytes, key: Optional[bytes]) -> bytes:
    """``payload`` (a JSON object) with its signature appended; unchanged without a key."""
    if not key:
        return payload
    body = payload.rstrip()
    if not body.endswith(b"}"):
        raise ValueError("not a JSON object")
    head = body[:-1]
    sep = b"" if head.rstrip().endswith(b"{") else b","
    return head + sep + b'"sig":"' + _mac(key, body).encode("ascii") + b'"}'


def verify(raw: bytes, key: bytes) -> str:
    """"ok", "unsigned" (no signature) or "bad" (wrong key, or changed in transit)."""
    m = _SUFFIX.search(raw)
    if m is None:
        return "unsigned"
    payload = raw[:m.start()] + b"}"
    return "ok" if hmac.compare_digest(_mac(key, payload), m.group(2).decode("ascii")) else "bad"
