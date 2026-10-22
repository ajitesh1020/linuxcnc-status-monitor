#!/usr/bin/env python3
"""Check MES events against contract fw.mes.event/1 (this folder).

Standard library only, Python 3.7+: the agent (zero-dependency .deb) and
MillWright (inside AXIS) run the same checks as FloorWatch, from their vendored
copy of this folder.

Two layers:
  * the JSON Schemas (envelope.schema.json, events/<type>.schema.json). Only the
    keywords listed in SUPPORTED are allowed in them; loading fails on any other,
    so a schema can never rely on something this checker skips. FloorWatch's
    tests compare the result with the ``jsonschema`` library on every fixture.
  * rules a schema cannot say (RULES below): who may produce a type, which
    correlation ids it needs, size and encoding limits, real dates, order.

    python validate.py event.json ...          # check event files
    python validate.py --check-manifest        # vendored copy unchanged?
    python validate.py --write-manifest        # canonical repo only

Error codes are stable: a schema keyword ("required", "pattern", ...) or
"rule:<name>". Tests and producers may match on them.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import sys
from collections import namedtuple
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT = "fw.mes.event/1"
MANIFEST = "MANIFEST.sha256"

Error = namedtuple("Error", "path code message")

ANNOTATIONS = {"$schema", "$id", "title", "description", "$defs"}
SUPPORTED = ANNOTATIONS | {
    "type", "enum", "const", "required", "properties", "additionalProperties",
    "propertyNames", "items", "minItems", "maxItems", "uniqueItems", "minLength",
    "maxLength", "pattern", "minimum", "maximum", "anyOf", "$ref"}

RULES = {
    "unknown_type": "the type is not in catalogue.json",
    "producer": "this producer kind may not send this type",
    "correlation_required": "a correlation id this type needs is null",
    "site_id_required": "only agents and MillWright may leave site_id null (not enrolled yet)",
    "machine_id_required": "only MillWright may leave machine_id null (the agent fills it)",
    "timestamp": "not a real UTC date and time",
    "time_order": "ended before it started",
    "raw": "scan data over the byte limit, not valid base64, or text with control characters",
    "fixture_position": "a fixture position names a scan that is not in scan_ids",
    "too_large": "the encoded event is over max_event_bytes",
    "encoding": "not UTF-8",
    "json": "not a JSON object, or NaN / Infinity",
    "duplicate_key": "a key appears twice in one object",
    "trailing_newline": "an identifier or code ends with a newline",
}

# Payload fields holding timestamps, checked for real dates (the pattern only
# checks the shape).
TIMESTAMP_FIELDS = {"cycle.observed": ("started_at", "ended_at"),
                    "program.version": ("seen_at",)}


class ContractError(Exception):
    """The contract folder itself is broken (not an event problem)."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _check_keywords(schema, where):
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key not in SUPPORTED:
                raise ContractError("%s: unsupported schema keyword %r" % (where, key))
            if key in ("properties", "$defs"):
                for name, sub in value.items():
                    _check_keywords(sub, "%s/%s/%s" % (where, key, name))
            elif key in ("items", "additionalProperties", "propertyNames"):
                _check_keywords(value, "%s/%s" % (where, key))
            elif key == "anyOf":
                for i, sub in enumerate(value):
                    _check_keywords(sub, "%s/anyOf/%d" % (where, i))
    elif not isinstance(schema, bool):
        raise ContractError("%s: a schema must be an object or a boolean" % where)


class Contract(object):
    """The catalogue, envelope and event schemas of one contract folder."""

    def __init__(self, folder=HERE):
        self.folder = folder
        self.catalogue = _read_json(os.path.join(folder, "catalogue.json"))
        if self.catalogue.get("contract") != CONTRACT:
            raise ContractError("catalogue.json is not %s" % CONTRACT)
        self.envelope = _read_json(os.path.join(folder, "envelope.schema.json"))
        _check_keywords(self.envelope, "envelope.schema.json")
        self.types = self.catalogue["types"]
        self.payloads = {}
        for type_, info in self.types.items():
            schema = _read_json(os.path.join(folder, *info["schema"].split("/")))
            _check_keywords(schema, info["schema"])
            self.payloads[type_] = schema
        self.max_event_bytes = int(self.catalogue["max_event_bytes"])
        self.max_raw_scan_bytes = int(self.catalogue["max_raw_scan_bytes"])


_DEFAULT = None


def default_contract():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Contract()
    return _DEFAULT


# ---------------------------------------------------------------------------
# JSON Schema subset
# ---------------------------------------------------------------------------
def _is_type(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float))
    raise ContractError("unknown type %r" % name)


def _equal(a, b):
    """JSON equality: 1 == 1.0, but True is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _resolve(ref, root):
    if not ref.startswith("#/$defs/"):
        raise ContractError("only local #/$defs/ references are supported: %r" % ref)
    try:
        return root["$defs"][ref[len("#/$defs/"):]]
    except KeyError:
        raise ContractError("unresolved reference %r" % ref)


def _join(path, key):
    return "%s/%s" % (path, key) if path else str(key)


def schema_errors(value, schema, root=None, path=""):
    """Errors of ``value`` against ``schema`` (JSON Schema subset), [] if valid."""
    root = schema if root is None else root
    if schema is True:
        return []
    if schema is False:
        return [Error(path, "false", "no value is allowed here")]
    if "$ref" in schema:
        return schema_errors(value, _resolve(schema["$ref"], root), root, path)
    if "anyOf" in schema:
        branches = [schema_errors(value, s, root, path) for s in schema["anyOf"]]
        if all(branches):
            best = min(branches, key=len)
            if all(e.code.startswith("rule:") for e in best):
                return best                # e.g. "G54\n": say what is wrong with it
            return [Error(path, "anyOf", "matches none of the allowed forms: " +
                          "; ".join(e.code + " " + e.message for e in best[:2]))]
    out = []
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_is_type(value, n) for n in names):
            return [Error(path, "type", "expected %s" % " or ".join(names))]
    if "const" in schema and not _equal(value, schema["const"]):
        out.append(Error(path, "const", "must be %r" % (schema["const"],)))
    if "enum" in schema and not any(_equal(value, v) for v in schema["enum"]):
        out.append(Error(path, "enum", "must be one of %s" % ", ".join(map(str, schema["enum"]))))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(Error(path, "minLength", "shorter than %d" % schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(Error(path, "maxLength", "longer than %d" % schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(Error(path, "pattern", "does not match %s" % schema["pattern"]))
        elif "pattern" in schema and schema["pattern"].endswith("$") and value.endswith("\n"):
            # Python's $ also matches before a final newline; the contract does not.
            out.append(Error(path, "rule:trailing_newline", RULES["trailing_newline"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(Error(path, "minimum", "below %s" % schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            out.append(Error(path, "maximum", "above %s" % schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(Error(path, "minItems", "fewer than %d items" % schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(Error(path, "maxItems", "more than %d items" % schema["maxItems"]))
        if schema.get("uniqueItems"):
            for i in range(len(value)):
                if any(_equal(value[i], value[j]) for j in range(i)):
                    out.append(Error(_join(path, i), "uniqueItems", "repeats an earlier item"))
                    break
        if "items" in schema:
            for i, item in enumerate(value):
                out += schema_errors(item, schema["items"], root, _join(path, i))
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                out.append(Error(_join(path, key), "required", "missing"))
        props = schema.get("properties", {})
        for key, item in value.items():
            if "propertyNames" in schema:
                bad = schema_errors(key, schema["propertyNames"], root, _join(path, key))
                if bad:
                    out.append(Error(_join(path, key), "propertyNames",
                                     "name not allowed: " + bad[0].message))
            if key in props:
                out += schema_errors(item, props[key], root, _join(path, key))
            elif "additionalProperties" in schema:
                out += schema_errors(item, schema["additionalProperties"], root, _join(path, key))
    return out


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
_TS = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]{1,6})?Z$")


def parse_timestamp(text):
    """A contract timestamp as a naive UTC datetime, or None if not a real time."""
    m = _TS.match(text) if isinstance(text, str) else None
    if not m or text.endswith("\n"):
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    frac = m.group(2) or ".0"
    return dt.replace(microsecond=int((frac[1:] + "000000")[:6]))


def _rule(path, name):
    return Error(path, "rule:" + name, RULES[name])


def validate_event(event, contract=None):
    """Every error of one decoded event; [] means valid."""
    c = contract or default_contract()
    errors = schema_errors(event, c.envelope)
    if not isinstance(event, dict):
        return errors
    type_ = event.get("type")
    info = c.types.get(type_) if isinstance(type_, str) else None
    if info is None:
        if not any(e.path == "type" for e in errors):
            errors.append(_rule("type", "unknown_type"))
        return errors
    payload = event.get("payload")
    if isinstance(payload, dict):
        errors += [Error(_join("payload", e.path) if e.path else "payload", e.code, e.message)
                   for e in schema_errors(payload, c.payloads[type_])]
    producer = event.get("producer") if isinstance(event.get("producer"), dict) else {}
    kind = producer.get("kind")
    if kind in ("agent", "millwright", "service") and kind not in info["producers"]:
        errors.append(_rule("producer/kind", "producer"))
    corr = event.get("correlation") if isinstance(event.get("correlation"), dict) else {}
    for key in info["requires"]:
        if key in corr and corr[key] is None:
            errors.append(_rule(_join("correlation", key), "correlation_required"))
    if "site_id" in event and event["site_id"] is None and kind == "service":
        errors.append(_rule("site_id", "site_id_required"))
    if "machine_id" in event and event["machine_id"] is None and kind in ("agent", "service"):
        errors.append(_rule("machine_id", "machine_id_required"))
    if isinstance(event.get("occurred_at"), str) and _TS.match(event["occurred_at"]) \
            and parse_timestamp(event["occurred_at"]) is None:
        errors.append(_rule("occurred_at", "timestamp"))
    if isinstance(payload, dict):
        errors += _payload_rules(type_, payload, c)
    return errors


def _payload_rules(type_, p, c):
    out = []
    times = {}
    for name in TIMESTAMP_FIELDS.get(type_, ()):
        if isinstance(p.get(name), str) and _TS.match(p[name]):
            times[name] = parse_timestamp(p[name])
            if times[name] is None:
                out.append(_rule("payload/" + name, "timestamp"))
    if type_ == "cycle.observed" and times.get("started_at") and times.get("ended_at") \
            and times["ended_at"] < times["started_at"]:
        out.append(_rule("payload/ended_at", "time_order"))
    if type_ == "scan.received" and isinstance(p.get("raw"), str):
        raw, enc = p["raw"], p.get("raw_encoding")
        if enc == "base64":
            try:
                data = base64.b64decode(raw.encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError, ValueError):
                data = None
        elif enc == "utf8":
            try:
                data = raw.encode("utf-8")
            except UnicodeEncodeError:
                data = None
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
                data = None
        else:
            data = b""
        if data is None or len(data) > c.max_raw_scan_bytes:
            out.append(_rule("payload/raw", "raw"))
    if type_ == "binding.requested" and isinstance(p.get("fixture_positions"), dict) \
            and isinstance(p.get("scan_ids"), list):
        for pos, scan in p["fixture_positions"].items():
            if scan not in p["scan_ids"]:
                out.append(_rule("payload/fixture_positions/" + pos, "fixture_position"))
    return out


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Duplicate()
    return dict(pairs)


class _Duplicate(ValueError):
    pass


def _no_constants(name):
    raise ValueError("JSON constant %s is not allowed" % name)


def decode(data, contract=None):
    """(event or None, errors) for the bytes of one event as sent."""
    c = contract or default_contract()
    if len(data) > c.max_event_bytes:
        return None, [_rule("", "too_large")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, [_rule("", "encoding")]
    try:
        event = json.loads(text, object_pairs_hook=_no_duplicates,
                           parse_constant=_no_constants)
    except _Duplicate:
        return None, [_rule("", "duplicate_key")]
    except ValueError:
        return None, [_rule("", "json")]
    if not isinstance(event, dict):
        return None, [_rule("", "json")]
    try:                                   # "\ud800" escapes decode to lone surrogates
        json.dumps(event, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None, [_rule("", "encoding")]
    return event, []


def validate_bytes(data, contract=None):
    """Every error of one encoded event (size, encoding, JSON, then content)."""
    event, errors = decode(data, contract)
    return errors if event is None else validate_event(event, contract)


def encode(event):
    """The canonical bytes of an event: what producers store and resend unchanged."""
    for value in _numbers(event):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("NaN and Infinity cannot be sent")
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")


def _numbers(value):
    if isinstance(value, dict):
        for v in value.values():
            for n in _numbers(v):
                yield n
    elif isinstance(value, list):
        for v in value:
            for n in _numbers(v):
                yield n
    elif isinstance(value, float):
        yield value


# ---------------------------------------------------------------------------
# Manifest (vendored copies prove they are unchanged)
# ---------------------------------------------------------------------------
def manifest_lines(folder=HERE):
    lines = []
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(dirpath, name), folder).replace(os.sep, "/")
            if rel == MANIFEST or name.endswith(".pyc"):
                continue
            with open(os.path.join(dirpath, name), "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            lines.append("%s  %s" % (digest, rel))
    return sorted(lines, key=lambda line: line[66:])


def manifest_problems(folder=HERE):
    """[] when MANIFEST.sha256 lists exactly the files present, byte for byte."""
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            want = [line.rstrip("\n") for line in f if line.strip()]
    except OSError as exc:
        return ["cannot read %s: %s" % (MANIFEST, exc)]
    have = manifest_lines(folder)
    want_by = {line[66:]: line[:64] for line in want}
    have_by = {line[66:]: line[:64] for line in have}
    out = []
    for rel in sorted(set(want_by) | set(have_by)):
        if rel not in have_by:
            out.append("missing: " + rel)
        elif rel not in want_by:
            out.append("not in the manifest: " + rel)
        elif want_by[rel] != have_by[rel]:
            out.append("changed: " + rel)
    return out


def manifest_digest(folder=HERE):
    """One SHA-256 for the whole contract (of MANIFEST.sha256): what CONTRACT_SOURCE pins."""
    with open(os.path.join(folder, MANIFEST), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_manifest(folder=HERE):
    with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(manifest_lines(folder)) + "\n")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--write-manifest"]:
        write_manifest()
        print("wrote %s (%s)" % (MANIFEST, manifest_digest()))
        return 0
    if args == ["--check-manifest"]:
        problems = manifest_problems()
        for p in problems:
            print(p)
        print("contract %s: %s" % (CONTRACT, "CHANGED" if problems else
                                   "unchanged (%s)" % manifest_digest()))
        return 1 if problems else 0
    if not args or args[0].startswith("-"):
        print("usage: validate.py EVENT.json ... | --check-manifest | --write-manifest")
        return 2
    bad = 0
    for path in args:
        with open(path, "rb") as f:
            errors = validate_bytes(f.read())
        for e in errors:
            print("%s: %s [%s] %s" % (path, e.path or "(event)", e.code, e.message))
        bad += bool(errors)
        if not errors:
            print("%s: valid" % path)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
