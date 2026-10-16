from library_fixtures import library_targets
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from harmonia.discover import recent_album_rows,_history_match_evidence,_history_candidates,cached_lastfm,cached_history_tracks,_lastfm_cache_key,refresh_lastfm
from harmonia.matching import Matcher
from harmonia import musicbrainz_client as mb
from harmonia import network
from harmonia import updates
from harmonia.apple_music_source import AppleMusicAPI
from harmonia.lidarr_requests import find_album,repair_links,server_id
from harmonia.storage import Repository

class Beta16Tests(unittest.TestCase):
    def test_build_number_version_is_comparable(self):
        self.assertGreater(updates.version('20261004.10'),updates.version('20261004.9'))
        self.assertIsNone(updates.version('invalid'))
    def test_stored_old_update_version_does_not_break_health(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite')
            repo.save({'updates':{'latest':{'channel':updates.CHANNEL,'latest_version':'3.0.0-beta.16'}}})
            with patch.object(updates,'__version__','20261011.9'):
                self.assertFalse(updates.stored_status(repo)['available'])
    def test_beta7_source_release_recognizes_matching_docker_build(self):
        with patch.object(updates,'__version__','3.0.0-beta7'),patch.object(updates,'__build__','20261011.9'):
            self.assertFalse(updates.available_version('20261011.9'))
            self.assertTrue(updates.available_version('20261011.10'))
            self.assertFalse(updates.available_version('v3.0.0-beta.6'))
            self.assertTrue(updates.available_version('v3.0.0-beta8'))

    def test_spotify_get_retries_incomplete_chunked_response(self):
        complete=Mock(status_code=200,text='ok')
        failure=network.exceptions.ChunkedEncodingError('incomplete')
        with patch.object(network._requests,'request',side_effect=[failure,complete]) as request,patch.object(network.time,'sleep'):
            result=network.get('https://open.spotify.com/embed/playlist/test')
        self.assertIs(result,complete);self.assertEqual(request.call_count,2)
    def test_generic_apple_artwork_template_is_rejected(self):
        self.assertEqual(AppleMusicAPI._normalize_artwork_url('https://is1-ssl.mzstatic.com/image/thumb/gen/{w}x{h}bb.jpg'),'')
    def test_compilation_scrobbles_collapse_across_track_artists(self):
        tracks=[{'name':'One','artist':{'name':'Artist A'},'album':{'#text':'Soundtrack'},'image':[{'#text':'https://img/cover.jpg'}],'date':{'uts':'10'}},{'name':'Two','artist':{'name':'Artist B'},'album':{'#text':'Soundtrack'},'image':[{'#text':'https://img/cover.jpg'}],'date':{'uts':'20'}}]
        rows=recent_album_rows(tracks)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['artist'],'Various Artists');self.assertEqual(rows[0]['tracks'],['One','Two']);self.assertEqual(len(rows[0]['history_key']),64)
    def test_album_name_masquerading_as_artist_does_not_win_credit(self):
        tracks=[
            {'name':'One','artist':{'name':'Hybrid Theory'},'album':{'#text':'Hybrid Theory'},'image':[{'#text':'https://img/cover.jpg'}]},
            {'name':'Two','artist':{'name':'Hybrid Theory'},'album':{'#text':'Hybrid Theory'},'image':[{'#text':'https://img/cover.jpg'}]},
            {'name':'Three','artist':{'name':'Linkin Park'},'album':{'#text':'Hybrid Theory'},'image':[{'#text':'https://img/cover.jpg'}]},
        ]
        row=recent_album_rows(tracks)[0]
        self.assertEqual(row['artist'],'Linkin Park')
        self.assertEqual(row['artists'],['Linkin Park'])
    def test_history_match_accepts_release_suffix_and_group_credit(self):
        row={'album':'Ocean Eyes - Single','artist':'Nick Hexum','artists':['Nick Hexum'],
             'tracks':['Ocean Eyes','Side B']}
        evidence=_history_match_evidence(row,'Ocean Eyes','Nick Hexum Quintet',
            [{'title':'Ocean Eyes'},{'title':'Side B'}])
        self.assertTrue(evidence['accepted'])
        self.assertEqual(evidence['album_score'],100)
        self.assertGreaterEqual(evidence['artist_score'],90)
    def test_history_match_accepts_any_collaboration_credit(self):
        row={'album':'Collision Course','artist':'Jay-Z','artists':['Jay-Z','Linkin Park'],
             'tracks':['Numb / Encore','Points of Authority / 99 Problems']}
        evidence=_history_match_evidence(row,'Collision Course','Jay-Z & Linkin Park',
            [{'title':'Numb / Encore'},{'title':'Points of Authority / 99 Problems'}])
        self.assertTrue(evidence['accepted'])
    def test_history_match_rejects_unrelated_common_album(self):
        row={'album':'Greatest Hits','artist':'Artist One','artists':['Artist One'],'tracks':['Unique Song']}
        evidence=_history_match_evidence(row,'Greatest Hits','Artist Two',[{'title':'Other Song'}])
        self.assertFalse(evidence['accepted'])
    def test_history_candidates_merge_lidarr_with_its_plex_copy(self):
        title='Burning London: The Clash Tribute'
        inventory={
            'lidarr':{'rows':[{'album_id':'lidarr-album','id':7,'title':title,'artist':'Various Artists','statistics':{'trackFileCount':12,'trackCount':12}}],
                      'tracks':[{'album_id':'lidarr-album','title':'White Man in Hammersmith Palais'}]},
            'plex':{'rows':[{'plex_album_id':'plex-album','plex_id':'plex-track','album':title,'album_artist':'Various Artists','artist':'311','title':'White Man in Hammersmith Palais','plex_album_thumb':'/library/metadata/1/thumb'}]}}
        row={'album':title,'artist':'311','artists':['311'],'tracks':['White Man in Hammersmith Palais']}
        candidates=_history_candidates(library_targets(inventory),row)
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['target_type'],'lidarr')
        self.assertEqual(candidates[0]['target_id'],'lidarr-album')
        self.assertTrue(candidates[0]['plex_present'])
        self.assertEqual(candidates[0]['plex_target_id'],'plex-album')
        self.assertEqual(candidates[0]['state'],'In Library')
        self.assertEqual(candidates[0]['artwork'],'/api/library/plex-albums/plex-album/image')
    def test_history_candidates_keep_a_plex_only_album(self):
        inventory={'lidarr':{'rows':[],'tracks':[]},'plex':{'rows':[{'plex_album_id':'plex-only','album':'Independent Album','album_artist':'Artist','title':'Song','plex_album_thumb':'/library/metadata/2/thumb'}]}}
        candidates=_history_candidates(library_targets(inventory),{'album':'Independent Album','artist':'Artist','artists':['Artist'],'tracks':['Song']})
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['target_type'],'plex')
        self.assertEqual(candidates[0]['artwork'],'/api/library/plex-albums/plex-only/image')
    def test_history_candidates_do_not_merge_an_ambiguous_lidarr_identity(self):
        albums=[{'album_id':key,'id':index,'title':'Shared Title','artist':'Artist','statistics':{}} for index,key in enumerate(('one','two'),1)]
        inventory={'lidarr':{'rows':albums,'tracks':[]},'plex':{'rows':[{'plex_album_id':'plex','album':'Shared Title','album_artist':'Artist','title':'Song'}]}}
        candidates=_history_candidates(library_targets(inventory),{'album':'Shared Title','artist':'Artist','artists':['Artist'],'tracks':['Song']})
        self.assertEqual(sum(1 for candidate in candidates if candidate['target_type']=='lidarr'),2)
        self.assertEqual(sum(1 for candidate in candidates if candidate['target_type']=='plex'),1)
    def test_recent_scrobbles_tolerate_string_metadata_fields(self):
        tracks=[{'name':'Song','artist':'Artist','album':'Album','@attr':'','date':'','image':['invalid']}]
        rows=recent_album_rows(tracks)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['album'],'Album');self.assertEqual(rows[0]['last_played'],0)
    def test_discover_cache_returns_expired_data_without_network(self):
        params={'user':'listener','period':'overall','limit':50}
        key=_lastfm_cache_key('user.getTopAlbums',params)
        repo=Mock();repo.load_one.return_value={'expires':0,'data':{'topalbums':{'album':[{'name':'Saved'}]}}}
        with patch('harmonia.discover.root_repository',return_value=repo),patch('harmonia.discover.network.get') as get:
            result=cached_lastfm('user.getTopAlbums',**params)
        self.assertEqual(result['topalbums']['album'][0]['name'],'Saved');get.assert_not_called()
    def test_missing_cached_history_does_not_contact_lastfm(self):
        with patch('harmonia.discover.root_repository',return_value=type('Repo',(),{'load':lambda self,n:{}})()),patch('harmonia.discover.cached_lastfm',return_value=None) as cached,patch('harmonia.discover.lastfm') as live,patch('harmonia.discover.lastfm_settings',return_value={'history_days':180}),patch('harmonia.discover.time.time',return_value=1_800_000_000):
            rows,truncated,complete=cached_history_tracks('listener')
        self.assertEqual(rows,[]);self.assertFalse(truncated);self.assertFalse(complete);self.assertEqual(cached.call_count,1);live.assert_not_called()
    def test_lastfm_refresh_populates_recommendation_cache(self):
        def response(method,**params):
            if method=='user.getRecentTracks':return {'recenttracks':{'track':[],'@attr':{'totalPages':'1'}}}
            if method=='user.getTopArtists':return {'topartists':{'artist':[]}}
            if method=='chart.getTopArtists':return {'artists':{'artist':[{'name':'Artist One'},{'name':'Artist Two'}]}}
            if method=='artist.getTopAlbums':return {'topalbums':{'album':[]}}
            raise AssertionError(method)
        with patch('harmonia.user_preferences.preferences',return_value={'lastfm_username':'listener'}),patch('harmonia.jobs.progress'),patch('harmonia.discover.lastfm',side_effect=response) as lookup:
            result=refresh_lastfm()
        methods=[call.args[0] for call in lookup.call_args_list]
        self.assertEqual(methods.count('chart.getTopArtists'),1);self.assertEqual(methods.count('artist.getTopAlbums'),2);self.assertIn('personalized album recommendations cached',result['summary'])
    def test_duration_only_affects_score_when_both_values_exist(self):
        source={'title':'Song','artist':'Artist','album':'Album','duration':200000}
        close=Matcher.score_candidate(source,{'title':'Song','artist':'Artist','album':'Album','duration':201000})
        far=Matcher.score_candidate(source,{'title':'Song','artist':'Artist','album':'Album','duration':240000})
        missing=Matcher.score_candidate(source,{'title':'Song','artist':'Artist','album':'Album'})
        self.assertEqual(close['duration_penalty'],0);self.assertGreater(far['duration_penalty'],0);self.assertEqual(missing['duration_penalty'],0);self.assertGreater(close['adjusted_score'],far['adjusted_score'])
    def test_confirmed_indexed_mirror_receives_search(self):
        response=Mock();response.raise_for_status.return_value=None;response.json.return_value={'artists':[{'id':'found'}]}
        with patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':'https://mirror.example','capabilities':['search_artist']}),patch.object(mb.network,'get',return_value=response) as get:
            result=mb.get(Mock(),'artist',{'query':'test'})
        self.assertTrue(get.call_args.args[0].startswith('https://mirror.example/'))
        self.assertEqual(result['_harmonia_route'],{'source':'mirror','host':'mirror.example','reason':''})
    def test_unconfirmed_mirror_search_stays_public(self):
        response=Mock();response.raise_for_status.return_value=None;response.json.return_value={'artists':[]}
        with patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':'https://mirror.example','capabilities':['lookup']}),patch.object(mb.network,'get',return_value=response) as get,patch.object(mb.time,'sleep'):
            result=mb.get(Mock(),'artist',{'query':'test'})
        self.assertTrue(get.call_args.args[0].startswith(mb.PUBLIC_BASE))
        self.assertEqual(result['_harmonia_route'],{'source':'public','host':'musicbrainz.org','reason':''})
    def test_mirror_failure_records_public_fallback(self):
        response=Mock();response.raise_for_status.return_value=None;response.json.return_value={'release-groups':[]}
        with patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':'https://mirror.example','capabilities':['lookup']}),patch.object(mb.network,'get',side_effect=[__import__('requests').Timeout(),response]),patch.object(mb.time,'sleep'):
            result=mb.get(Mock(),'release-group/abc')
        self.assertEqual(result['_harmonia_route'],{'source':'public_fallback','host':'musicbrainz.org','reason':'mirror_failed'})
    def test_empty_indexed_mirror_search_falls_back_to_public(self):
        empty=Mock();empty.raise_for_status.return_value=None;empty.json.return_value={'recordings':[]}
        public=Mock();public.raise_for_status.return_value=None;public.json.return_value={'recordings':[{'id':'found'}]}
        with patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':'https://mirror.example','capabilities':['search_recording']}),patch.object(mb.network,'get',side_effect=[empty,public]) as get,patch.object(mb.time,'sleep'):
            result=mb.get(Mock(),'recording',{'query':'recording:"Song"'})
        self.assertEqual(get.call_count,2);self.assertTrue(get.call_args_list[1].args[0].startswith(mb.PUBLIC_BASE));self.assertEqual(result['_harmonia_route'],{'source':'public_fallback','host':'musicbrainz.org','reason':'mirror_empty'})
    def test_request_link_accepts_same_saved_lidarr_album_with_changed_mbid(self):
        albums=[{'id':42,'foreignAlbumId':'lidarr-release-group','title':'The Best of Bow Wow Wow','artist':{'artistName':'Bow Wow Wow'}}]
        record={'lidarr_id':42,'title':'The Best of Bow Wow Wow','artist':'Bow Wow Wow'}
        self.assertEqual(find_album(albums,'requested-release-group',record)['id'],42)
    def test_request_link_title_fallback_must_be_unambiguous(self):
        albums=[{'id':1,'foreignAlbumId':'one','title':'Album','artist':{'artistName':'Artist'}},{'id':2,'foreignAlbumId':'two','title':'Album','artist':{'artistName':'Artist'}}]
        self.assertIsNone(find_album(albums,'missing',{'title':'Album','artist':'Artist'}))
    def test_request_link_accepts_inventory_album_id(self):
        album={'id':42,'album_id':'current-release-group','title':'The Best of Bow Wow Wow','artistName':'Bow Wow Wow','artwork':'cover.jpg'}
        found=find_album([album],'current-release-group',{'title':'The Best of Bow Wow Wow','artist':'Bow Wow Wow'})
        self.assertEqual(found['artwork'],'cover.jpg')
    def test_request_link_accepts_inventory_artist_string(self):
        album={'id':42,'album_id':'current-release-group','title':'Brothers in Arms','artist':'Dire Straits'}
        self.assertEqual(find_album([album],'missing',{'lidarr_id':42,'title':'Brothers in Arms','artist':'Dire Straits'})['id'],42)
    def test_link_repair_uses_saved_lidarr_album_when_release_group_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite');cfg={'url':'http://lidarr','enabled':True,'api_key':'secret'}
            repo.save({'lidarr_requests':{'requested-release-group':{'server':server_id(cfg),'lidarr_id':42,'title':'The Best of Bow Wow Wow','artist':'Bow Wow Wow','status':'not_in_lidarr','removed_from_lidarr':True,'download_status':'Not found in Lidarr'}}})
            client=Mock();client.call.return_value=[{'id':42,'foreignAlbumId':'lidarr-release-group','title':'The Best of Bow Wow Wow','artist':{'artistName':'Bow Wow Wow'}}]
            with patch('harmonia.lidarr.Client',return_value=client):result=repair_links(repo,cfg,'requested-release-group')
            saved=repo.load('lidarr_requests')['requested-release-group']
            self.assertEqual(result['missing'],0);self.assertEqual(saved['lidarr_id'],42);self.assertFalse(saved['removed_from_lidarr']);self.assertEqual(saved['lidarr_album_mbid'],'lidarr-release-group');self.assertNotEqual(saved['download_status'],'Not found in Lidarr')

class HarmoniaVersionTests(unittest.TestCase):
    def test_four_part_builds_compare_numerically(self):
        self.assertEqual(updates.version('v1.0.0.74'), (1,0,0,74))
        with patch.object(updates, '__version__', '1.0.0.74'):
            self.assertFalse(updates.available_version('1.0.0.74'))
            self.assertFalse(updates.available_version('1.0.0.73'))
            self.assertTrue(updates.available_version('1.0.0.75'))
            self.assertTrue(updates.available_version('1.0.1.76'))
    def test_notification_channels(self):
        self.assertEqual(updates.channel('stable'),'stable')
        self.assertEqual(updates.channel('beta'),'beta')
        self.assertEqual(updates.channel('invalid'),'stable')
