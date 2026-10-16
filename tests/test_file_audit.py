import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from harmonia.file_audit import inspect, decorate
from harmonia.file_tools import Settings


class Audit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cfg = Settings(
            audit_enabled=True, mappings=[{"lidarr_path": "/lidarr", "local_path": str(self.root)}]
        ).model_dump()
        self.inv = {
            "identity": "server",
            "checked_at": time.time(),
            "rows": [{"id": 1, "album_id": "album", "artist": "Artist", "title": "Album"}],
            "tracks": [
                {
                    "id": "1",
                    "artist": "Artist",
                    "album": "Album",
                    "title": "Song",
                    "file": "/lidarr/one.flac",
                    "recording_id": "rec",
                    "album_id": "album",
                }
            ],
        }
        self.tags = patch(
            "harmonia.local_library.read_tags",
            return_value={
                "title": "Song",
                "artist": "Artist",
                "album_artist": "Artist",
                "album": "Album",
                "recording_id": "rec",
            },
        )
        self.tags.start()
        self.addCleanup(self.tags.stop)

    def scan(self, previous=None, verify=False):
        return inspect(self.cfg, self.inv, {}, previous, verify)

    def kinds(self, value):
        return {r["issue"] for r in value["issues"]}

    def test_missing_and_unmanaged_are_evidence_not_catalog_changes(self):
        (self.root / "other.flac").write_bytes(b"data")
        result = self.scan()
        self.assertIn("Missing imported file", self.kinds(result))
        self.assertIn("Unmanaged file", self.kinds(result))
        self.assertEqual(self.inv["tracks"][0]["file"], "/lidarr/one.flac")
        self.assertEqual((self.root / "other.flac").read_bytes(), b"data")

    def test_empty_or_unavailable_mount_never_means_all_missing(self):
        result = self.scan()
        self.assertIn("Audit unavailable", self.kinds(result))
        self.assertNotIn("Missing imported file", self.kinds(result))
        self.root.rmdir()
        result = self.scan()
        self.assertNotIn("Missing imported file", self.kinds(result))

    def test_hash_verification_and_changed_file_invalidates_evidence(self):
        (self.root / "one.flac").write_bytes(b"same")
        (self.root / "two.flac").write_bytes(b"same")
        standard = self.scan()
        self.assertNotIn("Identical files", self.kinds(standard))
        verified = self.scan(standard, True)
        self.assertIn("Identical files", self.kinds(verified))
        with patch(
            "harmonia.file_audit.digest_file",
            side_effect=AssertionError("should reuse unchanged checksum"),
        ):
            reused = self.scan(verified, True)
        self.assertIn("Identical files", self.kinds(reused))
        (self.root / "two.flac").write_bytes(b"else")
        self.assertNotIn("Identical files", self.kinds(self.scan(reused)))
        self.assertNotIn("Identical files", self.kinds(self.scan(reused, True)))

    def test_stale_lidarr_does_not_classify_missing_or_unmanaged(self):
        self.inv["checked_at"] = 0
        (self.root / "other.flac").write_bytes(b"data")
        result = self.scan()
        self.assertNotIn("Missing imported file", self.kinds(result))
        self.assertNotIn("Unmanaged file", self.kinds(result))

    def test_hardlinks_are_not_two_physical_copies(self):
        (self.root / "one.flac").write_bytes(b"data")
        os.link(self.root / "one.flac", self.root / "two.flac")
        result = self.scan(verify=True)
        self.assertIn("Hard-linked paths", self.kinds(result))
        self.assertNotIn("Identical files", self.kinds(result))

    def test_unreadable_subtree_suppresses_missing(self):
        real_walk = os.walk

        def walk(root, **kwargs):
            kwargs["onerror"](PermissionError("Subfolder not readable"))
            yield from real_walk(root, **kwargs)

        (self.root / "other.flac").write_bytes(b"data")
        with patch("harmonia.file_audit.os.walk", side_effect=walk):
            result = self.scan()
        self.assertNotIn("Missing imported file", self.kinds(result))
        self.assertIn("Audit unavailable", self.kinds(result))

    def test_reviews_follow_exact_evidence(self):
        class Repo:
            reviews = {}

            def load(self, key):
                return self.reviews

        repo = Repo()
        row = {"issue": "Identical files", "files": ["a", "b"], "evidence": {"sha256": "old"}}
        old = decorate([row], repo)[0]
        repo.reviews[old["issue_id"]] = {"intentional": True}
        self.assertTrue(decorate([row], repo)[0]["intentional"])
        self.assertFalse(decorate([{**row, "evidence": {"sha256": "new"}}], repo)[0]["intentional"])
