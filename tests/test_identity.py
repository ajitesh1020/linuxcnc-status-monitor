"""Stable machine identity (agent_identity.py, MES plan P0.6).
Run: python -m unittest -v test_identity
"""
import json
import os
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_identity as ai  # noqa: E402


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = os.path.join(self._tmp.name, "linuxcnc-monitor-agent")   # not made yet
        self.path = ai.default_path(self.dir)

    def _doc(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_created_once_then_reused(self):
        first = ai.load_or_create(self.path)
        self.assertRegex(first, ai.UUID4_RE)
        self.assertEqual(str(uuid.UUID(first)), first)       # lowercase, hyphens
        self.assertEqual(uuid.UUID(first).version, 4)
        doc = self._doc()
        self.assertEqual(doc["machine_uuid"], first)
        self.assertRegex(doc["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        mtime = os.stat(self.path).st_mtime_ns
        for _ in range(3):
            self.assertEqual(ai.load_or_create(self.path), first)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)   # never rewritten
        self.assertEqual(os.listdir(self.dir), ["identity.json"])  # no temp files left

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_mode_0600(self):
        old = os.umask(0o022)
        try:
            ai.load_or_create(self.path)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def _assert_damaged_replaced(self, content):
        os.makedirs(self.dir)
        with open(self.path, "wb") as f:
            f.write(content)
        with self.assertLogs("agent_identity", "WARNING"):
            new = ai.load_or_create(self.path)
        self.assertRegex(new, ai.UUID4_RE)
        bad = [n for n in os.listdir(self.dir) if n.startswith("identity.json.bad-")]
        self.assertEqual(len(bad), 1)
        self.assertRegex(bad[0], r"^identity\.json\.bad-\d{8}T\d{6}Z$")
        with open(os.path.join(self.dir, bad[0]), "rb") as f:
            self.assertEqual(f.read(), content)                   # kept as it was
        self.assertEqual(ai.load_or_create(self.path), new)       # and now stable

    def test_damaged_files_are_kept_and_replaced(self):
        cases = [b"", b"{not json", b"\xff\xfe", b"[]", b'{"created_at": "x"}',
                 b'{"machine_uuid": 42}',
                 b'{"machine_uuid": "8D7C6B5A-4F3E-4D2C-8B1A-0F9E8D7C6B5A"}',  # upper case
                 b'{"machine_uuid": "8d7c6b5a4f3e4d2c8b1a0f9e8d7c6b5a"}',      # no hyphens
                 b'{"machine_uuid": "8d7c6b5a-4f3e-1d2c-8b1a-0f9e8d7c6b5a"}']  # version 1
        for content in cases:
            with self.subTest(content=content):
                self.setUp()
                self._assert_damaged_replaced(content)

    def test_valid_file_written_elsewhere_is_accepted(self):
        os.makedirs(self.dir)
        uid = "8d7c6b5a-4f3e-4d2c-8b1a-0f9e8d7c6b5a"
        with open(self.path, "w") as f:
            json.dump({"machine_uuid": uid, "created_at": "2026-10-22T09:15:02Z"}, f)
        self.assertEqual(ai.load_or_create(self.path), uid)
        self.assertEqual(ai.read(self.path), uid)

    def test_read_never_creates(self):
        self.assertIsNone(ai.read(self.path))
        self.assertFalse(os.path.exists(self.dir))

    def test_unwritable_location_gives_none(self):
        blocker = os.path.join(self._tmp.name, "a-file")
        open(blocker, "w").close()
        with self.assertLogs("agent_identity", "ERROR"):
            self.assertIsNone(ai.load_or_create(os.path.join(blocker, "identity.json")))

    def test_an_existing_file_wins_a_creation_race(self):
        os.makedirs(self.dir)
        uid = "11111111-2222-4333-8444-555555555555"
        real_link = os.link

        def racing_link(src, dst):            # another process writes first
            with open(dst, "w") as f:
                json.dump({"machine_uuid": uid, "created_at": "2026-10-22T09:15:02Z"}, f)
            return real_link(src, dst)

        os.link = racing_link
        try:
            self.assertEqual(ai._write_new(self.path), uid)
        finally:
            os.link = real_link
        self.assertEqual(ai.read(self.path), uid)
        self.assertEqual(os.listdir(self.dir), ["identity.json"])

    def test_resolve_path(self):
        self.assertEqual(ai.resolve_path("", "/x"), os.path.join("/x", "identity.json"))
        self.assertEqual(ai.resolve_path("  /a/id.json ", "/x"), "/a/id.json")
        self.assertEqual(ai.resolve_path("~/id.json", "/x"), os.path.expanduser("~/id.json"))


if __name__ == "__main__":
    unittest.main()
