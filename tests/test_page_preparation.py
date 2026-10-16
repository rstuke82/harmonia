"""Page preparation reuses only matching account data and bounded revision indexes."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harmonia import accounts, discover, library, library_inventory, saved_views, user_preferences
from harmonia.storage import Repository
from library_fixtures import model_from_inventory


class PagePreparation(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name))
        self.cfg = SimpleNamespace(repository=self.repo, config={"plex": {"token": "fixture"}})
        for target, value in [
            ("harmonia.accounts.root_repository", self.repo),
            ("harmonia.accounts.personal_repository", self.repo),
            ("harmonia.api._config", self.cfg),
        ]:
            mocked = patch(target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        library.invalidate()
        self.addCleanup(library.invalidate)

    def test_playlist_and_unrelated_account_updates_do_not_rebuild_library(self):
        with (
            accounts.as_user({"id": "one", "admin": True}),
            patch.object(library, "build_library", return_value={"fixture": True}) as build,
        ):
            first = library.cached_library()
            self.repo.put("runtime", "playlists", [{"last_synced": "now"}])
            self.repo.put("accounts", "someone", {"last_login": "now"})
            self.assertIs(library.cached_library(), first)
            self.assertEqual(build.call_count, 1)
            self.repo.put("artwork_overrides", "album:one:cover", {"url": "new"})
            library.cached_library()
            self.assertEqual(build.call_count, 2)

    def test_account_permissions_credentials_and_inventory_still_invalidate(self):
        with patch.object(library, "build_library", side_effect=lambda: object()) as build:
            with accounts.as_user({"id": "one"}):
                first = library.cached_library()
                self.cfg.config["plex"]["token"] = "changed"
                self.assertIsNot(library.cached_library(), first)
                self.repo.put("inventory", "plex", {"checked_at": 1})
                library.cached_library()
            with accounts.as_user({"id": "two"}):
                library.cached_library()
            with accounts.as_user({"id": "one", "can_library_issues": True}):
                library.cached_library()
            self.assertEqual(build.call_count, 5)

    def test_recently_used_library_survives_bounded_cache_eviction(self):
        with patch.object(library, "build_library", side_effect=lambda: object()) as build:
            for user in ("one", "two", "three", "four"):
                with accounts.as_user({"id": user}):
                    library.cached_library()
            with accounts.as_user({"id": "one"}):
                first = library.cached_library()
            with accounts.as_user({"id": "five"}):
                library.cached_library()
            with accounts.as_user({"id": "one"}):
                self.assertIs(library.cached_library(), first)
            self.assertEqual(build.call_count, 5)

    def test_invalidation_during_build_prevents_republishing_old_cache(self):
        def build():
            library.invalidate()
            return {"old": True}

        with patch.object(library, "build_library", side_effect=build):
            library.cached_library()
        self.assertFalse(library._view_cache)

    def test_unrelated_inventory_reads_do_not_wait_for_slow_account(self):
        started, release = threading.Event(), threading.Event()
        errors = []

        def inventory(repo, config):
            if accounts.actor()["id"] == "slow":
                started.set()
                if not release.wait(3):
                    raise AssertionError("other account was blocked")
            return {"rows": [], "checked_at": 1}, {}

        def slow():
            try:
                with accounts.as_user({"id": "slow"}):
                    library_inventory.indexed(self.cfg)
            except BaseException as exc:
                errors.append(exc)

        with (
            patch("harmonia.inventory.current", side_effect=inventory),
            patch("harmonia.lidarr.config", return_value={"url": ""}),
        ):
            thread = threading.Thread(target=slow)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                with accounts.as_user({"id": "fast"}):
                    self.assertEqual(library_inventory.indexed(self.cfg)["plex"]["checked_at"], 1)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_inventory_invalidated_during_loading_is_not_recached(self):
        def load(*args):
            library_inventory.clear()
            return {"rows": []}, {}

        with (
            patch("harmonia.inventory.current", side_effect=load),
            patch("harmonia.lidarr.config", return_value={"url": ""}),
        ):
            library_inventory.indexed(self.cfg)
        self.assertFalse(library_inventory._cache)

    def test_history_blocks_are_loaded_once_and_bridge_tracks_walked_once(self):
        class Counted(list):
            def __iter__(self):
                self.walks += 1
                return super().__iter__()

        tracks = Counted([{"id": "1", "album_id": "album", "title": "Song"}])
        tracks.walks = 0
        model = model_from_inventory(
            {
                "lidarr": {
                    "checked_at": 1,
                    "rows": [{"album_id": "album", "title": "Album", "artist": "Band"}],
                    "tracks": tracks,
                },
                "plex": {"checked_at": 1, "rows": []},
            }
        )
        model["_ctx"]["tracks"] = tracks
        original = self.repo.load
        loads = []

        def load(namespace):
            loads.append(namespace)
            return original(namespace)

        rows = [{"artist": "Band", "album": "Album", "mbid": "album"} for _ in range(100)]
        tracks.walks = 0
        with (
            patch.object(library, "cached_link_library", return_value=model),
            patch.object(self.repo, "load", side_effect=load),
            patch("harmonia.config.Config", return_value=self.cfg),
            patch("harmonia.discover.root_repository", return_value=self.repo),
        ):
            result = discover.availability(rows, deduplicate=False)
        self.assertEqual(len(result), 100)
        self.assertEqual(loads.count("blocked_artists"), 1)
        self.assertEqual(loads.count("blocked_albums"), 1)
        self.assertEqual(tracks.walks, 1)

    def test_empty_discovery_does_not_build_library(self):
        with patch.object(library, "cached_library", side_effect=AssertionError("empty discovery")):
            self.assertEqual(discover.availability([]), [])

    def test_preloaded_block_rules_match_live_rules(self):
        artists = [{"name": "Blocked"}]
        albums = [{"artist": "Band", "album": "Album"}]
        self.repo.put("blocked_artists", "rule", artists[0])
        self.repo.put("blocked_albums", "rule", albums[0])
        for row in [
            {"artist": "Blocked", "album": "Other"},
            {"artist": "Band", "album": "Album"},
            {"artist": "Allowed", "album": "Other"},
        ]:
            self.assertEqual(
                user_preferences.blocked(row),
                user_preferences.blocked(row, artist_rules=artists, album_rules=albums),
            )

    def test_pending_and_stale_responses_identify_preparation_job(self):
        from harmonia.jobs import Store

        with (
            patch.object(saved_views, "personal_repository", return_value=self.repo),
            patch.object(saved_views, "root_repository", return_value=self.repo),
        ):

            @saved_views.saved("page progress fixture", {"inventory"})
            def read():
                return {"ready": True}

            self.addCleanup(saved_views._providers.pop, "page progress fixture", None)
            self.addCleanup(saved_views._readers.pop, "page progress fixture", None)
            pending = read()
            ident = pending.headers["X-Saved-View-Job"]
            self.assertEqual(Store(self.repo).get(ident)["action"], "prepare_views")
            key = pending.headers["X-Saved-View"]
            with self.repo.connect() as db:
                db.execute("UPDATE prepared_views SET data=? WHERE key=?", ("{}", key))
            stale = read()
            self.assertEqual(stale.status_code, 200)
            self.assertEqual(stale.headers["X-Saved-View-Job"], ident)
