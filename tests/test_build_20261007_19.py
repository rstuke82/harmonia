import tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from harmonia import api_v1,jobs,lidarr_catalog
from harmonia.storage import Repository
from harmonia.lidarr_requests import server_id

class ScopedCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.repo=Repository(Path(self.tmp.name))
        self.cfg={'enabled':True,'url':'http://lidarr','api_key':'fixture'}
        self.other={'id':20,'album_id':'other','artist_id':2,'artist':'Other','title':'Other'}
        self.repo.save({'inventory':{'lidarr':{'identity':server_id(self.cfg),'checked_at':123,'rows':[{'id':10,'album_id':'group','artist_id':1,'title':'Old'},self.other],'tracks':[{'id':'old','album_id':'group','lidarr_album_id':10,'file_id':5,'file':'/old'},{'id':'other','album_id':'other','lidarr_album_id':20,'file':'/other'}],'artists':[{'id':1,'name':'Old'},{'id':2,'name':'Other'}],'rename_preview':{'checked_at':123,'rows':[{'artistId':1,'albumId':10},{'artistId':2,'albumId':20}],'failed_artists':[]}}}})
        self.client=Mock();self.client.call.side_effect=self.call
    def call(self,method,path,**kwargs):
        self.assertEqual(method,'GET')
        if path=='artist/1':return {'id':1,'artistName':'Artist','images':[]}
        if path=='album/10':return {'id':10,'artistId':1,'foreignAlbumId':'group','title':'New','secondaryTypes':['Compilation']}
        if path=='album':self.assertEqual(kwargs['params'],{'artistId':1});return [self.call('GET','album/10')]
        if path=='track':self.assertIn(kwargs['params'],[{'artistId':1},{'albumId':10}]);return [{'id':100,'albumId':10,'trackFileId':5,'title':'Song'}]
        if path=='trackfile':self.assertIn(kwargs['params'],[{'artistId':1},{'albumId':10}]);return [{'id':5,'path':'/new','relativePath':'new'}]
        if path=='rename':self.assertEqual(kwargs['params']['artistId'],1);return []
        raise AssertionError('Unscoped or unrelated API call: '+path)
    def refresh(self,target):
        with patch('harmonia.lidarr_catalog.config',return_value=self.cfg),patch('harmonia.lidarr_catalog.Client',return_value=self.client),patch('harmonia.library.invalidate'),patch('harmonia.inventory.scan',side_effect=AssertionError('full scan forbidden')),patch('harmonia.inventory.reconcile',side_effect=AssertionError('global reconcile forbidden')):
            return lidarr_catalog.refresh(self.repo,[target])
    def test_artist_rename_updates_only_affected_slice(self):
        result=self.refresh({'scope':'artist','id':1,'kind':'rename'})
        inv=self.repo.load_one('inventory','lidarr',{})
        self.assertEqual(result['updated'],1);self.assertFalse(result['partial_success'])
        self.assertIn(self.other,inv['rows']);self.assertEqual(inv['checked_at'],123)
        self.assertEqual([t['file'] for t in inv['tracks']],['/other','/new'])
        self.assertEqual(inv['rename_preview']['rows'],[{'artistId':2,'albumId':20}])
        self.assertEqual(next(t for t in inv['tracks'] if t['id']=='100')['file_id'],5)
    def test_album_rename_never_fetches_entire_artist_catalog(self):
        self.refresh({'scope':'album','id':10,'kind':'rename'})
        self.assertFalse(any(c.args[1]=='album' for c in self.client.call.call_args_list))
        for c in self.client.call.call_args_list:
            if c.args[1] in ('track','trackfile'):self.assertEqual(c.kwargs['params'],{'albumId':10})
        inv=self.repo.load_one('inventory','lidarr',{})
        self.assertEqual(inv['rows'][-1]['secondary_types'],['Compilation'])
        self.assertEqual(inv['tracks'][-1]['file_id'],5)
    def test_deletion_only_removes_affected_saved_entries(self):
        self.refresh({'scope':'artist','id':1,'kind':'artistdelete'})
        self.client.call.assert_not_called();inv=self.repo.load_one('inventory','lidarr',{})
        self.assertEqual(inv['rows'],[self.other]);self.assertEqual([t['id'] for t in inv['tracks']],['other'])
    def test_album_delete_cannot_remove_the_artist_without_album_identity(self):
        before=self.repo.load_one('inventory','lidarr',{})
        result=self.refresh({'scope':'artist','id':1,'kind':'albumdelete'})
        self.assertTrue(result['partial_success']);self.assertEqual(before,self.repo.load_one('inventory','lidarr',{}))
    def test_failed_read_retains_previous_slice(self):
        before=self.repo.load_one('inventory','lidarr',{});self.client.call.side_effect=RuntimeError('failed')
        result=self.refresh({'scope':'artist','id':1,'kind':'rename'})
        self.assertTrue(result['partial_success']);self.assertEqual(before,self.repo.load_one('inventory','lidarr',{}))
    def test_no_identity_never_falls_back_to_full_scan(self):
        result=self.refresh({'scope':'unknown','id':None,'kind':'rename'})
        self.assertTrue(result['partial_success']);self.client.call.assert_not_called()

class DurableTargets(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.repo=Repository(Path(self.tmp.name))
    def save(self,batch):self.repo.save({api_v1.CATALOG_BATCH_NAMESPACE:{api_v1.CATALOG_BATCH_KEY:batch}})
    def read(self):return self.repo.load_one(api_v1.CATALOG_BATCH_NAMESPACE,api_v1.CATALOG_BATCH_KEY,{})
    def execute(self,callback=None):
        with patch('harmonia.accounts.root_repository',return_value=self.repo),patch('harmonia.lidarr_catalog.refresh',side_effect=callback or (lambda repo,targets:{'summary':'Scoped','updated':len(targets)})),patch('harmonia.inventory.scan',side_effect=AssertionError('full scan forbidden')):
            return api_v1.execute_lidarr_webhook({'event':'catalog_batch'})
    def test_repeated_targets_coalesce_and_no_empty_followup(self):
        batch={}
        for _ in range(19):batch=api_v1._add_to_catalog_batch(batch,'rename',{'artist_id':1})
        self.save(batch);result=self.execute()
        self.assertIn('1 affected',result['summary']);self.assertIsNone(result['follow_up_job_id']);self.assertEqual(self.read()['targets'],{})
        self.assertIn('No newer',self.execute()['summary'])
    def test_new_event_during_refresh_is_preserved(self):
        self.save(api_v1._add_to_catalog_batch({},'rename',{'artist_id':1}))
        def arrivals(repo,targets):self.save(api_v1._add_to_catalog_batch(self.read(),'rename',{'artist_id':1}));return {'summary':'Scoped'}
        result=self.execute(arrivals);self.assertIsNotNone(result['follow_up_job_id']);self.assertEqual(self.read()['targets']['artist:1']['generation'],2)
    def test_batch_is_bounded_and_retains_remainder(self):
        batch={}
        for i in range(11):batch=api_v1._add_to_catalog_batch(batch,'rename',{'artist_id':i+1})
        self.save(batch);result=self.execute();self.assertIsNotNone(result['follow_up_job_id']);self.assertEqual(len(self.read()['targets']),1)
    def test_new_event_after_claim_has_a_queued_successor(self):
        first=api_v1._queue_lidarr_event(self.repo,{'eventType':'Rename','artist':{'id':1}},'owner')
        jobs.Store(self.repo).update(first['job_id'],status='running')
        second=api_v1._queue_lidarr_event(self.repo,{'eventType':'Rename','artist':{'id':2}},'owner')
        self.assertNotEqual(first['job_id'],second['job_id'])
    def test_album_delete_without_album_id_is_unknown_not_artist_deletion(self):
        api_v1._queue_lidarr_event(self.repo,{'eventType':'AlbumDelete','artist':{'id':1}},'owner')
        target=next(iter(self.read()['targets'].values()))
        self.assertEqual(target['scope'],'unknown')
    def test_same_artist_event_during_execution_gets_successor(self):
        first=api_v1._queue_lidarr_event(self.repo,{'eventType':'Rename','artist':{'id':1}},'owner')
        jobs.Store(self.repo).update(first['job_id'],status='running')
        second=api_v1._queue_lidarr_event(self.repo,{'eventType':'Rename','artist':{'id':1}},'owner')
        self.assertNotEqual(first['job_id'],second['job_id'])
        self.assertEqual(self.read()['generation'],2)
    def test_legacy_id_batch_migrates_without_full_scan(self):
        self.save({'generation':5,'processed_generation':0,'artist_ids':[1,2]})
        self.assertIn('2 affected',self.execute()['summary'])

if __name__=='__main__':unittest.main()
