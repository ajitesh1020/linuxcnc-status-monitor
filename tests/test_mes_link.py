"""Agent <-> MillWright (agent_mes_link.py, MES plan P1.6).
Run: python -m unittest -v test_mes_link
"""
import hashlib
import json
import os
import queue
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_mes_link as ml  # noqa: E402
import agent_mes_spool as ms  # noqa: E402
from test_mes_spool import MACHINE, mw_event  # noqa: E402

V = ms.contract()

# millwright.db as interface §4.3 / plan P1.4 fix it (MillWright's side).
MW_SCHEMA = """
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE mw_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL, occurred_at REAL NOT NULL, body TEXT NOT NULL, audit_id INTEGER);
"""


def ts(t):
    return ms.utc_text(t)


class MillWright:
    """Writes mw_data/ the way MillWright does."""

    def __init__(self, mw_data):
        self.mw_data = mw_data
        os.makedirs(mw_data, exist_ok=True)
        self.recreate()

    def recreate(self):
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(self.mw_data, "millwright.db" + suffix)
            if os.path.exists(p):
                os.unlink(p)
        self.epoch = str(uuid.uuid4())
        with sqlite3.connect(self.db) as db:
            db.executescript(MW_SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO state VALUES ('outbox_epoch', ?), ('station_id', ?)",
                       (self.epoch, str(uuid.uuid4())))

    @property
    def db(self):
        return os.path.join(self.mw_data, "millwright.db")

    def add(self, n):
        with sqlite3.connect(self.db) as db:
            for _ in range(n):
                seq = (db.execute("SELECT seq FROM sqlite_sequence WHERE name='mw_outbox'").fetchone() or (0,))[0] + 1
                body = mw_event(seq, self.epoch)
                db.execute("INSERT INTO mw_outbox(seq, event_id, type, occurred_at, body) VALUES(?,?,?,?,?)",
                           (seq, json.loads(body)["event_id"], "operator.session_started", 0.0, body.decode()))

    def ack(self):
        try:
            with open(os.path.join(self.mw_data, "mes_ack.json")) as f:
                return json.load(f)
        except OSError:
            return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mw_data = os.path.join(self.tmp.name, "config", "mw_data")
        self.spool_path = os.path.join(self.tmp.name, "agent", "mes_spool.db")

    def spool(self, **kw):
        s = ms.Spool(self.spool_path, MACHINE, "4f1c2a9e7b3d5c60", "1.7.0", **kw)
        self.addCleanup(s.close)
        return s

    def stored(self, kind="millwright"):
        with sqlite3.connect(self.spool_path) as db:
            out = [json.loads(b) for (b,) in db.execute("SELECT body FROM spool ORDER BY seq")]
        return [e for e in out if e["producer"]["kind"] == kind]

    def current(self, **kw):
        os.makedirs(self.mw_data, exist_ok=True)
        doc = {"execution_id": str(uuid.uuid4()), "started_at": ts(time.time()),
               "program_path": "/nc/bracket.ngc", "run_file": "/nc/bracket.ngc", "mode": "normal",
               "station_id": str(uuid.uuid4()), "operator_session_id": str(uuid.uuid4())}
        doc.update(kw)
        with open(os.path.join(self.mw_data, "current_execution.json"), "w") as f:
            json.dump(doc, f)
        return doc


class MatchTests(Base):
    def test_same_run(self):
        now = time.time()
        for delta in (-2.99, -0.2, 0.0, 0.9):           # timestamps keep ms only
            self.assertTrue(ml.match(self.current(started_at=ts(now + delta)), "/nc/bracket.ngc", now), delta)
        gen = self.current(started_at=ts(now), program_path="/nc/bracket.ngc", run_file="/mw/gen/atc_run_1.ngc")
        self.assertTrue(ml.match(gen, "/mw/gen/atc_run_1.ngc", now))          # Job tab: the generated file

    def test_not_the_same_run(self):
        now = time.time()
        cases = [dict(started_at=ts(now - 3.5)), dict(started_at=ts(now + 1.5)), dict(program_path="/nc/other.ngc",
                 run_file="/nc/other.ngc"), dict(ended_at=ts(now)), dict(execution_id="not-a-uuid"),
                 dict(execution_id=str(uuid.uuid4()).upper()), dict(operator_session_id=42),
                 dict(started_at="2026-10-22 09:08:10")]
        for kw in cases:
            self.assertFalse(ml.match(self.current(**kw), "/nc/bracket.ngc", now), kw)
        self.assertFalse(ml.match(None, "/nc/bracket.ngc", now))

    def test_file_over_1_kb_is_ignored(self):
        doc = self.current(program_path="/nc/" + "x" * 1100)
        self.assertIsNone(ml.read_current(self.mw_data))
        self.assertFalse(ml.match(ml.read_current(self.mw_data), doc["program_path"], time.time()))


class CycleLinkTests(Base):
    def link(self, size=8, wall=time.time):
        q = queue.Queue(size)
        return ml.CycleLink(self.mw_data, q, wall), q

    def test_explicit_when_millwright_wrote_the_run(self):
        doc = self.current()
        link, q = self.link()
        link.start("/nc/bracket.ngc")
        link.end("completed")
        f = q.get_nowait()
        self.assertEqual((f["execution_id"], f["operator_session_id"], f["method"], f["outcome"]),
                         (doc["execution_id"], doc["operator_session_id"], "explicit", "completed"))

    def test_own_id_and_unknown_operator_otherwise(self):
        link, q = self.link()
        link.start("/nc/bracket.ngc")
        link.end("aborted")
        f = q.get_nowait()
        self.assertEqual((f["method"], f["operator_session_id"]), ("none", None))
        self.assertEqual(str(uuid.UUID(f["execution_id"])), f["execution_id"])

    def test_late_file_is_picked_up_in_the_first_seconds_only(self):
        clock = [time.time()]
        link, q = self.link(wall=lambda: clock[0])
        link.start("/nc/bracket.ngc")
        doc = self.current(started_at=ts(clock[0] + 0.4))          # MillWright saw the start a bit later
        clock[0] += 0.6
        link.poll()
        link.end("completed")
        self.assertEqual(q.get_nowait()["execution_id"], doc["execution_id"])
        os.unlink(os.path.join(self.mw_data, "current_execution.json"))   # MillWright: run ended
        link.start("/nc/bracket.ngc")
        clock[0] += 5.0
        self.current(started_at=ts(clock[0]))                      # far too late: another run
        link.poll()
        link.end("completed")
        self.assertEqual(q.get_nowait()["method"], "none")

    def test_full_queue_never_blocks_the_loop(self):
        link, q = self.link(size=1)
        for _ in range(3):
            link.start("/nc/bracket.ngc")
            t0 = time.monotonic()
            with self.assertLogs("agent_mes_link", "ERROR") if q.full() else _null():
                link.end("completed")
            self.assertLess(time.monotonic() - t0, 0.05)
        self.assertEqual((q.qsize(), link.dropped), (1, 2))

    def test_end_does_not_wait_for_a_stalled_spool(self):
        s = self.spool()
        link, q = self.link()
        worker = ml.MesWorker(s, "", q, poll_s=0.01)
        s._lock.acquire()                                          # the spool hangs (disk stalled)
        try:
            worker.start()
            for _ in range(5):
                link.start("/nc/bracket.ngc")
                t0 = time.monotonic()
                link.end("completed")
                self.assertLess(time.monotonic() - t0, 0.05)
        finally:
            s._lock.release()
        worker.stop()
        self.assertEqual(len(self.stored("agent")), 5)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WorkerTests(Base):
    def test_cycle_observed_is_valid(self):
        s = self.spool()
        q = queue.Queue(8)
        link = ml.CycleLink(self.mw_data, q)
        doc = self.current()
        link.start("/nc/bracket.ngc")
        link.end("completed")
        ml.MesWorker(s, "", q).run_once()
        (e,) = self.stored("agent")
        self.assertEqual(e["type"], "cycle.observed")
        self.assertEqual(V.validate_event(e), [])
        self.assertEqual(e["correlation"]["execution_id"], doc["execution_id"])
        self.assertEqual(e["payload"]["correlation_method"], "explicit")

    def test_facts_wait_while_the_spool_is_full(self):
        s = self.spool()
        q = queue.Queue(8)
        w = ml.MesWorker(s, "", q)
        link = ml.CycleLink("", q)
        link.start("/a.ngc")
        link.end("completed")
        budget, s.budget = s.budget, 1
        w.run_once()
        self.assertEqual(self.stored("agent"), [])
        s.budget = budget
        w.run_once()
        self.assertEqual(len(self.stored("agent")), 1)


class OutboxTests(Base):
    def test_copied_unchanged_in_order_with_ack(self):
        mw = MillWright(self.mw_data)
        mw.add(5)
        s = self.spool()
        r = ml.OutboxReader(self.mw_data, s)
        self.assertEqual(r.step(), 5)
        with sqlite3.connect(mw.db) as db:
            bodies = [b for (b,) in db.execute("SELECT body FROM mw_outbox ORDER BY seq")]
        with sqlite3.connect(self.spool_path) as db:
            self.assertEqual([b for (b,) in db.execute("SELECT body FROM spool ORDER BY seq")], bodies)
        self.assertEqual(s.cursor("millwright"), (mw.epoch, 5))
        self.assertEqual(mw.ack(), {"epoch": mw.epoch, "seq": 5})
        mw.add(2)
        self.assertEqual(r.step(), 2)
        self.assertEqual([e["producer"]["seq"] for e in self.stored()], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(mw.ack()["seq"], 7)

    def test_never_writes_millwright_db(self):
        mw = MillWright(self.mw_data)
        mw.add(3)
        with open(mw.db, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        r = ml.OutboxReader(self.mw_data, self.spool())
        r.step()
        db = r._connect()
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM mw_outbox")
        db.close()
        with open(mw.db, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), before)

    def test_restart_does_not_read_twice(self):
        mw = MillWright(self.mw_data)
        mw.add(4)
        s = self.spool()
        ml.OutboxReader(self.mw_data, s).step()
        s.close()
        os.unlink(os.path.join(self.mw_data, "mes_ack.json"))
        s = self.spool()
        r = ml.OutboxReader(self.mw_data, s)
        self.assertEqual(r.step(), 0)
        self.assertEqual(mw.ack(), {"epoch": mw.epoch, "seq": 4})  # republished after the restart
        self.assertEqual(len(self.stored()), 4)

    def test_new_millwright_epoch_is_read_from_the_start(self):
        mw = MillWright(self.mw_data)
        mw.add(3)
        s = self.spool()
        r = ml.OutboxReader(self.mw_data, s)
        r.step()
        old = mw.epoch
        mw.recreate()
        mw.add(2)
        with self.assertLogs("agent_mes_link", "WARNING"):
            self.assertEqual(r.step(), 2)
        self.assertEqual([(e["producer"]["epoch"], e["producer"]["seq"]) for e in self.stored()],
                         [(old, 1), (old, 2), (old, 3), (mw.epoch, 1), (mw.epoch, 2)])
        self.assertEqual(mw.ack(), {"epoch": mw.epoch, "seq": 2})

    def test_full_spool_leaves_events_in_the_outbox(self):
        mw = MillWright(self.mw_data)
        mw.add(3)
        s = self.spool()
        budget, s.budget = s.budget, 1
        r = ml.OutboxReader(self.mw_data, s)
        with self.assertLogs("agent_mes_link", "ERROR"):
            self.assertEqual(r.step(), 0)
        self.assertEqual(r.step(), 0)                              # logged once
        self.assertIsNone(mw.ack())
        self.assertIsNone(s.cursor("millwright"))
        s.budget = budget
        self.assertEqual(r.step(), 3)
        self.assertEqual(mw.ack()["seq"], 3)

    def test_rejected_event_stops_reading_and_is_not_skipped(self):
        mw = MillWright(self.mw_data)
        mw.add(2)
        with sqlite3.connect(mw.db) as db:
            db.execute("UPDATE mw_outbox SET body=? WHERE seq=2", ('{"broken": true}',))
        mw.add(1)
        s = self.spool()
        r = ml.OutboxReader(self.mw_data, s)
        with self.assertLogs("agent_mes_link", "ERROR"):
            r.step()
        self.assertEqual((len(self.stored()), mw.ack()), (0, None))   # the batch waits, nothing skipped


CHILD = r"""
import sys
sys.path.insert(0, %(root)r)
import agent_mes_link as ml, agent_mes_spool as ms
ml.BATCH = 7
s = ms.Spool(%(spool)r, %(machine)r, "boot", "1.7.0")
r = ml.OutboxReader(%(mw)r, s)
print("ready", flush=True)
while r.step():
    pass
print("done", flush=True)
"""


class KillTests(Base):
    def test_kill_9_between_spool_and_ack_loses_or_doubles_nothing(self):
        mw = MillWright(self.mw_data)
        mw.add(300)
        code = CHILD % {"root": ROOT, "spool": self.spool_path, "machine": MACHINE, "mw": self.mw_data}
        rnd = random.Random(11)
        for _ in range(40):
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.stdout.readline().strip() != b"ready":
                p.kill()
                self.fail(p.stderr.read())
            time.sleep(rnd.uniform(0.0, 0.15))
            p.send_signal(signal.SIGKILL)
            p.wait()
            p.stdout.close()
            p.stderr.close()
            seqs = [e["producer"]["seq"] for e in self.stored()]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))          # in order, no gap, no double
            ack = mw.ack()
            if ack:
                self.assertLessEqual(ack["seq"], len(seqs))                # never ahead of the spool
            if len(seqs) == 300:
                break
        s = self.spool()
        ml.OutboxReader(self.mw_data, s).step()                            # finish what is left
        while ml.OutboxReader(self.mw_data, s).step():
            pass
        self.assertEqual([e["producer"]["seq"] for e in self.stored()], list(range(1, 301)))
        self.assertEqual(mw.ack(), {"epoch": mw.epoch, "seq": 300})


if __name__ == "__main__":
    unittest.main()
