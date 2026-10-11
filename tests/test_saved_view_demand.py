"""F23: bounded preparation demand, upgrade cleanup and foreground scheduling."""
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from playlist_bridge import api, saved_views, jobs, accounts
from playlist_bridge.storage import Repository
from playlist_bridge.lidarr import state_put


class SavedViewDemand(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name));self.store=jobs.Store(self.repo)
        for name in ('root_repository','personal_repository'):
            p=patch.object(saved_views,name,return_value=self.repo);p.start();self.addCleanup(p.stop)
        p=patch.object(saved_views,'actor',return_value=None);p.start();self.addCleanup(p.stop)
        p=patch.object(saved_views,'users',return_value={});p.start();self.addCleanup(p.stop)

    def provider(self,name='F23 album',deps=None):
        old_provider=saved_views._providers.get(name);old_reader=saved_views._readers.get(name)
        self.addCleanup(lambda:saved_views._providers.pop(name,None) if old_provider is None else saved_views._providers.__setitem__(name,old_provider))
        self.addCleanup(lambda:saved_views._readers.pop(name,None) if old_reader is None else saved_views._readers.__setitem__(name,old_reader))
        calls=[]
        @saved_views.saved(name,deps if deps is not None else {'inventory'})
        def read(album_id='fixture',refresh=False):
            calls.append(album_id)
            return {'album_id':album_id,'inventory':self.repo.load('inventory')}
        return read,calls

    def finish(self,response):
        ident=json.loads(response.body)['job_id'];job=self.store.get(ident)
        with patch('playlist_bridge.jobs.current',return_value=SimpleNamespace(store=self.store,id=ident,action="prepare_views",user_id=None)),patch('playlist_bridge.jobs.progress'):
            saved_views.execute(job['payload'])
        self.store.update(ident,status='completed')
        return ident

    def set_payload(self,ident,payload):
        with self.repo.connect() as db:db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),ident))

    def tick(self):
        with patch.object(accounts,'member_stores',return_value=[]),patch.object(saved_views,'_last_tick',0),patch.object(saved_views,'_last_download',time.monotonic()):
            saved_views.tick(self.store)

    def test_poll_bookkeeping_does_not_invalidate_history_but_requests_still_change(self):
        actual_deps=saved_views._providers['listening history v20261008_8'][1]
        history=saved_views.revision(self.repo,actual_deps)
        discovery=saved_views.revision(self.repo,saved_views.DISCOVER)
        requests=saved_views.revision(self.repo,saved_views.REQUESTS)
        for poll in range(3):state_put(self.repo,'lidarr_requests','one',{'download_status':'Waiting','download_checked_at':poll})
        self.assertEqual(history,saved_views.revision(self.repo,actual_deps))
        self.assertEqual(discovery,saved_views.revision(self.repo,saved_views.DISCOVER))
        self.assertNotEqual(requests,saved_views.revision(self.repo,saved_views.REQUESTS))
        state_put(self.repo,'lastfm_history_sync','user',{'batches':['new']})
        self.assertNotEqual(history,saved_views.revision(self.repo,actual_deps))

    def test_tick_does_not_rebuild_historical_pages_or_walk_their_revisions(self):
        read,calls=self.provider()
        for i in range(25):self.finish(read(str(i)))
        state_put(self.repo,'inventory','plex',{'checked_at':time.time(),'rows':[]})
        before=len(self.store.list())
        with patch.object(saved_views,'revision',side_effect=AssertionError('Tick must not inspect historical page dependencies')):
            self.tick()
        self.assertEqual(len(self.store.list()),before)
        self.assertEqual(len(calls),25)
        response=read('new');self.assertEqual(response.status_code,202)
        self.assertEqual(self.store.get(json.loads(response.body)['job_id'])['payload']['view_priority'],0)

    def test_stale_saved_data_remains_available_and_queued_revisions_coalesce(self):
        read,calls=self.provider();self.finish(read())
        state_put(self.repo,'inventory','plex',{'value':1})
        first=read();self.assertEqual(first.status_code,200)
        key=first.headers['x-saved-view'];ident=saved_views.load_row(self.repo,key)['job_id']
        self.assertEqual(self.store.get(ident)['payload']['view_priority'],1)
        state_put(self.repo,'inventory','plex',{'value':2})
        second=read()
        self.assertEqual(saved_views.load_row(self.repo,key)['job_id'],ident)
        self.assertEqual(len([j for j in self.store.list() if j['status']=='queued']),1)
        self.assertEqual(self.store.get(ident)['payload']['revision'],saved_views.revision(self.repo,saved_views._providers['F23 album'][1]))
        self.assertEqual(len(calls),1,'HTTP reads must not run providers')
        with patch('playlist_bridge.jobs.current',return_value=SimpleNamespace(store=self.store,id=ident,action="prepare_views",user_id=None)),patch('playlist_bridge.jobs.progress'):
            saved_views.execute(self.store.get(ident)['payload'])
        self.store.update(ident,status='completed')
        self.assertEqual(json.loads(read().body)['inventory']['plex']['value'],2)
        self.assertEqual(len(calls),2)

    def test_foreground_request_promotes_existing_startup_job_without_duplicate(self):
        read,_=self.provider();token=saved_views._warming.set(True)
        try:response=read()
        finally:saved_views._warming.reset(token)
        ident=json.loads(response.body)['job_id']
        self.assertEqual(self.store.get(ident)['payload']['view_priority'],2)
        self.assertEqual(json.loads(read().body)['job_id'],ident)
        self.assertEqual(self.store.get(ident)['payload']['view_priority'],0)
        self.assertEqual(len(self.store.list()),1)

    def test_dispatch_prioritizes_first_load_over_old_background_and_preserves_maintenance(self):
        background={'id':'old','action':'prepare_views','payload':{'view_priority':2}}
        foreground={'id':'new','action':'prepare_views','payload':{'view_priority':0}}
        stale={'id':'stale','action':'prepare_views','payload':{'view_priority':1}}
        maintenance={'id':'backup','action':'backup','payload':{}}
        rows=sorted([background,stale,foreground,maintenance],key=jobs.dispatch_priority)
        self.assertEqual([r['id'] for r in rows],['backup','new','stale','old'])

    def test_upgrade_retires_legacy_queue_but_keeps_running_and_promoted_jobs(self):
        read,_=self.provider();response=read();ident=json.loads(response.body)['job_id'];key=response.headers['x-saved-view']
        # Turn this fixture into a pre-fix queued job, then demand it again.
        payload=self.store.get(ident)['payload'];payload.pop('view_priority')
        self.set_payload(ident,payload)
        self.assertEqual(json.loads(read().body)['job_id'],ident)
        legacy=self.store.enqueue('prepare_views',{'key':'old-key','revision':'old'})
        running=self.store.enqueue('prepare_views',{'key':'running-key','revision':'old'});self.store.claim(running)
        self.assertEqual(saved_views.retire_legacy_queue(self.repo),[legacy])
        self.assertEqual(self.store.get(ident)['status'],'queued')
        self.assertEqual(self.store.get(running)['status'],'running')
        self.assertEqual(self.store.get(legacy)['status'],'cancelled')
        self.assertEqual(saved_views.retire_legacy_queue(self.repo),[])
        self.assertEqual(saved_views.load_row(self.repo,key)['job_id'],ident)

    def test_legacy_retirement_keeps_saved_data_and_allows_new_demand(self):
        read,_=self.provider();first=read();ident=json.loads(first.body)['job_id'];key=first.headers['x-saved-view']
        payload=self.store.get(ident)['payload'];payload.pop('view_priority');self.set_payload(ident,payload)
        with self.repo.connect() as db:db.execute('UPDATE prepared_views SET data=? WHERE key=?',(json.dumps({'saved':True}),key))
        saved_views.retire_legacy_queue(self.repo)
        row=saved_views.load_row(self.repo,key);self.assertIsNone(row['attempt_revision']);self.assertIsNone(row['job_id']);self.assertIsNotNone(row['data'])
        response=read();self.assertEqual(response.status_code,200);self.assertTrue(json.loads(response.body)['saved'])
        self.assertNotEqual(saved_views.load_row(self.repo,key)['job_id'],ident)

    def test_concurrent_readers_share_one_job(self):
        read,_=self.provider()
        with ThreadPoolExecutor(max_workers=4) as pool:responses=list(pool.map(lambda _:read(),range(8)))
        self.assertEqual(len({json.loads(r.body)['job_id'] for r in responses}),1)
        self.assertEqual(len(self.store.list()),1)

    def test_forced_refresh_promotes_stale_view_without_duplicate(self):
        read,_=self.provider();self.finish(read())
        state_put(self.repo,'inventory','plex',{'rows':[]})
        ordinary=read();key=ordinary.headers['x-saved-view'];ident=saved_views.load_row(self.repo,key)['job_id']
        forced=read(refresh=True)
        self.assertEqual(forced.status_code,200)
        self.assertEqual(saved_views.load_row(self.repo,key)['job_id'],ident)
        self.assertEqual(self.store.get(ident)['payload']['view_priority'],0)

    def test_legacy_cleanup_covers_member_stores_without_touching_other_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            member=jobs.Store(Repository(Path(folder)))
            legacy=member.enqueue('prepare_views',{'key':'member-old','revision':'old'})
            regular=member.enqueue('sync',{'scope':'all'})
            with patch.object(accounts,'member_stores',return_value=[({'id':'2'},member)]),patch.object(saved_views,'_last_tick',0),patch.object(saved_views,'_last_download',time.monotonic()):saved_views.tick(self.store)
            self.assertEqual(member.get(legacy)['status'],'cancelled')
            self.assertEqual(member.get(regular)['status'],'queued')
