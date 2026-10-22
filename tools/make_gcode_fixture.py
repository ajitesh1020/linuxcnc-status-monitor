#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Write contracts/gcode/fixtures/: real packets from agent_gcode's sender for a
small program with awkward text (non-ASCII, quotes, backslashes, tabs, CR LF),
for the dashboard's tests (copy the folder to floorwatch/contracts/gcode/).

    python3 tools/make_gcode_fixture.py

ts values are written as 0 so the output is reproducible; tests/test_gcode_contract.py
fails if the committed files no longer match what the sender produces.
"""
import hashlib
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent_gcode import GcodeFileSender  # noqa: E402

OUT = os.path.join(ROOT, "contracts", "gcode", "fixtures")
META = {"file_name": "fixture — ø3 टूल.ngc", "file_size": 0, "file_modified_ms": 1761100000000}


def program() -> str:
    lines = ["%", "(FIXTURE: Ø 3 mm cutter — टूल / उपकरण \"quoted\" back\\slash)", "G21 G90\tG17"]
    for i in range(160):
        lines.append(f"G1 X{i * 0.5:.3f} Y{(i * 7) % 40:.3f} F600 (ü{i})")
    lines += ["M5", "M30", "%"]
    return "\r\n".join(lines) + "\r\n"


def packets(text: str):
    meta = dict(META, file_size=len(text.encode("utf-8")))
    sender = GcodeFileSender(1000, "FIXTURE", 1, threaded=False, pace_s=0)
    out = []
    for raw in sender.packets(meta, text):
        p = json.loads(raw)
        p["ts"] = 0
        out.append(p)
    return out


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    text = program()
    with open(os.path.join(OUT, "sample_program.ngc"), "w", encoding="utf-8", newline="") as f:
        f.write(text)
    with open(os.path.join(OUT, "sample_packets.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for p in packets(text):
            f.write(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {OUT}: {len(text)} characters, sha256 "
          f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
