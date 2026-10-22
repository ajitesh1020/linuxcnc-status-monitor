"""tools/fleet_sim.py: a short run against a local listener, then `check`.
Run: python -m unittest -v
"""
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fleet_sim  # noqa: E402


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FleetSimTests(unittest.TestCase):
    def test_short_run_sends_agent_packets_and_writes_truth(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listen.bind(("127.0.0.1", 0))
        listen.settimeout(0.2)
        self.addCleanup(listen.close)
        got, stop = [], threading.Event()

        def receive():
            while not stop.is_set():
                try:
                    got.append(json.loads(listen.recvfrom(65535)[0]))
                except socket.timeout:
                    continue

        t = threading.Thread(target=receive, daemon=True)
        t.start()
        gcode = os.path.join(tmp.name, "p.ngc")
        with open(gcode, "w") as f:
            f.write("G0 X0\n" + "G1 X1 F100\n" * 500 + "M30\n")
        base = _free_port()
        rc = fleet_sim.main([
            "run", "--machines", "3", "--minutes", "0.08", "--settle", "1",
            "--target", f"127.0.0.1:{listen.getsockname()[1]}",
            "--journal-base-port", str(base), "--dir", tmp.name, "--gcode", gcode,
            "--cycle", "0.5,1", "--idle", "0.2,0.5", "--cmd-error-rate", "3000",
            "--estop-rate", "1000", "--seed", "3", "--report-s", "999"])
        stop.set()
        t.join(1)
        self.assertEqual(rc, 0)
        truth = json.load(open(os.path.join(tmp.name, "truth.json")))
        self.assertTrue(truth["final"])
        self.assertEqual(sorted(truth["machines"]), ["SIM-001", "SIM-002", "SIM-003"])
        status = [p for p in got if p.get("type") == "status"]
        self.assertTrue(any(p.get("type") == "gcode_file" for p in got))
        for name in truth["machines"]:
            mine = [p for p in status if p["machine_name"] == name]
            self.assertTrue(mine)
            for p in mine:
                for k in ("session_id", "packet_seq", "cmd_state", "cmd_serial",
                          "agent_id", "journal_seq", "journal_port"):
                    self.assertIn(k, p)
            by_session = {}
            for p in mine:
                by_session.setdefault(p["session_id"], []).append(p["packet_seq"])
            for seqs in by_session.values():
                self.assertEqual(seqs, sorted(seqs))
        self.assertGreater(sum(m["cmd_failures"] for m in truth["machines"].values()), 0)

        # `check` against a database holding exactly the truth passes, and fails
        # when one part is missing.
        db = os.path.join(tmp.name, "history.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE completed_cycles (machine_key TEXT, ended_ts_ms INT)")
        conn.execute("CREATE TABLE machine_events (machine_key TEXT, ts_ms INT, text TEXT)")
        for name, m in truth["machines"].items():
            conn.executemany("INSERT INTO completed_cycles VALUES (?, ?)",
                             [(name, i) for i in range(m["parts"])])
            rows = ([fleet_sim.EV_ESTOP[0]] * m["estops"] + [fleet_sim.EV_CMD] * m["cmd_failures"]
                    + [fleet_sim.EV_ABORT + " at line 3"] * m["aborts"])
            for j, n in enumerate(m["joint_faults"]):
                rows += [fleet_sim.EV_JOINT.format(j)] * n
            conn.executemany("INSERT INTO machine_events VALUES (?, ?, ?)",
                             [(name, i, text) for i, text in enumerate(rows)])
        conn.commit()
        truth_path = os.path.join(tmp.name, "truth.json")
        self.assertEqual(fleet_sim.main(["check", "--truth", truth_path, "--db", db]), 0)
        conn.execute("DELETE FROM completed_cycles WHERE rowid = (SELECT MIN(rowid) FROM "
                     "completed_cycles)")
        conn.commit()
        conn.close()
        if sum(m["parts"] for m in truth["machines"].values()):
            self.assertEqual(fleet_sim.main(["check", "--truth", truth_path, "--db", db]), 1)


    def test_every_counted_fault_is_visible(self):
        # A new E-stop / joint fault starting on the very sample the previous one
        # ends would merge into one fault no packet can show; truth must not count it.
        import argparse
        import random
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        args = argparse.Namespace(
            prefix="SIM-", dir=tmp.name, journal_interval=10.0, journal_base_port=0,
            journal_host="127.0.0.1", poll=1.0, heartbeat=30.0, gcode_resend=60.0,
            cycle="20,60", idle="3,15", abort_prob=0.0, estop_rate=1e6,
            joint_fault_rate=1e6, cmd_error_rate=0.0, restart_rate=0.0)
        sender = fleet_sim.Sender(("127.0.0.1", _free_port()), 0.0, 0.0, random.Random(1))
        self.addCleanup(sender.sock.close)
        m = fleet_sim.SimMachine(0, args, random.Random(7), sender, None)
        self.addCleanup(m.close)
        estop_edges, joint_edges = 0, [0] * fleet_sim.JOINTS
        prev = m._signature(0.0)
        now = 0.0
        for _ in range(3000):
            now += 0.1
            m.step(now, 0.1, quiet=False)
            sig = m._signature(now)
            estop_edges += sig[0] and not prev[0]
            for j, (fault, _) in enumerate(sig[3]):
                joint_edges[j] += fault and not prev[3][j][0]
            prev = sig
        self.assertGreater(m.truth["estops"], 20)
        self.assertEqual(m.truth["estops"], estop_edges)
        self.assertEqual(m.truth["joint_faults"], joint_edges)


if __name__ == "__main__":
    unittest.main()
