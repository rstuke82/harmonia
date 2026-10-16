"""Sync result, timestamp, health and projection contracts across destination failures."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harmonia import sync_engine
from harmonia.storage import Repository


class SyncOutcomes(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name))
        self.key = "spotify:37i9dQZF1DXcBWIGoYBM5M"
        self.playlist = {
            "source": "spotify",
            "source_id": self.key.split(":")[1],
            "source_url": "https://open.spotify.com/playlist/" + self.key.split(":")[1],
            "plex_playlist_id": "destination",
            "plex_playlist_name": "Test",
            "last_synced": "previous",
            "ready_to_sync": True,
        }
        self.cfg = SimpleNamespace(
            repository=self.repo,
            config={"playlists": [self.playlist]},
            mapping={},
            missing={},
            match_metadata={},
            ignored_tracks={},
            source_snapshots={},
            save=Mock(),
        )
        self.sync = sync_engine.Syncer(self.cfg)
        self.plex = Mock(base_url="http://plex", headers={})
        self.sync._get_plex = lambda: self.plex
        self.library = [
            {"plex_id": "a", "title": "One", "artist": "Artist"},
            {"plex_id": "b", "title": "Two", "artist": "Artist"},
        ]
        self.tracks = [
            {"title": "One", "artist": "Artist", "source_id": "one"},
            {"title": "Two", "artist": "Artist", "source_id": "two"},
            {"title": "One", "artist": "Artist", "source_id": "one"},
        ]
        self.stats = {"new_matches": [], "lost_matches": [], "ignored_tracks": []}

    def run_sync(self, actual, write=True, dry_run=False):
        self.plex.replace_playlist_tracks.return_value = write
        if isinstance(actual, Exception):
            self.plex.get_playlist_items.side_effect = actual
        else:
            self.plex.get_playlist_items.return_value = [{"plex_id": value} for value in actual]
        with (
            patch.object(
                sync_engine.network,
                "get",
                return_value=Mock(
                    status_code=200,
                    json=lambda: {"MediaContainer": {"Metadata": [{"ratingKey": "destination"}]}},
                ),
            ),
            patch.object(
                sync_engine.SpotifyAPI,
                "get_playlist_tracks",
                return_value=(
                    self.tracks,
                    {"name": "Test", "expected_track_count": 3, "complete": True},
                ),
            ),
            patch.object(
                self.sync,
                "_match_source_tracks",
                return_value=(
                    ["a", "b", "a"],
                    [],
                    {"One|Artist": "a", "Two|Artist": "b"},
                    self.library,
                    self.stats,
                ),
            ),
            patch("harmonia.sync_projection.update") as publish,
            patch.object(sync_engine, "print") as output,
        ):
            result = self.sync.sync_playlist(self.playlist, dry_run=dry_run)
        return result, publish, output

    def test_duplicate_only_omission_completes_warns_and_is_healthy(self):
        result, publish, output = self.run_sync(["a", "b"])
        self.assertEqual(result["errors"], 0)
        self.assertNotEqual(self.playlist["last_synced"], "previous")
        self.assertFalse(self.playlist["ready_to_sync"])
        health = self.repo.load_one("health", self.key)
        self.assertTrue(health["healthy"])
        self.assertEqual(health["duplicates_collapsed"], 1)
        self.assertEqual(health["missing_from_plex_playlist"], 0)
        self.assertTrue(publish.call_args.args[-1])
        self.assertIn(
            "duplicate entries that did not carry over",
            " ".join(str(call.args[0]) for call in output.call_args_list),
        )

    def test_distinct_track_loss_fails_without_advancing_success_timestamp(self):
        result, publish, _ = self.run_sync(["a"])
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.playlist["last_synced"], "previous")
        self.plex.update_playlist_metadata.assert_not_called()
        self.assertFalse(self.repo.load_one("health", self.key)["healthy"])
        self.assertFalse(publish.call_args.args[-1])

    def test_writer_failure_does_not_read_back_as_success(self):
        result, publish, _ = self.run_sync(["a", "b", "a"], write=False)
        self.assertEqual(result["errors"], 1)
        self.plex.get_playlist_items.assert_not_called()
        self.assertEqual(self.playlist["last_synced"], "previous")
        self.assertFalse(publish.call_args.args[-1])

    def test_readback_timeout_remains_a_failed_sync(self):
        result, publish, _ = self.run_sync(sync_engine.network.exceptions.Timeout())
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.playlist["last_synced"], "previous")
        self.assertFalse(publish.call_args.args[-1])
        self.assertIn(
            "Sync verification failed", self.repo.load_one("health_attempts", self.key)["error"]
        )

    def test_dry_run_does_not_write_health_state_or_destination(self):
        result, publish, _ = self.run_sync(["a", "b", "a"], dry_run=True)
        self.assertEqual(result["errors"], 0)
        self.plex.replace_playlist_tracks.assert_not_called()
        self.plex.update_playlist_metadata.assert_not_called()
        self.cfg.save.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(self.repo.load("health"), {})
        self.assertEqual(self.playlist["last_synced"], "previous")
