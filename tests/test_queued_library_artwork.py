import json,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import requests
from fastapi import HTTPException
from harmonia import discover,library,plex_images,image_download,accounts
from harmonia.storage import Repository
from library_fixtures import model_from_inventory

class UnifiedLibraryTests(unittest.TestCase):
    def model(self):
        return model_from_inventory({'lidarr':{'rows':[{'album_id':'release','title':'The Reason','artist':'Hoobastank','artwork':'stale-lidarr'}],'tracks':[{'id':'track','album_id':'release','title':'The Reason'}]},'plex':{'checked_at':time.time(),'rows':[{'plex_id':'playable','plex_album_id':'42','album':'The Reason','artist':'Hoobastank','title':'The Reason'}]}})
    def test_picker_uses_current_library_cover_and_one_unified_target(self):
        model=self.model();model['_rows_by_album']['release']['artwork']='/api/current-selected-cover?v=new'
        with patch.object(library,'cached_library',return_value=model):
            targets=library.link_targets()
            rows=discover._history_candidates(targets,{'album':'The Reason (20th Anniversary)','artist':'Hoobastank','tracks':['The Reason']})
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['artwork'],'/api/current-selected-cover?v=new')
        self.assertEqual(rows[0]['target_id'],'release');self.assertTrue(rows[0]['plex_present']);self.assertEqual(rows[0]['score'],100)
        self.assertNotIn('_track_records',rows[0])
    def test_saved_association_unifies_differently_named_editions(self):
        model=self.model();model['_rows_by_album']['plex:42']['linked_lidarr_album_id']='release'
        model['_plex_tracks_by_album']['42'][0]['album']='The Reason (Anniversary)'
        model['_album_links']={'release':{'plex_album_id':'42'}}
        with patch.object(library,'cached_library',return_value=model):targets=library.link_targets()
        self.assertEqual(len(targets),1);self.assertEqual(targets[0]['plex_tracks'][0]['plex_id'],'playable')
    def test_plex_only_and_account_scoped_tracks(self):
        model=self.model();model['_rows_by_album'].pop('release');model['_rows_by_album']['plex:42']['linked_lidarr_album_id']=None
        with patch.object(library,'cached_library',return_value=model):
            self.assertEqual(library.link_targets()[0]['target_type'],'plex')
        index={'plex':model['_plex'],'playable_tracks':model['_plex_by_id']}
        with patch('harmonia.api._config',return_value=SimpleNamespace()),patch.object(library,'indexed',return_value=index):
            self.assertEqual([t['plex_id'] for t in library.playable_tracks()],['playable'])
            model['_plex']['checked_at']=None
            with self.assertRaises(HTTPException) as error:library.playable_tracks()
        self.assertEqual(error.exception.status_code,409)
    def test_leading_article_needs_artist_and_song_evidence_and_preserves_ties(self):
        source={'album':'Family Values Tour ’98','artist':'Various Artists','tracks':['Song']}
        tracks=[{'title':'Song'}]
        self.assertEqual(discover._history_match_evidence(source,"The Family Values Tour '98",'Various Artists',tracks)['score'],100)
        self.assertLess(discover._history_match_evidence(source,"The Family Values Tour '98",'Various Artists',[])['album_score'],100)
        self.assertLess(discover._history_match_evidence(source,"The Family Values Tour '98",'Unrelated',tracks)['album_score'],100)
        candidates=[{'title':"The Family Values Tour '98",'artist':'Various Artists','tracks':tracks}]*2
        self.assertIsNone(discover._best_history_match(source,candidates))
    def test_library_model_cache_separates_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository(Path(tmp));library.invalidate()
            with patch('harmonia.api._config',return_value=SimpleNamespace(repository=repo)),patch.object(accounts,'root_repository',return_value=repo),patch.object(library,'build_library',side_effect=lambda:{'owner':accounts.actor()['id']}):
                with accounts.as_user({'id':'one'}):one=library.cached_library()
                with accounts.as_user({'id':'two'}):two=library.cached_library()
            self.assertEqual(one['owner'],'one');self.assertEqual(two['owner'],'two')
            library.invalidate()

class ArtworkRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.repo=Repository(Path(tmp.name))
        self.config=SimpleNamespace(repository=self.repo);self.client=SimpleNamespace(base_url='http://plex',headers={'X-Plex-Token':'secret'},machine_identifier='server',music_library_key='1')
        self.old='/library/metadata/42/thumb/old';self.new='/library/metadata/42/thumb/new'
        self.repo.save({'inventory':{'plex':{'machine_identifier':'server','rows':[{'plex_album_id':'42','plex_album_thumb':self.old}]}}})
        plex_images._missing.clear()
    def missing(self):
        response=requests.Response();response.status_code=404;return requests.HTTPError(response=response)
    def metadata(self,path):return image_download.ImageBytes(json.dumps({'MediaContainer':{'Metadata':[{'ratingKey':'42','thumb':path}]}}).encode(),{})
    def test_stale_thumbnail_recovers_target_only_and_persists_new_reference(self):
        outcomes=[self.missing(),self.metadata(self.new),image_download.ImageBytes(b'cover',{'Content-Type':'image/jpeg'})]
        with patch('harmonia.api._health_plex',return_value=self.client),patch.object(image_download,'download',side_effect=outcomes) as download:
            result=plex_images.serve(self.config,self.old)
            self.assertEqual(result.body,b'cover');self.assertEqual([c.args[0] for c in download.call_args_list],['http://plex'+self.old,'http://plex/library/metadata/42','http://plex'+self.new])
        self.assertEqual(self.repo.load_one('inventory','plex')['rows'][0]['plex_album_thumb'],self.new)
    def test_missing_image_is_negative_cached_and_never_scans_unknown_id(self):
        with patch('harmonia.api._health_plex',return_value=self.client),patch.object(image_download,'download',side_effect=self.missing()) as download:
            for _ in range(3):
                with self.assertRaises(HTTPException) as error:plex_images.serve(self.config,'/library/metadata/99/thumb/old')
                self.assertEqual(error.exception.status_code,404)
            self.assertEqual(download.call_count,1)
    def test_recovery_rejects_foreign_origin_and_rating(self):
        for path in ('https://attacker/cover','/library/metadata/99/thumb/new'):
            with patch.object(image_download,'download',return_value=self.metadata(path)):self.assertIsNone(plex_images._recover(self.config,self.client,self.old))
        self.assertEqual(self.repo.load_one('inventory','plex')['rows'][0]['plex_album_thumb'],self.old)
    def test_concurrent_cover_choice_wins_over_late_metadata(self):
        chosen='/library/metadata/42/thumb/chosen'
        def response(*a,**kw):
            self.repo.save({'inventory':{'plex':{'machine_identifier':'server','rows':[{'plex_album_id':'42','plex_album_thumb':chosen}]}}})
            return self.metadata(self.new)
        with patch.object(image_download,'download',side_effect=response):self.assertEqual(plex_images._recover(self.config,self.client,self.old),chosen)
        self.assertEqual(self.repo.load_one('inventory','plex')['rows'][0]['plex_album_thumb'],chosen)
    def test_explicit_choice_invalidates_same_url_cached_bytes(self):
        with patch('harmonia.api._health_plex',return_value=self.client),patch.object(image_download,'download',side_effect=[image_download.ImageBytes(b'old',{'Content-Type':'image/jpeg'}),image_download.ImageBytes(b'new',{'Content-Type':'image/jpeg'})]) as download:
            self.assertEqual(plex_images.serve(self.config,self.old).body,b'old');self.assertEqual(plex_images.serve(self.config,self.old).body,b'old')
            plex_images.invalidate(self.repo)
            self.assertEqual(plex_images.serve(self.config,self.old).body,b'new');self.assertEqual(download.call_count,2)
    def test_old_inflight_bytes_cannot_refill_cache_after_cover_choice(self):
        def delayed(*a,**kw):
            plex_images.invalidate(self.repo)
            return image_download.ImageBytes(b'old-inflight-cover',{'Content-Type':'image/jpeg'})
        with patch('harmonia.api._health_plex',return_value=self.client),patch.object(image_download,'download',side_effect=delayed):
            result=plex_images.serve(self.config,self.old)
        self.assertEqual(result.headers['cache-control'],'private, no-store')
        self.assertEqual(list((Path(self.repo.directory)/'plex-image-cache').glob('*.bin')),[])
    def test_network_failure_is_sanitized_and_retryable(self):
        with patch('harmonia.api._health_plex',return_value=self.client),patch.object(image_download,'download',side_effect=requests.ConnectionError('secret upstream')):
            with self.assertRaises(HTTPException) as error:plex_images.serve(self.config,self.old)
        self.assertEqual(error.exception.status_code,502);self.assertNotIn('secret',error.exception.detail)

class ArtworkConcurrencyTests(unittest.TestCase):
    def test_identical_requests_share_download_but_different_credentials_do_not(self):
        entered=threading.Event();release=threading.Event();calls=[];results=[]
        def stream(*a,**kw):calls.append(kw['headers']);entered.set();release.wait(2);return image_download.ImageBytes(b'image',{})
        def caller(headers):results.append(image_download.download('http://plex/image',headers=headers))
        with patch.object(image_download,'_stream_download',side_effect=stream):
            first=threading.Thread(target=caller,args=({'token':'one'},));first.start();self.assertTrue(entered.wait(1))
            second=threading.Thread(target=caller,args=({'token':'one'},));second.start()
            third=threading.Thread(target=caller,args=({'token':'two'},));third.start()
            time.sleep(.05);release.set()
            for worker in (first,second,third):worker.join(2);self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls),2);self.assertEqual(len(results),3)
    def test_busy_slot_waits_briefly_and_is_released_after_download(self):
        slots=threading.BoundedSemaphore(1);slots.acquire()
        timer=threading.Timer(.05,slots.release);timer.start()
        response=SimpleNamespace(headers={},raise_for_status=lambda:None,iter_content=lambda **kw:iter([b'cover']),close=lambda:None)
        with patch.object(image_download,'_downloads',slots),patch('harmonia.redirect_policy.get',return_value=response):
            result=image_download.download('http://plex/image',seconds=1)
        timer.join();self.assertEqual(result.content,b'cover');self.assertTrue(slots.acquire(blocking=False));slots.release()

class OptionalLidarrLibraryTests(unittest.TestCase):
    def test_plex_only_availability_uses_current_library_cover_without_lidarr(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository(Path(tmp));repo.save({'lidarr':{'settings':{'enabled':False}}})
            model=model_from_inventory({'plex':{'checked_at':time.time(),'rows':[{'plex_id':'1','plex_album_id':'42','title':'Song','album':'Album','artist':'Artist'}]}})
            model['_rows_by_album']['plex:42']['artwork']='/api/library/current?v=chosen'
            with patch.object(accounts,'root_repository',return_value=repo),patch.object(accounts,'personal_repository',return_value=repo),patch('harmonia.user_preferences.personal_repository',return_value=repo),patch.object(discover,'root_repository',return_value=repo),patch('harmonia.api._config',return_value=SimpleNamespace(repository=repo,config={'plex':{'url':'http://plex'}})),patch.object(library,'cached_library',return_value=model):
                result=discover.availability([{'album':'Album','artist':'Artist','tracks':['Song']}],apply_preferences=False)
            self.assertEqual(result[0]['availability'],'Available')
            self.assertEqual(result[0]['artwork'],'/api/library/current?v=chosen')
