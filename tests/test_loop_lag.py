"""loop_lag_ms (agent_packets.LoopLag, MES plan P2.7).
Run: python -m unittest -v test_loop_lag
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_packets import LoopLag  # noqa: E402


class LoopLagTests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(LoopLag().stats())

    def test_percentiles_and_max(self):
        lag = LoopLag()
        for i in range(100):
            lag.add(1000.0 + i * 0.1, float(i))          # 0..99 ms
        self.assertEqual(lag.stats(), {"p50": 50, "p99": 99, "max": 99})
        lag.add(1010.0, 250.4)
        self.assertEqual(lag.stats()["max"], 250)
        self.assertTrue(all(isinstance(v, int) for v in lag.stats().values()))

    def test_only_the_last_60_seconds(self):
        lag = LoopLag()
        lag.add(0.0, 900.0)                               # an old spike...
        for i in range(10):
            lag.add(61.0 + i, 2.0)
        self.assertEqual(lag.stats(), {"p50": 2, "p99": 2, "max": 2})   # ...is gone

    def test_early_samples_count_as_zero(self):
        lag = LoopLag()
        lag.add(1.0, -3.0)
        self.assertEqual(lag.stats(), {"p50": 0, "p99": 0, "max": 0})


if __name__ == "__main__":
    unittest.main()
