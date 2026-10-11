import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from playlist_bridge import accounts,api,discover,history_links
from playlist_bridge.storage import Repository

class HistoryRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name))
        for target in ('playlist_bridge.accounts.root_repository','playlist_bridge.discover.root_repository'):
            p=patch(target,return_value=self.repo);p.start();self.addCleanup(p.stop)
        self.source=history_links.HistorySource(history_key='a'*64,artist='Performer',album='Compilation (Deluxe)')
        self.album='12345678-1234-1234-1234-123456789abc'
        self.member={'id':'11','admin':False,'can_request':True,'auto_approve':True,'plex_token':'member'}
        self.repo.save({'lidarr':{'settings':{'enabled':True,'url':'http://lidarr','api_key':'key'}}})

    def gaps(self,files=0,present=True):
        row={'history_key':self.source.history_key,'artist':self.source.artist,'album':self.source.album,'plays':3,'tracks':['Song'],'artists':['Performer']}
        inventory={'lidarr':{'rows':[{'album_id':self.album,'statistics':{'trackFileCount':files}}] if present else []}}
        self.repo.save({'inventory':inventory})
        fn=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/discover/history-gaps').__wrapped__
        with patch('playlist_bridge.user_preferences.preferences',return_value={'lastfm_username':'user'}),patch.object(discover,'cached_history_tracks',return_value=([],False,True)),patch.object(discover,'recent_album_rows',return_value=[row]),patch.object(discover,'lastfm_settings',return_value={'history_min_plays':1,'history_sort':'plays','history_max_suggestions':25,'history_days':0}),patch.object(discover,'availability',side_effect=lambda rows:[r|{'lidarr_album_id':self.album,'statistics':{'trackFileCount':files},'availability':'Awaiting Plex' if files else 'Requested'} for r in rows]):
            return fn()['rows']

    def test_request_keeps_waiting_album_then_hides_imported_without_plex(self):
        history_links.requested(self.source,self.album,'job')
        self.assertEqual(len(self.gaps(0)),1)
        self.assertEqual(self.gaps(12),[])

    def test_existing_imported_match_disappears_without_manual_link(self):
        self.assertEqual(self.gaps(12),[])
        self.assertEqual(len(self.gaps(0)),1)

    def test_deleted_target_reappears_even_with_saved_request_association(self):
        history_links.requested(self.source,self.album,'failed-job')
        self.assertEqual(len(self.gaps(0,present=False)),1)

    def test_member_request_association_is_personal_and_preserves_history_identity(self):
        with accounts.as_user(self.member):
            history_links.requested(self.source,self.album,'job')
            member_repo=accounts.personal_repository()
        self.assertEqual(self.repo.load('lastfm_history_links'),{})
        saved=member_repo.load('lastfm_history_links')[self.source.history_key]
        self.assertEqual(saved['target_id'],self.album)
        self.assertEqual(saved['album'],'Compilation (Deluxe)')
        self.assertEqual(saved['origin'],'request')

    def test_picker_and_automatic_match_share_all_artist_credits_and_track_score(self):
        row={'album':'Album','artist':'Guest','artists':['Guest','Artist'],'tracks':['Song']}
        album={'album_id':self.album,'id':7,'title':'Album','artist':'Artist','statistics':{'trackFileCount':1}}
        tracks=[{'album_id':self.album,'title':'Song'}]
        inventory={'lidarr':{'rows':[album],'tracks':tracks},'plex':{'rows':[]}}
        candidate=discover._history_candidates(inventory,row)[0]
        evidence=discover._history_match_evidence(row,'Album','Artist',tracks)
        self.assertEqual(candidate['score'],evidence['score'])
        self.assertEqual(candidate['score'],100)
        self.assertTrue(candidate['accepted'])
        self.assertIsNotNone(discover._best_history_match(row,[{'title':'Album','artist':'Artist','tracks':tracks,'album':album}]))

    def test_same_title_unrelated_artist_does_not_confirm_without_track_evidence(self):
        row={'album':'Greatest Hits','artist':'Artist One','tracks':['One']}
        inventory={'lidarr':{'rows':[{'album_id':self.album,'title':'Greatest Hits','artist':'Artist Two','statistics':{'trackFileCount':20}}],'tracks':[{'album_id':self.album,'title':'Other'}]},'plex':{'rows':[]}}
        candidate=discover._history_candidates(inventory,row)[0]
        self.assertFalse(candidate['accepted'])
        self.assertLess(candidate['score'],100)

    def test_failed_request_does_not_save_link_pending_approval_preserves_source(self):
        route=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/requests-user/add')
        model=route.__annotations__['body']
        body=model(album_id=self.album,title='Chosen album',artist='Album artist',history_source=self.source)
        with accounts.as_user(self.member),patch('playlist_bridge.lidarr.prepare',side_effect=ValueError('preview failed')):
            with self.assertRaises(ValueError):route(body)
            self.assertEqual(accounts.personal_repository().load('lastfm_history_links'),{})
        with accounts.as_user(self.member|{'auto_approve':False}):
            result=route(body)
            self.assertFalse(result['queued'])
            self.assertEqual(accounts.personal_repository().load('lastfm_history_links'),{})
        pending=next(iter(self.repo.load('pending_requests').values()))
        self.assertEqual(pending['history_source']['history_key'],self.source.history_key)

    def test_successful_member_request_saves_source_after_queueing(self):
        route=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/requests-user/add')
        body=route.__annotations__['body'](album_id=self.album,title='Chosen title',artist='Album artist',history_source=self.source)
        add_route=next(r for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/add')
        with accounts.as_user(self.member),patch('playlist_bridge.lidarr.prepare',return_value={'preview_id':'12345678-1234-1234-1234-123456789abc'}),patch.object(add_route,'endpoint',return_value={'id':'queued-job'}):
            route(body)
            saved=accounts.personal_repository().load('lastfm_history_links')[self.source.history_key]
        self.assertEqual(saved['target_id'],self.album)
        self.assertEqual(saved['job_id'],'queued-job')
        self.assertEqual(saved['artist'],'Performer')

    def test_real_availability_resolves_exact_imported_match_without_plex(self):
        import time
        row={'album':'Album','artist':'Guest','artists':['Guest','Artist'],'tracks':['Song']}
        album={'album_id':self.album,'id':7,'title':'Album','artist':'Artist','statistics':{'trackFileCount':12,'trackCount':12}}
        plex={'rows':[],'checked_at':time.time()}
        lidarr={'rows':[album],'tracks':[{'id':'track','album_id':self.album,'title':'Song'}],'checked_at':time.time()}
        config=SimpleNamespace(repository=self.repo)
        with patch('playlist_bridge.config.Config',return_value=config),patch('playlist_bridge.inventory.current',return_value=(plex,lidarr)),patch('playlist_bridge.track_bridge.context',return_value=None),patch('playlist_bridge.user_preferences.preferences',return_value={}),patch('playlist_bridge.user_preferences.blocked',return_value=False):
            result=discover.availability([row])[0]
        self.assertEqual(result['lidarr_album_id'],self.album)
        self.assertFalse(result['plex_present'])
        self.assertTrue(discover.history_present(result))

    def test_admin_add_persists_history_source_after_real_queue_insert(self):
        import time
        import uuid
        from playlist_bridge import lidarr
        preview_id=str(uuid.uuid4());cfg=lidarr.config(self.repo)
        self.repo.save({'lidarr_previews':{preview_id:{'at':time.time(),'config_hash':lidarr.fingerprint(cfg),'album_id':self.album,'title':'Selected edition','artist':'Various Artists'}}})
        route=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/add')
        with accounts.as_user({'id':'1','admin':True}):
            job=route(lidarr.Confirm(preview_id=preview_id,history_source=self.source))
        self.assertEqual(job['status'],'queued')
        saved=self.repo.load('lastfm_history_links')[self.source.history_key]
        self.assertEqual(saved['target_id'],self.album)
        self.assertEqual(saved['job_id'],job['id'])

    def test_admin_search_existing_also_saves_history_identity(self):
        from playlist_bridge import lidarr
        from unittest.mock import Mock
        route=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/search-existing')
        client=Mock();client.call.return_value=[{'id':7,'foreignAlbumId':self.album,'title':'Selected edition','artist':{'artistName':'Album Artist'}}]
        with accounts.as_user({'id':'1','admin':True}),patch.object(lidarr,'Client',return_value=client):
            job=route(lidarr.ExistingSearch(album_id=self.album,history_source=self.source))
        self.assertEqual(job['action'],'lidarr_search')
        self.assertEqual(self.repo.load('lastfm_history_links')[self.source.history_key]['target_id'],self.album)
