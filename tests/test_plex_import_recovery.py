import json
import time
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from harmonia.album_finalize import _wait_for_plex_album, _execute
from harmonia.plex_client import PlexAPI
from harmonia.lidarr_downloads import queue_plex_rechecks
from harmonia.lidarr_requests import server_id
from harmonia.storage import Repository
from harmonia.jobs import Store


class PlexImportRecoveryTests(unittest.TestCase):
    def test_album_search_fetches_children_and_paginates(self):
        plex=object.__new__(PlexAPI);plex.base_url='http://plex';plex.music_library_key='1';plex.headers={}
        responses=[{'MediaContainer':{'Metadata':[{'type':'album','title':'Album','ratingKey':'9','addedAt':123,'thumb':'/album/cover'}]}},
                   {'MediaContainer':{'Metadata':[{'ratingKey':'1','title':'A','parentTitle':'Album','grandparentTitle':'Artist'}],'totalSize':201}},
                   {'MediaContainer':{'Metadata':[{'ratingKey':'2','title':'B','parentTitle':'Album','grandparentTitle':'Artist'}],'totalSize':201}}]
        with patch('harmonia.network.get',side_effect=[Mock(json=Mock(return_value=v)) for v in responses]) as get:
            rows=plex.search_library_subset('Album')
        self.assertEqual([r['plex_id'] for r in rows],['1','2'])
        self.assertEqual([r['plex_album_added_at'] for r in rows],[123,123])
        self.assertEqual([r['plex_album_thumb'] for r in rows],['/album/cover','/album/cover'])
        self.assertEqual(get.call_args_list[0].kwargs['params']['type'],9)
        self.assertEqual(get.call_args_list[2].kwargs['params']['X-Plex-Container-Start'],200)
        self.assertTrue(get.call_args_list[1].args[0].endswith('/9/children'))

    def test_partial_album_waits_for_all_imported_files_and_filters_artist(self):
        plex=Mock();plex.search_library_subset.side_effect=[
            [{'album':'Album','album_artist':'Artist','files':['/music/1']}],
            [{'album':'Album','album_artist':'Other','files':['/music/2']},
             {'album':'Album','album_artist':'Artist','files':['/music/1','/music/2']}]]
        with patch('harmonia.album_finalize.time.sleep'),patch('harmonia.jobs.current',return_value=None):
            rows=_wait_for_plex_album(plex,{'title':'Album'},{'artistName':'Artist'},True,(0,2),['/music/1','/music/2'])
        self.assertEqual(len(rows),1);self.assertEqual(plex.search_library_subset.call_count,2)

    def test_durable_checks_deduplicate_and_skip_wrong_server_failures_and_future(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder));store=Store(repo);cfg={'enabled':True,'url':'http://lidarr'}
            base={'server':server_id(cfg),'lidarr_id':1,'finalize_status':'awaiting_plex','download_status':'Imported into Lidarr','webhook_imported_at':time.time()}
            with repo.connect() as db:
                for key,extra in [('old',{}),('wrong',{'server':'other'}),('failure',{'webhook_failure':{'event':'ImportFailed'}}),('future',{'plex_check_after':10**12})]:
                    db.execute('INSERT INTO state(namespace,key,value) VALUES(?,?,?)',('lidarr_requests',key,json.dumps({**base,**extra})))
            with patch('harmonia.lidarr.config',return_value=cfg):
                queue_plex_rechecks(store);queue_plex_rechecks(Store(Repository(Path(folder))))
            with repo.connect() as db:rows=db.execute("SELECT payload FROM jobs WHERE action='album_finalize'").fetchall()
            self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0][0])['album_id'],'old')

    def test_interrupted_check_resumes_but_cancelled_check_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder));store=Store(repo);cfg={'enabled':True,'url':'http://lidarr'}
            record={'server':server_id(cfg),'lidarr_id':1,'finalize_status':'awaiting_plex','download_status':'Imported into Lidarr','webhook_imported_at':time.time()}
            with repo.connect() as db:db.execute('INSERT INTO state(namespace,key,value) VALUES(?,?,?)',('lidarr_requests','rg',json.dumps(record)))
            with patch('harmonia.lidarr.config',return_value=cfg):
                queue_plex_rechecks(store)
                first=repo.load_one('lidarr_requests','rg',{})['finalize_job_id']
                with repo.connect() as db:db.execute("UPDATE jobs SET status='interrupted' WHERE id=?",(first,))
                queue_plex_rechecks(Store(Repository(Path(folder))))
                second=repo.load_one('lidarr_requests','rg',{})['finalize_job_id']
                self.assertNotEqual(first,second)
                with repo.connect() as db:db.execute("UPDATE jobs SET status='cancelled' WHERE id=?",(second,))
                queue_plex_rechecks(store)
            with repo.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)

    def run_check(self,found,change_event=False,age=0,attempts=0):
        stack=ExitStack();self.addCleanup(stack.close)
        folder=stack.enter_context(tempfile.TemporaryDirectory());repo=Repository(Path(folder))
        cfg={'url':'http://lidarr','enabled':True};record={'server':server_id(cfg),'lidarr_id':1,'finalize_status':'awaiting_plex','download_status':'Imported into Lidarr','webhook_imported_at':time.time(),'webhook_checked_at':10,'plex_check_started_at':time.time()-age,'plex_check_attempts':attempts}
        with repo.connect() as db:db.execute('INSERT INTO state(namespace,key,value) VALUES(?,?,?)',('lidarr_requests','rg',json.dumps(record)))
        client=Mock();client.call.side_effect=[{'id':1,'foreignAlbumId':'rg','artistId':2,'title':'Album'},{'artistName':'Artist'}]
        plex=Mock()
        patches={'harmonia.accounts.root_repository':repo,'harmonia.lidarr.config':cfg,
                 'harmonia.lidarr.Client':client,'harmonia.api._config':{},'harmonia.api._health_plex':plex,
                 'harmonia.album_finalize._tracks':[{'file':'/music/1'}],
                 'harmonia.album_finalize._merge_lidarr':{},'harmonia.file_tools.settings':{},
                 'harmonia.file_tools.mapped':'/music/1','harmonia.inventory.identity':'plex',
                 'harmonia.inventory.reconcile':{},'harmonia.artwork_sync.queue_pending':None,
                 'harmonia.library_cache.invalidate':None,'harmonia.jobs.current':None}
        mocks={target:stack.enter_context(patch(target,return_value=value)) for target,value in patches.items()}
        scan=stack.enter_context(patch('harmonia.album_finalize._scan_paths'))
        def lookup(*args,**kwargs):
            if change_event:
                from harmonia.lidarr_requests import save
                save(repo,'rg',webhook_checked_at=11,webhook_failure={'event':'ImportFailed'},download_status='Import failed')
            return found
        stack.enter_context(patch('harmonia.album_finalize._wait_for_plex_album',side_effect=lookup))
        result=_execute({'album_id':'rg','lidarr_id':1,'server':server_id(cfg),'plex_recheck':True,'expected_webhook_at':10})
        scan.assert_not_called();plex.refresh_library_path.assert_not_called()
        if not found:
            mocks['harmonia.album_finalize._merge_lidarr'].assert_not_called()
            mocks['harmonia.inventory.reconcile'].assert_not_called()
            mocks['harmonia.library_cache.invalidate'].assert_not_called()
        return repo, result

    def test_pending_check_persists_backoff_and_preserves_import_timestamp(self):
        repo,result=self.run_check([]);record=repo.load_one('lidarr_requests','rg',{})
        self.assertTrue(result['pending']);self.assertEqual(record['plex_check_attempts'],1)
        self.assertGreater(record['plex_check_after'],record['finalized_at']);self.assertLess(record['webhook_imported_at'],record['finalized_at'])

    def test_completed_check_updates_inventory_and_stops_retrying(self):
        repo,result=self.run_check([{'plex_id':'9','album':'Album','album_artist':'Artist','files':['/music/1']}])
        self.assertFalse(result['pending']);self.assertEqual(repo.load('inventory')['plex']['count'],1)
        self.assertEqual(repo.load_one('lidarr_requests','rg',{})['finalize_status'],'completed')
        self.assertEqual(repo.load_one('lidarr_requests','rg',{})['plex_check_after'],0)

    def test_new_failure_during_check_is_preserved(self):
        repo,result=self.run_check([{'plex_id':'9'}],True)
        self.assertEqual(repo.load_one('lidarr_requests','rg',{})['download_status'],'Import failed')
        self.assertNotIn('plex',repo.load('inventory'));self.assertIn('outdated',result['summary'])

    def test_expired_queued_check_stops_without_service_reads(self):
        repo,result=self.run_check([],age=1201)
        self.assertFalse(result['pending']);self.assertTrue(result['partial_success'])
        record=repo.load_one('lidarr_requests','rg',{})
        self.assertEqual(record['finalize_status'],'failed');self.assertEqual(record['plex_check_after'],0)

    def test_sixth_unsuccessful_check_stops(self):
        repo,result=self.run_check([],attempts=5)
        self.assertFalse(result['pending']);self.assertEqual(repo.load_one('lidarr_requests','rg',{})['plex_check_attempts'],6)
        self.assertEqual(repo.load_one('lidarr_requests','rg',{})['finalize_status'],'failed')

    def test_upgrade_expiry_and_global_queue_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder));store=Store(repo);cfg={'enabled':True,'url':'http://lidarr'}
            base={'server':server_id(cfg),'lidarr_id':1,'finalize_status':'awaiting_plex','download_status':'Imported into Lidarr'}
            with repo.connect() as db:
                for key,extra in [('old',{'webhook_imported_at':time.time()-7200}),('unknown',{}),('max',{'webhook_imported_at':time.time(),'plex_check_attempts':6}),('new1',{'webhook_imported_at':time.time()}),('new2',{'webhook_imported_at':time.time()})]:
                    db.execute('INSERT INTO state(namespace,key,value) VALUES(?,?,?)',('lidarr_requests',key,json.dumps({**base,**extra})))
            with patch('harmonia.lidarr.config',return_value=cfg):queue_plex_rechecks(store);queue_plex_rechecks(store)
            for key in ['old','unknown','max']:self.assertEqual(repo.load_one('lidarr_requests',key,{})['finalize_status'],'failed')
            with repo.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_network_errors_count_toward_retry_limit(self):
        from harmonia.album_finalize import execute
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder));record={'plex_check_attempts':5,'webhook_imported_at':time.time(),'webhook_checked_at':10}
            with repo.connect() as db:db.execute('INSERT INTO state(namespace,key,value) VALUES(?,?,?)',('lidarr_requests','rg',json.dumps(record)))
            with patch('harmonia.accounts.root_repository',return_value=repo),patch('harmonia.album_finalize._execute',side_effect=ValueError('Plex unavailable')):
                with self.assertRaises(ValueError):execute({'album_id':'rg','plex_recheck':True,'expected_webhook_at':10})
            result=repo.load_one('lidarr_requests','rg',{})
            self.assertEqual(result['plex_check_attempts'],6);self.assertEqual(result['finalize_status'],'failed');self.assertEqual(result['plex_check_after'],0)
