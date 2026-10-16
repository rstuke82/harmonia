import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from datetime import datetime, timezone
from harmonia.storage import Repository
from harmonia.jobs import Store, next_run
from harmonia import inventory, library_cache, queued_settings, playlist_schedules
from harmonia.lidarr import state_put
from harmonia.matching import Matcher


class Beta2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / "bridge.sqlite")
        self.store = Store(self.repo)

    def test_persistent_inventory_isolation_and_invalidation(self):
        client = SimpleNamespace(base_url="plex", music_library_key="1", headers={})
        inventory.publish(
            self.repo, "plex", inventory.identity(client), [{"plex_id": "1"}], time.monotonic()
        )
        inventory.publish(self.repo, "lidarr", "other", [{"id": 2}], time.monotonic())
        self.assertEqual(set(self.repo.load("inventory")), {"plex", "lidarr"})
        reopened = Repository(self.repo.directory) if hasattr(self.repo, "path") else self.repo
        self.assertFalse(inventory.snapshot(reopened, "plex", "wrong-server"))
        library_cache.invalidate()
        fetch = Mock(return_value=[{"plex_id": "fresh"}])
        with patch("harmonia.api.job_store", return_value=self.store):
            with library_cache.reuse():
                self.assertEqual(library_cache.load(client, fetch)[0]["plex_id"], "1")
            fetch.assert_not_called()
            library_cache.invalidate(discard_persistent=True)
            with self.assertRaises(__import__("fastapi").HTTPException):
                library_cache.load(client, fetch)
            fetch.assert_not_called()
        self.assertIn("lidarr", self.repo.load("inventory"))

    def test_snapshot_decodes_only_requested_inventory(self):
        inventory.publish(self.repo, "plex", "plex-server", [{"plex_id": "1"}], time.monotonic())
        inventory.publish(self.repo, "lidarr", "lidarr-server", [{"id": 2}], time.monotonic())
        with patch.object(
            self.repo,
            "load",
            side_effect=AssertionError("must not decode the entire inventory namespace"),
        ):
            self.assertEqual(
                inventory.snapshot(self.repo, "lidarr", "lidarr-server")["rows"], [{"id": 2}]
            )
            self.assertFalse(inventory.snapshot(self.repo, "plex", "wrong-server"))

    def test_retired_flags_cannot_be_queued(self):
        from fastapi import HTTPException

        c = SimpleNamespace(config={"playlists": [{"source": "spotify", "source_id": "1"}]})
        with patch("harmonia.api._config", return_value=c):
            with self.assertRaises(HTTPException):
                queued_settings.enqueue(["spotify:1"], {"auto_sync": True})

    def test_schedule_time_and_overlap(self):
        v = playlist_schedules.Schedule(mode="custom", days=[0], hour=2, minute=0).model_dump()
        nxt = next_run(
            playlist_schedules.expression(v),
            "America/Chicago",
            datetime(2026, 9, 24, tzinfo=timezone.utc),
        )
        self.assertEqual(nxt, "2026-09-27T07:00:00+00:00")
        state_put(
            self.repo,
            "playlist_schedules",
            "spotify:1",
            {**v, "timezone": "UTC", "next_run": "2000-01-01T00:00:00+00:00"},
        )
        state_put(self.repo, "playlist_schedules", "spotify:2", {**v, "mode": "disabled"})
        c = SimpleNamespace(
            config={"playlists": [{"source": "spotify", "source_id": str(i)} for i in [1, 2]]}
        )
        with patch("harmonia.api._config", return_value=c):
            self.store.enqueue("sync", {"scope": "selected", "playlist_keys": ["spotify:1"]})
            playlist_schedules.due(self.store)
        saved = self.repo.load("playlist_schedules")
        self.assertEqual(len(saved), 2)
        self.assertTrue(saved["spotify:1"]["last_event"].startswith("Skipped"))
        self.assertGreater(saved["spotify:1"]["next_run"], "2026")

    def test_reconcile_preserves_manual_and_never_saves_config(self):
        c = SimpleNamespace(
            config={
                "plex": {"url": "plex", "token": "x", "music_library_key": "1"},
                "playlists": [{"source": "spotify", "source_id": "1"}],
            },
            source_snapshots={},
            missing={"spotify:1": [{"title": "Song", "artist": "Artist", "album": "Album"}]},
            mapping={"spotify:1": {"Song|Artist": "missing-manual-id"}},
            save=Mock(),
        )
        client = SimpleNamespace(
            base_url="plex",
            music_library_key="1",
            headers={"X-Plex-Token": "x", "Accept": "application/json"},
        )
        inventory.publish(
            self.repo,
            "plex",
            inventory.identity(client),
            [{"plex_id": "different", "title": "Song", "artist": "Artist", "album": "Album"}],
            time.monotonic(),
        )
        syncer = Mock()
        syncer._get_match_provenance.return_value = "manual"
        syncer._find_ignored_track_key.return_value = None
        with (
            patch("harmonia.api.job_store", return_value=self.store),
            patch("harmonia.api._config", return_value=c),
            patch("harmonia.sync_engine.Syncer", return_value=syncer),
        ):
            result = inventory.reconcile()
        self.assertEqual(result["available"], 0)
        c.save.assert_not_called()
        self.assertEqual(c.mapping["spotify:1"]["Song|Artist"], "missing-manual-id")

    def test_failed_scan_keeps_previous_snapshot(self):
        inventory.publish(self.repo, "plex", "original", [{"plex_id": "1"}], time.monotonic())
        with (
            patch("harmonia.api.job_store", return_value=self.store),
            patch("harmonia.api._config"),
            patch("harmonia.api._health_plex") as plex,
        ):
            plex.return_value._fetch_library.side_effect = RuntimeError("Plex unavailable")
            with self.assertRaises(RuntimeError):
                inventory.scan("plex")
        self.assertEqual(self.repo.load("inventory")["plex"]["identity"], "original")

    def test_safe_identity_normalization(self):
        for source, target, artist, other, album in [
            (
                "Sex & Candy",
                "Sex and Candy",
                "Marcy Playground",
                "Marcy Playground",
                "Marcy Playground",
            ),
            ("We Got the Beat", "We Got the Beat", "The Go-Go's", "Go-Go’s", "Beauty and the Beat"),
        ]:
            a = {"title": source, "artist": artist, "album": album}
            b = {"title": target, "artist": other, "album": album, "plex_id": "1"}
            self.assertEqual(Matcher.match_track(a, [b]), "1")
        source = {"title": "The Distance", "artist": "Cake", "album": ""}
        live = {"title": "The Distance", "artist": "CAKE", "album": "Live From 6A", "plex_id": "2"}
        self.assertIsNone(Matcher.match_track(source, [live]))
        remix = {**live, "title": "The Distance (Remix)", "album": "Remixes"}
        self.assertIsNone(Matcher.match_track(remix, [{**source, "plex_id": "3"}]))


if __name__ == "__main__":
    unittest.main()
