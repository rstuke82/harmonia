"""X-Ecutioners automatic suggestions remain valid in saved queues and sync."""

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harmonia import accounts, api, artist_credits, match_queue, track_bridge
from harmonia.apple_music_source import AppleMusicAPI
from harmonia.matching import Matcher
from harmonia.storage import Repository


class ArtistCreditQueue(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name))
        self.source = {
            "title": "It's Goin' Down (feat. Mister Hahn)",
            "artist": "X-Ecutioners featuring Mister Hahn & Mike Shinoda",
            "album": "Built from Scratch",
            "duration": 248000,
            "artists": [{"name": "-Ecutioners"}, {"name": "Mister Hahn"}, {"name": "Mike Shinoda"}],
        }
        self.candidate = {
            "title": "It’s Goin’ Down",
            "artist": "X-Ecutioners",
            "album": "Built From Scratch",
            "duration": 248000,
            "plex_id": "correct",
        }
        token = track_bridge._context.set(None)
        self.addCleanup(track_bridge._context.reset, token)

    def test_artist_names_are_not_split_at_x(self):
        for name in ("X-Ecutioners", "X Ambassadors", "Generation X", "Static-X", "X-Ray Spex"):
            self.assertEqual(artist_credits.names(name), [name])
        self.assertEqual(artist_credits.names("Artist x Guest"), ["Artist", "Guest"])
        self.assertEqual(
            artist_credits.names("Generation X featuring Guest"), ["Generation X", "Guest"]
        )

    def test_new_apple_tracks_keep_lead_artist(self):
        source = AppleMusicAPI._track_from_item(
            {
                "title": self.source["title"],
                "artistName": self.source["artist"],
                "albumName": self.source["album"],
                "duration": 248000,
            }
        )
        self.assertEqual(source["artists"][0]["name"], "X-Ecutioners")
        self.assertEqual(Matcher.match_track(source, [self.candidate], {}), "correct")

    def test_old_saved_credits_and_picker_return_same_automatic_candidate(self):
        picker = {key: self.source[key] for key in ("title", "artist", "album", "duration")}
        self.assertEqual(Matcher.match_track(picker, [self.candidate], {}), "correct")
        self.assertEqual(Matcher.match_track(self.source, [self.candidate], {}), "correct")
        self.assertEqual(self.source["artists"][0]["name"], "-Ecutioners")  # read-only repair

    def test_saved_credits_also_match_through_lidarr_recording_links(self):
        catalog = {
            **self.candidate,
            "id": "catalog",
            "plex_id": "catalog",
            "recording_id": "recording",
        }
        context = {
            "pending": False,
            "tracks": [catalog],
            "links": {"catalog": {"plex_id": "correct"}},
            "source_links": {},
            "resolved": {},
        }
        token = track_bridge._context.set(context)
        try:
            self.assertEqual(Matcher.match_track(self.source, [self.candidate], {}), "correct")
        finally:
            track_bridge._context.reset(token)

    def test_deliberately_different_credits_are_not_rewritten(self):
        source = {**self.source, "artists": [{"name": "Another Artist"}, {"name": "Mister Hahn"}]}
        self.assertEqual(artist_credits.structured(source), source["artists"])
        self.assertIsNone(Matcher.match_track(source, [self.candidate], {}))

    def test_duration_and_guest_conflicts_remain_rejected(self):
        for change in (
            {"duration": 290000},
            {"title": "It’s Goin’ Down (feat. Different Guest)"},
            {"recording_id": "different"},
        ):
            candidate = {**self.candidate, **change}
            source = {**self.source}
            if "recording_id" in change:
                source["recording_id"] = "one"
            with self.subTest(change=change):
                self.assertIsNone(Matcher.match_track(source, [candidate], {}))

    def test_automatic_endpoint_keeps_structured_credits(self):
        request = api.MissingCandidateRequest(**self.source)
        with (
            patch.object(api, "_config"),
            patch("harmonia.library.playable_tracks", return_value=[self.candidate]),
            patch.object(api, "_record_log"),
            patch.object(Matcher, "match_track", wraps=Matcher.match_track) as match,
        ):
            result = api.retry_automatic(request)
        self.assertEqual(result["candidate"]["plex_id"], "correct")
        self.assertEqual(match.call_args.args[0]["artists"], self.source["artists"])

    def execute(self, candidate):
        playlist = {
            "source": "applemusic",
            "source_id": "replay",
            "plex_playlist_name": "Replay 2024",
        }
        config = SimpleNamespace(
            repository=self.repo,
            config={"playlists": [playlist]},
            mapping={},
            missing={},
            match_metadata={},
            ignored_tracks={},
            save=Mock(),
        )
        change = {
            "playlist_key": "applemusic:replay",
            "playlist_name": "Replay 2024",
            "search_key": self.source["title"] + "|" + self.source["artist"],
            "source": self.source,
            "previous_plex_id": None,
            "plex_id": "correct",
            "provenance": "automatic",
        }
        match_queue.stage_changes(self.repo, [change])
        store = SimpleNamespace(repository=self.repo)
        with (
            patch.object(api, "_config", return_value=config),
            patch.object(api, "job_store", return_value=store),
            patch.object(accounts, "root_repository", return_value=self.repo),
            patch("harmonia.library.playable_tracks", return_value=[candidate]),
            patch("harmonia.process_lock.ProcessLock", return_value=contextlib.nullcontext()),
        ):
            result = match_queue.execute(
                {"changes": match_queue.available(self.repo), "save_only": True}
            )
        return result, config

    def test_existing_automatic_draft_with_bad_credit_applies_without_rescan(self):
        result, config = self.execute(self.candidate)
        self.assertEqual(result["matches_saved"], 1)
        self.assertEqual(next(iter(config.mapping["applemusic:replay"].values())), "correct")
        self.assertEqual(match_queue.available(self.repo), [])

    def test_changed_candidate_error_identifies_track_and_preserves_draft(self):
        with self.assertRaisesRegex(
            ValueError, "Automatic match needs review: .*Goin.*Replay 2024"
        ):
            self.execute({**self.candidate, "duration": 290000})
        self.assertEqual(len(match_queue.available(self.repo)), 1)
