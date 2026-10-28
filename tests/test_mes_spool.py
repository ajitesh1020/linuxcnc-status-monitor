"""MES spool (agent_mes_spool.py, MES plan P2.4).
Run: python -m unittest -v test_mes_spool
"""
import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import agent_mes_spool as ms  # noqa: E402

V = ms.contract()
MACHINE = "8d7c6b5a-4f3e-4d2c-8b1a-0f9e8d7c6b5a"
SCANNER = {"scanner_id": "Scanner A", "state": "connected"}


def mw_event(seq, epoch, producer="4d5e6f70-8192-43a4-b5c6-d7e8f9011223"):
    """A MillWright outbox body, as MillWright encodes it."""
    with open(os.path.join(ROOT, "contracts", "mes", "v1", "fixtures", "valid",
                           "operator_session_started.json"), encoding="utf-8") as f:
        e = json.load(f)
    e["event_id"] = str(uuid.uuid4())
    e["producer"] = {"kind": "millwright", "id": producer, "epoch": epoch, "seq": seq}
    e["site_id"] = e["machine_id"] = None
    return V.encode(e)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "agent", "mes_spool.db")
        self.clock = [1_800_000_000.0]

    def spool(self, **kw):
        kw.setdefault("budget_mb", 1024)
        s = ms.Spool(self.path, MACHINE, "4f1c2a9e7b3d5c60", "1.7.0", now=lambda: self.clock[0], **kw)
        self.addCleanup(s.close)
        return s

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT seq, event_id, type, occurred_at, body, state FROM spool ORDER BY seq").fetchall()

    def own(self):
        return [json.loads(r[4]) for r in self.rows() if json.loads(r[4])["producer"]["kind"] == "agent"]


class SchemaTests(Base):
    def test_schema_exactly_as_the_plan(self):
        s = self.spool()
        with sqlite3.connect(self.path) as db:
            cols = {t: [(c[1], c[2], c[3], c[4], c[5]) for c in db.execute("PRAGMA table_info(%s)" % t)]
                    for t in ("meta", "spool", "cursors")}
        self.assertEqual(cols["meta"], [("key", "TEXT", 0, None, 1), ("value", "TEXT", 1, None, 0)])
        self.assertEqual(cols["spool"], [
            ("seq", "INTEGER", 0, None, 1), ("event_id", "TEXT", 1, None, 0), ("type", "TEXT", 1, None, 0),
            ("occurred_at", "INTEGER", 1, None, 0), ("body", "TEXT", 1, None, 0),
            ("state", "TEXT", 1, "'pending'", 0), ("attempts", "INTEGER", 1, "0", 0),
            ("acked_at", "INTEGER", 0, None, 0), ("reason", "TEXT", 0, None, 0)])
        self.assertEqual(cols["cursors"], [("source", "TEXT", 0, None, 1), ("epoch", "TEXT", 0, None, 0),
                                           ("position", "INTEGER", 1, None, 0)])
        self.assertEqual(s._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(s._db.execute("PRAGMA synchronous").fetchone()[0], 2)   # FULL
        with sqlite3.connect(self.path) as db:
            meta = dict(db.execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["machine_id"], MACHINE)
        self.assertNotIn("site_id", meta)                                       # null until enrolment
        for k in ("producer_id", "epoch"):
            self.assertEqual(str(uuid.UUID(meta[k])), meta[k])


class OwnEventTests(Base):
    def test_events_are_valid_encoded_and_numbered(self):
        s = self.spool()
        for i in range(3):
            self.assertEqual(s.add("scanner.state", SCANNER, self.clock[0] + i), i + 1)
        rows = self.rows()
        for i, (seq, event_id, type_, occurred, body, state) in enumerate(rows):
            e = json.loads(body)
            self.assertEqual(V.validate_bytes(body.encode()), [])
            self.assertEqual(V.encode(e), body.encode())
            self.assertEqual((e["event_id"], type_, state, occurred), (event_id, "scanner.state", "pending",
                                                                     int((self.clock[0] + i) * 1000)))
            self.assertEqual(e["producer"], {"kind": "agent", "id": s.producer_id, "epoch": s.epoch, "seq": i + 1})
            self.assertEqual((e["machine_id"], e["site_id"]), (MACHINE, None))
            self.assertEqual(e["clock"]["boot_id"], "4f1c2a9e7b3d5c60")
            self.assertEqual(e["source"], {"app": "linuxcnc-status-agent", "version": "1.7.0",
                                           "record": "spool:%d" % seq})
        self.assertEqual(s.stats["pending"], 3)

    def test_rejected_event_stores_nothing(self):
        s = self.spool()
        with self.assertRaises(ms.SpoolError):
            s.add("scanner.state", {"scanner_id": "A", "state": "exploded"}, self.clock[0])
        s.add("scanner.state", SCANNER, self.clock[0])
        self.assertEqual([e["producer"]["seq"] for e in self.own()], [1])   # no gap left


class MillWrightEventTests(Base):
    def test_stored_unchanged_with_the_cursor_in_one_transaction(self):
        s = self.spool()
        epoch = str(uuid.uuid4())
        bodies = [mw_event(i, epoch) for i in (1, 2, 3)]
        s.add("scanner.state", SCANNER, self.clock[0])
        self.assertEqual(s.append(bodies, ("millwright", epoch, 3)), [2, 3, 4])
        self.assertEqual([r[4].encode() for r in self.rows()[1:]], bodies)       # byte for byte
        self.assertEqual(s.cursor("millwright"), (epoch, 3))
        s.add("scanner.state", SCANNER, self.clock[0])
        self.assertEqual([e["producer"]["seq"] for e in self.own()], [1, 2])     # own seq unaffected

    def test_a_bad_body_stores_nothing_and_keeps_the_cursor(self):
        s = self.spool()
        epoch = str(uuid.uuid4())
        s.append([mw_event(1, epoch)], ("millwright", epoch, 1))
        for bad in (b"{not json", mw_event(2, epoch).replace(b'"millwright"', b'"robot"', 1)):
            with self.assertRaises(ms.SpoolError):
                s.append([mw_event(2, epoch), bad], ("millwright", epoch, 3))
        self.assertEqual((len(self.rows()), s.cursor("millwright")), (1, (epoch, 1)))


class EpochTests(Base):
    def test_reopen_keeps_epoch_and_counts_on(self):
        s = self.spool()
        s.add("scanner.state", SCANNER, self.clock[0])
        epoch, producer = s.epoch, s.producer_id
        s.close()
        s = self.spool()
        s.add("scanner.state", SCANNER, self.clock[0])
        self.assertEqual((s.epoch, s.producer_id), (epoch, producer))
        self.assertEqual([e["producer"]["seq"] for e in self.own()], [1, 2])

    def test_recreated_database_new_epoch_same_producer(self):
        s = self.spool()
        s.add("scanner.state", SCANNER, self.clock[0])
        epoch, producer = s.epoch, s.producer_id
        s.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)
        with self.assertLogs("agent_mes_spool", "WARNING"):
            s = self.spool()
        s.add("scanner.state", SCANNER, self.clock[0])
        (e,) = self.own()
        self.assertNotEqual(s.epoch, epoch)
        self.assertEqual((e["producer"]["id"], e["producer"]["epoch"], e["producer"]["seq"]),
                         (producer, s.epoch, 1))

    def test_restored_older_copy_gets_a_new_epoch(self):
        s = self.spool()
        s.add("scanner.state", SCANNER, self.clock[0])
        s.add("scanner.state", SCANNER, self.clock[0])
        s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup = self.path + ".backup"
        shutil.copyfile(self.path, backup)                         # backup at seq 2
        s.add("scanner.state", SCANNER, self.clock[0])             # seq 3 used after the backup
        old = s.epoch
        s.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)
        shutil.copyfile(backup, self.path)                         # restore
        with self.assertLogs("agent_mes_spool", "WARNING") as logs:
            s = self.spool()
        self.assertIn("restored", "".join(logs.output))
        s.add("scanner.state", SCANNER, self.clock[0])
        ids = [(e["producer"]["epoch"], e["producer"]["seq"]) for e in self.own()]
        self.assertEqual(ids, [(old, 1), (old, 2), (s.epoch, 1)])  # seq 3 of the old epoch never reused
        self.assertNotEqual(s.epoch, old)

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_sidecar_is_private(self):
        s = self.spool()
        self.assertEqual(os.stat(s.sidecar).st_mode & 0o777, 0o600)


class BudgetTests(Base):
    def test_full_spool_refuses_and_stores_nothing(self):
        s = self.spool()
        s.budget = s.stats["bytes"] + 40_000                        # room for some events only
        stored = 0
        with self.assertLogs("agent_mes_spool", "WARNING"):
            try:
                for _ in range(1000):
                    s.add("scanner.state", SCANNER, self.clock[0])
                    stored += 1
            except ms.SpoolFull:
                pass
        self.assertEqual(s.stats["state"], "full")
        self.assertEqual(len(self.rows()), stored)
        epoch = str(uuid.uuid4())
        with self.assertRaises(ms.SpoolFull):
            s.append([mw_event(1, epoch)], ("millwright", epoch, 1))
        self.assertIsNone(s.cursor("millwright"))                  # the outbox is not advanced
        s.add("scanner.state", SCANNER, self.clock[0], force=True)  # e.g. reporting the full spool
        self.assertEqual(len(self.rows()), stored + 1)


OLD_SITE = "7844c24c-f48b-4f71-b0e9-a18edf34e157"
NEW_SITE = "cb6a4a6c-7625-4042-8f42-b4f28b7bb75d"


class OtherSiteTests(Base):
    """Re-enrolment with another site (--mes-enroll, waiting_guard)."""

    def fill(self):
        s = self.spool()
        s.add("scanner.state", SCANNER, self.clock[0])                 # 1: site null (before enrolment)
        s.set_site_id(OLD_SITE)
        for _ in range(3):
            s.add("scanner.state", SCANNER, self.clock[0])             # 2-4: old site
        s.append([mw_event(1, str(uuid.uuid4()))])                     # 5: MillWright, site null
        s.mark_acked([2])                                              # 2 already sent
        return s

    def test_counts_only_waiting_events_of_another_site(self):
        self.fill()
        self.assertEqual(ms.waiting_for_other_site(self.path, NEW_SITE), {OLD_SITE: 2})
        self.assertEqual(ms.waiting_for_other_site(self.path, OLD_SITE), {})   # same site: fine
        self.assertEqual(ms.waiting_for_other_site(self.path, None), {OLD_SITE: 2})
        self.assertEqual(ms.waiting_for_other_site(os.path.join(self.tmp.name, "none.db"), NEW_SITE), {})

    def test_drop_marks_them_with_a_reason_and_leaves_the_rest(self):
        s = self.fill()
        self.clock[0] += 60
        n = ms.drop_waiting_for_other_site(self.path, NEW_SITE, "why", now=lambda: self.clock[0])
        self.assertEqual(n, 2)
        with sqlite3.connect(self.path) as db:
            got = db.execute("SELECT seq, state, reason, acked_at FROM spool ORDER BY seq").fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in got], [
            (1, "pending", None), (2, "acked", None), (3, "dropped", "why"), (4, "dropped", "why"),
            (5, "pending", None)])
        self.assertEqual(got[2][3], int(self.clock[0] * 1000))
        self.assertEqual([seq for seq, _ in s.pending()], [1, 5])       # never sent
        s.mark_acked([3, 4])                                            # a late receipt changes nothing
        self.assertEqual(self.rows()[2][5], "dropped")
        self.assertEqual(ms.waiting_for_other_site(self.path, NEW_SITE), {})

    def test_dropped_rows_are_pruned_like_acked_ones(self):
        s = self.fill()
        ms.drop_waiting_for_other_site(self.path, NEW_SITE, "why", now=lambda: self.clock[0])
        self.clock[0] += 8 * 86400
        self.assertEqual(s.prune(), 3)                                  # 2 (acked), 3 and 4 (dropped)
        self.assertEqual([r[0] for r in self.rows()], [1, 5])

    def test_spool_without_reason_column_gets_it(self):
        os.makedirs(os.path.dirname(self.path))
        with sqlite3.connect(self.path) as db:
            db.executescript(ms.SCHEMA.replace(", reason TEXT", ""))
        self.assertNotIn("reason", [c[1] for c in sqlite3.connect(self.path).execute("PRAGMA table_info(spool)")])
        self.assertEqual(ms.drop_waiting_for_other_site(self.path, NEW_SITE, "why"), 0)
        self.assertIn("reason", [c[1] for c in sqlite3.connect(self.path).execute("PRAGMA table_info(spool)")])
        self.spool()                                                    # and Spool opens it

    def test_old_spool_opened_by_the_agent_gets_the_column(self):
        os.makedirs(os.path.dirname(self.path))
        with sqlite3.connect(self.path) as db:
            db.executescript(ms.SCHEMA.replace(", reason TEXT", ""))
        self.spool()
        self.assertIn("reason", [c[1] for c in sqlite3.connect(self.path).execute("PRAGMA table_info(spool)")])


class PruneTests(Base):
    def test_only_old_acked_rows_go(self):
        s = self.spool()
        for _ in range(4):
            s.add("scanner.state", SCANNER, self.clock[0])
        s.mark_acked([1, 2])
        self.clock[0] += 3 * 86400
        s.mark_acked([3])
        self.clock[0] += 5 * 86400                                  # 1, 2 acked 8 days ago; 3 five; 4 pending
        self.assertEqual(s.prune(), 2)
        self.assertEqual([(r[0], r[5]) for r in self.rows()], [(3, "acked"), (4, "pending")])
        self.clock[0] += 100 * 86400
        s.prune()
        self.assertEqual([(r[0], r[5]) for r in self.rows()], [(4, "pending")])   # never pending ones
        self.assertEqual(s.pending(), [(4, self.rows()[0][4].encode())])


CHILD = r"""
import sys, uuid, json
sys.path.insert(0, %(root)r)
sys.path.insert(0, %(tests)r)
import agent_mes_spool as ms
from test_mes_spool import mw_event, MACHINE, SCANNER
s = ms.Spool(%(path)r, MACHINE, "boot", "1.7.0")
epoch = %(epoch)r
pos = (s.cursor("millwright") or (epoch, 0))[1]
print("ready", flush=True)
while True:
    s.add("scanner.state", SCANNER, 1800000000.0)
    k = 1 + pos %% 3
    s.append([mw_event(pos + i + 1, epoch) for i in range(k)], ("millwright", epoch, pos + k))
    pos += k
"""


class KillTests(Base):
    def test_kill_9_mid_write_loses_or_doubles_nothing(self):
        epoch = str(uuid.uuid4())
        code = CHILD % {"root": ROOT, "tests": os.path.dirname(os.path.abspath(__file__)),
                        "path": self.path, "epoch": epoch}
        rnd = random.Random(7)
        for _ in range(6):
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            line = p.stdout.readline().strip()
            if line != b"ready":
                p.kill()
                self.fail(p.stderr.read())
            time.sleep(rnd.uniform(0.05, 0.4))
            p.send_signal(signal.SIGKILL)
            p.wait()
            p.stdout.close()
            p.stderr.close()
            with sqlite3.connect(self.path) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                bodies = [json.loads(b) for (b,) in db.execute("SELECT body FROM spool ORDER BY seq")]
                cursor = db.execute("SELECT position FROM cursors WHERE source='millwright'").fetchone()
            own = [e["producer"]["seq"] for e in bodies if e["producer"]["kind"] == "agent"]
            mw = [e["producer"]["seq"] for e in bodies if e["producer"]["kind"] == "millwright"]
            self.assertEqual(own, list(range(1, len(own) + 1)))            # no gap, no repeat
            self.assertEqual(mw, list(range(1, len(mw) + 1)))
            self.assertEqual(cursor[0] if cursor else 0, len(mw))          # cursor == what is stored
        s = self.spool()                                                   # no false "restored"
        self.assertEqual(s.producer_seq, len(own))


if __name__ == "__main__":
    unittest.main()
