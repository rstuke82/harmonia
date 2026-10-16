"""Public provider payloads, artwork fallbacks and safe error boundaries."""

import json
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from harmonia import apple_music_source as apple, spotify_source as spotify
from harmonia.runtime_paths import APPLE_ARTWORK_SIZE

SPOTIFY = "37i9dQZF1DXcBWIGoYBM5M"
APPLE = "https://music.apple.com/us/playlist/test/pl.123"


def page(entity):
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"state": {"data": {"entity": entity}}}}})
        + "</script>"
    )


class SpotifyArtwork(unittest.TestCase):
    def test_artwork_host_and_asset_validation(self):
        good = [
            "https://i.scdn.co/image/abc",
            "https://mosaic.scdn.co/abc",
            "https://image-cdn-fa.spotifycdn.com/image/abc",
            "https://cdn.spotifycdn.com/cover.jpg",
        ]
        bad = [
            None,
            {},
            "http://i.scdn.co/image/a",
            "https:///x",
            "https://example.org/a.png",
            "https://i.scdn.co.evil.org/image/a",
            "https://i.scdn.co/static/js/a",
            "https://i.scdn.co/image/a.css",
        ]
        for url in good:
            with self.subTest(url=url):
                self.assertEqual(spotify.SpotifyAPI._clean_artwork_url(url), url)
        for url in bad:
            with self.subTest(url=url):
                self.assertEqual(spotify.SpotifyAPI._clean_artwork_url(url), "")

    def test_artwork_embedded_entities_og_and_eager_images(self):
        urls = ["https://i.scdn.co/image/abc", "https://i.scdn.co/image/def"]
        for html, entity in [
            ("", {"images": [{"url": urls[0]}]}),
            ('<meta property="og:image" content="' + urls[0] + '">', {}),
            ('<meta content="' + urls[0] + '" property="og:image">', {}),
            ('<img loading="eager" src="' + urls[0] + '">', {}),
            (urls[0], {}),
        ]:
            self.assertEqual(spotify.SpotifyAPI._extract_artwork_url(html, entity), urls[0])
        self.assertEqual(spotify.SpotifyAPI._extract_artwork_url("nothing", {}), "")

    def test_oembed_rejects_failure_nonimage_and_invalid_identifier(self):
        for response in [
            Mock(status_code=503),
            Mock(status_code=200, json=lambda: {"thumbnail_url": "https://example.com/a.png"}),
            spotify.network.Timeout(),
        ]:
            with patch.object(
                spotify.network,
                "get",
                **(
                    {"side_effect": response}
                    if isinstance(response, Exception)
                    else {"return_value": response}
                ),
            ):
                self.assertEqual(spotify.SpotifyAPI._fetch_oembed_artwork(SPOTIFY), "")
        with patch.object(spotify.network, "get") as get:
            self.assertEqual(spotify.SpotifyAPI._fetch_oembed_artwork("not-an-id"), "")
        get.assert_not_called()

    def test_fetch_prefers_oembed_then_entity_then_public_page(self):
        entity = {"name": "Playlist", "trackList": [], "trackCount": 0}
        cover = "https://i.scdn.co/image/abc"
        for embedded, oembed, wanted_calls in [
            ({}, cover, 1),
            ({"images": [{"url": cover}]}, "", 1),
            ({}, "", 2),
        ]:
            responses = [
                Mock(status_code=200, text=page({**entity, **embedded})),
                Mock(status_code=200, text='<meta property="og:image" content="' + cover + '">'),
            ]
            with (
                patch.object(spotify.network, "get", side_effect=responses) as get,
                patch.object(spotify.SpotifyAPI, "_fetch_oembed_artwork", return_value=oembed),
            ):
                tracks, meta = spotify.SpotifyAPI().get_playlist_tracks(SPOTIFY)
            self.assertEqual(tracks, [])
            self.assertEqual(meta["image_url"], cover)
            self.assertEqual(get.call_count, wanted_calls)

    def test_public_page_failure_keeps_tracks(self):
        entity = {"name": "Playlist", "trackList": [{"name": "Song", "album": "Album", "id": "1"}]}
        with (
            patch.object(
                spotify.network,
                "get",
                side_effect=[
                    Mock(status_code=200, text=page(entity)),
                    spotify.network.ConnectionError(),
                ],
            ),
            patch.object(spotify.SpotifyAPI, "_fetch_oembed_artwork", return_value=""),
        ):
            tracks, meta = spotify.SpotifyAPI().get_playlist_tracks(SPOTIFY)
        self.assertEqual(tracks[0]["artist"], "Unknown Artist")
        self.assertEqual(meta["image_url"], "")

    def test_http_bad_json_empty_entity_and_connection_failures(self):
        for response in [
            Mock(status_code=403),
            Mock(
                status_code=200,
                text='<script id="__NEXT_DATA__" type="application/json">{broken}</script>',
            ),
            Mock(status_code=200, text=page({})),
            spotify.network.ConnectionError("offline"),
        ]:
            with patch.object(
                spotify.network,
                "get",
                **(
                    {"side_effect": response}
                    if isinstance(response, Exception)
                    else {"return_value": response}
                ),
            ):
                with self.assertRaises(Exception):
                    spotify.SpotifyAPI().get_playlist_tracks(SPOTIFY, fetch_artwork=False)
        with patch.object(spotify.network, "get") as get:
            with self.assertRaises(Exception):
                spotify.SpotifyAPI().get_playlist_tracks("invalid")
        get.assert_not_called()


class AppleArtwork(unittest.TestCase):
    def test_template_sizes_without_rewriting_fixed_images(self):
        a = apple.AppleMusicAPI
        template = "https://is1.mzstatic.com/image/thumb/Music/a/{w}x{h}{c}.{f}"
        wanted = f"https://is1.mzstatic.com/image/thumb/Music/a/{APPLE_ARTWORK_SIZE}x{APPLE_ARTWORK_SIZE}bb.jpg"
        for value in [template, [None, template], {"contentUrl": template}, {"src": template}]:
            self.assertEqual(a._normalize_artwork_url(value), wanted)
        fixed = "https://is1.mzstatic.com/image/thumb/Music/a/1200x630bb.jpg"
        self.assertEqual(a._normalize_artwork_url(fixed), fixed)
        for value in [
            None,
            5,
            {},
            [],
            "http://is1.mzstatic.com/a",
            "https://is1.mzstatic.com/image/thumb/gen/{w}x{h}bb.jpg",
        ]:
            self.assertEqual(a._normalize_artwork_url(value), "")

    def test_artwork_dimensions_handle_bad_metadata(self):
        a = apple.AppleMusicAPI
        for value, wanted in [
            ({"width": "300", "height": "300"}, (300, 300)),
            ({"maximumWidth": 600, "maximumHeight": 800}, (600, 800)),
            ({"width": "bad", "height": "bad"}, (None, None)),
            ("https://a/1200x630bb.jpg", (1200, 630)),
            (None, (None, None)),
        ]:
            self.assertEqual(a._artwork_dimensions_from_value(value), wanted)

    def test_square_playlist_cover_beats_social_preview(self):
        square = "https://is1.mzstatic.com/image/thumb/Music/a/600x600bb.jpg"
        wide = "https://is1.mzstatic.com/image/thumb/Music/a/1200x630wp.jpg"
        data = [
            {
                "schemaContent": {
                    "@type": "MusicPlaylist",
                    "name": "Playlist",
                    "description": "Description",
                    "image": wide,
                }
            },
            {
                "name": "Playlist",
                "kind": "playlist",
                "artwork": {"url": square, "width": 600, "height": 600},
            },
        ]
        meta = apple.AppleMusicAPI._extract_metadata(
            data, BeautifulSoup('<meta property="og:image" content="' + wide + '">', "html.parser")
        )
        self.assertEqual(meta["name"], "Playlist")
        self.assertEqual(meta["image_url"], square)

    def test_metadata_html_fallbacks_and_no_artwork_default(self):
        for html, title in [
            ('<meta name="apple:title" content="Name">', "Name"),
            ('<meta property="og:title" content="Other">', "Other"),
            ("", "Apple Music Playlist"),
        ]:
            soup = BeautifulSoup(
                html + '<meta property="og:description" content="Description">', "html.parser"
            )
            for method in [
                apple.AppleMusicAPI._extract_metadata,
                apple.AppleMusicAPI._extract_metadata_without_artwork,
            ]:
                meta = method({}, soup)
                self.assertEqual(meta["name"], title)
                self.assertEqual(meta["description"], "Description")

    def test_artwork_fetch_and_invalid_public_pages(self):
        data = [{"schemaContent": {"@type": "MusicPlaylist", "name": "Empty", "trackCount": 0}}]
        html = '<script id="serialized-server-data">' + json.dumps(data) + "</script>"
        with patch.object(
            apple.network, "get", return_value=Mock(status_code=200, content=html.encode())
        ):
            tracks, meta = apple.AppleMusicAPI().get_playlist_tracks(APPLE)
        self.assertEqual(tracks, [])
        self.assertTrue(meta["complete"])
        for content in [
            b"no script",
            b'<script id="serialized-server-data"></script>',
            b'<script id="serialized-server-data">bad json</script>',
            b'<script id="serialized-server-data">{}</script>',
        ]:
            with patch.object(
                apple.network, "get", return_value=Mock(status_code=200, content=content)
            ):
                with self.assertRaises(Exception):
                    apple.AppleMusicAPI().get_playlist_tracks(APPLE)
