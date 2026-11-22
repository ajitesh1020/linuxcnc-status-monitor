"""tools/fleet_sim_mes.py (MES plan P4.5): the simulator's MES events are
contract-valid and deterministic for a seed, truth.json says what each scenario
must leave in the service's record, and `check` finds every difference.

With FLOORWATCH_SRC set to a FloorWatch checkout's `src` folder, the events also
go through the site service's own ingest, projections and scan resolver
in-process (out of order between machines, with resends and outages) and the
ledger must match truth exactly.

Run: python -m unittest -v
"""
import base64
import copy
import json
import os
import random
import re
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fleet_sim  # noqa: E402
import fleet_sim_mes as M  # noqa: E402

V = M.contract()
def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CATALOGUE = read_json(os.path.join(M.CONTRACT_DIR, "catalogue.json"))
SITE = "5f0c1d2e-8a3b-4c5d-9e6f-0a1b2c3d4e5f"


def machines(n, seed=0):
    rng = random.Random("machines:%d" % seed)
    return [("SIM-%03d" % (i + 1), M.new_uuid(rng)) for i in range(n)]


def run_fleet(n=13, seed=7, seconds=120.0, **kw):
    events = []
    fleet = M.MesFleet(machines(n), seed=seed, cycle=(2, 4), idle=(0.5, 1.0), site_id=SITE,
                       emit=lambda m, e: events.append((m.name, e)), **kw)
    M.simulate(fleet, seconds)
    return fleet, events


def by_scenario(truth, scenario):
    return [u for u in truth["units"].values() if u["scenario"] == scenario]


class Events(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet, cls.events = run_fleet()
        cls.truth = cls.fleet.truth_dict()

    def test_every_event_is_contract_valid(self):
        self.assertGreater(len(self.events), 500)
        for name, e in self.events:
            body = V.encode(e)
            self.assertEqual(V.validate_bytes(body), [], (e["type"], e))
            self.assertLessEqual(len(body), CATALOGUE["max_event_bytes"])
            spec = CATALOGUE["types"][e["type"]]
            self.assertIn(e["producer"]["kind"], spec["producers"])
            for need in spec["requires"]:
                self.assertTrue(e["correlation"][need], (e["type"], need))

    def test_producers_ids_and_sequences(self):
        seqs, ids, kinds = {}, set(), {}
        for name, e in self.events:
            p = e["producer"]
            seqs.setdefault((p["id"], p["epoch"]), []).append(p["seq"])
            self.assertNotIn(e["event_id"], ids)
            ids.add(e["event_id"])
            kinds.setdefault((name, p["kind"]), set()).add(p["id"])
            if p["kind"] == "millwright":                     # contract rule 7
                self.assertIsNone(e["machine_id"])
                self.assertIsNone(e["site_id"])
            else:
                self.assertEqual(e["site_id"], SITE)
        for key, s in seqs.items():
            self.assertEqual(s, list(range(1, len(s) + 1)), key)
        for key, pids in kinds.items():
            self.assertEqual(len(pids), 1, key)              # one stable producer per kind and machine
        self.assertEqual(len(seqs), 2 * 13)

    def test_types_and_scenarios_all_present(self):
        types = {e["type"] for _, e in self.events}
        self.assertEqual(types, {"scan.received", "binding.requested", "binding.cleared",
                                 "operator.session_started", "operator.session_ended",
                                 "execution.started", "execution.ended", "tool.snapshot"})
        self.assertEqual(set(self.truth["scenarios"]), set(M.SCENARIOS))
        modes = {e["payload"]["mode"] for _, e in self.events if e["type"] == "execution.started"}
        self.assertEqual(modes, {"normal", "dryrun", "probe", "run_from_here"})
        outcomes = {e["payload"]["outcome"] for _, e in self.events if e["type"] == "execution.ended"}
        self.assertEqual(outcomes, {"completed", "aborted", "interrupted_unknown"})

    def test_deterministic_for_a_seed(self):
        _f1, a = run_fleet(seconds=40)
        _f2, b = run_fleet(seconds=40)
        self.assertEqual([V.encode(e) for _, e in a], [V.encode(e) for _, e in b])
        _f3, c = run_fleet(seed=8, seconds=40)
        self.assertNotEqual([V.encode(e) for _, e in a][:50], [V.encode(e) for _, e in c][:50])

    def test_nonce_changes_ids_not_the_plan(self):
        f1 = M.MesFleet(machines(3), seed=1, cycle=(2, 4), idle=(0.5, 1))
        f2 = M.MesFleet(machines(3), seed=1, nonce="run-2", cycle=(2, 4), idle=(0.5, 1))
        M.simulate(f1, 30)
        M.simulate(f2, 30)
        self.assertNotEqual(f1.tag + f1.machines[0].agent.epoch, f2.tag + f2.machines[0].agent.epoch)
        self.assertEqual(f1.truth.scenarios, f2.truth.scenarios)

    def test_labels_match_the_profiles(self):
        doc = read_json(M.PROFILES_FILE)
        unit = next(p for p in doc["profiles"] if p["name"] == "sim-pcb-unit")
        good, wrong = self.fleet.unit_label(), self.fleet.unit_label(rev="R2")
        m = re.fullmatch(unit["match"]["pattern"], good.raw)
        self.assertEqual(m.group("id"), good.ident)
        self.assertIsNone(re.fullmatch(unit["match"]["pattern"], wrong.raw))
        lot = self.fleet.lot_label()
        text = base64.b64decode(lot.raw).decode("ascii")
        self.assertIn(M.GS, text)
        self.assertIn("10" + lot.ident + M.GS, text)
        self.assertTrue(M.GTIN.isdigit() and len(M.GTIN) == 14)
        panel = self.fleet.panel_label()
        self.assertEqual(panel.raw.split("|")[0], panel.ident)


class TruthPerScenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet, cls.events = run_fleet()
        cls.truth = cls.fleet.truth_dict()
        cls.starts = {e["correlation"]["execution_id"]: e for _, e in cls.events if e["type"] == "execution.started"}

    def one(self, scenario):
        units = by_scenario(self.truth, scenario)
        self.assertTrue(units, scenario)
        return units

    def test_unit(self):
        for u in self.one("unit"):
            self.assertEqual(len(u["attempts"]), 1)
            a = u["attempts"][0]
            self.assertIn(a["state"], ("completed_pending_inspection", "aborted"))
            self.assertEqual((a["fixture_position"], a["bound"], a["held"], a["heads"]), ("A", True, False, 1))
            self.assertEqual(a["counts_as_production"], True)
            self.assertEqual(u["scans"][0]["status"], "done")
            self.assertEqual(u["bindings"][0]["state"], "consumed")

    def test_panel_positions_in_one_run(self):
        runs = {}
        for u in self.one("panel"):
            self.assertEqual(u["namespace"], "panel")
            a = u["attempts"][0]
            runs.setdefault(a["execution_id"], []).append(a["fixture_position"])
        for eid, pos in runs.items():
            self.assertGreaterEqual(len(pos), 2)
            self.assertEqual(sorted(pos), ["P%d" % (i + 1) for i in range(len(pos))])
            self.assertEqual(len(self.starts[eid]["payload"]["bound_scan_ids"]), len(pos))

    def test_lot(self):
        for u in self.one("lot"):
            a = u["attempts"][0]
            self.assertEqual((u["namespace"], a["fixture_position"], a["bound"]), ("lot", None, True))

    def test_duplicate_read_is_one_attempt(self):
        for u in self.one("duplicate_read"):
            self.assertEqual(len(u["scans"]), 2)
            self.assertEqual([b["state"] for b in u["bindings"]], ["superseded", "consumed"])
            self.assertEqual(len(u["attempts"]), 1)
            self.assertEqual(u["attempts"][0]["scan_id"], u["scans"][1]["scan_id"])
            self.assertIsNone(u["scans"][0]["status"])          # validated or superseded: timing

    def test_wrong_revision(self):
        wrong = [s for s in self.truth["scans"].values() if s["scenario"] == "wrong_revision"]
        self.assertTrue(wrong)
        for s in wrong:
            self.assertTrue(s["raw"].endswith("-R2"))
            self.assertEqual((s["status"], s["binding"]), ("unresolved", "superseded"))
        clears = [e for _, e in self.events if e["type"] == "binding.cleared"]
        self.assertEqual(len(clears), len(wrong))
        for u in self.one("wrong_revision"):                    # the good unit run instead
            self.assertEqual(u["attempts"][0]["state"], "completed_pending_inspection")

    def test_setup_then_production(self):
        for u in self.one("setup"):
            setup, real = u["attempts"]
            self.assertIn(setup["mode"], ("dryrun", "probe"))
            self.assertEqual((setup["state"], setup["counts_as_production"], setup["bound"],
                              setup["fixture_position"]), ("setup", False, False, "A"))
            self.assertEqual((real["mode"], real["counts_as_production"], real["bound"]), ("normal", True, True))
            self.assertEqual(setup["scan_id"], real["scan_id"])
            self.assertEqual(u["bindings"][0]["state"], "consumed")

    def test_run_from_here_is_a_continuation(self):
        for u in self.one("run_from_here"):
            first, cont = u["attempts"]
            self.assertEqual(first["state"], "aborted")
            self.assertEqual(cont["mode"], "run_from_here")
            self.assertEqual(cont["continuation_of"], {"execution_id": first["execution_id"],
                                                       "scan_id": first["scan_id"]})
            self.assertEqual((cont["counts_as_production"], cont["bound"], cont["fixture_position"]),
                             (False, False, None))
            start = self.starts[cont["execution_id"]]["payload"]
            self.assertNotEqual(start["run_file_sha256"], start["program_sha256"])

    def test_aborted_and_interrupted(self):
        for u in self.one("aborted"):
            self.assertEqual(u["attempts"][0]["state"], "aborted")
        refs = {x["ref"] for x in self.truth["exceptions"] if x["kind"] == "interrupted_run"}
        units = self.one("interrupted")
        self.assertEqual(refs, {u["attempts"][0]["execution_id"] for u in units})
        for u in units:
            self.assertEqual(u["attempts"][0]["state"], "interrupted_unknown")

    def test_two_heads_one_attempt(self):
        for u in self.one("two_heads"):
            self.assertEqual(len(u["attempts"]), 1)
            self.assertEqual(u["attempts"][0]["heads"], 2)
            eid = u["attempts"][0]["execution_id"]
            snaps = [e for _, e in self.events if e["type"] == "tool.snapshot"
                     and e["correlation"]["execution_id"] == eid]
            self.assertEqual([len(s["payload"]["heads"]) for s in snaps], [2])

    def test_operator_change_mid_run(self):
        for u in self.one("operator_change"):
            a = u["attempts"][0]
            self.assertEqual(len(a["participants"]), 1)
            self.assertNotEqual(a["participants"][0], a["operator_session_id"])
            ended = [e for _, e in self.events if e["type"] == "operator.session_ended"
                     and e["correlation"]["operator_session_id"] == a["operator_session_id"]]
            self.assertEqual(ended[0]["payload"]["reason"], "replaced")

    def test_late_scan_has_no_attempt(self):
        for u in self.one("late_scan"):
            self.assertEqual((u["attempts"], u["bindings"]), ([], []))
            self.assertEqual(u["scans"][0]["status"], "validated")

    def test_conflict_two_machines_one_exception(self):
        units = self.one("conflict")
        pairs = {x["pair"]: x for x in self.truth["exceptions"] if x["kind"] == "unit_on_two_machines"}
        self.assertEqual(len(pairs), len(units))
        for u in units:
            self.assertEqual(len(u["attempts"]), 2)
            a, b = u["attempts"]
            self.assertNotEqual(a["machine"], b["machine"])
            self.assertEqual(a["held"], b["held"])
            self.assertTrue(a["held"].startswith("pair:"))
            x = pairs[a["held"][5:]]
            self.assertEqual(sorted(x["ref_one_of"]), sorted([a["execution_id"], b["execution_id"]]))

    def test_every_machine_logs_out(self):
        for m in self.truth["machines"].values():
            self.assertFalse(m["busy"])
        starts = sum(1 for _, e in self.events if e["type"] == "operator.session_started")
        ends = sum(1 for _, e in self.events if e["type"] == "operator.session_ended")
        self.assertEqual(starts, ends)


# ---------------------------------------------------------------------------
# The comparison, against a fake ledger built from truth
# ---------------------------------------------------------------------------
class FakeLedger:
    """The service's record as truth says it should be (what `compare` reads)."""

    def __init__(self, truth):
        self.units, self.scans, self.exc = {}, {}, []
        held_runs = {}
        for x in truth["exceptions"]:
            if x["kind"] == "unit_on_two_machines":
                held_runs[x["pair"]] = x["ref_one_of"][1]
        n = 0
        for key, u in truth["units"].items():
            ids = {}
            atts = []
            for a in u["attempts"]:
                n += 1
                aid = "att-%d" % n
                ids[(a["execution_id"], a["scan_id"])] = aid
                held = a["held"]
                if isinstance(held, str):
                    held = held_runs[held[5:]] == a["execution_id"]
                atts.append({"attempt_id": aid, "execution_id": a["execution_id"], "scan_id": a["scan_id"],
                             "machine_id": a["machine_id"], "state": a["state"], "mode": a["mode"],
                             "counts_as_production": a["counts_as_production"],
                             "fixture_position": a["fixture_position"],
                             "binding_id": "b-%d" % n if a["bound"] else None, "held": held,
                             "continuation_of": None, "identified_after_start": False,
                             "operator_session_id": a["operator_session_id"],
                             "heads": [{"head": h} for h in range(a["heads"])],
                             "participants": [{"operator_session_id": p, "from_at": 1, "to_at": 2}
                                              for p in a["participants"]]})
            for t, g in zip(u["attempts"], atts):
                if t["continuation_of"]:
                    c = t["continuation_of"]
                    g["continuation_of"] = ids[(c["execution_id"], c["scan_id"])]
            self.units[key] = {"attempts": atts,
                               "scans": [{"scan_id": s["scan_id"], "machine_id": s["machine_id"],
                                          "status": s["status"] or "validated"} for s in u["scans"]],
                               "bindings": [dict(b) for b in u["bindings"]]}
        for sid, s in truth["scans"].items():
            self.scans[sid] = {"status": s["status"], "bindings": [s["binding"]]}
        for x in truth["exceptions"]:
            if x["kind"] == "interrupted_run":
                self.exc.append({"kind": "interrupted_run", "ref": x["ref"],
                                 "detail": {"attempts": ["a"] * x["attempts"]}})
            else:
                self.exc.append({"kind": x["kind"], "ref": held_runs[x["pair"]], "detail": {}})

    def unit(self, namespace, ident):
        return copy.deepcopy(self.units.get("%s/%s" % (namespace, ident),
                                            {"attempts": [], "scans": [], "bindings": []}))

    def scan(self, sid):
        return copy.deepcopy(self.scans.get(sid))

    def exceptions(self):
        return copy.deepcopy(self.exc)


class Compare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fleet, _ = run_fleet(seconds=60)
        cls.truth = fleet.truth_dict()

    def setUp(self):
        self.ledger = FakeLedger(self.truth)

    def unit_of(self, scenario):
        return next(k for k, u in self.truth["units"].items() if u["scenario"] == scenario and u["attempts"])

    def bad(self):
        return M.compare(self.truth, self.ledger)[0]

    def test_exact_match(self):
        bad, counts = M.compare(self.truth, self.ledger)
        self.assertEqual(bad, [])
        self.assertEqual(counts["units"], len(self.truth["units"]))
        self.assertEqual(counts["attempts"], sum(len(u["attempts"]) for u in self.truth["units"].values()))

    def test_missing_attempt(self):
        key = self.unit_of("unit")
        self.ledger.units[key]["attempts"].pop()
        bad = self.bad()
        self.assertEqual(len(bad), 1)
        self.assertIn("missing attempt", bad[0])

    def test_extra_attempt(self):
        key = self.unit_of("unit")
        extra = dict(self.ledger.units[key]["attempts"][0], execution_id="other-run", attempt_id="x")
        self.ledger.units[key]["attempts"].append(extra)
        self.assertTrue(any("extra attempt run other-run" in b for b in self.bad()))

    def test_duplicated_attempt(self):
        key = self.unit_of("unit")
        a = self.ledger.units[key]["attempts"][0]
        self.ledger.units[key]["attempts"].append(dict(a, attempt_id="dup"))
        self.assertTrue(any("recorded 2 times" in b for b in self.bad()))

    def test_wrong_state(self):
        key = self.unit_of("two_heads")
        self.ledger.units[key]["attempts"][0]["state"] = "running"
        bad = self.bad()
        self.assertEqual(len(bad), 1)
        self.assertIn("state 'running', truth 'completed_pending_inspection'", bad[0])

    def test_setup_counted_as_production(self):
        key = self.unit_of("setup")
        a = self.ledger.units[key]["attempts"][0]
        a["counts_as_production"], a["state"] = True, "completed_pending_inspection"
        self.assertIn("counts_as_production True, truth False", self.bad()[0])

    def test_continuation_lost(self):
        key = self.unit_of("run_from_here")
        self.ledger.units[key]["attempts"][1]["continuation_of"] = None
        self.assertIn("continuation_of None", self.bad()[0])

    def test_binding_fixture_heads_participants(self):
        key = self.unit_of("operator_change")
        a = self.ledger.units[key]["attempts"][0]
        a["binding_id"], a["fixture_position"] = None, None
        a["heads"].append({"head": 1})
        a["participants"][0]["to_at"] = None
        line = self.bad()[0]
        for text in ("binding none, truth yes", "fixture_position None, truth 'A'", "heads 2, truth 1",
                     "participant interval is still open"):
            self.assertIn(text, line)

    def test_identified_after_start(self):
        key = self.unit_of("lot")
        self.ledger.units[key]["attempts"][0]["identified_after_start"] = True
        self.assertIn("identified_after_start", self.bad()[0])

    def test_conflict_both_or_none_held(self):
        key = self.unit_of("conflict")
        for a in self.ledger.units[key]["attempts"]:
            a["held"] = True
        self.assertTrue(any("2 of its two attempts held" in b for b in self.bad()))
        for a in self.ledger.units[key]["attempts"]:
            a["held"] = False
        self.assertTrue(any("0 of its two attempts held" in b for b in self.bad()))

    def test_conflict_exception_missing_or_on_the_wrong_run(self):
        key = self.unit_of("conflict")
        runs = [a["execution_id"] for a in self.ledger.units[key]["attempts"]]
        x = next(e for e in self.ledger.exc if e["kind"] == "unit_on_two_machines" and e["ref"] in runs)
        x["ref"] = [r for r in runs if r != x["ref"]][0]
        self.assertTrue(any("but run" in b and "is held" in b for b in self.bad()))
        self.ledger.exc.remove(x)
        self.assertTrue(any("0 unit_on_two_machines exception(s)" in b for b in self.bad()))

    def test_interrupted_exception_missing_and_extra_exception(self):
        x = next(e for e in self.ledger.exc if e["kind"] == "interrupted_run")
        self.ledger.exc.remove(x)
        self.assertTrue(any("interrupted_run for run %s" % x["ref"] in b for b in self.bad()))
        self.ledger.exc.append(x)
        self.ledger.exc.append({"kind": "binding_other_machine", "ref": x["ref"], "detail": {}})
        self.ledger.exc.append({"kind": "seq_conflict", "ref": "some-event",
                                "detail": {"via": self.truth["machines"]["SIM-001"]["machine_id"]}})
        self.ledger.exc.append({"kind": "seq_conflict", "ref": "not-ours", "detail": {"via": "someone-else"}})
        bad = self.bad()
        self.assertEqual(len(bad), 2, bad)
        self.assertTrue(all(b.startswith("extra exception") for b in bad))

    def test_scan_and_binding_states(self):
        key = self.unit_of("duplicate_read")
        self.ledger.units[key]["bindings"][0]["state"] = "bound"
        self.ledger.units[key]["scans"][1]["status"] = "bound"
        sid = next(iter(self.truth["scans"]))
        self.ledger.scans[sid]["status"] = "validated"
        bad = self.bad()
        self.assertEqual(len(bad), 3, bad)

    def test_check_prints_and_exits(self):
        out = []
        self.assertEqual(M.check({"final": True, "mes": self.truth}, self.ledger, out=out.append), 0)
        self.assertIn("exact", out[-1])
        self.ledger.units[self.unit_of("unit")]["attempts"].pop()
        out = []
        self.assertEqual(M.check({"final": True, "mes": self.truth}, self.ledger, out=out.append), 1)
        self.assertTrue(out[0].startswith("MISMATCH"))


class DbLedgerShape(unittest.TestCase):
    """DbLedger reads the service's tables (schema of FloorWatch migrations 0001-0010)."""

    def test_reads_a_minimal_ledger(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "ledger.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE attempts (attempt_id TEXT, execution_id TEXT, machine_id TEXT, scan_id TEXT,
              binding_id TEXT, fixture_position TEXT, state TEXT, mode TEXT, held INTEGER,
              continuation_of TEXT, profile TEXT, entity_kind TEXT, namespace TEXT, norm_id TEXT,
              identified_after_start INTEGER, operator_session_id TEXT, started_at INTEGER);
            CREATE TABLE execution_heads (execution_id TEXT, head INTEGER);
            CREATE TABLE attempt_participants (attempt_id TEXT, operator_session_id TEXT, from_at INTEGER,
              to_at INTEGER);
            CREATE TABLE scans (scan_id TEXT, machine_id TEXT, status TEXT, namespace TEXT, normalized TEXT,
              received_at INTEGER);
            CREATE TABLE bindings (binding_id TEXT, scan_id TEXT, machine_id TEXT, fixture_position TEXT,
              state TEXT, created_at INTEGER);
            CREATE TABLE exceptions (exception_id TEXT, kind TEXT, ref TEXT, detail TEXT);
            INSERT INTO attempts VALUES ('a1','e1','m1','s1','b1','A','setup','dryrun',0,NULL,'p','unit','pcb',
              'X-1',0,'o1',1);
            INSERT INTO attempts VALUES ('a2','e2','m1','s1','b1','A','aborted','normal',1,'a1','p','unit','pcb',
              'X-1',0,'o1',2);
            INSERT INTO execution_heads VALUES ('e2',0),('e2',1);
            INSERT INTO attempt_participants VALUES ('a2','o2',3,4);
            INSERT INTO scans VALUES ('s1','m1','done','pcb','X-1',1),('s2','m1','unresolved',NULL,NULL,2);
            INSERT INTO bindings VALUES ('b1','s1','m1','A','consumed',1),('b2','s2','m1','A','superseded',2);
            INSERT INTO exceptions VALUES ('x1','interrupted_run','e2','{"attempts": ["a2"]}');
        """)
        c.commit()
        c.close()
        led = M.DbLedger(path)
        self.addCleanup(led.close)
        u = led.unit("pcb", "X-1")
        a1, a2 = u["attempts"]
        self.assertEqual((a1["counts_as_production"], a2["counts_as_production"]), (False, False))
        self.assertEqual((a2["held"], len(a2["heads"]), a2["participants"][0]["operator_session_id"]),
                         (True, 2, "o2"))
        self.assertEqual([s["scan_id"] for s in u["scans"]], ["s1"])
        self.assertEqual(u["bindings"][0]["state"], "consumed")
        self.assertEqual(led.scan("s2"), {"status": "unresolved", "bindings": ["superseded"]})
        self.assertIsNone(led.scan("nope"))
        self.assertEqual(led.exceptions()[0]["detail"], {"attempts": ["a2"]})


class CheckCli(unittest.TestCase):
    def test_check_needs_a_source_and_mes_scenarios_listed(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "truth.json")
        with open(path, "w") as f:
            json.dump({"final": True, "machines": {}}, f)
        self.assertEqual(fleet_sim.main(["check", "--truth", path]), 2)
        self.assertEqual(fleet_sim.MES_SCENARIOS, M.SCENARIOS)

    def test_unknown_scenario_refused(self):
        with self.assertRaises(ValueError):
            M.MesFleet(machines(2), scenarios=["unit", "nope"])
        f = M.MesFleet(machines(1), scenarios=["unit", "conflict"])
        self.assertEqual(f.scenarios, ["unit"])                   # a conflict needs two machines


# ---------------------------------------------------------------------------
# Through the site service's own code (optional: FLOORWATCH_SRC=<floorwatch>/src)
# ---------------------------------------------------------------------------
FW_SRC = os.environ.get("FLOORWATCH_SRC", "")


@unittest.skipUnless(FW_SRC and os.path.isdir(os.path.join(FW_SRC, "floorwatch_service")),
                     "set FLOORWATCH_SRC to a FloorWatch checkout's src folder")
class ThroughTheService(unittest.TestCase):
    """The events go through FloorWatch's ingest (contract check, dedup,
    projections) and scan resolver, delivered per machine in order but out of
    order between machines, with lost receipts (resends) and outages; the
    operator waits for the scans to be bound, as live. The ledger must match
    truth exactly."""

    def run_through(self, seed, n=13, seconds=150.0, outage=0.0, resend=0.0):
        sys.path.insert(0, FW_SRC)
        from floorwatch_service import connectors, ingest, lookup, profiles, store
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        conn = store.open_store(os.path.join(tmp.name, "ledger.db"))
        self.addCleanup(conn.close)
        doc = read_json(M.PROFILES_FILE)
        profiles.save(conn, doc["profiles"], "fleet_sim test")
        ctx, breakers = connectors.context(tmp.name), lookup.Breakers()
        rng = random.Random("net:%d" % seed)
        queues = {}
        down = {}

        class Oracle:
            def bound(self, m, scan_ids):
                q = ",".join("?" * len(scan_ids))
                n = conn.execute("SELECT COUNT(*) FROM bindings WHERE machine_id = ? AND state = 'bound' "
                                 "AND scan_id IN (%s)" % q, [m.machine_id, *scan_ids]).fetchone()[0]
                return n == len(scan_ids)

            def acked(self, m, event_ids):
                q = ",".join("?" * len(event_ids))
                return conn.execute("SELECT COUNT(*) FROM raw_events WHERE event_id IN (%s)" % q,
                                    list(event_ids)).fetchone()[0] == len(event_ids)

        fleet = M.MesFleet(machines(n, seed), seed=seed, cycle=(2, 4), idle=(0.5, 1.0), site_id=SITE,
                           oracle=Oracle(), emit=lambda m, e: queues.setdefault(m, []).append(e))
        receipts = {}

        def deliver(now, final=False):
            ms = list(queues)
            rng.shuffle(ms)                                  # machines in any order
            for m in ms:
                if not final and down.get(m.name, 0) > now:
                    continue                                 # this agent cannot reach the service
                if not final and outage and rng.random() < outage:
                    down[m.name] = now + rng.uniform(2, 15)
                    continue
                batch = queues[m][:200]
                if not batch:
                    continue
                rc = ingest.ingest(conn, ingest.Caller(m.machine_id, SITE), [json.loads(V.encode(e)) for e in batch])
                for r in rc:
                    receipts[r.status] = receipts.get(r.status, 0) + 1
                if not final and resend and rng.random() < resend:
                    continue                                 # receipt lost: the same batch again later
                del queues[m][:len(batch)]
            profiles.resolve_pending(conn, ctx, breakers)

        now, dt = 0.0, 0.25
        while now < seconds:
            fleet.step(now, False)
            deliver(now)
            now += dt
        while now < seconds + 600 and not fleet.idle_all:
            fleet.step(now, True)
            deliver(now)
            now += dt
        fleet.close()
        for _ in range(20):
            deliver(now, final=True)
        self.assertTrue(fleet.idle_all)
        self.assertEqual(sum(len(q) for q in queues.values()), 0)
        truth = fleet.truth_dict()
        self.assertEqual(set(truth["scenarios"]), set(M.SCENARIOS))
        ledger = M.DbLedger(os.path.join(tmp.name, "ledger.db"))
        self.addCleanup(ledger.close)
        bad, counts = M.compare(truth, ledger)
        self.assertEqual(bad, [], "\n".join(bad[:20]))
        self.assertEqual(receipts.get("rejected", 0), 0)
        self.assertEqual(receipts.get("quarantined", 0), 0)
        return receipts, counts

    def test_in_order_delivery(self):
        receipts, counts = self.run_through(1)
        self.assertGreater(counts["attempts"], 100)

    def test_outages_and_resends(self):
        receipts, _ = self.run_through(2, outage=0.02, resend=0.1)
        self.assertGreater(receipts.get("duplicate", 0), 0)


if __name__ == "__main__":
    unittest.main()
