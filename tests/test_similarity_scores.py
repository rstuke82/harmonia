"""Pinned score contracts preserve matching thresholds across the RapidFuzz migration."""

import json
import unittest
from pathlib import Path
from harmonia import fuzzy_scores


class SimilarityScores(unittest.TestCase):
    def test_recording_credits_unicode_punctuation_and_token_scores(self):
        rows = json.loads((Path(__file__).parent / "fixtures/similarity_scores.json").read_text())
        for left, right, ratio, sorted_score, set_score in rows:
            with self.subTest(left=left, right=right):
                self.assertEqual(fuzzy_scores.ratio(left, right), ratio)
                self.assertEqual(fuzzy_scores.token_sort_ratio(left, right), sorted_score)
                self.assertEqual(fuzzy_scores.token_set_ratio(left, right), set_score)
