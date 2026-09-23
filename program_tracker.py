#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of linuxcnc-status-monitor. It is free software under the
# GNU General Public License v2 or later. See the LICENSE file for details.
"""
program_tracker.py  —  v1.0.0
==============================
Works out, from sampled LinuxCNC line numbers, where a cycle started, how far
it got, and whether it reached the program end (M2 / M30).

Pure logic — no ``linuxcnc`` import — so it is unit-testable off-machine.

Why not just compare motion_line with the M2/M30 line?
-------------------------------------------------------
  * M2 / M30 are not motions, so ``stat.motion_line`` never reaches their line.
    It stops at the LAST MOTION line before the program end.
  * Files usually end ``M30`` then ``%``; the ``%`` line never executes at all.
  * At cycle start ``motion_line`` still holds the previous run's last line
    (stale) until the first new move is issued, which looks like a mid-program
    start ("Run From Here") if read too early.

So the file scan records:
  first_motion_line — first line that contains a move
  tail_line         — last move line before the program end
  end_line          — the M2 / M30 line (``%`` only if the file has neither)

and a cycle is COMPLETE when, at the moment it goes idle, the last executed
move is the tail move (or ``current_line`` was seen on the M2/M30 line).
Stale line numbers are ignored until they change after cycle start.
"""

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# First executable line: skip blanks, comments, %, and O-word lines.
_SKIP_RE = re.compile(r"^(\s*$|;|%|\(|o\s*\d)", re.IGNORECASE)
# Program end word — M2 / M30 (also M02, M030), not M20 / M300.
_END_WORD_RE = re.compile(r"(?<![A-Z0-9.])M\s*0*(?:2|30)(?![0-9.])", re.IGNORECASE)
_PERCENT_RE = re.compile(r"^\s*%\s*$")
# A move: an axis word, or G0-G4 / G38.x probe / G73-G76 / G81-G89 canned cycles.
_MOTION_RE = re.compile(
    r"(?<![A-Z#<_])(?:[XYZABCUVW]\s*[-+.\d\[#]"
    r"|G\s*0*[0-4](?![\d.])|G\s*38\.\d|G\s*7[3-6](?![\d.])|G\s*8[1-9](?![\d.]))",
    re.IGNORECASE,
)
# Lines with axis words that set offsets rather than move.
_OFFSET_ONLY_RE = re.compile(r"G\s*(?:10|92|28\.1|30\.1)(?![\d])", re.IGNORECASE)
_COMMENT_RE = re.compile(r"\(.*?\)")


def _code_part(raw: str) -> str:
    """Strip ( ) comments and ; comments."""
    return _COMMENT_RE.sub("", raw).split(";", 1)[0].strip()


def _is_motion(code: str) -> bool:
    return bool(code) and bool(_MOTION_RE.search(code)) and not _OFFSET_ONLY_RE.search(code)


class ProgramInfo:
    """Line landmarks of one loaded G-code file (all 1-based, -1 = not found)."""

    def __init__(self, path: str = "", first_exec_line: int = 1,
                 first_motion_line: int = -1, tail_line: int = -1,
                 end_line: int = -1) -> None:
        self.path              = path
        self.first_exec_line   = first_exec_line
        self.first_motion_line = first_motion_line
        self.tail_line         = tail_line
        self.end_line          = end_line

    @classmethod
    def from_lines(cls, lines, path: str = "") -> "ProgramInfo":
        first_exec = -1
        motion_lines = []
        end_word = -1          # first M2/M30 — nothing after it executes
        last_percent = -1
        for lineno, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if first_exec == -1 and not _SKIP_RE.match(stripped):
                first_exec = lineno
            if _PERCENT_RE.match(stripped):
                last_percent = lineno
                continue
            code = _code_part(stripped)
            if end_word == -1 and code and _END_WORD_RE.search(code):
                end_word = lineno
            if end_word == -1 and _is_motion(code):
                motion_lines.append(lineno)
        end_line = end_word if end_word != -1 else (
            last_percent if last_percent > max(first_exec, 1) else -1)
        info = cls(
            path=path,
            first_exec_line=first_exec if first_exec != -1 else 1,
            first_motion_line=motion_lines[0] if motion_lines else -1,
            tail_line=motion_lines[-1] if motion_lines else -1,
            end_line=end_line,
        )
        return info

    # ------------------------------------------------------------------
    def start_tolerance(self) -> int:
        """Lines past the first move that still count as 'from the top'.
        Covers moves that finished between two samples."""
        span = max(self.tail_line - self.first_motion_line, 0)
        return max(5, span // 50)

    def is_near_start(self, line: int) -> bool:
        if self.first_motion_line <= 0:
            return True
        return line <= self.first_motion_line + self.start_tolerance()

    def progress(self, line: int) -> float:
        """0.0 … 1.0 — how far through the program's moves `line` is."""
        if self.first_motion_line <= 0 or self.tail_line <= 0:
            return 0.0
        span = self.tail_line - self.first_motion_line
        if span <= 0:
            return 1.0 if line >= self.tail_line else 0.0
        return min(1.0, max(0.0, (line - self.first_motion_line) / span))


class ProgramScanner:
    """Keeps a ProgramInfo for the currently loaded file; rescans on change
    (new path, or same path edited and reloaded)."""

    def __init__(self) -> None:
        self.info = ProgramInfo()
        self._sig: tuple = ("", 0.0, 0)

    def load(self, file_path: str) -> None:
        if not file_path:
            if self._sig[0]:
                self.info, self._sig = ProgramInfo(), ("", 0.0, 0)
            return
        try:
            st = os.stat(file_path)
            sig = (file_path, st.st_mtime, st.st_size)
        except OSError:
            sig = (file_path, 0.0, 0)
        if sig == self._sig:
            return
        self._sig = sig
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                self.info = ProgramInfo.from_lines(f, path=file_path)
        except OSError as exc:
            logger.error("Cannot read G-code file '%s': %s", file_path, exc)
            self.info = ProgramInfo(path=file_path)
            return
        i = self.info
        logger.debug(
            "Program scan: file=%s first_exec=%d first_move=%d tail_move=%d end=%d",
            os.path.basename(file_path), i.first_exec_line, i.first_motion_line,
            i.tail_line, i.end_line,
        )
        if i.end_line == -1:
            logger.warning("No M2/M30 in '%s' — cycles cannot be counted as parts.",
                           os.path.basename(file_path))


class CycleLineTracker:
    """Per-cycle line bookkeeping. Call begin() at cycle start with the line
    numbers seen while idle, then sample() every tick."""

    def __init__(self) -> None:
        self.begin(0, 0)

    def begin(self, idle_motion_line: int, idle_current_line: int) -> None:
        self._stale_motion   = idle_motion_line
        self._stale_current  = idle_current_line
        self._motion_live    = False
        self._current_live   = False
        self.start_line: Optional[int] = None
        self.last_motion_line: Optional[int] = None
        self.max_line        = 0
        self.end_seen        = False

    def sample(self, motion_line: int, current_line: int, prog: ProgramInfo) -> None:
        motion_line  = int(motion_line or 0)
        current_line = int(current_line or 0)
        if not self._motion_live and motion_line > 0 and motion_line != self._stale_motion:
            self._motion_live = True
        if self._motion_live and motion_line > 0:
            if self.start_line is None:
                self.start_line = motion_line
            self.last_motion_line = motion_line
            self.max_line = max(self.max_line, motion_line)
        if not self._current_live and current_line > 0 and current_line != self._stale_current:
            self._current_live = True
        if self._current_live and prog.end_line > 0 and current_line >= prog.end_line:
            self.end_seen = True

    @property
    def start_known(self) -> bool:
        return self.start_line is not None

    def is_run_from_here(self, prog: ProgramInfo) -> Optional[bool]:
        """None until the first new move is seen."""
        if self.start_line is None:
            return None
        return not prog.is_near_start(self.start_line)

    def progress(self, prog: ProgramInfo) -> float:
        if self.start_line is None:
            return 0.0
        return prog.progress(self.max_line)

    def reached_end(self, prog: ProgramInfo) -> bool:
        """Evaluate once the cycle has gone idle."""
        if self.end_seen:
            return True
        if prog.end_line <= 0:
            return False
        if prog.tail_line <= 0:
            return True        # program has no moves at all — it just ran to M2
        return self.last_motion_line is not None and self.last_motion_line >= prog.tail_line
