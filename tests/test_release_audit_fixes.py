import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI
from harmonia import accounts, library, saved_views, album_apple_artwork
from harmonia.edition_preferences import findings, ranker
from harmonia.database_maintenance import clear_cache, status
from harmonia.storage import Repository
from harmonia.jobs import Store
import test_album_detail_reads as fixtures


class ArtworkLinkFix(unittest.TestCase):
    setUp = fixtures.AlbumReads.setUp
    prepare = fixtures.AlbumReads.prepare

    def routes(self):
        app = FastAPI()
        library.register(app)
        put = next(
            r.endpoint
            for r in app.routes
            if r.path.endswith("/service-links") and "PUT" in r.methods
        )
        reset = next(r.endpoint for r in app.routes if r.path.endswith("/service-links/{provider}"))
        choices = next(
            r.endpoint
            for r in app.routes
            if r.path == "/api/library/artwork/{target}/{target_id}/{kind}"
        )
        return put, reset, choices

    def response(self, album=123):
        return Mock(
            status_code=200,
            json=lambda: {
                "results": [
                    {
                        "wrapperType": "collection",
                        "collectionType": "Album",
                        "collectionId": album,
                        "artworkUrl100": f"https://is1-ssl.mzstatic.com/{album}/100x100bb.jpg",
                    }
                ]
            },
        )

    def test_changed_link_changes_candidate_and_preserves_selected_cover(self):
        put, reset, choices = self.routes()
        self.repo.save(
            {
                "artwork_overrides": {
                    "album:" + fixtures.ALBUM + ":cover": {
                        "url": "https://saved.example/cover.jpg",
                        "provider": "manual",
                    }
                }
            }
        )
        with (
            accounts.as_user(self.user),
            patch("harmonia.fanart.lookup", return_value={}),
            patch.object(album_apple_artwork.network, "get", return_value=self.response()) as get,
        ):
            put(
                fixtures.ALBUM,
                SimpleNamespace(provider="apple", url="https://music.apple.com/gb/album/new/123"),
            )
            selected = self.repo.load_one("artwork_overrides", "album:" + fixtures.ALBUM + ":cover")
            self.assertEqual(selected["url"], "https://saved.example/cover.jpg")
            result = choices("album", fixtures.ALBUM, "cover")
            self.assertEqual(
                next(r["url"] for r in result["candidates"] if r["provider"] == "apple"),
                "https://is1-ssl.mzstatic.com/123/1200x1200bb.jpg",
            )
            self.assertEqual(
                get.call_args.kwargs["params"], {"id": "123", "country": "gb", "entity": "album"}
            )
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            get.return_value = self.response(456)
            put(
                fixtures.ALBUM,
                SimpleNamespace(provider="apple", url="https://music.apple.com/us/album/new/456"),
            )
            result = choices("album", fixtures.ALBUM, "cover")
            self.assertEqual(
                next(r["url"] for r in result["candidates"] if r["provider"] == "apple"),
                "https://is1-ssl.mzstatic.com/456/1200x1200bb.jpg",
            )
            get.side_effect = AssertionError("Artwork reads must use saved Apple metadata")
            choices("album", fixtures.ALBUM, "cover")
            reset(fixtures.ALBUM, "apple")
            result = choices("album", fixtures.ALBUM, "cover")
            self.assertEqual(
                next(r["url"] for r in result["candidates"] if r["provider"] == "apple"),
                "https://example.org/apple.jpg",
            )

    def test_failed_lookup_suppresses_old_candidate_and_reports_retry(self):
        put, _, choices = self.routes()
        import requests

        with (
            accounts.as_user(self.user),
            patch("harmonia.fanart.lookup", return_value={}),
            patch.object(
                album_apple_artwork.network, "get", side_effect=requests.ConnectionError()
            ),
        ):
            result = put(
                fixtures.ALBUM,
                SimpleNamespace(provider="apple", url="https://music.apple.com/us/album/new/123"),
            )
            self.assertIn("retry", result["apple"]["artwork_message"])
            result = choices("album", fixtures.ALBUM, "cover")
            self.assertFalse(any(r["provider"] == "apple" for r in result["candidates"]))
            self.assertIn("retry", result["apple_message"])
        with (
            accounts.as_user(self.user),
            patch.object(album_apple_artwork.network, "get", return_value=self.response()),
        ):
            put(
                fixtures.ALBUM,
                SimpleNamespace(provider="apple", url="https://music.apple.com/us/album/new/123"),
            )
            self.assertTrue(album_apple_artwork.artwork(self.repo, fixtures.ALBUM))

    def test_cleared_link_and_stale_lookup_never_show_wrong_art(self):
        self.repo.save(
            {
                "library_album_service_links": {
                    fixtures.ALBUM: {"apple": "https://music.apple.com/us/album/new/456"}
                },
                album_apple_artwork.NAMESPACE: {
                    fixtures.ALBUM: {
                        "url": "https://music.apple.com/us/album/old/123",
                        "artwork": "old",
                    }
                },
            }
        )
        self.assertEqual(album_apple_artwork.artwork(self.repo, fixtures.ALBUM, "automatic"), "")
        self.repo.save({"library_album_service_links": {fixtures.ALBUM: {"apple": ""}}})
        self.assertEqual(album_apple_artwork.artwork(self.repo, fixtures.ALBUM, "automatic"), "")

    def test_wrong_album_response_and_redirect_are_not_used(self):
        for response in (self.response(999), Mock(status_code=302)):
            with patch.object(album_apple_artwork.network, "get", return_value=response) as get:
                result = album_apple_artwork.refresh(
                    self.repo, fixtures.ALBUM, "https://music.apple.com/us/album/new/123"
                )
                self.assertEqual(result["artwork"], "")
                self.assertTrue(result["message"])
                self.assertEqual(get.call_count, 1)

    def test_old_saved_link_recovers_when_picker_requests_refresh(self):
        _, _, choices = self.routes()
        app = FastAPI()
        library.register(app)
        refresh = next(
            r.endpoint
            for r in app.routes
            if r.path == "/api/library/albums/{album_id}/artwork/apple/refresh"
        )
        self.repo.save(
            {
                "library_album_service_links": {
                    fixtures.ALBUM: {"apple": "https://music.apple.com/gb/album/new/123"}
                }
            }
        )
        with (
            accounts.as_user(self.user),
            patch("harmonia.fanart.lookup", return_value={}),
            patch.object(album_apple_artwork.network, "get", return_value=self.response()) as get,
        ):
            before = choices("album", fixtures.ALBUM, "cover")
            self.assertTrue(before["apple_refresh_required"])
            get.assert_not_called()
            refresh(fixtures.ALBUM)
            after = choices("album", fixtures.ALBUM, "cover")
            self.assertFalse(after["apple_refresh_required"])
            self.assertTrue(any(r["provider"] == "apple" for r in after["candidates"]))
            self.assertEqual(get.call_count, 1)

    def test_picker_excludes_plex_even_as_a_previous_selection(self):
        _, _, choices = self.routes()
        self.plex["rows"][0].update(plex_artist_thumb="/portrait", plex_artist_art="/background")
        self.repo.save(
            {
                "artwork_overrides": {
                    "album:" + fixtures.ALBUM + ":cover": {
                        "url": "/old-plex-image",
                        "provider": "plex",
                    }
                }
            }
        )
        with accounts.as_user(self.user), patch("harmonia.fanart.lookup", return_value={}):
            result = choices("album", fixtures.ALBUM, "cover")
            for kind in ("portrait", "hero"):
                self.assertFalse(
                    any(r["provider"] == "plex" for r in choices("artist", "1", kind)["candidates"])
                )
        self.assertFalse(any(r["provider"] == "plex" for r in result["candidates"]))

    def test_hidden_link_never_requests_refresh(self):
        _, _, choices = self.routes()
        self.repo.save({"library_album_service_links": {fixtures.ALBUM: {"apple": ""}}})
        with accounts.as_user(self.user), patch("harmonia.fanart.lookup", return_value={}):
            result = choices("album", fixtures.ALBUM, "cover")
        self.assertFalse(result["apple_refresh_required"])

    def test_grassroots_anniversary_lookup_response(self):
        url = "https://music.apple.com/us/album/grassroots-30th-anniversary-edition/1755466686"
        image = "https://is1-ssl.mzstatic.com/image/thumb/Music211/v4/e6/6b/a8/e66ba831-ee93-1ecc-0e52-b93000c9b655/196871953429.jpg/100x100bb.jpg"
        # Relevant fields from the live Apple lookup observed October 9, 2026.
        response = Mock(
            status_code=200,
            json=lambda: {
                "resultCount": 1,
                "results": [
                    {
                        "wrapperType": "collection",
                        "collectionType": "Album",
                        "collectionId": 1755466686,
                        "artistName": "311",
                        "collectionName": "Grassroots (30th Anniversary Edition)",
                        "artworkUrl100": image,
                    }
                ],
            },
        )
        with patch.object(album_apple_artwork.network, "get", return_value=response) as get:
            result = album_apple_artwork.refresh(self.repo, fixtures.ALBUM, url)
        self.assertEqual(result["artwork"], image.replace("100x100bb", "1200x1200bb"))
        self.assertEqual(result["message"], "")
        self.assertEqual(get.call_args.kwargs["params"]["id"], "1755466686")


class PreferenceFix(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name))
        patcher = patch("harmonia.lidarr.config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, formats=None, countries=None, official=True):
        self.repo.save(
            {
                "musicbrainz_settings": {
                    "preferences": {
                        "preferred_formats": ["CD", "Digital Media"]
                        if formats is None
                        else formats,
                        "preferred_countries": ["US", "XW"] if countries is None else countries,
                        "prefer_official": official,
                    }
                }
            }
        )

    def inventory(
        self, fmt="Digital Media", country="United States", status="Official", alternative="CD"
    ):
        return {
            "checked_at": 9999999999,
            "rows": [
                {
                    "album_id": "group",
                    "releases": [
                        {
                            "foreignReleaseId": "current",
                            "monitored": True,
                            "status": status,
                            "country": [country],
                            "media": [{"mediumFormat": fmt}],
                        },
                        {
                            "foreignReleaseId": "alternative",
                            "status": "Official",
                            "country": ["United States"],
                            "media": [{"mediumFormat": alternative}],
                        },
                    ],
                }
            ],
        }

    def test_any_allowed_format_and_country_is_acceptable(self):
        for formats in (["CD", "Digital Media"], ["Digital Media", "CD"]):
            self.configure(formats)
            for fmt in formats:
                for country in ("United States", "[Worldwide]"):
                    self.assertFalse(findings(self.inventory(fmt, country), self.repo))

    def test_only_suggest_acceptable_alternative(self):
        self.configure()
        self.assertEqual(
            findings(self.inventory("Vinyl"), self.repo)[0]["evidence"]["preferred_release"],
            "alternative",
        )
        self.configure(["Digital Media"])
        self.assertFalse(findings(self.inventory("Vinyl"), self.repo))

    def test_empty_preferences_and_official_switch(self):
        self.configure([], [])
        self.assertFalse(findings(self.inventory("Vinyl", "Japan"), self.repo))
        self.configure()
        self.assertTrue(findings(self.inventory(status="Bootleg"), self.repo))
        self.configure(official=False)
        self.assertFalse(findings(self.inventory(status="Bootleg"), self.repo))

    def test_request_ranking_still_prefers_first_format(self):
        releases = self.inventory()["rows"][0]["releases"]
        self.assertEqual(
            min(releases, key=ranker({"preferred_formats": ["CD", "Digital Media"]}))[
                "foreignReleaseId"
            ],
            "alternative",
        )


class PersistentCacheFix(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name))
        for name in ("root_repository", "personal_repository"):
            for module in ("accounts", "saved_views"):
                patcher = patch(f"harmonia.{module}.{name}", return_value=self.repo)
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher = patch("harmonia.accounts.users", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clear_counts_persistent_views_and_reprepares(self):
        @saved_views.saved("audit cache fix", deps={"inventory"})
        def page():
            return {"fresh": True}

        with accounts.as_user(None):
            first = page()
            key = first.headers["X-Saved-View"]
            with self.repo.connect() as db:
                db.execute(
                    "UPDATE prepared_views SET data=?,revision=? WHERE key=?",
                    (
                        json.dumps({"old": True}),
                        saved_views.revision(self.repo, {"inventory", "library_views_epoch"}),
                        key,
                    ),
                )
            self.assertEqual(status(self.repo)["cache_groups"]["library_views"]["entries"], 1)
            before = saved_views.revision(self.repo, {"library_views_epoch"})
            self.assertEqual(clear_cache(self.repo, "library_views")["deleted"], 1)
            self.assertIsNone(saved_views.load_row(self.repo, key))
            self.assertNotEqual(before, saved_views.revision(self.repo, {"library_views_epoch"}))
            second = page()
            self.assertEqual(second.status_code, 202)
            self.assertNotEqual(json.loads(first.body)["job_id"], json.loads(second.body)["job_id"])
            job = Store(self.repo).get(json.loads(second.body)["job_id"])
            with (
                patch(
                    "harmonia.jobs.current",
                    return_value=SimpleNamespace(
                        store=Store(self.repo), id=job["id"], action="prepare_views"
                    ),
                ),
                patch("harmonia.jobs.progress"),
            ):
                saved_views.execute(job["payload"])
            self.assertEqual(json.loads(page().body), {"fresh": True})

    def test_inflight_preparation_cannot_restore_cleared_data(self):
        @saved_views.saved("audit racing cache fix", deps={"inventory"})
        def page():
            clear_cache(self.repo, "library_views")
            reader()
            return {"old": True}

        reader = page
        with accounts.as_user(None):
            pending = reader()
            key = pending.headers["X-Saved-View"]
            job = Store(self.repo).get(json.loads(pending.body)["job_id"])
            context = SimpleNamespace(store=Store(self.repo), id=job["id"], action="prepare_views")
            with (
                patch("harmonia.jobs.current", return_value=context),
                patch("harmonia.jobs.progress"),
            ):
                saved_views.execute(job["payload"])
            row = saved_views.load_row(self.repo, key)
            self.assertIsNone(row["data"])
            self.assertNotEqual(row["job_id"], job["id"])

    def test_root_clear_includes_personal_cache(self):
        member = Repository(self.repo.directory / "users" / "2")
        with member.connect() as db:
            db.execute(
                "INSERT INTO prepared_views(key,name,params,permissions,data) VALUES('member','library','{}','{}','{}')"
            )
        with (
            patch("harmonia.accounts.users", return_value={"2": {"id": "2", "admin": False}}),
            patch("harmonia.accounts.personal_repository", return_value=member),
        ):
            self.assertEqual(clear_cache(self.repo, "library_views")["deleted"], 1)
        self.assertIsNone(saved_views.load_row(member, "member"))


if __name__ == "__main__":
    unittest.main()
