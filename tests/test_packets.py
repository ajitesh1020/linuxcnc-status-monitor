"""Tests for packet timing: idle suppression and immediate fault edges.
Run: python -m unittest -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_packets import PacketGate, fault_signature  # noqa: E402


class FakeStat:
    def __init__(self, **kw):
        self.estop = False
        self.task_state = 4
        self.exec_state = 2
        self.joints = 2
        self.joint = [{"fault": False}, {"fault": False}]
        self.__dict__.update(kw)


class FaultSignatureTests(unittest.TestCase):
    def test_each_fault_flag_changes_the_signature(self):
        base = fault_signature(FakeStat())
        self.assertEqual(fault_signature(FakeStat()), base)
        for stat in (FakeStat(estop=True), FakeStat(task_state=3),
                     FakeStat(exec_state=1),
                     FakeStat(joint=[{"fault": True}, {"fault": False}]),
                     FakeStat(joint=[{"fault": False}, {"max_hard_limit": True}])):
            self.assertNotEqual(fault_signature(stat), base)

    def test_ordinary_exec_states_are_not_faults(self):
        # While running exec_state moves between DONE / WAITING_FOR_* ...
        self.assertEqual(fault_signature(FakeStat(exec_state=7)),
                         fault_signature(FakeStat(exec_state=2)))

    def test_missing_or_broken_fields_do_not_raise(self):
        class Broken:
            @property
            def joint(self):
                raise RuntimeError("nml")
            joints = 3
        sig = fault_signature(Broken())
        self.assertEqual(sig[3], ())
        self.assertEqual(fault_signature(object())[:3], (False, 0, False))


class PacketGateTests(unittest.TestCase):
    """Simulate the 10 Hz sample loop; return the times a packet is sent."""

    def run_loop(self, seconds, state_at, sig_at, errors_at=lambda t: False,
                 poll=1.0, heartbeat=30.0, dt=0.1):
        gate, sent = PacketGate(poll, heartbeat), []
        for i in range(int(round(seconds / dt))):
            t = round(i * dt, 3)
            state = state_at(t)
            if not gate.tick(t, state, sig_at(t)):
                continue
            if gate.should_send(t, state, errors_at(t)):
                gate.sent(t, state)
                sent.append(t)
        return sent

    def test_idle_sends_one_packet_then_heartbeats(self):
        sent = self.run_loop(65, lambda t: "IDLE", lambda t: ("ok",))
        self.assertEqual(sent, [0.0, 30.0, 60.0])

    def test_running_sends_every_poll_interval(self):
        sent = self.run_loop(3.15, lambda t: "RUNNING", lambda t: ("ok",))
        self.assertEqual(sent, [0.0, 1.0, 2.0, 3.0])

    def test_short_idle_fault_pulse_sends_both_edges_at_once(self):
        # A fault set at 5.0 s and cleared at 5.3 s — between heartbeats.
        sig = lambda t: ("fault",) if 5.0 <= t < 5.3 else ("ok",)
        sent = self.run_loop(20, lambda t: "IDLE", sig)
        self.assertEqual(sent, [0.0, 5.0, 5.3])

    def test_fault_edge_inside_poll_interval_while_running(self):
        sig = lambda t: ("fault",) if t >= 1.4 else ("ok",)
        sent = self.run_loop(2.55, lambda t: "RUNNING", sig)
        self.assertEqual(sent, [0.0, 1.0, 1.4, 2.4])

    def test_cycle_state_change_is_immediate(self):
        state = lambda t: "RUNNING" if t >= 2.35 else "IDLE"
        sent = self.run_loop(3.0, state, lambda t: ("ok",))
        self.assertEqual(sent[:2], [0.0, 2.4])

    def test_caught_message_is_never_suppressed(self):
        sent = self.run_loop(5, lambda t: "IDLE", lambda t: ("ok",),
                             errors_at=lambda t: t == 3.0)
        self.assertEqual(sent, [0.0, 3.0])

    def test_unsent_fault_edge_stays_pending(self):
        gate = PacketGate(1.0, 30.0)
        self.assertTrue(gate.tick(0.0, "IDLE", ("ok",)))
        gate.should_send(0.0, "IDLE"); gate.sent(0.0, "IDLE")
        self.assertTrue(gate.tick(0.1, "IDLE", ("fault",)))
        self.assertTrue(gate.should_send(0.1, "IDLE"))
        # The packet could not be built (no sent()) — next sample tries again.
        self.assertTrue(gate.tick(0.2, "IDLE", ("fault",)))
        self.assertTrue(gate.should_send(0.2, "IDLE"))
        gate.sent(0.2, "IDLE")
        self.assertFalse(gate.tick(0.3, "IDLE", ("fault",)))


if __name__ == "__main__":
    unittest.main()
