import unittest
from harmonia.matching import Matcher
class CreditDuration(unittest.TestCase):
    def setUp(self):
        self.source={'title':'Yeah! (feat. Lil Jon & Ludacris)','artist':'USHER, Lil Jon, Ludacris','album':'Confessions','duration':250200}
        self.plex={'plex_id':'one','title':'Yeah!','artist':'Usher','album':'Confessions','duration':250000}
    def test_featured_comma_credit_matches(self):
        self.assertEqual(Matcher._match_catalog_track(self.source,[self.plex],{}),'one')
        self.assertIn('duration',Matcher.score_candidate(self.source,self.plex)['artist_credit_reason'])
    def test_structured_lead_without_album(self):
        source={**self.source,'title':'Yeah!','album':'','artists':['Usher','Lil Jon','Ludacris']}
        self.assertEqual(Matcher._match_catalog_track(source,[self.plex],{}),'one')
    def test_wrong_artist_not_rescued(self):
        self.assertIsNone(Matcher._match_catalog_track(self.source,[{**self.plex,'artist':'Other'}],{}))
    def test_missing_or_far_duration_not_rescued(self):
        for duration in [None,0,float('nan'),300000]:
            with self.subTest(duration=duration):
                self.assertIsNone(Matcher._match_catalog_track({**self.source,'duration':duration},[self.plex],{}))
    def test_wrong_title_not_rescued(self):
        self.assertIsNone(Matcher._match_catalog_track(self.source,[{**self.plex,'title':'Other song'}],{}))
    def test_live_version_not_rescued(self):
        self.assertIsNone(Matcher._match_catalog_track(self.source,[{**self.plex,'title':'Yeah! (live)','album':'Live'}],{}))
    def test_unstructured_band_name_not_split(self):
        self.assertNotIn('Lead artist',Matcher.score_candidate({**self.source,'title':'Yeah!','artist':'Earth, Wind & Fire'},self.plex)['artist_credit_reason'])
