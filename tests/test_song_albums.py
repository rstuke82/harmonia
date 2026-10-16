import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from harmonia.song_albums import containing_albums


class SongAlbums(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(title="Talk", artist="Khalid")
        self.credit = [{"name": "Khalid", "artist": {"id": "artist", "name": "Khalid"}}]
        self.record = {
            "id": "recording",
            "title": "Talk",
            "artist-credit": self.credit,
            "releases": [],
        }

    def release(self, title="Free Spirit", kind="Album", secondary=None, credits=None):
        return {
            "artist-credit": self.credit if credits is None else credits,
            "release-group": {
                "id": title,
                "title": title,
                "primary-type": kind,
                "secondary-types": secondary or [],
            },
        }

    def search(self, releases, prefs=None):
        get = Mock(return_value={"releases": releases})
        return containing_albums(
            None, [self.record], self.request, prefs or {"prefer_studio": True}, get
        )[0]["releases"]

    def test_album_found_beyond_embedded_releases(self):
        self.assertEqual(self.search([self.release()])[0]["release-group"]["title"], "Free Spirit")

    def test_keeps_other_releases_separate_and_own_single_visible(self):
        rows = self.search(
            [
                self.release(secondary=["Compilation"]),
                self.release(kind="Single"),
                self.release(credits=[{"name": "Various Artists"}]),
            ]
        )
        self.assertEqual([r["release-group"]["other-release"] for r in rows], [True, False, True])

    def test_studio_preference(self):
        release = self.release(secondary=["Live"])
        self.assertTrue(self.search([release])[0]["release-group"]["other-release"])
        self.assertEqual(len(self.search([release], {"prefer_studio": False})), 1)

    def test_retains_release_artist_credit(self):
        self.assertEqual(
            self.search([self.release()])[0]["release-group"]["artist-credit"], self.credit
        )

    def test_pagination(self):
        get = Mock(
            side_effect=[
                {"releases": [self.release(secondary=["Compilation"])] * 100, "release-count": 101},
                {"releases": [self.release()], "release-count": 101},
            ]
        )
        result = containing_albums(None, [self.record], self.request, {}, get)
        self.assertEqual(len(result[0]["releases"]), 101)
        self.assertEqual(get.call_args.args[2]["offset"], 100)

    def test_other_song_not_browsed(self):
        get = Mock()
        self.record["title"] = "Talk remix"
        self.assertEqual(containing_albums(None, [self.record], self.request, {}, get), [])
        get.assert_not_called()

    def test_featured_search_identity(self):
        from harmonia.song_albums import search_identity

        self.assertEqual(
            search_identity("Pitbull,\u00a0John Ryan", "Fireball (feat. John Ryan)"),
            ("Pitbull", "Fireball"),
        )

    def test_comma_band_name_without_explicit_feature_preserved(self):
        from harmonia.song_albums import search_identity

        self.assertEqual(
            search_identity("Earth, Wind & Fire", "September"), ("Earth, Wind & Fire", "September")
        )

    def test_more_than_five_recordings_are_browsed(self):
        records = [{**self.record, "id": str(i)} for i in range(6)]
        get = Mock(return_value={"releases": []})
        self.assertEqual(len(containing_albums(None, records, self.request, {}, get)), 6)

    def test_musicbrainz_query_uses_parsed_identity(self):
        import tempfile
        from pathlib import Path
        from harmonia.storage import Repository
        from harmonia import lidarr
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory))
            with (
                patch("harmonia.musicbrainz_client.get", return_value={"recordings": []}) as get,
                patch("harmonia.api._record_log"),
            ):
                lidarr.musicbrainz_search(
                    repo,
                    {},
                    lidarr.Lookup(
                        artist="Pitbull, John Ryan",
                        title="Fireball (feat. John Ryan)",
                        fields_search=True,
                    ),
                )
            self.assertEqual(
                get.call_args.args[2]["query"], 'recording:"Fireball" AND artist:"Pitbull"'
            )
