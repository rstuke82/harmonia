import unittest
from harmonia.matching import Matcher
from harmonia.track_bridge import match, _context


class DurationRecording(unittest.TestCase):
    def evaluate(self, duration=338500, other=372533, first=336773, cached=False):
        source = {
            "title": "Wish You Were Here",
            "artist": "Pink Floyd",
            "album": "Wish You Were Here",
            "duration": duration,
        }
        tracks = [
            {**source, "id": "L1", "plex_id": "L1", "recording_id": "R1", "duration": first},
            {**source, "id": "L2", "plex_id": "L2", "recording_id": "R2", "duration": other},
        ]
        ctx = {
            "tracks": tracks,
            "links": {"L1": {"plex_id": "P1"}, "L2": {"plex_id": "P2"}},
            "pending": False,
        }
        if cached:
            import hashlib
            from harmonia.track_bridge import identity

            key = hashlib.sha256(repr(identity(source)).encode()).hexdigest()
            ctx["source_links"] = {key: {"lidarr_id": "L2", "identity": list(identity(tracks[1]))}}
        token = _context.set(ctx)
        try:
            return match(
                Matcher,
                source,
                [{**t, "plex_id": "P" + str(i + 1)} for i, t in enumerate(tracks)],
                {},
            )
        finally:
            _context.reset(token)

    def test_pink_floyd_standard(self):
        self.assertEqual(self.evaluate(), "P1")

    def test_alternate_recording(self):
        self.assertEqual(self.evaluate(duration=372500), "P2")

    def test_seconds(self):
        self.assertEqual(self.evaluate(duration=338.5), "P1")

    def test_near_tie(self):
        self.assertIsNone(self.evaluate(other=339000))

    def test_missing_source(self):
        self.assertIsNone(self.evaluate(duration=None))

    def test_missing_competitor(self):
        self.assertIsNone(self.evaluate(other=None))

    def test_far_from_all(self):
        self.assertIsNone(self.evaluate(duration=350000))

    def test_invalid_durations(self):
        for value in [True, 0, -1, "invalid", float("nan"), float("inf")]:
            with self.subTest(value=value):
                self.assertIsNone(self.evaluate(duration=value))

    def test_cached_wrong_recording_reconsidered(self):
        self.assertEqual(self.evaluate(cached=True), "P1")
