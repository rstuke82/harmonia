import unittest
from types import SimpleNamespace
from unittest.mock import patch
import requests
from fastapi import HTTPException
from harmonia import library_artwork as covers, image_download


class ArtworkFallback(unittest.TestCase):
    plex = "/api/library/plex-albums/42/image?v=old"
    chosen = "https://example.com/chosen.jpg"
    alternate = "https://coverartarchive.org/release-group/mb/front-500"

    def setUp(self):
        p = patch(
            "harmonia.library_inventory.indexed",
            return_value={
                "plex_albums": {"42": {"plex_album_thumb": "/library/metadata/42/thumb/old"}}
            },
        )
        p.start()
        self.addCleanup(p.stop)

    def test_missing_plex_uses_chosen_cover_before_other_sources(self):
        with (
            patch("harmonia.plex_images.serve", side_effect=HTTPException(404, "missing")),
            patch(
                "harmonia.image_download.download",
                return_value=image_download.ImageBytes(b"chosen", {"Content-Type": "image/jpeg"}),
            ) as download,
        ):
            result = covers.serve(SimpleNamespace(), [self.plex, self.chosen, self.alternate])
        self.assertEqual(result.body, b"chosen")
        self.assertEqual(download.call_args.args[0], self.chosen)
        self.assertTrue(download.call_args.kwargs["external"])

    def test_busy_and_network_errors_do_not_replace_plex(self):
        for status in (502, 503):
            with (
                patch("harmonia.plex_images.serve", side_effect=HTTPException(status, "busy")),
                patch("harmonia.image_download.download") as download,
            ):
                with self.assertRaises(HTTPException) as exc:
                    covers.serve(SimpleNamespace(), [self.plex, self.chosen])
                self.assertEqual(exc.exception.status_code, status)
                download.assert_not_called()

    def test_no_plex_link_uses_external_without_plex_io(self):
        with (
            patch("harmonia.plex_images.serve") as plex,
            patch(
                "harmonia.image_download.download",
                return_value=image_download.ImageBytes(b"cover", {"Content-Type": "image/png"}),
            ),
        ):
            self.assertEqual(covers.serve(SimpleNamespace(), [self.chosen]).body, b"cover")
            plex.assert_not_called()

    def test_missing_external_can_fall_back_but_transient_error_cannot(self):
        for status, expected in ((404, True), (410, True), (500, False)):
            response = requests.Response()
            response.status_code = status
            error = requests.HTTPError(response=response)
            with patch(
                "harmonia.image_download.download",
                side_effect=[
                    error,
                    image_download.ImageBytes(b"alt", {"Content-Type": "image/jpeg"}),
                ],
            ) as download:
                if expected:
                    self.assertEqual(
                        covers.serve(SimpleNamespace(), [self.chosen, self.alternate]).body, b"alt"
                    )
                else:
                    with self.assertRaises(HTTPException) as exc:
                        covers.serve(SimpleNamespace(), [self.chosen, self.alternate])
                    self.assertEqual(exc.exception.status_code, 502)
                    self.assertEqual(download.call_count, 1)

    def test_hidden_plex_album_does_not_fetch_or_fall_back(self):
        with (
            patch("harmonia.library_inventory.indexed", return_value={"plex_albums": {}}),
            patch("harmonia.image_download.download") as download,
        ):
            with self.assertRaises(HTTPException):
                covers.serve(SimpleNamespace(), [self.plex, self.chosen])
            download.assert_not_called()

    def test_url_versions_change_with_cover_choice(self):
        self.assertNotEqual(
            covers.url("plex:42", [self.plex, self.chosen]),
            covers.url("plex:42", [self.plex, self.alternate]),
        )


class SavedLibraryCoverIntegration(unittest.TestCase):
    def setUp(self):
        from test_album_detail_reads import AlbumReads

        AlbumReads.setUp(self)

    def test_pending_chosen_cover_is_used_without_plex_and_not_overwritten(self):
        from test_album_detail_reads import ALBUM
        from harmonia import accounts, library
        from harmonia.lidarr import state_put

        self.current.return_value = ({"checked_at": 1, "rows": []}, self.lidarr)
        chosen = {
            "url": "https://example.org/chosen.jpg",
            "provider": "Apple Music",
            "selected_at": 1,
            "sync_status": "pending",
        }
        state_put(self.repo, "artwork_overrides", "album:" + ALBUM + ":cover", chosen)
        with accounts.as_user(self.user):
            model = library.cached_library()
            self.assertTrue(
                model["_rows_by_album"][ALBUM]["artwork"].startswith(
                    "/api/library/albums/" + ALBUM + "/artwork?v="
                )
            )
            self.assertEqual(model["_artwork_sources"][ALBUM][0], chosen["url"])
            app = __import__("fastapi").FastAPI()
            library.register(app)
            endpoint = next(
                r.endpoint for r in app.routes if r.path == "/api/library/albums/{album_id}/artwork"
            )
            with (
                patch(
                    "harmonia.image_download.download",
                    return_value=image_download.ImageBytes(
                        b"cover", {"Content-Type": "image/jpeg"}
                    ),
                ),
                patch("harmonia.plex_images.serve") as plex,
            ):
                self.assertEqual(endpoint(ALBUM).body, b"cover")
                plex.assert_not_called()
            self.assertEqual(
                self.repo.load_one("artwork_overrides", "album:" + ALBUM + ":cover"), chosen
            )

    def test_unknown_library_album_cannot_trigger_an_image_download(self):
        from harmonia import accounts, library

        app = __import__("fastapi").FastAPI()
        library.register(app)
        endpoint = next(
            r.endpoint for r in app.routes if r.path == "/api/library/albums/{album_id}/artwork"
        )
        with accounts.as_user(self.user), patch("harmonia.image_download.download") as download:
            with self.assertRaises(HTTPException):
                endpoint("not-in-library")
            download.assert_not_called()
