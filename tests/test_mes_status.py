"""Operator readout: mes_status.json and --mes-status (agent_mes_status.py, MES plan P2.6).
Run: python -m unittest -v test_mes_status
"""
from contextlib import closing
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_mes_link as ml  # noqa: E402
import agent_mes_status as mst  # noqa: E402
from test_mes_link import Base, MillWright, V  # noqa: E402

MACHINE = "65f21446-90e2-45d4-8ecb-3748fd200688"


def session_event(mw, kind, name="Ajitesh", role="operator", session=None):
    """Append operator.session_<kind> to the fake MillWright's outbox; returns the session id."""
    session = session or str(uuid.uuid4())
    with open(os.path.join(ROOT, "contracts", "mes", "v1", "fixtures", "valid",
                           "operator_session_%s.json" % kind), encoding="utf-8") as f:
        e = json.load(f)
    with closing(sqlite3.connect(mw.db)) as db, db:
        seq = (db.execute("SELECT seq FROM sqlite_sequence WHERE name='mw_outbox'").fetchone() or (0,))[0] + 1
        e["event_id"] = str(uuid.uuid4())
        e["producer"] = {"kind": "millwright", "id": str(uuid.uuid4()), "epoch": mw.epoch, "seq": seq}
        e["site_id"] = e["machine_id"] = None
        e["payload"].update(operator_name=name, role=role)
        e["correlation"]["operator_session_id"] = session
        body = V.encode(e).decode()
        db.execute("INSERT INTO mw_outbox(seq, event_id, type, occurred_at, body) VALUES(?,?,?,?,?)",
                   (seq, e["event_id"], "operator.session_" + kind, 0.0, body))
    return session


class OperatorTests(Base):
    def test_login_and_logout_are_followed(self):
        mw = MillWright(self.mw_data)
        r = ml.OutboxReader(self.mw_data, self.spool())
        r.step()
        self.assertIsNone(r.operator)
        sid = session_event(mw, "started")
        r.step()
        self.assertEqual((r.operator["name"], r.operator["role"], r.operator["operator_session_id"]),
                         ("Ajitesh", "operator", sid))
        self.assertTrue(r.operator["since"].endswith("Z"))
        session_event(mw, "ended", session=sid)
        r.step()
        self.assertIsNone(r.operator)

    def test_after_an_agent_restart_the_login_is_known_at_once(self):
        mw = MillWright(self.mw_data)
        s = self.spool()
        sid = session_event(mw, "started", name="Manager", role="manager")
        ml.OutboxReader(self.mw_data, s).step()            # copied before the restart
        r = ml.OutboxReader(self.mw_data, s)               # agent restarted: nothing new to copy
        self.assertEqual(r.step(), 0)
        self.assertEqual((r.operator["name"], r.operator["operator_session_id"]), ("Manager", sid))

    def test_catching_up_never_shows_an_older_login(self):
        mw = MillWright(self.mw_data)
        a = session_event(mw, "started", name="A")
        session_event(mw, "ended", session=a)
        session_event(mw, "started", name="B")
        r = ml.OutboxReader(self.mw_data, self.spool())
        seen = []
        orig = r._see_operator

        def spy(seq, body):
            orig(seq, body)
            seen.append(r.operator and r.operator["name"])
        r._see_operator = spy
        r.step()
        self.assertEqual(set(seen), {"B"})                 # A's rows are older than the latest: skipped
        self.assertEqual(r.operator["name"], "B")

    def test_new_millwright_database_forgets_the_old_login(self):
        mw = MillWright(self.mw_data)
        session_event(mw, "started")
        r = ml.OutboxReader(self.mw_data, self.spool())
        r.step()
        mw.recreate()
        with self.assertLogs("agent_mes_link", "WARNING"):
            r.step()
        self.assertIsNone(r.operator)

    def test_login_is_shown_even_when_the_spool_is_full(self):
        mw = MillWright(self.mw_data)
        r = ml.OutboxReader(self.mw_data, self.spool(budget_mb=0.001))
        session_event(mw, "started")
        with self.assertLogs("agent_mes_link", "ERROR"):
            r.step()
        self.assertEqual(r.operator["name"], "Ajitesh")


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, mst.FILE_NAME)
        self.now = [100.0]
        self.wall = [1_800_000_000.0]
        self.queue = {"pending": 3, "bytes": 4096, "state": "ok"}
        self.link = {"state": "ok", "last_ok_ms": 1_799_999_990_000}
        self.operator = None

    def writer(self):
        return mst.StatusWriter(self.path, lambda: mst.build(MACHINE, self.queue, self.link, self.operator),
                                clock=lambda: self.now[0], wall=lambda: self.wall[0])

    def doc(self):
        with open(self.path) as f:
            return json.load(f)

    def test_document(self):
        w = self.writer()
        self.operator = {"name": "Ajitesh", "role": "operator", "assurance": "selected_name",
                         "operator_session_id": str(uuid.uuid4()), "since": "2026-10-26T07:16:00.000Z"}
        self.assertTrue(w.step())
        d = self.doc()
        self.assertEqual(d["schema"], mst.SCHEMA)
        self.assertEqual(d["updated_at_ms"], 1_800_000_000_000)
        self.assertTrue(d["running"])
        self.assertEqual(d["machine_uuid"], MACHINE)
        self.assertEqual(d["operator"]["name"], "Ajitesh")
        self.assertEqual(d["queue"], self.queue)
        self.assertEqual(d["link"], self.link)
        self.assertIsNone(d["scan"])                       # no scanners yet (P2.1)
        self.assertEqual(d["bound"], [])
        self.assertEqual(os.listdir(self.tmp.name), [mst.FILE_NAME])   # no temp file left

    def test_written_on_change_and_refreshed_while_idle(self):
        w = self.writer()
        self.assertTrue(w.step())
        self.now[0] += 1
        self.assertFalse(w.step())                         # nothing changed
        self.queue = dict(self.queue, pending=4)
        self.assertTrue(w.step())                          # changed
        self.now[0] += mst.REFRESH_S - 0.1
        self.assertFalse(w.step())
        self.now[0] += 0.2
        self.wall[0] += 6
        self.assertTrue(w.step())                          # refreshed: readers see the agent alive
        self.assertEqual(self.doc()["updated_at_ms"], 1_800_000_006_000)

    def test_at_most_two_writes_a_second(self):
        w = mst.StatusWriter(self.path, lambda: mst.build(MACHINE, {"pending": time.monotonic_ns()}, None, None))
        writes = []
        orig = w.step
        w.step = lambda **kw: writes.append(orig(**kw))
        w.start()
        time.sleep(1.2)
        w._halt.set()
        w.join(5)
        self.assertLessEqual(sum(writes), 3)               # changes every call, still ≤ 2 Hz

    def test_stop_writes_running_false(self):
        w = self.writer()
        w.start()
        w.stop()
        self.assertFalse(self.doc()["running"])

    def test_write_error_is_logged_once_and_never_raises(self):
        w = mst.StatusWriter(os.path.join(self.tmp.name, "missing-dir", mst.FILE_NAME),
                             lambda: mst.build(MACHINE, None, None, None))
        with self.assertLogs("agent_mes_status", "ERROR") as cm:
            self.assertFalse(w.step())
            self.assertFalse(w.step(force=True))
        self.assertEqual(len(cm.records), 1)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, mst.FILE_NAME)
        self.wall = 1_800_000_000.0

    def put(self, **kw):
        d = dict(mst.build(MACHINE, {"pending": 2, "bytes": 1, "state": "ok"}, {"state": "ok"}, None),
                 updated_at_ms=int(self.wall * 1000))
        d.update(kw)
        mst.write_atomic(self.path, d)

    def read(self, after=0.0):
        return mst.read(self.path, wall=lambda: self.wall + after)

    def test_states(self):
        self.assertEqual(self.read()[2], "missing")
        with open(self.path, "w") as f:
            f.write("{half")
        self.assertEqual(self.read()[2], "unreadable")
        self.put(schema="something-else/1")
        self.assertEqual(self.read()[2], "unreadable")
        self.put()
        doc, age, problem = self.read(after=2)
        self.assertEqual((problem, round(age)), ("", 2))
        self.assertEqual(self.read(after=mst.STALE_S + 1)[2], "stale")
        self.put(running=False)
        self.assertEqual(self.read()[2], "stopped")

    def test_remove(self):
        self.put()
        mst.remove(self.path)
        mst.remove(self.path)                              # already gone: fine
        self.assertFalse(os.path.exists(self.path))


class FormatTests(unittest.TestCase):
    wall = staticmethod(lambda: 1_800_000_000.0)

    def lines(self, problem="", **kw):
        d = mst.build(MACHINE, kw.pop("queue", {"pending": 0}), kw.pop("link", {"state": "ok",
                      "last_ok_ms": 1_799_999_995_000}), kw.pop("operator", None), **kw)
        d["updated_at_ms"] = 1_799_999_999_000
        return "\n".join(mst.format_lines(d, 1.0, problem, wall=self.wall))

    def test_normal(self):
        out = self.lines(operator={"name": "Ajitesh", "role": "operator", "assurance": "selected_name"})
        self.assertIn("operator : Ajitesh (operator, selected_name)", out)
        self.assertIn("last scan: none", out)
        self.assertIn("queue    : 0 waiting to send", out)
        self.assertIn("link     : ok, last OK 5 s ago  [green]", out)

    def test_nobody_full_spool_and_link_states(self):
        out = self.lines(queue={"pending": 9, "state": "full"}, link={"state": "rejected"})
        self.assertIn("nobody logged in", out)
        self.assertIn("SPOOL FULL, new scans refused  [red]", out)
        self.assertIn("link     : rejected, last OK never  [red]", out)
        self.assertIn("[grey]", self.lines(link={"state": "not_enrolled"}))
        self.assertIn("[amber]", self.lines(link={"state": "offline"}))

    def test_scan_colours(self):
        for status, colour in mst.SCAN_COLOUR.items():
            out = self.lines(scan={"code": "LOT-42", "status": status, "reason": "unknown_code"
                                   if status == "rejected" else None})
            self.assertIn("last scan: LOT-42  %s" % status, out)
            self.assertIn("[%s]" % colour, out)
        self.assertIn("— unknown_code", self.lines(scan={"code": "X", "status": "rejected",
                                                         "reason": "unknown_code"}))

    def test_stale_stopped_missing(self):
        self.assertIn("agent NOT RUNNING", self.lines("stale"))
        self.assertIn("agent stopped", self.lines("stopped"))
        self.assertIn("MES is off", "\n".join(mst.format_lines(None, None, "missing")))
        self.assertIn("unreadable", "\n".join(mst.format_lines(None, None, "unreadable")))


if __name__ == "__main__":
    unittest.main()
