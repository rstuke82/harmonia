import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from harmonia.jobs import Store, wait_reason
from harmonia.storage import Repository
from harmonia.file_tools import mapped, Settings
from harmonia.library import issues


class Beta7(unittest.TestCase):
    def test_expensive_metadata_and_scans_wait_for_health(self):
        running = [{"account": "owner", "action": "health"}]
        for action in (
            "library_metadata",
            "local_catalog",
            "local_scan",
            "lidarr_scan",
            "lidarr_add",
        ):
            if action in ("lidarr_add", "lidarr_scan", "library_metadata", "local_catalog"):
                self.assertIsNone(wait_reason(action, "owner", running))
            else:
                self.assertIsNotNone(wait_reason(action, "owner", running))
        self.assertIsNotNone(wait_reason("sync", "owner", running))
        self.assertIsNotNone(wait_reason("health", "member", running))

    def test_capacity_and_backup_barrier(self):
        self.assertIsNotNone(
            wait_reason("library_metadata", "b", [{"account": "a", "action": "library_metadata"}])
        )
        self.assertIsNotNone(wait_reason("backup", "a", [{"account": "b", "action": "health"}]))
        self.assertIsNotNone(
            wait_reason("library_metadata", "a", [{"account": "b", "action": "restore_backup"}])
        )
        self.assertIsNotNone(wait_reason("sync", "a", [], True))
        self.assertIsNone(wait_reason("backup", "a", [], True))

    def test_claim_specific_lane_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Repository(Path(tmp)))
            first = store.enqueue("sync", {})
            second = store.enqueue("library_metadata", {"artist_id": "1"})
            self.assertEqual(store.claim(second)["id"], second)
            self.assertEqual(store.get(first)["status"], "queued")
            self.assertIsNone(store.claim(second))
            self.assertEqual(store.get(second)["worker"], "metadata")

    def test_path_mapping_boundaries(self):
        cfg = Settings(
            mappings=[{"lidarr_path": "/music", "local_path": "/mounted", "plex_path": "/plex"}]
        ).model_dump()
        self.assertEqual(mapped("/music/artist/file.flac", config=cfg), "/mounted/artist/file.flac")
        self.assertEqual(mapped("/music-other/file.flac", config=cfg), "")
        with self.assertRaises(ValueError):
            Settings(mappings=[{"lidarr_path": "/music/../secret"}])

    def test_duplicate_findings_are_not_destructive(self):
        plex = {
            "rows": [
                {"plex_id": "10", "files": ["/a.flac"], "title": "Song"},
                {"plex_id": "11", "files": ["/a.flac"], "title": "Song"},
            ]
        }
        ts = [
            {"id": "1", "title": "Song", "file": "/a.flac", "recording_id": "r", "album_id": "a"},
            {"id": "2", "title": "Song", "file": "/b.flac", "recording_id": "r", "album_id": "a"},
        ]
        with patch("harmonia.file_tools.settings", return_value={"mappings": []}):
            rows = issues({"tracks": ts, "links": {}, "pending": True}, plex)
        kinds = {r["issue"] for r in rows}
        self.assertIn("Duplicate Plex entries", kinds)
        self.assertIn("Possible duplicate files", kinds)
        self.assertNotIn("Plex-only / unlinked", kinds)
        self.assertEqual(len(plex["rows"]), 2)
