#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
agent_mes_forward.py  —  enrolment and forwarding to the site service (MES plan P2.5)
====================================================================================
The site service (FloorWatch `floorwatch_service`, API in FloorWatch
docs/MES_SERVICE_API.md) takes the events in the MES spool over mutual TLS.

Enrolment, once per machine (`lcnc-status-agent --mes-enroll URL CODE`):
  1. fetch the site CA without verification and STOP unless its SHA-256
     fingerprint starts with the code's last 16 hex digits — nothing secret is
     sent before this check;
  2. make a key and CSR with the `openssl` command (EC P-256);
  3. send code + CSR + machine_uuid over TLS that trusts only that CA;
  4. keep in ~/linuxcnc-monitor-agent/mes/ (0700; files 0600): client.key,
     client.crt, ca.crt, enrolment.json {service_url, site_id, machine_id,
     not_after}.

Forwarder (own thread, never the sampling loop): GET /v1/capabilities at start
and every hour; sends pending spool rows of the listed types, oldest first,
within the service's limits; marks rows acknowledged per receipt (all four
receipt statuses are final). 503, timeouts and broken connections mark nothing:
the same rows go again later (backoff 1 s -> 5 min, with jitter). The service
deduplicates, so a resend after a lost receipt answers "duplicate". A revoked
or expired certificate stops forwarding until the machine is enrolled again.

`status` (a dict replaced atomically) is what the status packet carries as
`mes_link`: {"state": ok|offline|not_enrolled|cert_expired|rejected,
"last_ok_ms": n} — last_ok_ms is the last 200 from the service, absent if never.
"""

import gzip
import hashlib
import http.client
import json
import logging
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from agent_identity import UUID4_RE

logger = logging.getLogger(__name__)

MES_DIRNAME = "mes"
KEY_FILE, CERT_FILE, CA_FILE, ENROLMENT_FILE = "client.key", "client.crt", "ca.crt", "enrolment.json"
TIMEOUT_S = 30.0
CAPS_EVERY_S = 3600.0
IDLE_POLL_S = 2.0
RECHECK_S = 60.0              # not enrolled / stopped: look at the files again this often
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 300.0
RECEIPTS = ("stored", "duplicate", "quarantined", "rejected")
# Used until the service has answered /v1/capabilities (its documented defaults).
DEFAULT_LIMITS = {"max_batch_events": 200, "max_batch_bytes": 262144, "max_event_bytes": 16384}
GZIP_FROM = 1024              # compress bodies bigger than this

STATES = ("ok", "offline", "not_enrolled", "cert_expired", "rejected")


class EnrolError(Exception):
    """Enrolment did not complete; nothing was changed on this machine."""


def mes_dir(user_dir: str) -> str:
    return os.path.join(user_dir, MES_DIRNAME)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_CODE_RE = re.compile(r"^([A-Z0-9]{12})([0-9a-f]{16})$")


def parse_code(code: str) -> Tuple[str, str]:
    """'K7QX-M2PA-9TRF-3fa9c0d1e2b4a6f8' -> (code as sent, CA fingerprint prefix).
    Dashes, spaces and case do not matter."""
    flat = re.sub(r"[\s-]", "", code or "")
    m = _CODE_RE.match(flat[:12].upper() + flat[12:].lower())
    if not m:
        raise EnrolError("enrolment code not recognised: expected 12 characters and 16 hex "
                         "digits, e.g. K7QX-M2PA-9TRF-3fa9c0d1e2b4a6f8")
    return code.strip(), m.group(2)


def ca_fingerprint(pem: str) -> str:
    """SHA-256 of the certificate's DER bytes, lower-case hex."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def split_url(url: str) -> Tuple[str, int]:
    u = urlsplit((url or "").strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("service URL must look like https://<host>:8443 (got %r)" % url)
    return u.hostname, u.port or 8443


def _write_private(path: str, data: bytes) -> None:
    """Write `data` to `path` atomically, mode 0600."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_enrolment(directory: str) -> Optional[Dict[str, Any]]:
    """enrolment.json when it and the three PEM files are there, else None."""
    try:
        with open(os.path.join(directory, ENROLMENT_FILE), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), str)
                                            for k in ("service_url", "site_id", "machine_id")):
        return None
    if not all(os.path.isfile(os.path.join(directory, n)) for n in (KEY_FILE, CERT_FILE, CA_FILE)):
        return None
    return doc


def _fmt_ms(ms: Any) -> str:
    if not isinstance(ms, int) or isinstance(ms, bool):
        return "unknown"
    return time.strftime("%Y-%m-%d", time.gmtime(ms // 1000))


def describe(directory: str, configured_url: str, now: Callable[[], float] = time.time) -> str:
    """One line for --check (never shows the key)."""
    doc = load_enrolment(directory)
    if doc is None:
        return "not enrolled (lcnc-status-agent --mes-enroll https://<service>:8443 <code>)"
    na = doc.get("not_after")
    expired = isinstance(na, int) and na <= now() * 1000
    text = "%s, site %s, certificate valid until %s%s" % (
        doc["service_url"], doc["site_id"], _fmt_ms(na), " [EXPIRED: enrol again]" if expired else "")
    if not configured_url.strip():
        text += "  [mes_service_url not set in config: nothing is forwarded]"
    elif configured_url.strip() != doc["service_url"]:
        text += "  [config mes_service_url is %s]" % configured_url.strip()
    return text


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------
def fetch_ca(host: str, port: int, timeout: float = TIMEOUT_S) -> str:
    """Step 1: GET /v1/enroll/ca WITHOUT verification (it is checked against the
    code afterwards, before anything secret is sent)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        conn.request("GET", "/v1/enroll/ca")
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise EnrolError("the service answered %d to GET /v1/enroll/ca" % resp.status)
    pem = body.decode("ascii", "replace")
    if "-----BEGIN CERTIFICATE-----" not in pem:
        raise EnrolError("the service did not send a CA certificate")
    return pem


def make_csr(workdir: str, openssl: str = "openssl") -> Tuple[bytes, bytes]:
    """Step 2: (key PEM, CSR PEM) from the openssl command."""
    exe = shutil.which(openssl)
    if not exe:
        raise EnrolError("the 'openssl' command is not installed (sudo apt install openssl)")
    key, csr = os.path.join(workdir, "client.key"), os.path.join(workdir, "client.csr")
    old = os.umask(0o077)
    try:
        r = subprocess.run([exe, "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                            "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=lcnc-agent"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    finally:
        os.umask(old)
    if r.returncode != 0:
        raise EnrolError("openssl could not make the key: %s" % r.stderr.decode("utf-8", "replace").strip())
    with open(key, "rb") as f:
        key_pem = f.read()
    with open(csr, "rb") as f:
        csr_pem = f.read()
    return key_pem, csr_pem


def post_enroll(host: str, port: int, ca_file: str, body: Dict[str, Any],
                timeout: float = TIMEOUT_S) -> Tuple[int, Dict[str, Any]]:
    """Step 3: POST /v1/enroll over TLS that trusts only the checked CA."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.check_hostname = True
    data = json.dumps(body).encode("utf-8")
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        conn.request("POST", "/v1/enroll", body=data,
                     headers={"Content-Type": "application/json", "Content-Length": str(len(data))})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        doc = {}
    return resp.status, doc if isinstance(doc, dict) else {}


def enroll(url: str, code: str, machine_id: str, machine_name: str, directory: str,
           get_ca: Callable[[str, int], str] = fetch_ca,
           csr_maker: Callable[[str], Tuple[bytes, bytes]] = make_csr,
           poster: Callable[[str, int, str, Dict[str, Any]], Tuple[int, Dict[str, Any]]] = post_enroll
           ) -> Dict[str, Any]:
    """Enrol this machine; returns the enrolment.json content. Raises EnrolError
    and leaves the files of an earlier enrolment untouched on any failure."""
    try:
        host, port = split_url(url)
    except ValueError as exc:
        raise EnrolError(str(exc))
    sent_code, fp_prefix = parse_code(code)
    if not machine_id:
        raise EnrolError("this machine has no machine_uuid (identity.json): start the agent once first")
    try:
        ca_pem = get_ca(host, port)
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise EnrolError("cannot reach %s: %s" % (url, exc))
    try:
        fp = ca_fingerprint(ca_pem)
    except ValueError:
        raise EnrolError("the service's CA certificate cannot be read")
    if not fp.startswith(fp_prefix):
        # The code was NOT sent. Either the code belongs to another site or
        # something between here and the service is pretending to be it.
        raise EnrolError("the service's CA fingerprint %s… does not match the code (%s…): "
                         "wrong service or wrong code — nothing was sent" % (fp[:16], fp_prefix))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    work = tempfile.mkdtemp(prefix=".enrol-", dir=directory)
    try:
        ca_file = os.path.join(work, CA_FILE)
        with open(ca_file, "w", encoding="ascii") as f:
            f.write(ca_pem)
        key_pem, csr_pem = csr_maker(work)
        try:
            status, doc = poster(host, port, ca_file, {
                "code": sent_code, "csr": csr_pem.decode("ascii"),
                "machine_id": machine_id, "machine_name": machine_name})
        except ssl.SSLCertVerificationError as exc:
            raise EnrolError("the service's certificate does not match %s (%s); the admin can add "
                             "this name with 'floorwatch-service server-cert'" % (host, exc.verify_message))
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise EnrolError("enrolment request failed: %s" % exc)
        if status != 200:
            raise EnrolError("the service refused the enrolment (%d %s)%s" % (
                status, doc.get("error", ""), (": " + doc["message"]) if doc.get("message") else ""))
        cert, site, mid = doc.get("certificate"), doc.get("site_id"), doc.get("machine_id")
        if not (isinstance(cert, str) and isinstance(site, str) and mid == machine_id):
            raise EnrolError("the service's answer is incomplete")
        if isinstance(doc.get("ca"), str) and ca_fingerprint(doc["ca"]) != fp:
            raise EnrolError("the service answered with another CA than the one checked")
        enrolment = {"service_url": url.strip().rstrip("/"), "site_id": site, "machine_id": mid,
                     "not_after": doc.get("not_after")}
        _write_private(os.path.join(directory, KEY_FILE), key_pem)
        _write_private(os.path.join(directory, CERT_FILE), cert.encode("ascii"))
        _write_private(os.path.join(directory, CA_FILE), ca_pem.encode("ascii"))
        _write_private(os.path.join(directory, ENROLMENT_FILE),
                       json.dumps(enrolment, indent=2).encode("utf-8"))
        return enrolment
    finally:
        shutil.rmtree(work, ignore_errors=True)


def set_config_value(cfg_path: str, key: str, value: str) -> None:
    """Set `key: "value"` in a flat config.yaml (replace the line or append),
    keeping the file's mode."""
    try:
        with open(cfg_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        mode = os.stat(cfg_path).st_mode & 0o777
    except FileNotFoundError:
        lines, mode = [], 0o600
    new = '%s: "%s"' % (key, value.replace("\\", "\\\\").replace('"', '\\"'))
    pat = re.compile(r"^\s*%s\s*:" % re.escape(key))
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = new
            break
    else:
        lines.append(new)
    tmp = "%s.tmp-%d" % (cfg_path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, cfg_path)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class Transport:
    """HTTPS with the client certificate, trusting only the site CA. One
    connection per request (the service speaks HTTP/1.0)."""

    def __init__(self, url: str, directory: str, timeout: float = TIMEOUT_S) -> None:
        self.host, self.port = split_url(url)
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=os.path.join(directory, CA_FILE))
        self.ctx.check_hostname = True
        self.ctx.load_cert_chain(os.path.join(directory, CERT_FILE), os.path.join(directory, KEY_FILE))

    def request(self, method: str, path: str, body: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, bytes]:
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ctx)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()


def _error_of(raw: bytes) -> str:
    try:
        doc = json.loads(raw.decode("utf-8"))
        return str(doc.get("error", "")) if isinstance(doc, dict) else ""
    except (UnicodeDecodeError, ValueError):
        return ""


def _cert_problem(exc: BaseException) -> Optional[str]:
    """For a TLS error: 'cert_expired' / 'rejected' when the service refused our
    certificate, None when it is a network problem (or the service's cert)."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return None                       # the SERVICE's certificate: logged, treated as offline
    text = "%s %s" % (getattr(exc, "reason", "") or "", exc)
    if "EXPIRED" in text.upper():
        return "cert_expired"
    if any(a in text.upper() for a in ("BAD_CERTIFICATE", "UNKNOWN_CA", "CERTIFICATE_REVOKED",
                                       "CERTIFICATE_UNKNOWN", "ACCESS_DENIED",
                                       "CERTIFICATE_REQUIRED")):
        return "rejected"
    return None


# ---------------------------------------------------------------------------
# Forwarder
# ---------------------------------------------------------------------------
class Forwarder(threading.Thread):
    def __init__(self, spool, service_url: str, directory: str, machine_id: str,
                 transport_factory: Optional[Callable[[str, str], Any]] = None,
                 wall: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[], float] = random.random) -> None:
        super().__init__(name="mes-forwarder", daemon=True)
        self.spool, self.url, self.dir, self.machine_id = spool, service_url.strip(), directory, machine_id
        self.make_transport = transport_factory or (lambda url, d: Transport(url, d))
        self.wall, self.clock, self.rand = wall, clock, rand
        self._halt = threading.Event()
        self.transport = None
        self.types: Optional[List[str]] = None       # from capabilities; None = not asked yet
        self.limits = dict(DEFAULT_LIMITS)
        self.batch_cap = self.limits["max_batch_events"]
        self._caps_at: Optional[float] = None
        self._backoff = 0.0
        self._cert_stamp = None
        self._skipped_logged = set()
        self.status: Dict[str, Any] = {"state": "not_enrolled"}
        self._last_ok_ms: Optional[int] = None
        self.sent = {s: 0 for s in RECEIPTS}

    # -- status -------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        old = self.status.get("state")
        st: Dict[str, Any] = {"state": state}
        if self._last_ok_ms is not None:
            st["last_ok_ms"] = self._last_ok_ms
        self.status = st                                  # replaced atomically
        if state != old:
            log = logger.info if state == "ok" else logger.warning
            log("MES site service link: %s -> %s", old, state)

    def _ok(self) -> None:
        self._last_ok_ms = int(self.wall() * 1000)
        self._backoff = 0.0
        self._set_state("ok")

    # -- enrolment files ------------------------------------------------------
    def _stamp(self):
        try:
            st = os.stat(os.path.join(self.dir, CERT_FILE))
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _load(self) -> bool:
        """(Re)load the enrolment. False when forwarding cannot start."""
        self.transport = None
        if not self.url:
            self._set_state("not_enrolled")
            return False
        doc = load_enrolment(self.dir)
        if doc is None:
            self._set_state("not_enrolled")
            return False
        if doc["machine_id"] != self.machine_id:
            logger.error("MES enrolment in %s is for machine %s, but this machine is %s: "
                         "enrol again", self.dir, doc["machine_id"], self.machine_id)
            self._set_state("not_enrolled")
            return False
        if doc["service_url"] != self.url.rstrip("/"):
            logger.warning("mes_service_url %s differs from the enrolled service %s; using %s",
                           self.url, doc["service_url"], self.url)
        self._cert_stamp = self._stamp()
        na = doc.get("not_after")
        if isinstance(na, int) and not isinstance(na, bool) and na <= self.wall() * 1000:
            self._set_state("cert_expired")
            return False
        if UUID4_RE.match(doc["site_id"]):
            try:
                self.spool.set_site_id(doc["site_id"])
            except Exception as exc:
                logger.error("MES spool: site_id not stored: %s", exc)
        else:
            logger.error("MES enrolment in %s has a malformed site_id %r: events keep site_id null",
                         self.dir, doc["site_id"])
        try:
            self.transport = self.make_transport(self.url, self.dir)
        except (OSError, ssl.SSLError, ValueError) as exc:
            logger.error("MES enrolment files in %s cannot be used: %s", self.dir, exc)
            self._set_state("not_enrolled")
            return False
        self.types, self._caps_at = None, None
        return True

    # -- one request ----------------------------------------------------------
    def _call(self, method: str, path: str, body: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, bytes]]:
        """(status, body), or None after a network failure (state set)."""
        try:
            status, raw = self.transport.request(method, path, body, headers)
        except ssl.SSLError as exc:
            problem = _cert_problem(exc)
            if problem:
                logger.error("MES site service refused this machine's certificate: %s", exc)
                self._refused(problem)
            else:
                if isinstance(exc, ssl.SSLCertVerificationError):
                    logger.error("MES site service certificate not accepted: %s", exc.verify_message)
                self._offline("%s" % exc)
            return None
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            self._offline("%s: %s" % (type(exc).__name__, exc))
            return None
        if status in (401, 403):
            logger.error("MES site service: %d %s — forwarding stops until the machine is "
                         "enrolled again", status, _error_of(raw) or "refused")
            self._refused("rejected")
            return None
        return status, raw

    def _offline(self, why: str) -> None:
        self._backoff = BACKOFF_MIN_S if self._backoff <= 0 else min(self._backoff * 2, BACKOFF_MAX_S)
        if self.status.get("state") != "offline":
            logger.warning("MES site service not reachable (%s); retrying", why)
        self._set_state("offline")

    def _refused(self, state: str) -> None:
        self.transport = None
        self._set_state(state)

    def _wait_s(self) -> float:
        """Backoff with jitter: between half and all of the current backoff."""
        return self._backoff * (0.5 + 0.5 * self.rand())

    # -- capabilities --------------------------------------------------------
    def _capabilities(self) -> bool:
        r = self._call("GET", "/v1/capabilities")
        if r is None:
            return False
        status, raw = r
        if status != 200:
            self._offline("capabilities: HTTP %d" % status)
            return False
        try:
            doc = json.loads(raw.decode("utf-8"))
            types = doc["contracts"]["fw.mes.event/1"]["event_types"]
            if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
                raise ValueError("event_types")
            limits = doc.get("limits") or {}
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            self._offline("capabilities not understood (%s)" % exc)
            return False
        for k in DEFAULT_LIMITS:
            v = limits.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                self.limits[k] = v
        self.batch_cap = min(self.batch_cap, self.limits["max_batch_events"]) or 1
        if self.types is not None and set(types) != set(self.types):
            logger.info("MES site service now accepts: %s", ", ".join(sorted(types)))
        self.types = list(types)
        self._caps_at = self.clock()
        self._ok()
        return True

    # -- one batch -------------------------------------------------------------
    def _batch(self) -> List[Tuple[int, bytes]]:
        """Pending rows of accepted types, oldest first, within the limits."""
        rows = self.spool.pending(min(self.batch_cap, self.limits["max_batch_events"]), types=self.types)
        out, size = [], len(b'{"events":[]}')
        max_ev, max_bytes = self.limits["max_event_bytes"], self.limits["max_batch_bytes"]
        for seq, body in rows:
            if len(body) > max_ev or len(body) + len(b'{"events":[]}') > max_bytes:
                if seq not in self._skipped_logged:       # stays pending; never dropped
                    self._skipped_logged.add(seq)
                    logger.error("MES spool row %d (%d bytes) is over the service's limit (%d): kept, "
                                 "not sent", seq, len(body), max_ev)
                continue
            extra = len(body) + (1 if out else 0)
            if size + extra > max_bytes:
                break
            out.append((seq, body))
            size += extra
        return out

    def send_once(self) -> int:
        """Send one batch. Returns rows acknowledged (0 = nothing to do or failed)."""
        batch = self._batch()
        if not batch:
            return 0
        payload = b'{"events":[' + b",".join(b for _, b in batch) + b"]}"
        headers = {"Content-Type": "application/json"}
        if len(payload) > GZIP_FROM:
            payload = gzip.compress(payload, 6)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(payload))
        r = self._call("POST", "/v1/events", payload, headers)
        if r is None:
            return 0
        status, raw = r
        if status == 200:
            return self._receipts(batch, raw)
        if status == 413:
            self.batch_cap = max(1, len(batch) // 2)
            logger.warning("MES site service: batch of %d too large; sending %d at a time",
                           len(batch), self.batch_cap)
            return 0
        if status == 400:
            # Cannot succeed unchanged: send smaller batches and wait the longest backoff.
            logger.error("MES site service refused a batch of %d as malformed (%s)",
                         len(batch), _error_of(raw) or raw[:200])
            self.batch_cap = max(1, len(batch) // 2)
            self._backoff = BACKOFF_MAX_S
            return 0
        self._offline("HTTP %d %s" % (status, _error_of(raw)))
        return 0

    def _receipts(self, batch: List[Tuple[int, bytes]], raw: bytes) -> int:
        try:
            receipts = json.loads(raw.decode("utf-8"))["receipts"]
            if not isinstance(receipts, list):
                raise ValueError("receipts")
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            self._offline("receipts not understood (%s)" % exc)
            return 0
        acked: List[int] = []
        v = self.spool.validate
        for (seq, body), rc in zip(batch, receipts):
            if not isinstance(rc, dict) or rc.get("status") not in RECEIPTS:
                continue
            event, _ = v.decode(body)
            if not event or rc.get("event_id") != event.get("event_id"):
                logger.error("MES receipt for %r does not match spool row %d; not acknowledged",
                             rc.get("event_id"), seq)
                continue
            st = rc["status"]
            self.sent[st] += 1
            if st in ("quarantined", "rejected"):
                logger.warning("MES event %s (%s) %s by the site service: %s%s", rc["event_id"],
                               event.get("type"), st, rc.get("code", ""),
                               (" at " + rc["path"]) if rc.get("path") else "")
            acked.append(seq)
        if len(receipts) != len(batch):
            logger.error("MES site service sent %d receipts for %d events", len(receipts), len(batch))
        if acked:
            self.spool.mark_acked(acked)
        self._ok()
        if self.batch_cap < self.limits["max_batch_events"]:
            self.batch_cap = min(self.limits["max_batch_events"], self.batch_cap * 2)
        return len(acked)

    # -- loop ------------------------------------------------------------------
    def step(self) -> float:
        """One round. Returns how long to wait before the next."""
        if self.transport is None:
            if self.status.get("state") in ("rejected", "cert_expired") \
                    and self._stamp() == self._cert_stamp:
                return RECHECK_S              # same certificate: still refused; wait for a new enrolment
            if not self._load():
                return RECHECK_S
        if self.types is None or self.clock() - (self._caps_at or 0) >= CAPS_EVERY_S:
            if not self._capabilities():
                return self._wait_s() if self.transport is not None else RECHECK_S
        n = self.send_once()
        if self.transport is None:
            return RECHECK_S
        if self.status.get("state") == "offline" or self._backoff > 0:
            return self._wait_s()
        return 0.0 if n else IDLE_POLL_S

    def stop(self, timeout: float = 5.0) -> None:
        self._halt.set()
        self.join(timeout)

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                wait = self.step()
            except Exception as exc:                    # never let the MES side die quietly
                logger.error("MES forwarder: %s", exc)
                wait = RECHECK_S
            if wait > 0:
                self._halt.wait(wait)
