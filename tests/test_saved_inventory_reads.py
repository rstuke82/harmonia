import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import HTTPException
from harmonia.storage import Repository
from harmonia.jobs import Store
from harmonia import inventory, library_cache, plex_images, track_routes
from harmonia.plex_client import PlexAPI


class SavedInventoryReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name))
        self.store = Store(self.repo)
        self.client = object.__new__(PlexAPI)
        self.client.base_url = "http://plex"
        self.client.music_library_key = "1"
        self.client.headers = {"X-Plex-Token": "owner", "Accept": "application/json"}
        self.config = SimpleNamespace(repository=self.repo)
        library_cache.invalidate()

    def publish(self, rows):
        inventory.publish(
            self.repo, "plex", inventory.identity(self.client), rows, time.monotonic()
        )
        value = self.repo.load_one("inventory", "plex")
        value["checked_at"] = 1
        self.repo.save({"inventory": {"plex": value}})

    def test_stale_snapshot_used_without_network_and_updates_visible(self):
        self.publish([{"plex_id": "1"}])
        with (
            patch("harmonia.api.job_store", return_value=self.store),
            patch.object(
                self.client, "_fetch_library", side_effect=AssertionError("Read must not fetch")
            ),
        ):
            self.assertEqual(self.client.search_library("")[0]["plex_id"], "1")
            with library_cache.reuse(5):
                self.assertEqual(self.client.search_library("")[0]["plex_id"], "1")
            self.publish([{"plex_id": "2"}])
            self.assertEqual(self.client.search_library("")[0]["plex_id"], "2")

    def test_account_credentials_and_missing_snapshot_never_fall_back(self):
        self.publish([{"plex_id": "private"}])
        self.client.headers = {"X-Plex-Token": "member", "Accept": "application/json"}
        with (
            patch("harmonia.api.job_store", return_value=self.store),
            patch.object(
                self.client, "_fetch_library", side_effect=AssertionError("Read must not fetch")
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                self.client.search_library("")
            self.assertEqual(error.exception.status_code, 409)

    def test_empty_completed_scan_is_not_missing(self):
        self.publish([])
        with (
            patch("harmonia.api.job_store", return_value=self.store),
            patch.object(
                self.client, "_fetch_library", side_effect=AssertionError("Read must not fetch")
            ),
        ):
            self.assertEqual(self.client.search_library(""), [])

    def test_explicit_refresh_can_verify_live_but_does_not_replace_saved_scan(self):
        self.publish([{"plex_id": "saved"}])
        with (
            patch("harmonia.api.job_store", return_value=self.store),
            patch.object(
                self.client, "_fetch_library", return_value=[{"plex_id": "live"}]
            ) as fetch,
        ):
            with library_cache.reuse(refresh=True):
                self.assertEqual(self.client.search_library("")[0]["plex_id"], "live")
                self.client.search_library("")
                self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.client.search_library("")[0]["plex_id"], "saved")

    def test_images_reuse_bytes_and_changed_path_or_account_refetches(self):
        response = Mock(
            content=b"cover",
            headers={"Content-Type": "image/jpeg"},
            iter_content=lambda **kwargs: iter([b"cover"]),
        )
        with (
            patch("harmonia.api._health_plex", return_value=self.client),
            patch("harmonia.plex_images.network.get", return_value=response) as get,
        ):
            self.assertEqual(plex_images.serve(self.config, "/thumb/1").body, b"cover")
            plex_images.serve(self.config, "/thumb/1")
            self.assertEqual(get.call_count, 1)
            plex_images.serve(self.config, "/thumb/2")
            self.assertEqual(get.call_count, 2)
            self.client.headers = {"X-Plex-Token": "member"}
            plex_images.serve(self.config, "/thumb/1")
            self.assertEqual(get.call_count, 3)

    def test_occurrence_inspection_skips_unselected_playlists(self):
        class ScopedHealth(dict):
            def get(self, key, default=None):
                if key != "text:chosen":
                    raise AssertionError("Unselected playlist inspected")
                return {"source_preview": []}

        config = SimpleNamespace(
            config={
                "playlists": [
                    {"source": "text", "source_id": "other"},
                    {"source": "text", "source_id": "chosen"},
                ]
            },
            health=ScopedHealth(),
            source_snapshots={},
            repository=self.repo,
        )
        with patch("harmonia.sync_engine.Syncer"):
            result = track_routes.inspect(
                config, {"title": "Song", "artist": "Band"}, {"text:chosen"}
            )
        self.assertEqual(result["memberships"], [])
