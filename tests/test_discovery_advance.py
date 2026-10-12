import unittest
from harmonia.lidarr import _discovery_title

class AdvanceDiscoveryTests(unittest.TestCase):
    def test_supported_release_qualifiers(self):
        for title in ['Album (Advance)', 'Album [ADVANCE]', 'Album - Advance', 'Album (Advance Copy)', 'Album (Advance Promo)', 'Album (Advance) (Deluxe Edition)']:
            with self.subTest(title=title):
                self.assertEqual(_discovery_title(title), 'Album')

    def test_legitimate_titles_preserved(self):
        for title in ['Advance and Follow', 'Album (Advanced Studies)', 'Album (Advance and Follow)', 'Album - Advance and Follow']:
            with self.subTest(title=title):
                self.assertEqual(_discovery_title(title), title)
