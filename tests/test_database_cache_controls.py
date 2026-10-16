import json
import tempfile
import unittest
from pathlib import Path

from harmonia.database_maintenance import clear_cache, status
from harmonia.discover import recent_album_rows
from harmonia.fanart import artist_images
from harmonia.storage import Repository


class Beta15Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name))

    def test_database_health_and_cache_counts(self):
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO state VALUES('musicbrainz_cache','artist:one',?)",
                (json.dumps({"name": "One"}),),
            )
            db.execute(
                "INSERT INTO state VALUES('artwork_overrides','artist:1:hero',?)",
                (json.dumps({"url": "manual"}),),
            )
        report = status(self.repo, run_checks=True)
        self.assertEqual(report["quick_check"], "ok")
        self.assertEqual(report["foreign_key_issues"], 0)
        self.assertEqual(report["cache_groups"]["musicbrainz"]["entries"], 1)
        result = clear_cache(self.repo, "musicbrainz")
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.repo.load("artwork_overrides")["artist:1:hero"]["url"], "manual")

    def test_artist_hero_prefers_banner_then_background(self):
        data = {
            "musicbanner": [{"url": "https://assets.fanart.tv/banner.jpg"}],
            "artistbackground": [{"url": "https://assets.fanart.tv/background.jpg"}],
        }
        rows = artist_images(data, "hero")
        self.assertEqual(rows[0]["type"], "musicbanner")
        self.assertEqual(rows[0]["url"], "https://assets.fanart.tv/banner.jpg")

    def test_recent_lastfm_tracks_group_into_missing_album_candidates(self):
        tracks = [
            {
                "name": "One",
                "artist": {"name": "Artist"},
                "album": {"#text": "Album"},
                "date": {"uts": "100"},
            },
            {
                "name": "Two",
                "artist": {"name": "artist"},
                "album": {"#text": "album"},
                "date": {"uts": "200"},
            },
            {
                "name": "Live",
                "artist": {"name": "Artist"},
                "album": {"#text": "Album"},
                "@attr": {"nowplaying": "true"},
            },
            {"name": "No album", "artist": {"name": "Artist"}, "album": {"#text": ""}},
        ]
        rows = recent_album_rows(tracks)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tracks"], ["One", "Two"])
        self.assertEqual(rows[0]["plays"], 2)
        self.assertEqual(rows[0]["last_played"], 200)

    def test_featured_guest_does_not_relabel_album_various_artists(self):
        tracks = [
            {"name": "One", "artist": {"name": "Taylor Swift"}, "album": {"#text": "Showgirl"}},
            {"name": "Two", "artist": {"name": "Taylor Swift"}, "album": {"#text": "Showgirl"}},
            {"name": "Three", "artist": {"name": "Taylor Swift"}, "album": {"#text": "Showgirl"}},
            {
                "name": "Guest",
                "artist": {"name": "Taylor Swift & Guest"},
                "album": {"#text": "Showgirl"},
            },
        ]
        row = recent_album_rows(tracks)[0]
        self.assertEqual(row["artist"], "Taylor Swift")
        self.assertEqual(len(row["artists"]), 2)


if __name__ == "__main__":
    unittest.main()
