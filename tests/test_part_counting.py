"""End-to-end part counting: program scan + line tracking + calculator.
Run: python -m unittest -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cycle_time_calculator import CycleTimeCalculator  # noqa: E402
import tempfile  # noqa: E402
from program_tracker import (CycleLineTracker, MacroMoves, ProgramFile, ProgramInfo,  # noqa: E402
                             subroutine_dirs)

# 1 %            6 G0 X10 Y10 ... moves on 6..105
# 2 (header)     106 G0 Z10     (tail move)
# 3 G21 G90      107 M5
# 4 T1 M6        108 M30        (end)
# 5 M3 S8000     109 %
PROGRAM = (["%", "(splash)", "G21 G90", "T1 M6", "M3 S8000"]
           + [f"G1 X{i} Y{i} F500" for i in range(100)]
           + ["G0 Z10", "M5", "M30", "%"])


# The header of SJ-942_Cutting1_2.NC (AXIS check, 26 Oct 2026): a G04 dwell on
# line 10, then variables; the first real move is line 20.
DWELL_PROGRAM = (["(TOOLDIA 2.0000)", "(GERBER FILE)", "(HEADER_BEGIN)", "",
                  "G90 (Absolute)", "G21", "G64 P0.01", "G54", "M3 S40000", "G04 P3",
                  "(HEADER_END)", "(====)", "#<Z_Retract> = 6.0000",
                  "#<Z_Depth> = -5.5000", "#<Z_Feed> = 400", "#<XY_Feed> = 600",
                  "(====)", "G21", "M3 S40000", "G00 Z20.000"]
                 + [f"G01 X{i} Y{i} F600" for i in range(100)]
                 + ["G00 Z#<Z_Retract>", "M5", "M30"])


class ProgramScanTests(unittest.TestCase):
    def test_landmarks(self):
        p = ProgramInfo.from_lines(PROGRAM)
        self.assertEqual(p.first_motion_line, 6)
        self.assertEqual(p.tail_line, 106)
        self.assertEqual(p.end_line, 108)   # M30, not the trailing %

    def test_percent_only_program(self):
        p = ProgramInfo.from_lines(["%", "G0 X1", "G1 X2", "%"])
        self.assertEqual(p.end_line, 4)
        self.assertEqual(p.tail_line, 3)

    def test_offset_and_comment_lines_are_not_moves(self):
        p = ProgramInfo.from_lines(
            ["G10 L2 P1 X0 Y0", "(move X10)", "G92 X0", "G0 X5", "M2"])
        self.assertEqual(p.first_motion_line, 4)
        self.assertEqual(p.end_line, 5)

    def test_dwell_is_not_a_move(self):
        for dwell in ("G04 P3", "G4 P0.5", "g4p2", "G004 P1"):
            p = ProgramInfo.from_lines(["G21", dwell, "M3 S1000", "G0 X1", "G4 P1", "M2"])
            self.assertEqual(p.first_motion_line, 4, dwell)
            self.assertEqual(p.tail_line, 4, dwell)       # a trailing dwell is not the tail

    def test_g0_to_g3_and_canned_cycles_are_still_moves(self):
        for move in ("G0", "G00", "G1 F100", "G2", "G03", "G38.2", "G81", "G73"):
            p = ProgramInfo.from_lines(["G21", move, "M2"])
            self.assertEqual(p.first_motion_line, 2, move)
        for other in ("G40", "G43 H1", "G41.1", "G49", "G4"):
            p = ProgramInfo.from_lines(["G21", other, "M2"])
            self.assertEqual(p.first_motion_line, -1, other)

    def test_g28_and_g30_are_moves(self):
        for move in ("G28", "G30", "g28", "G028", "G28 (park)", "G91 G28 Z0"):
            p = ProgramInfo.from_lines(["G21", "G0 X1", move, "M2"])
            self.assertEqual(p.tail_line, 3, move)
        for other in ("G28.1", "G30.1", "G280", "G300"):
            p = ProgramInfo.from_lines(["G21", "G0 X1", other, "M2"])
            self.assertEqual(p.tail_line, 2, other)

    def test_m20_and_m300_are_not_program_end(self):
        p = ProgramInfo.from_lines(["G0 X1", "M20", "M300", "G0 X2", "M02"])
        self.assertEqual(p.end_line, 5)


class _Machine:
    """Feeds (motion_line, current_line) samples through tracker + calculator
    exactly as status.py's state machine does."""

    def __init__(self, program=PROGRAM, path=None):
        self.prog = ProgramInfo.from_lines(program, path=path or "")
        self.calc = CycleTimeCalculator()
        self.tracker = CycleLineTracker()
        self.idle = (0, 0)
        self._ms = 0

    def _track(self, m, c):
        self.tracker.sample(m, c, self.prog)
        rfh = self.tracker.is_run_from_here(self.prog)
        if rfh is not None:
            self.calc.classify_start(rfh)
            self.calc.set_progress(self.tracker.progress(self.prog))

    def load(self, program, path):
        """Load another file, as the operator does in AXIS."""
        self.prog = ProgramInfo.from_lines(program, path=path)

    def run(self, lines, duration_ms=60_000):
        """lines: executed motion lines in order; the cycle then goes idle."""
        self.tracker.begin(*self.idle)
        self.calc.start_cycle(run_from_here=None, program=self.prog.path or None)
        # First tick after start still shows the previous run's (stale) line.
        self._track(*self.idle)
        for ln in lines:
            self._track(ln, ln)
        self._ms += duration_ms
        self.calc._elapsed_ms_unsafe = (  # deterministic elapsed incl. carry
            lambda d=duration_ms: d + self.calc._carry_ms)
        if self.tracker.reached_end(self.prog):
            self.calc.signal_cycle_complete()
        self.calc.stop_cycle()
        last = lines[-1] if lines else self.idle[0]
        self.idle = (last, last)
        return self.calc.snapshot()


# The end of SJ-942_Cutting1_2.NC (two-PC J test, 28 Oct 2026): the second
# G00 Z20 does not move (already at Z20), so LinuxCNC never shows it; the bare
# G28 is the last line shown before M30.
G28_FOOTER_PROGRAM = (["G21", "G00 Z20.000"]
                      + [f"G01 X{i} Y{i} F600" for i in range(100)]
                      + ["G00 Z20.00", "(FOOTER_BEGIN)", "G00 Z20.00", "(park)", "M5",
                         "G28 (If required move machine to parking position)",
                         "M30( End of program)", "(FOOTER_END)"])


class PartCountingScenarioTests(unittest.TestCase):
    def test_run_ending_with_bare_g28_completes(self):
        m = _Machine(G28_FOOTER_PROGRAM)
        self.assertEqual((m.prog.tail_line, m.prog.end_line), (108, 109))
        snap = m.run(list(range(2, 104)) + [108])     # 105 never shows
        self.assertEqual((snap.parts_produced, snap.abort_count), (1, 0))

    def test_run_stopped_before_g28_is_aborted(self):
        m = _Machine(G28_FOOTER_PROGRAM)
        snap = m.run(list(range(2, 104)))
        self.assertEqual((snap.parts_produced, snap.abort_count), (0, 1))

    def test_full_run_counts_even_with_stale_line_from_previous_run(self):
        m = _Machine()
        for n in range(1, 5):   # the log case: same program four times
            snap = m.run(list(range(6, 107, 3)) + [106])
            self.assertEqual(snap.parts_produced, n)
        self.assertEqual(snap.run_from_here_count, 0)
        self.assertEqual(snap.abort_count, 0)

    def test_first_sample_a_few_lines_in_is_still_from_top(self):
        m = _Machine()
        snap = m.run(list(range(9, 107)))   # first sample 3 lines past first move
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.run_from_here_count, 0)

    def test_abort_then_run_from_here_to_end_is_one_part(self):
        m = _Machine()
        snap = m.run(list(range(6, 56)), duration_ms=30_000)   # stopped at ~half
        self.assertEqual((snap.parts_produced, snap.abort_count), (0, 1))
        self.assertTrue(snap.part_in_progress)
        snap = m.run(list(range(50, 107)), duration_ms=35_000)  # Run From Here @50
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.abort_count, 1)
        self.assertEqual(snap.run_from_here_count, 1)
        self.assertEqual(snap.last_completed_ms, 65_000)   # carried 30 s + 35 s
        self.assertEqual(snap.partial_parts, 0)
        self.assertFalse(snap.part_in_progress)

    def test_abort_then_restart_from_top_records_half_part(self):
        m = _Machine()
        m.run(list(range(6, 57)), duration_ms=30_000)   # 50 % of the moves
        snap = m.run(list(range(6, 107)))               # restarted from the top
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.partial_parts, 1)
        self.assertAlmostEqual(snap.partial_part_equiv, 0.5, places=2)
        self.assertEqual(snap.last_partial_pct, 50.0)
        self.assertEqual(snap.last_completed_ms, 60_000)   # carry was dropped

    def test_stop_in_middle_is_abort_not_part(self):
        m = _Machine()
        snap = m.run(list(range(6, 80)))
        self.assertEqual(snap.parts_produced, 0)
        self.assertEqual(snap.abort_count, 1)
        self.assertGreater(snap.progress_pct, 70)

    def test_two_interrupted_rfh_segments_then_finish(self):
        m = _Machine()
        m.run(list(range(6, 40)), duration_ms=10_000)    # abort
        m.run(list(range(38, 70)), duration_ms=10_000)   # RFH, stopped again
        snap = m.run(list(range(68, 107)), duration_ms=10_000)
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.abort_count, 1)             # only the first stop
        self.assertEqual(snap.last_completed_ms, 30_000)


    def test_dwell_before_first_move_is_still_from_top(self):
        # The AXIS check case: first move seen is line 20, the dwell on line 10.
        m = _Machine(DWELL_PROGRAM, "/nc/sj942.ngc")
        snap = m.run(list(range(20, 122)))
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.run_from_here_count, 0)

    def test_aborted_part_is_not_carried_into_another_program(self):
        # The AXIS check case: axis.ngc aborted, then a different file run.
        m = _Machine(PROGRAM, "/nc/a.ngc")
        snap = m.run(list(range(6, 56)), duration_ms=37_000)    # abort at ~half
        self.assertTrue(snap.part_in_progress)
        m.load(PROGRAM, "/nc/b.ngc")
        snap = m.run(list(range(50, 107)), duration_ms=98_000)  # mid-start, other file
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.last_completed_ms, 98_000)        # no 37 s carried
        self.assertEqual(snap.partial_parts, 1)                 # a.ngc's part
        self.assertAlmostEqual(snap.partial_part_equiv, 0.5, places=1)
        self.assertEqual(snap.abort_count, 1)

    def test_aborted_part_then_other_program_from_top(self):
        m = _Machine(PROGRAM, "/nc/a.ngc")
        m.run(list(range(6, 56)), duration_ms=37_000)
        m.load(DWELL_PROGRAM, "/nc/b.ngc")
        snap = m.run(list(range(20, 122)), duration_ms=98_000)
        self.assertEqual((snap.parts_produced, snap.partial_parts), (1, 1))
        self.assertEqual(snap.run_from_here_count, 0)
        self.assertEqual(snap.last_completed_ms, 98_000)

    def test_same_file_still_carries_time(self):
        m = _Machine(PROGRAM, "/nc/a.ngc")
        m.run(list(range(6, 56)), duration_ms=30_000)
        m.load(PROGRAM, "/nc/a.ngc")                           # reloaded, same file
        snap = m.run(list(range(50, 107)), duration_ms=35_000)
        self.assertEqual(snap.last_completed_ms, 65_000)
        self.assertEqual(snap.partial_parts, 0)


class SubroutineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)

    def test_subroutine_dirs_from_ini(self):
        ini = os.path.join(self.d, "m.ini")
        with open(ini, "w") as f:
            f.write("[DISPLAY]\nSUBROUTINE_PATH = nope\n[RS274NGC]\n# c\n"
                    "SUBROUTINE_PATH = mw_macros:/abs/subs : \nUSER_M_PATH = mw_macros\n[EMC]\n")
        self.assertEqual(subroutine_dirs(ini), [os.path.join(self.d, "mw_macros"), "/abs/subs"])
        self.assertEqual(subroutine_dirs(os.path.join(self.d, "missing.ini")), [])
        self.assertEqual(subroutine_dirs(""), [])

    def test_program_file_keeps_the_program_through_subroutines(self):
        sub_dir = os.path.join(self.d, "macros")
        prog, sub = os.path.join(self.d, "p.ngc"), os.path.join(sub_dir, "rack_change.ngc")
        pf = ProgramFile([sub_dir])
        self.assertEqual(pf.update(prog, 0, False), (prog, False))
        self.assertEqual(pf.update(sub, 1, True), (prog, True))           # M6 remap
        self.assertEqual(pf.update(sub, 0, True), (prog, True))           # folder alone is enough
        self.assertEqual(pf.update(prog, 1, True), (prog, False))         # O-call in the same file: its lines
        self.assertEqual(pf.update(prog, 0, True), (prog, False))
        self.assertEqual(pf.update(sub, 0, False), (prog, True))          # idle, left in the sub
        other = os.path.join(self.d, "q.ngc")
        self.assertEqual(pf.update(other, 0, False), (other, False))      # a new program loaded
        self.assertEqual(pf.update("", 0, True), (other, False))
        self.assertEqual(pf.update("", 0, False), ("", False))

    def test_program_file_without_a_known_program(self):
        sub_dir = os.path.join(self.d, "macros")
        sub = os.path.join(sub_dir, "warmup.ngc")
        self.assertEqual(ProgramFile([sub_dir]).update(sub, 1, True), ("", True))   # agent started mid-call
        pf = ProgramFile([sub_dir])
        self.assertEqual(pf.update(sub, 0, False), (sub, False))         # a macro opened on its own
        self.assertEqual(pf.update(sub, 0, True), (sub, False))
        self.assertEqual(ProgramFile().update(sub, 0, True), (sub, False))  # no INI folders known

    def test_lines_without_a_move_are_not_motion_lines(self):
        prog = ProgramInfo.from_lines(["G21", "M6 T1", "G4 P2", "G53 G0 Z50", "M30", "G0 X1"])
        self.assertEqual([n for n in range(0, 9) if prog.is_move_line(n)], [4, 6])
        self.assertTrue(ProgramInfo().is_move_line(98))                     # not scanned: no check
        t = CycleLineTracker()
        t.begin(0, 0)
        for line in (98, 103, 110):                                         # the remap's last moves
            t.sample(line, 4, prog)
        self.assertIsNone(t.start_line)
        self.assertFalse(t.reached_end(prog))                               # 110 is not past the tail
        t.sample(4, 5, prog)
        self.assertEqual((t.start_line, t.is_run_from_here(prog)), (4, False))

    def test_run_ending_in_a_remap_is_judged_by_read_line(self):
        # trace 15:00: 28-line pick/drop check, M30 on line 27, the run goes idle at the remap's line 110
        lines = ["(c)", "(c)", "", "G21 G90 G94", "", "(pass)"] + ["M6 T1 G43 H1", "G4 P2"] * 9 \
            + ["", "G53 G0 Z76", "M30"]
        prog = ProgramInfo.from_lines(lines)
        self.assertEqual((prog.tail_line, prog.end_line), (26, 27))
        for stop in (False, True):
            t = CycleLineTracker()
            t.begin(0, 110)
            for ln in (92, 98, 103, 110, 73, 77, 82, 92, 98):
                t.hold(ln, ln)
            if not stop:
                for ln in (98, 103, 110):
                    t.sample(ln, ln, prog, read_line=27)
            self.assertEqual(t.reached_end(prog), not stop)
            self.assertIsNone(t.start_line)

    def test_read_ahead_without_a_remap_does_not_count(self):
        prog = ProgramInfo.from_lines(PROGRAM)
        t = CycleLineTracker()
        t.begin(0, 0)
        for ln in (6, 50, 70):
            t.sample(ln, ln, prog, read_line=108)        # read-ahead already at M30
        self.assertFalse(t.reached_end(prog))             # stopped at line 70 of 106

    def test_tracker_ignores_lines_inside_a_subroutine(self):
        prog = ProgramInfo.from_lines(["G21", "M6 T1", "G4 P2", "G53 G0 Z50", "M30"])
        t = CycleLineTracker()
        t.begin(0, 0)
        for line in (90, 91, 92):
            t.hold(line, line)
        t.sample(92, 92, prog)                                            # stale sub line, back in the program
        self.assertIsNone(t.start_line)
        t.sample(4, 4, prog)
        self.assertEqual(t.start_line, 4)
        self.assertFalse(t.is_run_from_here(prog))
        self.assertTrue(t.reached_end(prog))

# LX052_drill_ATC.ngc (29 Oct 2026): header, first move G0 Z20 on 27, G28 G91 Z0 on 28, M6 T01 on 30,
# G00 Z20 on 34, drill holes from 37 on (every line a move), tail 633, M30 634.
ATC_PROGRAM = (["(header)"] * 26 + ["G0 Z20.0000", "G28 G91 Z0", "G90", "M6 T01", "G43 H01", "M3 S40000",
                                    "G4 P2", "G00 Z20.0000", "", "(T01)"]
               + [f"X{i} Y{i}" for i in range(596)] + ["G0 Z20", "M30"])
# MillWright's rack_change.ngc: 114 lines, moves on these lines.
RACK_MOVES = (72, 73, 74, 75, 77, 82, 91, 92, 93, 94, 98, 103, 109, 110)
RACK_CHANGE = ["G53 G0 Z0" if n in RACK_MOVES else "(c)" for n in range(1, 115)]


class RemapTailTests(unittest.TestCase):
    """29 Oct 2026: every LX052 run from the top was logged as Run From Here. After its first M6,
    LinuxCNC is back in the program while motion still runs rack_change.ngc's last moves (103, 110);
    at 10 Hz those were never seen inside the remap, and in LX052 they are move lines too."""

    def setUp(self):
        self.prog = ProgramInfo.from_lines(ATC_PROGRAM)
        self.assertEqual((self.prog.first_motion_line, self.prog.tail_line, self.prog.end_line), (27, 633, 634))
        self.moves = frozenset(RACK_MOVES)

    def _start(self, idle, held, back, sub_moves):
        t = CycleLineTracker()
        t.begin(idle, idle)
        t.sample(idle, idle, self.prog)                      # first tick: stale line
        for ln in held:                                      # stat.file = rack_change.ngc
            t.hold(ln, ln, sub_moves)
        for ln in back:                                      # back in the program
            t.sample(ln, 37, self.prog)
        return t

    def test_remap_tail_after_first_m6_is_not_a_start_line(self):
        # journal 11:51:16-11:51:30: 28 (G28, while LinuxCNC is already in the remap), 92, 98; back: 98, 110, 37
        t = self._start(0, (28, 92, 98), (98, 110, 37), self.moves)
        self.assertEqual((t.start_line, t.is_run_from_here(self.prog)), (37, False))
        # 12:06:49-12:07:18: 27, 73, 77, 82, 92, 98; back: 98, 103, 110, 34, 37
        t = self._start(633, (27, 73, 77, 82, 92, 98), (98, 103, 110, 34, 37), self.moves)
        self.assertEqual((t.start_line, t.is_run_from_here(self.prog)), (34, False))

    def test_without_the_macro_lines_the_tail_still_looks_like_run_from_here(self):
        t = self._start(0, (28, 92, 98), (98, 110, 37), frozenset())   # macro file unreadable
        self.assertEqual((t.start_line, t.is_run_from_here(self.prog)), (110, True))

    def test_program_lines_count_again_after_its_first_move(self):
        # A program move on a line number seen inside the remap (98) is the program's once it has moved.
        t = self._start(0, (92, 98), (98, 110, 37, 60, 92, 98, 633), self.moves)
        self.assertEqual((t.start_line, t.max_line, t.last_motion_line), (37, 633, 633))
        self.assertTrue(t.reached_end(self.prog))
        self.assertFalse(t.from_sub)

    def test_program_move_on_an_earlier_remap_line_counts(self):
        # The remap's moves at or before the last one seen inside it have already run: 73 is the program's.
        t = self._start(0, (92, 98), (98, 73), self.moves)
        self.assertEqual(t.start_line, 73)

    def test_a_later_m6_hides_the_remap_tail_again(self):
        t = self._start(0, (92, 98), (98, 110, 37, 300), self.moves)
        for ln in (98, 103):                                 # second tool change
            t.hold(ln, ln, self.moves)
        for ln in (110, 109, 305):
            t.sample(ln, 305, self.prog)
        self.assertEqual((t.start_line, t.last_motion_line, t.max_line), (37, 305, 305))

    def test_stopped_after_the_first_m6_is_an_abort(self):
        m = _Machine(ATC_PROGRAM)
        m.tracker.begin(0, 0)
        m.calc.start_cycle(run_from_here=None, program=None)
        m._track(0, 0)
        for ln in (27, 73, 77, 98):
            m.tracker.hold(ln, ln, self.moves)
        for ln in (98, 103, 110, 34, 37):
            m._track(ln, ln)
        self.assertFalse(m.tracker.reached_end(m.prog))
        m.calc._elapsed_ms_unsafe = lambda: 101_000                    # as _Machine.run
        m.calc.stop_cycle()
        snap = m.calc.snapshot()
        self.assertEqual((snap.abort_count, snap.run_from_here_count, snap.parts_produced), (1, 0, 0))


class MacroMovesTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.d)
        self.path = os.path.join(self.d, "rack_change.ngc")
        with open(self.path, "w") as f:
            f.write("\n".join(RACK_CHANGE) + "\n")

    def test_reads_move_lines_once_per_file_version(self):
        mm = MacroMoves()
        self.assertEqual(mm.lines(self.path), frozenset(RACK_MOVES))
        with open(self.path, "a") as f:
            f.write("G0 X1\n")                                       # line 115, new size
        self.assertIn(115, mm.lines(self.path))

    def test_cached_lines_are_not_read_again(self):
        mm = MacroMoves()
        first = mm.lines(self.path)
        orig = open
        import builtins
        builtins.open = lambda *a, **k: self.fail("read again")      # noqa: E731
        try:
            self.assertIs(mm.lines(self.path), first)
        finally:
            builtins.open = orig

    def test_missing_empty_and_big_files_give_no_lines(self):
        mm = MacroMoves()
        self.assertEqual(mm.lines(""), frozenset())
        self.assertEqual(mm.lines(os.path.join(self.d, "nope.ngc")), frozenset())
        big = os.path.join(self.d, "big.ngc")
        with open(big, "w") as f:
            f.write("G0 X1\n" * (MacroMoves.MAX_BYTES // 6 + 10))
        self.assertEqual(mm.lines(big), frozenset())


if __name__ == "__main__":
    unittest.main()
