from harmonia import network as runtime_requests
from harmonia.apple_music_source import AppleMusicAPI
import unittest
from unittest.mock import Mock
from harmonia.playlist_urls import canonical_playlist, apple_playlist_get
from harmonia import api


class PlaylistURLSecurity(unittest.TestCase):
    def test_untrusted_origins_are_rejected(self):
        for url in (
            "http://127.0.0.1:9999/playlist/music.apple.com/pl.audit",
            "https://music.apple.com.evil.example/us/playlist/x/pl.audit",
            "https://evil.example/us/playlist/music.apple.com/pl.audit",
            "https://music.apple.com@127.0.0.1/us/playlist/x/pl.audit",
            "https://user@music.apple.com/us/playlist/x/pl.audit",
            "http://music.apple.com/us/playlist/x/pl.audit",
            "https://music.apple.com:9999/us/playlist/x/pl.audit",
            "https://music.apple.com/album/x/pl.audit",
            "https://evil.example/playlist/spotify.com",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    canonical_playlist(url)
                with self.assertRaises(Exception):
                    api._source_for_url(url)

    def test_valid_urls_have_fixed_origin_and_no_query(self):
        for host in ("music.apple.com", "itunes.apple.com"):
            self.assertEqual(
                canonical_playlist(f"https://{host}/gb/playlist/replay/pl.rp-123?x=1"),
                ("applemusic", "https://music.apple.com/gb/playlist/playlist/pl.rp-123"),
            )
        key = "7eahWLng9go8LDR5gcW6A3"
        self.assertEqual(
            canonical_playlist("spotify:playlist:" + key),
            ("spotify", "https://open.spotify.com/playlist/" + key),
        )
        self.assertEqual(
            canonical_playlist("pl.audit", "applemusic")[1],
            "https://music.apple.com/us/playlist/playlist/pl.audit",
        )

    def test_saved_unsafe_apple_url_never_reaches_network(self):
        from unittest.mock import patch

        with patch.object(runtime_requests, "get") as get:
            with self.assertRaises(ValueError):
                AppleMusicAPI().get_playlist_tracks(
                    "http://127.0.0.1/playlist/music.apple.com/pl.audit"
                )
            get.assert_not_called()

    def test_apple_fetch_uses_canonical_destination(self):
        from unittest.mock import patch

        with patch.object(runtime_requests, "get", return_value=Mock(status_code=404)) as get:
            with self.assertRaises(Exception):
                AppleMusicAPI().get_playlist_tracks(
                    "https://itunes.apple.com/gb/playlist/name/pl.audit?redirect=http://127.0.0.1"
                )
            self.assertEqual(
                get.call_args.args[0], "https://music.apple.com/gb/playlist/playlist/pl.audit"
            )
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_pasted_link_remains_supported(self):
        source, url, _ = api._source_for_url(
            "[My playlist](https://music.apple.com/us/playlist/example/pl.audit?x=1)"
        )
        self.assertEqual(source, "applemusic")
        self.assertEqual(url, "https://music.apple.com/us/playlist/playlist/pl.audit")

    def test_external_redirect_is_not_followed(self):
        for target in (
            "https://127.0.0.1/private",
            "http://music.apple.com/private",
            "https://music.apple.com.evil.example/private",
            "https://user@music.apple.com/private",
        ):
            client = Mock()
            client.get.return_value = Mock(status_code=302, headers={"Location": target})
            with self.assertRaises(ValueError):
                apple_playlist_get(client, "https://music.apple.com/us/playlist/x/pl.audit")
            self.assertEqual(client.get.call_count, 1)
            self.assertFalse(client.get.call_args.kwargs["allow_redirects"])

    def test_official_relative_redirect_and_loop_limit(self):
        client = Mock()
        client.get.side_effect = [
            Mock(status_code=302, headers={"Location": "/gb/playlist/x/pl.audit"}),
            Mock(status_code=200),
        ]
        self.assertEqual(
            apple_playlist_get(
                client, "https://music.apple.com/us/playlist/x/pl.audit"
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get.call_args.args[0], "https://music.apple.com/gb/playlist/x/pl.audit"
        )
        client = Mock()
        client.get.return_value = Mock(status_code=302, headers={"Location": "/loop"})
        with self.assertRaises(ValueError):
            apple_playlist_get(client, "https://music.apple.com/start")
        self.assertEqual(client.get.call_count, 6)


if __name__ == "__main__":
    unittest.main()
