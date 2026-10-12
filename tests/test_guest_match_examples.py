import unittest
from harmonia.matching import Matcher

class ScreenshotGuestMatches(unittest.TestCase):
    def pair(self):
        return (dict(title='Separate Ways (Worlds Apart) [feat. Lzzy Hale]',artist='Daughtry',album='Separate Ways (Worlds Apart) [feat. Lzzy Hale] - Single',duration=316000),dict(title='Separate Ways (Worlds Apart)',artist='Daughtry',album='Separate Ways (Worlds Apart)',duration=316000,plex_id='correct'))

    def test_single_omitted_credit_accepted_without_network_or_ids(self):
        source,candidate=self.pair()
        self.assertEqual(Matcher.score_candidate(source,candidate)['adjusted_score'],100)
        self.assertEqual(Matcher._match_catalog_track(source,[candidate],{}),'correct')

    def test_fun_punctuation_does_not_trigger_recording_conflict(self):
        source=dict(title='We Are Young (feat. Janelle Monáe)',artist='Fun.',album='Some Nights',duration=251000)
        candidate=dict(title='We Are Young',artist='Fun',album='Some Nights',duration=251000,plex_id='correct')
        self.assertEqual(Matcher.score_candidate(source,candidate)['album_provenance_penalty'],0)
        self.assertEqual(Matcher._match_catalog_track(source,[candidate],{}),'correct')

    def test_single_evidence_does_not_override_conflicting_recordings(self):
        source,candidate=self.pair()
        for edits in ({'title':'Separate Ways (Worlds Apart) [feat. Other Guest]'}, {'duration':319000}, {'duration':None}, {'album':'Another album'}, {'artist':'Other Artist'}):
            with self.subTest(edits=edits):
                self.assertIsNone(Matcher._match_catalog_track(source,[{**candidate,**edits}],{}))
        self.assertIsNone(Matcher._match_catalog_track({**source,'recording_id':'one'},[{**candidate,'recording_id':'two'}],{}))
        self.assertIsNone(Matcher._match_catalog_track(source,[candidate,{**candidate,'plex_id':'ambiguous'}],{}))
