"""End-to-end part counting: program scan + line tracking + calculator.
Run: python -m unittest -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cycle_time_calculator import CycleTimeCalculator  # noqa: E402
from program_tracker import CycleLineTracker, ProgramInfo  # noqa: E402

# 1 %            6 G0 X10 Y10 ... moves on 6..105
# 2 (header)     106 G0 Z10     (tail move)
# 3 G21 G90      107 M5
# 4 T1 M6        108 M30        (end)
# 5 M3 S8000     109 %
PROGRAM = (["%", "(splash)", "G21 G90", "T1 M6", "M3 S8000"]
           + [f"G1 X{i} Y{i} F500" for i in range(100)]
           + ["G0 Z10", "M5", "M30", "%"])


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

    def test_m20_and_m300_are_not_program_end(self):
        p = ProgramInfo.from_lines(["G0 X1", "M20", "M300", "G0 X2", "M02"])
        self.assertEqual(p.end_line, 5)


class _Machine:
    """Feeds (motion_line, current_line) samples through tracker + calculator
    exactly as status.py's state machine does."""

    def __init__(self):
        self.prog = ProgramInfo.from_lines(PROGRAM)
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

    def run(self, lines, duration_ms=60_000):
        """lines: executed motion lines in order; the cycle then goes idle."""
        self.tracker.begin(*self.idle)
        self.calc.start_cycle(run_from_here=None)
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


class PartCountingScenarioTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
