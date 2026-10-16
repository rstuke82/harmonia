import unittest
from harmonia.matching import Matcher
from harmonia.track_bridge import match, _context


class GuestOmission(unittest.TestCase):
    def pair(self, title, artist, album):
        import re

        source = {"title": title, "artist": artist, "album": album, "duration": 240000}
        candidate = {
            **source,
            "plex_id": "correct",
            "title": re.sub(r"\s*[([]feat\. .*?[)\]]", "", title),
            "duration": 240500,
        }
        return source, candidate

    def test_same_album_missing_credits_recovers_affected_examples(self):
        examples = [
            ("Heavy Water (feat. Common Kings)", "Dirty Heads", "Midnight Control"),
            ("Uptown Funk (feat. Bruno Mars)", "Mark Ronson", "Uptown Special"),
            ("Showdown (feat. Collie Buddz)", "Stick Figure", "Wisdom"),
            ("Havana (feat. Young Thug)", "Camila Cabello", "Camila"),
            ("Smooth (feat. Rob Thomas)", "Santana", "Supernatural"),
        ]
        for values in examples:
            source, candidate = self.pair(*values)
            with self.subTest(title=source["title"]):
                self.assertEqual(Matcher._match_catalog_track(source, [candidate], {}), "correct")
                self.assertEqual(
                    Matcher.score_candidate(source, candidate)["artist_credit_reason"],
                    "Guest credit omitted; album and duration agree",
                )

    def test_album_edition_and_single_suffix_equivalence(self):
        source, candidate = self.pair("Smooth (feat. Rob Thomas)", "Santana", "Supernatural")
        source["album"] = "Supernatural (Remastered) [Bonus Track Version]"
        self.assertFalse(Matcher._recording_version_mismatch(source, candidate))
        source, candidate = self.pair("Song (feat. Guest)", "Lead", "Album - EP")
        candidate["album"] = "Album"
        self.assertFalse(Matcher._recording_version_mismatch(source, candidate))

    def test_missing_album_or_duration_and_far_duration_do_not_rescue(self):
        source, candidate = self.pair("Song (feat. Guest)", "Lead", "Album")
        for edits in (
            {"album": ""},
            {"duration": None},
            {"duration": float("nan")},
            {"duration": 260000},
        ):
            with self.subTest(edits=edits):
                self.assertIsNone(
                    Matcher._match_catalog_track({**source, **edits}, [candidate], {})
                )

    def test_multiple_unexplained_recordings_require_review_including_cached_selection(self):
        source, candidate = self.pair("Song (feat. Guest)", "Lead", "Album")
        other = {**candidate, "plex_id": "other", "duration": 240800}
        key = source["title"] + "|" + source["artist"]
        self.assertIsNone(
            Matcher._match_catalog_track(source, [candidate, other], {key: "correct"})
        )
        token = _context.set(None)
        try:
            self.assertIsNone(match(Matcher, source, [candidate, other], {key: "correct"}))
        finally:
            _context.reset(token)

    def test_explicit_wrong_guest_is_not_rescued_by_album_duration_or_shared_id(self):
        source, candidate = self.pair("Song (feat. Guest)", "Lead", "Album")
        candidate["title"] = "Song (feat. Different Guest)"
        self.assertIsNone(Matcher._match_catalog_track(source, [candidate], {}))
        self.assertTrue(
            Matcher._recording_version_mismatch(
                {**source, "recording_id": "same"}, {**candidate, "recording_id": "same"}
            )
        )

    def test_same_recording_id_supports_missing_album_and_duration(self):
        source, candidate = self.pair("Song (feat. Guest)", "Lead", "Album")
        self.assertFalse(
            Matcher._recording_version_mismatch(
                {**source, "album": "", "duration": None, "recording_id": "same"},
                {**candidate, "recording_id": "same"},
            )
        )

    def test_normal_sync_can_recover_match_cleared_by_build81(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from harmonia.sync_engine import Syncer

        source, candidate = self.pair(
            "Uptown Funk (feat. Bruno Mars)", "Mark Ronson", "Uptown Special"
        )
        engine = Syncer(SimpleNamespace(mapping={"playlist": {}}, read_only=True))
        engine._get_plex = Mock()
        engine._is_track_ignored = Mock(return_value=False)
        token = _context.set(None)
        try:
            result = engine._match_source_tracks.__wrapped__(
                engine, [source], "playlist", [candidate], record_provenance=False
            )
        finally:
            _context.reset(token)
        self.assertEqual(result[0], ["correct"])
        self.assertEqual(result[1], [])
