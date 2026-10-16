"""Provider parsing and sync safety checks without live service calls."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harmonia import apple_music_source, spotify_source, sync_engine, sync_workflow
from harmonia.storage import Repository

SPOTIFY = "37i9dQZF1DXcBWIGoYBM5M"
APPLE = "https://music.apple.com/us/playlist/test/pl.123"


class SpotifyFetching(unittest.TestCase):
    def page(self, entity):
        payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        return (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )

    def fetch(self, entity):
        response = Mock(status_code=200, text=self.page(entity))
        with patch.object(spotify_source.network, "get", return_value=response) as get:
            tracks, metadata = spotify_source.SpotifyAPI().get_playlist_tracks(
                SPOTIFY, fetch_artwork=False
            )
        self.assertEqual(get.call_count, 1)
        return tracks, metadata

    def test_order_duplicates_credits_duration_and_completeness(self):
        song = {
            "title": "Song",
            "artists": [{"name": "Lead"}, {"name": "Guest"}],
            "album": {"name": "Album"},
            "uri": "spotify:track:id",
            "durationInMillis": 210000,
        }
        tracks, metadata = self.fetch(
            {"name": "Playlist", "trackList": [song, song], "trackListTotal": 2}
        )
        self.assertEqual([t["source_id"] for t in tracks], ["id", "id"])
        self.assertEqual(tracks[0]["artist"], "Lead, Guest")
        self.assertEqual(tracks[0]["album"], "Album")
        self.assertEqual(tracks[0]["duration"], 210000)
        self.assertTrue(metadata["complete"])

    def test_paginated_payload_reports_incomplete_observation(self):
        tracks, metadata = self.fetch(
            {
                "name": "Playlist",
                "tracks": {
                    "items": [{"track": {"name": "One", "subtitle": "Artist", "id": "1"}}],
                    "total": 2,
                },
            }
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(metadata["expected_track_count"], 2)
        self.assertFalse(metadata["complete"])

    def test_malformed_payload_and_timeout_have_actionable_errors(self):
        for reply, message in (
            (Mock(status_code=200, text="no serialized payload"), "playlist data"),
            (spotify_source.network.exceptions.Timeout(), "timed out"),
        ):
            with patch.object(
                spotify_source.network,
                "get",
                **(
                    {"side_effect": reply}
                    if isinstance(reply, Exception)
                    else {"return_value": reply}
                ),
            ):
                with self.assertRaisesRegex(Exception, message):
                    spotify_source.SpotifyAPI().get_playlist_tracks(SPOTIFY, fetch_artwork=False)


class AppleFetching(unittest.TestCase):
    def fetch(self, data):
        html = (
            '<h1>Playlist</h1><script id="serialized-server-data" type="application/json">'
            + json.dumps(data)
            + "</script>"
        )
        response = Mock(status_code=200, content=html.encode())
        with patch("harmonia.playlist_urls.apple_playlist_get", return_value=response):
            return apple_music_source.AppleMusicAPI().get_playlist_tracks(
                APPLE, fetch_artwork=False
            )

    def test_section_order_and_duplicate_occurrences_are_preserved(self):
        song = {
            "title": "Song",
            "artistName": "Lead, Guest",
            "albumName": "Album",
            "id": "1",
            "duration": 210000,
        }
        tracks, metadata = self.fetch(
            {
                "sections": [{"items": [song, song]}],
                "schemaContent": {"@type": "MusicPlaylist", "trackCount": 2},
            }
        )
        self.assertEqual([t["source_id"] for t in tracks], ["1", "1"])
        self.assertEqual([a["name"] for a in tracks[0]["artists"]], ["Lead", "Guest"])
        self.assertTrue(metadata["complete"])

    def test_confirmed_empty_playlist_is_distinct_from_missing_track_data(self):
        tracks, metadata = self.fetch(
            {"schemaContent": {"@type": "MusicPlaylist", "trackCount": 0}}
        )
        self.assertEqual(tracks, [])
        self.assertTrue(metadata["complete"])
        with self.assertRaisesRegex(Exception, "no playlist tracks"):
            self.fetch({"sections": []})

    def test_unrecognized_layout_falls_back_to_track_objects(self):
        song = {"title": "Song", "artistName": "Artist", "id": "1", "duration": 210000}
        tracks, _ = self.fetch(
            {
                "newLayout": {"songs": [song, song]},
                "schemaContent": {"@type": "MusicPlaylist", "trackCount": 1},
            }
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["source_id"], "1")


class SyncSourceSafety(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.playlist = {
            "source": "spotify",
            "source_id": SPOTIFY,
            "source_url": "https://open.spotify.com/playlist/" + SPOTIFY,
            "plex_playlist_id": "destination",
            "plex_playlist_name": "Test",
        }
        self.cfg = SimpleNamespace(
            repository=Repository(Path(folder.name)),
            config={"playlists": [self.playlist]},
            save=Mock(),
            source_snapshots={},
            mapping={},
            missing={},
            match_metadata={},
            ignored_tracks={},
        )
        self.sync = sync_engine.Syncer(self.cfg)
        self.plex = Mock(base_url="http://plex", headers={})
        self.sync._get_plex = lambda: self.plex
        self.destination = Mock(
            status_code=200,
            json=lambda: {"MediaContainer": {"Metadata": [{"ratingKey": "destination"}]}},
        )

    def test_fetch_failure_never_changes_plex_contents(self):
        with (
            patch.object(sync_workflow.network, "get", return_value=self.destination),
            patch.object(
                sync_workflow.SpotifyAPI,
                "get_playlist_tracks",
                side_effect=RuntimeError("Service unavailable"),
            ),
        ):
            result = self.sync.sync_playlist(self.playlist)
        self.assertEqual(result["errors"], 1)
        self.plex.replace_playlist_tracks.assert_not_called()
        self.plex.clear_playlist.assert_not_called()
        self.cfg.save.assert_not_called()

    def test_incomplete_source_is_rejected_before_matching_or_writing(self):
        with (
            patch.object(sync_workflow.network, "get", return_value=self.destination),
            patch.object(
                sync_workflow.SpotifyAPI,
                "get_playlist_tracks",
                return_value=([{"title": "One", "artist": "Artist"}], {"expected_track_count": 2}),
            ),
            patch.object(self.sync, "_match_source_tracks") as matching,
        ):
            with self.assertRaisesRegex(ValueError, "1 of 2 expected tracks"):
                self.sync.sync_playlist(self.playlist)
        matching.assert_not_called()
        self.plex.replace_playlist_tracks.assert_not_called()
        self.cfg.save.assert_not_called()
