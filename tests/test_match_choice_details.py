import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from playlist_bridge.storage import Repository
from playlist_bridge import api,inventory
from playlist_bridge.config import Config
from playlist_bridge.plex_client import PlexAPI
from playlist_bridge.sync_engine import Syncer
from playlist_bridge.apple_music_source import AppleMusicAPI
from playlist_bridge.matching import Matcher
from playlist_bridge.match_candidates import consolidate

class MatchChoiceDetails(unittest.TestCase):
    def test_deduplicate_same_id_or_recording_but_keep_versions_and_unknowns(self):
        base={'plex_id':'1','title':'Please Please Please','artist':'Sabrina Carpenter','album':'Short n Sweet','duration':186000,'track_number':1,'_album_id':'album','_guids':['plex://track/one']}
        same={**base,'plex_id':'2'}
        dolly={**base,'plex_id':'3','artist':'Sabrina Carpenter & Dolly Parton','duration':190000,'track_number':14,'_guids':['plex://track/dolly']}
        unknown={**base,'plex_id':'4','_guids':[]}
        rows=consolidate([base,dict(base),same,dolly,unknown])
        self.assertEqual([r['plex_id'] for r in rows],['1','3','4'])
        self.assertEqual(rows[0]['other_entries'][0]['plex_id'],'2')
        self.assertFalse(any(k.startswith('_') for r in rows for k in r))
    def test_different_album_position_or_duration_never_collapsed(self):
        base={'plex_id':'1','title':'Song','artist':'Band','album':'Record','_album_id':'a','duration':200000,'_files':['/same'],'_guids':['plex://track/one']}
        for difference in [{'track_number':2},{'duration':210000},{'_album_id':'b'},{'edition':'Live'}]:
            self.assertEqual(len(consolidate([base,{**base,'plex_id':'2',**difference}])),2)
    def test_plex_and_source_snapshots_keep_duration_and_track_credit(self):
        rows=PlexAPI._parse_library_tracks({'MediaContainer':{'Metadata':[{'ratingKey':'1','title':'Song','duration':210000,'index':14,'parentIndex':1,'originalTitle':'Sabrina Carpenter & Dolly Parton','grandparentTitle':'Sabrina Carpenter'}]}})
        self.assertEqual(rows[0]['duration'],210000);self.assertEqual(rows[0]['track_number'],14)
        self.assertIn('Dolly Parton',rows[0]['track_artist'])
        self.assertEqual(Syncer._snapshot_entries([{'title':'Song','artist':'Band','duration':210000}])[0]['duration'],210000)
        track=AppleMusicAPI._track_from_item({'name':'Song','artistName':'Band','duration':210000})
        self.assertEqual(track['duration'],210000)
    def test_source_duration_formats(self):
        from playlist_bridge.track_duration import source_duration
        self.assertEqual(source_duration({'duration':{'totalMilliseconds':210000}}),210000)
        self.assertEqual(source_duration({'duration':'3:30'}),210)
        self.assertIsNone(source_duration({'duration':{'unexpected':1}}))
        self.assertIsNone(source_duration({'duration':'nan'}))
    def test_picker_loads_global_aliases_and_uses_saved_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory));repo.save({'runtime':{'plex':{'url':'http://plex','token':'fixture','music_library_key':'1'}},'artist_aliases':{'Canonical Band':['Different Alias']}})
            with patch('playlist_bridge.accounts.personal_repository',return_value=repo),patch('playlist_bridge.accounts.root_repository',return_value=repo),patch('playlist_bridge.accounts.actor',return_value=None),patch('playlist_bridge.api.job_store',return_value=type('Store',(),{'repository':repo})()),patch('playlist_bridge.api._record_log'),patch.object(PlexAPI,'_fetch_library',side_effect=AssertionError('Picker must use saved inventory')),patch('playlist_bridge.network.get',side_effect=AssertionError('Picker must not contact Plex')):
                config=Config(read_only=True,namespaces=['artist_aliases']);client=type('Client',(),{'base_url':'http://plex','music_library_key':'1','headers':{'X-Plex-Token':'fixture','Accept':'application/json'}})()
                inventory.publish(repo,'plex',inventory.identity(client),[{'plex_id':'1','title':'Song','artist':'Canonical Band','album':'Record','duration':200000}],time.monotonic())
                Matcher.set_artist_aliases({})
                request=api.MissingCandidateRequest(title='Song',artist='Different Alias',album='Record',duration=200000)
                candidates=api.missing_candidates(request)
                self.assertEqual(candidates[0]['artist_score'],100)
                Matcher.set_artist_aliases({})
                self.assertEqual(api.retry_automatic(request)['candidate']['plex_id'],'1')
        Matcher.set_artist_aliases({})
