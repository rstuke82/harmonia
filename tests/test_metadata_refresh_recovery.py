import tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harmonia import jobs,file_tools,metadata_refresh as batch,saved_views
from harmonia.storage import Repository
from harmonia.lidarr import state_put

class MetadataRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name));self.store=jobs.Store(self.repo)
        self.artists=[{'id':str(i),'mbid':'mb'+str(i),'name':'Artist '+str(i)} for i in range(3)]
        self.ctx=SimpleNamespace(store=self.store,id=None,stop=threading.Event())
        for target,value in [('harmonia.accounts.root_repository',self.repo),('harmonia.saved_views.root_repository',self.repo),('harmonia.api._config',SimpleNamespace(repository=self.repo)),('harmonia.inventory.current',({}, {'artists':self.artists})),('harmonia.file_tools.settings',{'itunes_enrichment':True}),('harmonia.metadata_sources.settings',{}),('harmonia.lidarr.config',{'url':'https://lidarr.example','api_key':'test'}),('harmonia.jobs.current',self.ctx)]:
            p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)
        p=patch('harmonia.jobs.progress');p.start();self.addCleanup(p.stop)
    def start(self):
        self.ctx.id=self.store.enqueue('library_metadata_all',{});self.store.update(self.ctx.id,status='running')
    def test_restart_preserves_completed_and_retries_failed(self):
        self.start()
        def refresh(payload):
            if payload['artist_id']=='1':raise ValueError('secret must not be stored')
            if payload['artist_id']=='2':self.ctx.stop.set();raise jobs.Cancelled()
            return {}
        with patch.object(file_tools,'refresh_metadata',side_effect=refresh),patch('harmonia.jobs.output'):
            with self.assertRaises(jobs.Cancelled):file_tools.refresh_all_metadata()
        state=self.repo.load_one(batch.NAMESPACE,'run',{})
        self.assertEqual(set(state['done']),{'0'});self.assertEqual(state['status'],'interrupted')
        self.assertNotIn('secret',str(state));self.assertEqual(self.store.get(self.ctx.id)['result']['remaining'],2)
        self.store.update(self.ctx.id,status='cancelled');batch.recover(self.store);batch.recover(self.store)
        queued=[j for j in self.store.list() if j['status']=='queued'];self.assertEqual(len(queued),1)
        self.ctx.id=queued[0]['id'];self.ctx.stop.clear();self.store.update(self.ctx.id,status='running')
        with patch.object(file_tools,'refresh_metadata',return_value={}) as refresh:result=file_tools.refresh_all_metadata()
        self.assertEqual([call.args[0]['artist_id'] for call in refresh.call_args_list],['1','2'])
        self.assertEqual(result['resumed'],1);self.assertEqual(result['remaining'],0)
    def test_supplemental_failure_is_retried_and_completed_run_starts_fresh(self):
        self.start()
        with patch.object(file_tools,'refresh_metadata',side_effect=[{}, {'failed_providers':['Fanart.tv']},{}]):result=file_tools.refresh_all_metadata()
        self.assertEqual(result['remaining'],1)
        self.store.update(self.ctx.id,status='completed');self.start()
        with patch.object(file_tools,'refresh_metadata',return_value={}) as refresh:file_tools.refresh_all_metadata()
        self.assertEqual(refresh.call_count,1);self.assertEqual(refresh.call_args.args[0]['artist_id'],'1')
        self.store.update(self.ctx.id,status='completed');self.start()
        with patch.object(file_tools,'refresh_metadata',return_value={}) as refresh:file_tools.refresh_all_metadata()
        self.assertEqual(refresh.call_count,3)
    def test_explicit_cancel_not_automatically_resumed(self):
        self.start();self.store.update(self.ctx.id,cancel_requested=True,status='interrupted')
        batch.recover(self.store);self.assertFalse(any(j['status']=='queued' for j in self.store.list()))
    def test_only_soft_changes_are_batched_and_orphaned_runs_unfreeze(self):
        self.start();state_put(self.repo,'library_artist_metadata','a',{'name':'old'})
        state={'status':'running','job_id':self.ctx.id,'published':batch.generations(self.repo),'published_at':time.time()};batch.save(self.repo,state)
        stamp=saved_views.revision(self.repo,saved_views.LIBRARY)
        for i in range(10):state_put(self.repo,'library_artist_metadata','a',{'name':i})
        state_put(self.repo,'fanart_cache','new',{})
        self.assertEqual(stamp,saved_views.revision(self.repo,saved_views.LIBRARY))
        state_put(self.repo,'artwork_overrides','album:a:cover',{'url':'chosen'})
        chosen=saved_views.revision(self.repo,saved_views.LIBRARY);self.assertNotEqual(stamp,chosen)
        batch.publish(self.repo,state,force=True);self.assertNotEqual(chosen,saved_views.revision(self.repo,saved_views.LIBRARY))
        current=saved_views.revision(self.repo,saved_views.LIBRARY)
        state_put(self.repo,'library_artist_metadata','a',{'name':'after'})
        self.assertEqual(current,saved_views.revision(self.repo,saved_views.LIBRARY))
        self.store.update(self.ctx.id,status='interrupted');self.assertNotEqual(current,saved_views.revision(self.repo,saved_views.LIBRARY))
    def test_publication_interval(self):
        self.start();state={'status':'running','job_id':self.ctx.id,'published':batch.generations(self.repo),'published_at':100}
        state_put(self.repo,'library_artist_catalog','a',{})
        with patch.object(batch.time,'time',return_value=159):batch.publish(self.repo,state)
        self.assertEqual(state['published'],{})
        with patch.object(batch.time,'time',return_value=160):batch.publish(self.repo,state)
        self.assertIn('library_artist_catalog',state['published'])

    def test_revoked_admin_is_not_automatically_resumed(self):
        self.start();self.store.update(self.ctx.id,status='interrupted')
        batch.recover(self.store,{'id':2,'admin':False})
        self.assertFalse(any(j['status']=='queued' for j in self.store.list()))
    def test_member_projection_uses_root_batch_but_permissions_are_immediate(self):
        self.start();member=Repository(self.repo.directory/'users'/'2')
        state_put(self.repo,'library_artist_metadata','a',{'name':'old'})
        state={'status':'running','job_id':self.ctx.id,'published':batch.generations(self.repo),'published_at':time.time()};batch.save(self.repo,state)
        before=saved_views.revision(member,saved_views.LIBRARY)
        state_put(self.repo,'library_artist_metadata','a',{'name':'new'})
        self.assertEqual(before,saved_views.revision(member,saved_views.LIBRARY))
        state_put(self.repo,'accounts','2',{'disabled':True})
        self.assertNotEqual(before,saved_views.revision(member,saved_views.LIBRARY))

    def test_full_queue_does_not_block_startup_recovery_can_retry(self):
        from fastapi import HTTPException
        self.start();self.store.update(self.ctx.id,status='interrupted')
        with patch.object(self.store,'enqueue',side_effect=HTTPException(429,'full')):batch.recover(self.store)
        batch.recover(self.store)
        self.assertEqual(len([j for j in self.store.list() if j['status']=='queued']),1)

    def test_changed_artist_identity_only_retries_that_artist(self):
        self.start()
        with patch.object(file_tools,'refresh_metadata',side_effect=[{}, {}, {'failed_providers':['Last.fm']}]):file_tools.refresh_all_metadata()
        self.store.update(self.ctx.id,status='completed');self.start()
        state_put(self.repo,'library_artist_links','1',{'musicbrainz_id':'new-identity'})
        with patch.object(file_tools,'refresh_metadata',return_value={}) as refresh:result=file_tools.refresh_all_metadata()
        self.assertEqual(result['resumed'],1)
        self.assertEqual([c.args[0]['artist_id'] for c in refresh.call_args_list],['1','2'])
