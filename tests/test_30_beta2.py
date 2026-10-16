from harmonia.process_lock import ProcessLock
from harmonia.matching import Matcher
from harmonia.config import Config
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch
from test_30 import MultiUser
from harmonia import accounts, api, tasks, source_history
from harmonia.musicbrainz_settings import search_scores,Preferences
from harmonia.lidarr import Lookup
from harmonia.jobs import Store,Manager

class Beta2(MultiUser):
    def test_admin_can_view_other_users_jobs(self):
        with accounts.as_user(self.a):
            jid=api.job_store().enqueue('sync',{'scope':'all'})
        status,data=asyncio.run(self.http('/api/jobs',user=self.admin))
        self.assertEqual(status,200);self.assertIn(jid,[j['id'] for j in json.loads(data)])
        status,_=asyncio.run(self.http('/api/jobs/'+jid+'/live',user=self.admin));self.assertEqual(status,200)
        status,_=asyncio.run(self.http('/api/jobs/'+jid,user=self.b));self.assertEqual(status,404)

    def test_disabled_playlist_permission(self):
        self.a['can_playlists']=False;accounts.put('accounts',self.a['id'],self.a)
        status,_=asyncio.run(self.http('/api/jobs',method='POST',user=self.a,body={'action':'sync'}));self.assertEqual(status,403)

    def test_preferences_and_blocklist_are_personal(self):
        endpoint=lambda p,m:next(r.endpoint for r in api.app.routes if getattr(r,'path','')==p and m in getattr(r,'methods',set()))
        from harmonia.user_preferences import blocked
        from harmonia.lidarr import state_put
        with accounts.as_user(self.a):
            state_put(accounts.personal_repository(),'blocked_artists','test',{'name':'Radiohead'})
            self.assertTrue(blocked({'artist':'Radiohead'}))
            self.assertTrue(blocked({'artist':'Various Artists','artists':['Radiohead','Weezer']}))
            self.assertFalse(blocked({'artist':'Various Artists','artists':['Weezer','Blur']}))
            state_put(accounts.personal_repository(),'blocked_albums','album',{'artist':'Weezer','album':'Blue Album','mbid':'11111111-1111-1111-1111-111111111111'})
            self.assertTrue(blocked({'artist':'Weezer','title':'Different display title','musicbrainz_id':'11111111-1111-1111-1111-111111111111'}))
            self.assertTrue(blocked({'artist':'Weezer','title':'Blue Album'}))
        with accounts.as_user(self.b):self.assertFalse(blocked({'artist':'Radiohead'}))

    def test_source_history_occurrences_and_retries(self):
        t={'title':'Song','artist':'Artist','album':'Album'}
        source_history.record(self.repo,'test',[t]);self.assertFalse(self.repo.load('source_audit'))
        source_history.record(self.repo,'test',[t,t]);source_history.record(self.repo,'test',[t,t])
        rows=list(self.repo.load('source_audit').values());self.assertEqual(len(rows),1);self.assertEqual(rows[0]['added'][0]['count'],1)
        source_history.record(self.repo,'test',[]);self.assertEqual(len(self.repo.load('source_audit')),1)

    def test_interval_start_time(self):
        from harmonia.jobs import next_run
        from datetime import datetime,timezone
        cron=tasks.expression(3,'02:30')
        self.assertEqual(tasks.hours_for(cron),3)
        self.assertIn('02:30',next_run(cron,'UTC',datetime(2026,9,25,1,tzinfo=timezone.utc)))

    def test_artist_collaboration_and_versions(self):
        source={'title':'The Middle','artist':'Zedd, Maren Morris & Grey','artists':[{'name':'Zedd'},{'name':'Maren Morris'},{'name':'Grey'}],'album':'The Middle - Single'}
        candidate={'title':'The Middle','artist':'Zedd','album':'The Middle','plex_id':'1'}
        with patch('harmonia.track_bridge.context',return_value=None):
            self.assertEqual(Matcher.match_track(source,[candidate]),'1')
            self.assertIsNone(Matcher.match_track(source,[{**candidate,'album':'Live in London'}]))
            self.assertIsNone(Matcher.match_track(source,[{**candidate,'artist':'Jimmy Eat World'}]))
        band={'title':'Song','artist':'Earth, Wind & Fire','album':'Album'}
        # Existing artist variants should not be weakened globally.
        self.assertLess(Matcher._artist_score(band['artist'],'Earth'),85)

    def test_query_score_keeps_editions(self):
        rows=[{'title':'BRAT (Live)','artist':'Charli xcx'},{'title':'BRAT','artist':'Charli XCX'}]
        found=search_scores(rows,Lookup(artist='Charli xcx',album='BRAT'))
        self.assertTrue(found[0]['query_exact']);self.assertFalse(found[1]['query_exact'])

    def test_custom_musicbrainz_url_validation(self):
        self.assertEqual(Preferences(server_url='http://musicbrainz.local/').server_url,'http://musicbrainz.local')
        with self.assertRaises(ValueError):Preferences(server_url='file:///etc/passwd')

    def test_pending_imports_do_not_run(self):
        accounts.put('accounts','15',{'id':'15','name':'Pending','admin':False,'pending_login':True})
        self.assertNotIn('15',[u['id'] for u,s in accounts.member_stores()])

    def test_account_process_locks_are_independent(self):
        with accounts.as_user(self.a):
            with ProcessLock():
                with accounts.as_user(self.b):
                    with ProcessLock():pass

    def test_same_destination_is_locked_across_accounts(self):
        from harmonia.job_locks import destinations
        for user in (self.a,self.b):
            with accounts.as_user(user):
                c=Config();c.config['playlists']=[{'source':'spotify','source_id':user['id'],'plex_playlist_id':'same'}];c.save()
        with accounts.as_user(self.a):
            with destinations(api.job_store(),{}):
                with accounts.as_user(self.b):
                    with self.assertRaises(RuntimeError):
                        with destinations(api.job_store(),{}):pass

    def test_sync_applies_only_its_playlist_drafts(self):
        from harmonia.match_queue import stage_changes
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[{'source':'spotify','source_id':'one'},{'source':'spotify','source_id':'two'}];c.save()
            repo=api.job_store().repository
            stage_changes(repo,[{'playlist_key':'spotify:'+key,'search_key':'Song|Artist','source':{},'plex_id':'1'} for key in ('one','two')])
            with patch('harmonia.match_queue.execute') as apply,patch.object(api,'_execute_job',return_value={'ok':True}):
                api.execute_job('sync',{'scope':'selected','playlist_keys':['spotify:one']})
            self.assertEqual([r['playlist_key'] for r in apply.call_args.args[0]['changes']],['spotify:one'])
            self.assertTrue(apply.call_args.args[0]['save_only'])

    def test_available_requires_both_and_blocks_cached_results(self):
        from harmonia.discover import availability
        from harmonia.lidarr import state_put
        now=time.time()
        plex={'checked_at':now,'rows':[{'artist':'Artist','album':'Album','plex_id':'1','plex_album_id':'album-pid'}]}
        lidarr={'checked_at':now,'rows':[{'artist':'Artist','title':'Album','album_id':'id','statistics':{'trackCount':1}}]}
        with accounts.as_user(self.a),patch('harmonia.inventory.current',return_value=(plex,lidarr)):
            self.assertEqual(availability([{'artist':'Artist','album':'Album'}])[0]['availability'],'Available')
            state_put(accounts.personal_repository(),'blocked_artists','x',{'name':'Artist'})
            self.assertEqual(availability([{'artist':'Artist','album':'Album'}]),[])
        with accounts.as_user(self.b),patch('harmonia.inventory.current',return_value=(plex,{'checked_at':now,'rows':[]})):
            self.assertEqual(availability([{'artist':'Artist','album':'Album'}])[0]['availability'],'Needs Attention')

    def test_new_inventory_does_not_queue_retry(self):
        from harmonia.inventory import scan
        with accounts.as_user(self.a):
            repo=api.job_store().repository;repo.save({'missing':{'spotify:test':[{'title':'Song','artist':'Artist'}]}})
            client=type('Client',(),{'base_url':'http://plex','music_library_key':'1','headers':{},'machine_identifier':'server','_fetch_library':lambda self:[{'plex_id':'1'}]})()
            with patch('harmonia.api._health_plex',return_value=client), \
                 patch('harmonia.artwork_sync.queue_pending'), \
                 patch('harmonia.accounts.member_stores',return_value=[]):
                for _ in range(2):scan('plex')
            self.assertEqual(len([j for j in api.job_store().list() if j['action']=='retry_missing']),0)

    def test_submission_receipt_is_private_and_deduplicated(self):
        sid='11aa22bb-1234-4321-aaaa-123456789abc'
        body={'action':'sync','submission_id':sid}
        status,data=asyncio.run(self.http('/api/jobs',method='POST',user=self.a,body=body))
        self.assertEqual(status,202);jid=json.loads(data)['id']
        _,data=asyncio.run(self.http('/api/jobs',method='POST',user=self.a,body=body));self.assertEqual(json.loads(data)['id'],jid)
        status,data=asyncio.run(self.http('/api/jobs/submission/'+sid,user=self.a));self.assertEqual(status,200);self.assertEqual(json.loads(data)['id'],jid)
        status,_=asyncio.run(self.http('/api/jobs/submission/'+sid,user=self.b));self.assertEqual(status,404)

    def test_description_refresh_preserves_success_timestamp(self):
        from harmonia.description_refresh import execute
        from harmonia.playlist_description import MARKER
        from unittest.mock import MagicMock
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[{'source':'spotify','source_id':'one','plex_playlist_id':'123','last_synced':'2026-09-25T10:00:00+00:00'}];c.save()
            description='Source text'+MARKER+'Last successful sync: Sep 25, 2026 at 5:00 AM CDT\nTracks: 25 source · 25 matched'
            response=MagicMock();response.json.return_value={'MediaContainer':{'Metadata':[{'summary':description}]}}
            with patch.object(api,'_health_plex',return_value=MagicMock()),patch('harmonia.description_refresh.network.get',return_value=response),patch('harmonia.description_refresh.network.put') as write:
                execute({'playlist_keys':['spotify:one']})
            summary=write.call_args.kwargs['params']['summary'];self.assertIn('Next scheduled sync:',summary);self.assertIn('Last successful sync: Sep 25, 2026 at 5:00 AM CDT',summary);self.assertIn('25 matched',summary)
            self.assertEqual(Config().config['playlists'][0]['last_synced'],'2026-09-25T10:00:00+00:00')

    def test_worker_runs_two_accounts_but_serializes_exclusive_jobs(self):
        from unittest.mock import MagicMock
        manager=Manager(self.repo)
        a_store=Store(accounts.personal_repository())
        with accounts.as_user(self.a):a_store=api.job_store()
        with accounts.as_user(self.b):b_store=api.job_store()
        a_store.enqueue('sync',{'scope':'all'});b_store.enqueue('sync',{'scope':'all'})
        started=[];barrier=threading.Barrier(2);both=threading.Event();release=threading.Event();exclusive=threading.Event()
        def run(job,store,user):
            if job['action']=='backup':exclusive.set();store.update(job['id'],status='completed');return
            started.append(user['id']);barrier.wait(timeout=5);both.set();release.wait(timeout=5);store.update(job['id'],status='completed')
        from harmonia import playlist_schedules
        with patch.object(Store,'due'),patch.object(playlist_schedules,'due'),patch.object(accounts,'member_stores',return_value=[(self.a,a_store),(self.b,b_store)]),patch.object(manager,'execute',side_effect=run):
            manager.thread=threading.Thread(target=manager.loop);manager.thread.start()
            try:
                self.assertTrue(both.wait(timeout=5));self.assertEqual(set(started),{self.a['id'],self.b['id']})
                manager.store.enqueue('backup',{});self.assertFalse(exclusive.wait(timeout=1.2))
                release.set();self.assertTrue(exclusive.wait(timeout=5))
            finally:release.set();manager.close()

    def test_library_cache_keeps_separate_accounts_warm(self):
        from harmonia.library_cache import reuse,load,invalidate
        from types import SimpleNamespace
        from unittest.mock import Mock
        invalidate();a=SimpleNamespace(base_url='http://plex',music_library_key='1',headers={'X-Plex-Token':'a'});b=SimpleNamespace(base_url='http://plex',music_library_key='1',headers={'X-Plex-Token':'b'})
        first=Mock(return_value=[{'plex_id':'a'}]);second=Mock(return_value=[{'plex_id':'b'}])
        with patch('harmonia.inventory.snapshot',return_value={}):
            with reuse(refresh=True):self.assertEqual(load(a,first)[0]['plex_id'],'a')
            with reuse(refresh=True):self.assertEqual(load(b,second)[0]['plex_id'],'b')
            with reuse(refresh=True):self.assertEqual(load(a,first)[0]['plex_id'],'a')
        self.assertEqual(first.call_count,2);self.assertEqual(second.call_count,1)
        invalidate()
