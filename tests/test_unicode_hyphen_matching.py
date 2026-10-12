import unittest
from harmonia.discover import _history_match_evidence,_canonical_album
from harmonia.matching import Matcher

class UnicodeHyphens(unittest.TestCase):
    def evidence(self,album,artist='Three Days Grace',names=None):
        names=names or ['Pain','Riot','Never Too Late','Animal I Have Become']
        return _history_match_evidence({'album':'One-X','artist':'Three Days Grace','tracks':names},album,artist,[{'title':name} for name in names])
    def test_one_x_equivalent_hyphens_are_exact_with_four_exact_tracks(self):
        for char in '-‐‑‒–—―−':
            with self.subTest(character=char):
                value=self.evidence('One'+char+'X')
                self.assertEqual(value['score'],100);self.assertEqual(value['album_score'],100)
                self.assertEqual(value['matched_tracks'],4)
    def test_unicode_hyphen_in_artist_and_track_is_equivalent(self):
        self.assertEqual(Matcher._normalize_match_text('JAY‐Z'),Matcher._normalize_match_text('JAY-Z'))
        self.assertEqual(Matcher._normalize_match_text('X‑Static'),Matcher._normalize_match_text('X-Static'))
    def test_edition_normalization_still_works(self):
        self.assertEqual(_canonical_album('One‑X (Deluxe Edition)'),_canonical_album('One-X'))
    def test_distinct_titles_remain_distinct(self):
        self.assertNotEqual(_canonical_album('One-X'),_canonical_album('One X'))
        self.assertLess(self.evidence('One-Y')['album_score'],100)
    def test_matching_title_does_not_erase_artist_disagreement(self):
        value=self.evidence('One‑X','An unrelated artist')
        self.assertLess(value['artist_score'],82);self.assertLess(value['score'],100)
