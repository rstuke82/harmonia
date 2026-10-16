import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from harmonia import accounts, discover, user_preferences, saved_views
from harmonia.storage import Repository


class HistoryExclusions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name))
        self.other = Repository(Path(tmp.name) / "other")
        for target in ["harmonia.discover.root_repository", "harmonia.accounts.root_repository"]:
            p = patch(target, return_value=self.repo)
            p.start()
            self.addCleanup(p.stop)
        p = patch(
            "harmonia.user_preferences.personal_repository",
            side_effect=lambda: self.other
            if (accounts.actor() or {}).get("id") == "other"
            else self.repo,
        )
        p.start()
        self.addCleanup(p.stop)
        p = patch(
            "harmonia.accounts.personal_repository",
            side_effect=lambda: self.other
            if (accounts.actor() or {}).get("id") == "other"
            else self.repo,
        )
        p.start()
        self.addCleanup(p.stop)
        app = FastAPI()
        user_preferences.register(app)
        discover.register(app)
        self.route = lambda path, method="GET": next(
            r.endpoint for r in app.routes if getattr(r, "path", "") == path and method in r.methods
        )
        self.ignore = self.route("/api/discover/history-ignored", "POST")
        self.restore = self.route("/api/discover/history-ignored/{key}", "DELETE")
        self.listing = self.route("/api/discover/history-ignored")
        self.Model = self.ignore.__annotations__["body"]

    def test_ignore_is_account_owned_and_restorable_without_global_blocklist(self):
        with accounts.as_user({"id": "me"}):
            first = self.ignore(
                self.Model(album="An Audiobook", artist="Author", artists=["Author"])
            )
            second = self.ignore(self.Model(album="An Audiobook", artist="Author"))
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(self.listing()), 1)
            self.assertEqual(self.repo.load("blocked_albums"), {})
        with accounts.as_user({"id": "other"}):
            self.assertEqual(self.listing(), [])
            self.restore(first["id"])
        with accounts.as_user({"id": "me"}):
            self.assertEqual(len(self.listing()), 1)
            self.restore(first["id"])
            self.assertEqual(self.listing(), [])

    def test_ignore_survives_artwork_and_credit_changes_but_not_other_artist(self):
        rule = {"album": "Tribute", "artist": "Various Artists", "artists": ["Author", "Guest"]}
        self.assertTrue(
            user_preferences.history_ignored(
                {"album": "TRIBUTE", "artist": "Guest", "artists": ["Guest"], "artwork": "new"},
                [rule],
            )
        )
        self.assertFalse(
            user_preferences.history_ignored({"album": "Tribute", "artist": "Other"}, [rule])
        )
        self.assertFalse(
            user_preferences.history_ignored({"album": "Another book", "artist": "Author"}, [rule])
        )
        self.assertFalse(
            user_preferences.history_ignored(
                {"album": "Tribute", "artist": "Author", "mbid": "new"}, [{**rule, "mbid": "old"}]
            )
        )

    def test_history_exclusions_are_applied_before_suggestion_limit_and_restore(self):
        settings = {
            "history_min_plays": 1,
            "history_sort": "plays",
            "history_max_suggestions": 1,
            "history_days": 0,
        }
        tracks = [
            {"name": "Chapter", "artist": "Author", "album": "An Audiobook"},
            {"name": "Song", "artist": "Artist", "album": "Music Album"},
        ]
        endpoint = self.route("/api/discover/history-gaps").__wrapped__
        with (
            patch(
                "harmonia.user_preferences.preferences",
                return_value={"lastfm_username": "listener"},
            ),
            patch("harmonia.discover.lastfm_settings", return_value=settings),
            patch("harmonia.discover.cached_history_tracks", return_value=(tracks, False, True)),
            patch("harmonia.discover.availability", side_effect=lambda rows: rows),
            accounts.as_user({"id": "me"}),
        ):
            self.assertEqual(endpoint()["rows"][0]["album"], "An Audiobook")
            rule = self.ignore(self.Model(album="An Audiobook", artist="Author"))
            self.assertEqual(endpoint()["rows"][0]["album"], "Music Album")
            self.restore(rule["id"])
            self.assertEqual(endpoint()["rows"][0]["album"], "An Audiobook")
        self.assertIn("lastfm_history_ignored", saved_views.DISCOVER)
