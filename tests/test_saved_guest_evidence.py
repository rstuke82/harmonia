"""Saved metadata corroborates omitted guests without collapsing recordings."""

import unittest
from harmonia.matching import Matcher


class SavedGuestEvidence(unittest.TestCase):
    def pair(self):
        source = dict(
            title="Dear Future Self (Hands Up) [feat. Wyclef Jean]",
            artist="Fall Out Boy",
            album="Believers Never Die (Volume Two)",
            duration=171163,
        )
        candidate = dict(
            title="Dear Future Self (Hands Up)",
            artist="Fall Out Boy",
            album="Greatest Hits: Believers Never Die, Volume Two",
            duration=170063,
            plex_id="1296938",
        )
        return source, candidate

    def test_album_label_and_omitted_guest_match_without_ids(self):
        source, candidate = self.pair()
        details = Matcher.score_candidate(source, candidate)
        self.assertEqual(details["adjusted_score"], 100)
        self.assertEqual(details["album_penalty"], 0)
        self.assertEqual(details["album_provenance_penalty"], 0)
        self.assertEqual(Matcher._match_catalog_track(source, [candidate], {}), "1296938")

    def test_weezer_featured_single_stays_separate(self):
        source = dict(
            title="Records (feat. Noga Erez)",
            artist="Weezer",
            album="Records (feat. Noga Erez) - Single",
            duration=212000,
        )
        candidate = dict(
            title="Records",
            artist="Weezer",
            album="SZNZ: Summer",
            duration=208000,
            plex_id="original",
        )
        self.assertIsNone(Matcher._match_catalog_track(source, [candidate], {}))
        # Even equal lengths cannot replace a differently identified album.
        candidate["duration"] = source["duration"]
        self.assertIsNone(Matcher._match_catalog_track(source, [candidate], {}))

    def test_guests_ids_artist_title_and_volume_conflicts_stay_blocked(self):
        source, candidate = self.pair()
        for edits in (
            {"title": "Dear Future Self (Hands Up) [feat. Different Guest]"},
            {"artist": "Different Band"},
            {"title": "Dear Future Self (Feet Up)"},
            {"album": "Greatest Hits: Believers Never Die, Volume One"},
            {"album": "Greatest Hits"},
            {"duration": 168000},
            {"duration": None},
        ):
            with self.subTest(edits=edits):
                self.assertIsNone(
                    Matcher._match_catalog_track(source, [{**candidate, **edits}], {})
                )
        self.assertIsNone(
            Matcher._match_catalog_track(
                {**source, "recording_id": "featured"},
                [{**candidate, "recording_id": "original"}],
                {},
            )
        )

    def test_duration_boundary(self):
        source, candidate = self.pair()
        candidate["duration"] = source["duration"] - 2000
        self.assertEqual(Matcher._match_catalog_track(source, [candidate], {}), "1296938")
        candidate["duration"] -= 1
        self.assertIsNone(Matcher._match_catalog_track(source, [candidate], {}))

    def test_ambiguous_recordings_require_review_even_with_cache(self):
        source, candidate = self.pair()
        alternate = {**candidate, "plex_id": "other", "duration": 170200}
        key = source["title"] + "|" + source["artist"]
        self.assertIsNone(Matcher._match_catalog_track(source, [candidate, alternate], {}))
        self.assertIsNone(
            Matcher._match_catalog_track(source, [candidate, alternate], {key: "1296938"})
        )

    def test_other_compilation_still_has_penalty(self):
        source, candidate = self.pair()
        source["title"] = candidate["title"]
        candidate["album"] = "Greatest Hits: Other Collection"
        self.assertEqual(Matcher.score_candidate(source, candidate)["album_penalty"], 14)
