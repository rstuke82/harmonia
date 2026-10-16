"""Album linking shares Library semantics without preparing the full catalog or Issues."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI, Response

from harmonia import accounts, library
from harmonia.storage import Repository
import test_album_detail_reads as album_fixtures

ARTIST, ALBUM = album_fixtures.ARTIST, album_fixtures.ALBUM


class LinkIndexSemantics(unittest.TestCase):
    def setUp(self):
        album_fixtures.AlbumReads.setUp(self)

    def test_owned_targets_match_full_library_without_issues_or_catalog_only_rows(self):
        self.repo.put(
            "library_artist_catalog",
            ARTIST,
            {
                "rows": [
                    {
                        "id": ALBUM,
                        "title": "Partial",
                        "official": True,
                        "secondary_types": ["Live"],
                    },
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "title": "Catalog only",
                        "official": True,
                    },
                ]
            },
        )
        with accounts.as_user(self.user):
            full = library.build_library()
            with (
                patch("harmonia.file_audit.report", side_effect=AssertionError("File audit")),
                patch.object(library, "issues", side_effect=AssertionError("Issues")),
                patch(
                    "harmonia.edition_preferences.findings",
                    side_effect=AssertionError("Edition findings"),
                ),
            ):
                light = library.build_link_library()
            self.assertEqual(library.link_targets(light), library.link_targets(full))
        self.assertLess(len(light["rows"]), len(full["rows"]))
        self.assertEqual(light["artists"], [])
        self.assertEqual(light["errors"], [])

    def test_chosen_artwork_and_cached_version_change_together(self):
        self.current.return_value = ({"checked_at": 1, "rows": []}, self.lidarr)
        library.invalidate()
        with accounts.as_user(self.user):
            first = library.cached_link_library()
            chosen = {
                "url": "https://example.org/new-cover.jpg",
                "provider": "Apple Music",
                "selected_at": 2,
            }
            self.repo.put("artwork_overrides", "album:" + ALBUM + ":cover", chosen)
            second = library.cached_link_library()
            full = library.build_library()
        self.assertNotEqual(
            first["_rows_by_album"][ALBUM]["artwork"], second["_rows_by_album"][ALBUM]["artwork"]
        )
        self.assertEqual(
            second["_rows_by_album"][ALBUM]["artwork"], full["_rows_by_album"][ALBUM]["artwork"]
        )
        self.assertEqual(second["_artwork_sources"][ALBUM], full["_artwork_sources"][ALBUM])
        self.assertEqual(
            self.repo.load_one("artwork_overrides", "album:" + ALBUM + ":cover"), chosen
        )

    def test_owned_album_cover_does_not_force_full_library(self):
        app = FastAPI()
        library.register(app)
        endpoint = next(
            r.endpoint for r in app.routes if r.path == "/api/library/albums/{album_id}/artwork"
        )
        with (
            accounts.as_user(self.user),
            patch.object(library, "cached_library", side_effect=AssertionError("Full model")),
            patch("harmonia.library_artwork.serve", return_value=Response(b"cover")),
        ):
            self.assertEqual(endpoint(ALBUM).body, b"cover")

    def test_actual_history_candidate_endpoint_uses_only_link_index(self):
        from harmonia import discover

        app = FastAPI()
        discover.register(app)
        endpoint = next(
            r.endpoint for r in app.routes if r.path == "/api/discover/history-gaps/candidates"
        )
        body = next(iter(__import__("inspect").signature(endpoint).parameters.values())).annotation
        with (
            accounts.as_user(self.user),
            patch.object(library, "cached_library", side_effect=AssertionError("Full model")),
        ):
            result = endpoint(
                body(history_key="f" * 64, artist="Artist", album="Partial", tracks=["Song"])
            )
        self.assertTrue(any(row["target_id"] == ALBUM for row in result["rows"]))


class LinkIndexCache(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name))
        self.cfg = SimpleNamespace(repository=self.repo, config={"plex": {"token": "one"}})
        for target in [
            "harmonia.accounts.root_repository",
            "harmonia.accounts.personal_repository",
        ]:
            patcher = patch(target, return_value=self.repo)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("harmonia.api._config", return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        library.invalidate()
        self.addCleanup(library.invalidate)

    def test_issue_reviews_profiles_jobs_and_requests_do_not_rebuild(self):
        with (
            accounts.as_user({"id": "one", "admin": True}),
            patch.object(library, "build_link_library", side_effect=lambda: object()) as build,
        ):
            first = library.cached_link_library()
            for namespace in [
                "file_audit",
                "file_issue_reviews",
                "library_artist_metadata",
                "library_lastfm_artist_metadata",
                "runtime",
                "lidarr_requests",
                "job_activity",
            ]:
                self.repo.put(namespace, "changed", {"value": 1})
                self.assertIs(library.cached_link_library(), first, namespace)
            self.assertEqual(build.call_count, 1)
            for namespace in [
                "artwork_overrides",
                "inventory",
                "track_links",
                "plex_album_links",
                "blocked_albums",
                "metadata_sources",
            ]:
                self.repo.put(namespace, "changed", {"value": 1})
                newer = library.cached_link_library()
                self.assertIsNot(newer, first, namespace)
                first = newer
            self.assertEqual(build.call_count, 7)

    def test_identity_permissions_and_credentials_remain_isolated(self):
        with patch.object(library, "build_link_library", side_effect=lambda: object()) as build:
            with accounts.as_user({"id": "one"}):
                first = library.cached_link_library()
                self.cfg.config["plex"]["token"] = "changed"
                self.assertIsNot(library.cached_link_library(), first)
            with accounts.as_user({"id": "two"}):
                library.cached_link_library()
            with accounts.as_user({"id": "one", "admin": True}):
                library.cached_link_library()
            self.assertEqual(build.call_count, 4)

    def test_mutation_or_explicit_invalidation_during_build_is_not_cached(self):
        for mutate in [
            lambda: self.repo.put("inventory", "plex", {"checked_at": 1}),
            library.invalidate,
        ]:
            library.invalidate()
            with patch.object(
                library, "build_link_library", side_effect=lambda: (mutate(), object())[1]
            ):
                library.cached_link_library()
            self.assertFalse(library._link_cache)

    def test_background_full_view_does_not_hold_linking_lock(self):
        import threading

        started, release = threading.Event(), threading.Event()
        errors = []

        def slow_build():
            started.set()
            if not release.wait(3):
                raise AssertionError("Linking waited for the full view")
            return {"full": True}

        def full_view():
            try:
                with accounts.as_user({"id": "one", "admin": True}):
                    library.cached_library()
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(library, "build_library", side_effect=slow_build),
            patch.object(library, "build_link_library", return_value={"link": True}),
        ):
            thread = threading.Thread(target=full_view)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                with accounts.as_user({"id": "one", "admin": True}):
                    self.assertEqual(library.cached_link_library(), {"link": True})
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
