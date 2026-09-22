"""Tests for run-from-here continuation in CycleTimeCalculator.
Run: python -m unittest -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cycle_time_calculator import CycleTimeCalculator  # noqa: E402


class RunFromHereContinuationTests(unittest.TestCase):
    def setUp(self):
        self.calc = CycleTimeCalculator()

    def _stop_with(self, ms):
        # Force a deterministic elapsed time for the pending stop.
        self.calc._elapsed_ms_unsafe = lambda: ms  # type: ignore[assignment]
        self.calc.stop_cycle()

    def test_abort_carries_time_then_rfh_completes_as_part(self):
        # 1) Fresh run, stopped before the end line → abort + carry.
        self.calc.start_cycle(run_from_here=False)
        self._stop_with(5000)
        snap = self.calc.snapshot()
        self.assertEqual(snap.abort_count, 1)
        self.assertEqual(snap.parts_produced, 0)
        self.assertEqual(self.calc._carry_ms, 5000)

        # 2) Run From Here, reaches the end line → counts as a part, total time
        #    includes the carried 5000 ms, and carry resets.
        self.calc.start_cycle(run_from_here=True)
        self.calc.signal_cycle_complete()
        self._stop_with(12000)   # carried 5000 + 7000 more
        snap = self.calc.snapshot()
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.last_completed_ms, 12000)
        self.assertEqual(self.calc._carry_ms, 0)

    def test_fresh_complete_counts_and_no_carry(self):
        self.calc.start_cycle(run_from_here=False)
        self.calc.signal_cycle_complete()
        self._stop_with(9000)
        snap = self.calc.snapshot()
        self.assertEqual(snap.parts_produced, 1)
        self.assertEqual(snap.last_completed_ms, 9000)
        self.assertEqual(self.calc._carry_ms, 0)

    def test_rfh_incomplete_keeps_carry_without_abort(self):
        self.calc.start_cycle(run_from_here=False)
        self._stop_with(4000)             # abort #1, carry 4000
        self.calc.start_cycle(run_from_here=True)
        self._stop_with(6000)             # continuation, still not complete
        snap = self.calc.snapshot()
        self.assertEqual(snap.abort_count, 1)     # no new abort for the continuation
        self.assertEqual(self.calc._carry_ms, 6000)

    def test_tiny_fresh_cycle_discarded(self):
        self.calc.start_cycle(run_from_here=False)
        self._stop_with(500)              # < 1000 ms minimum, no carry
        snap = self.calc.snapshot()
        self.assertEqual(snap.abort_count, 0)
        self.assertEqual(snap.parts_produced, 0)


if __name__ == "__main__":
    unittest.main()
