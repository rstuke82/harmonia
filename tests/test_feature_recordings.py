import unittest
from harmonia.matching import Matcher
from harmonia.track_bridge import match, _context


class FeaturedRecordings(unittest.TestCase):
    def setUp(self):
        self.standard = {
            "plex_id": "27",
            "title": "Records",
            "artist": "Weezer",
            "album": "SZNZ: Summer",
            "duration": 200000,
        }
        self.feature = {
            "plex_id": "51",
            "title": "Records (feat. Noga Erez)",
            "artist": "Weezer",
            "album": "Records (feat. Noga Erez) - Single",
            "duration": 200000,
        }

    def test_distinct_versions_resolve_to_distinct_tracks_even_with_equal_duration(self):
        for track in (self.standard, self.feature):
            self.assertEqual(
                Matcher._match_catalog_track(track, [self.standard, self.feature], {}),
                track["plex_id"],
            )

    def test_missing_feature_never_selects_standard_and_reverse_is_also_rejected(self):
        self.assertIsNone(Matcher._match_catalog_track(self.feature, [self.standard], {}))
        self.assertIsNone(Matcher._match_catalog_track(self.standard, [self.feature], {}))

    def test_guest_in_artist_or_structured_credits_is_supported(self):
        for extra in ({"artist": "Weezer feat. Noga Erez"}, {"artists": ["Weezer", "Noga Erez"]}):
            candidate = {**self.standard, **extra, "plex_id": "guest"}
            self.assertEqual(Matcher._match_catalog_track(self.feature, [candidate], {}), "guest")

    def test_different_guest_and_shared_recording_identity(self):
        other = {**self.feature, "title": "Records (feat. Someone Else)", "album": "Records"}
        self.assertIsNone(Matcher._match_catalog_track(self.feature, [other], {}))
        self.assertFalse(
            Matcher._recording_version_mismatch(
                {**self.feature, "recording_id": "shared"},
                {**self.standard, "recording_id": "shared"},
            )
        )

    def test_saved_automatic_mapping_cannot_bypass_guest_guard(self):
        key = self.feature["title"] + "|" + self.feature["artist"]
        self.assertIsNone(Matcher._match_catalog_track(self.feature, [self.standard], {key: "27"}))
        token = _context.set(None)
        try:
            self.assertIsNone(match(Matcher, self.feature, [self.standard], {key: "27"}))
        finally:
            _context.reset(token)

    def test_lidarr_bridge_does_not_select_standard_feature_mismatch(self):
        track = {**self.standard, "id": "lidarr-standard"}
        token = _context.set(
            {
                "pending": False,
                "tracks": [track],
                "links": {"lidarr-standard": {"plex_id": "27"}},
                "resolved": {},
            }
        )
        try:
            self.assertIsNone(match(Matcher, self.feature, [self.standard], {}))
        finally:
            _context.reset(token)

    def test_sync_rechecks_automatic_and_legacy_but_preserves_explicit_manual_choices(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from harmonia.sync_engine import Syncer

        key = self.feature["title"] + "|" + self.feature["artist"]
        for provenance in ("automatic", "legacy", "manual"):
            engine = Syncer(SimpleNamespace(mapping={"playlist": {key: "27"}}, read_only=True))
            engine._get_plex = Mock()
            engine._is_track_ignored = Mock(return_value=False)
            engine._get_match_provenance = Mock(return_value=provenance)
            engine._previous_match_snapshot = Mock(return_value=self.standard)
            token = _context.set(None)
            try:
                result = engine._match_source_tracks.__wrapped__(
                    engine, [self.feature], "playlist", [self.standard], record_provenance=False
                )
            finally:
                _context.reset(token)
            self.assertEqual(result[0], ["27"] if provenance == "manual" else [])
            self.assertEqual(len(result[1]), 0 if provenance == "manual" else 1)
