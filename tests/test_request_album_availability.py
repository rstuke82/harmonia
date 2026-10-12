import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harmonia import accounts,api,discover,lidarr_requests
from harmonia.storage import Repository

class RequestAlbumAvailability(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name))
        self.cfg={'enabled':True,'url':'http://lidarr','api_key':'key'}
        self.repo.save({'lidarr':{'settings':self.cfg},'plex_album_links':{'scope':{'group':{'plex_album_id':'501'}}}})
        for target in ('harmonia.accounts.root_repository','harmonia.discover.root_repository'):
            p=patch(target,return_value=self.repo);p.start();self.addCleanup(p.stop)
        p=patch('harmonia.config.Config',return_value=SimpleNamespace(repository=self.repo,config={'plex':{'url':'http://plex','music_library_key':'1','token':'key'}}));p.start();self.addCleanup(p.stop)
        p=patch('harmonia.track_bridge.context',return_value={'scope':'scope','links':{},'tracks':[]});p.start();self.addCleanup(p.stop)
        self.lidarr={'checked_at':time.time(),'rows':[{'id':42,'album_id':'group','artist':'Matchbox Twenty','title':'Mad Season','statistics':{'trackCount':17,'trackFileCount':17}}],'tracks':[]}
        self.plex={'checked_at':time.time(),'rows':[{'plex_id':str(i),'plex_album_id':'501','album_artist':'Different album credit','artist':'Performer','album':'Mad Season (Deluxe Edition)'} for i in range(17)]}

    def test_each_duplicate_request_uses_resolved_group_and_saved_plex_album(self):
        records={key:{'server':lidarr_requests.server_id(self.cfg),'artist':'Matchbox Twenty','title':'Mad Season','lidarr_id':42,'status':'search_completed','download_status':'Imported into Lidarr','lidarr_album_mbid':'group'} for key in ('group','older-request')}
        self.repo.save({'lidarr_requests':records,'inventory':{'lidarr':self.lidarr}})
        fn=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/requests').__wrapped__
        with accounts.as_user({'id':'1','admin':True}),patch('harmonia.inventory.current',return_value=(self.plex,self.lidarr)):
            rows=fn()
        self.assertEqual(len(rows),2)
        for r in rows:
            self.assertEqual(r['availability'],'Available')
            self.assertTrue(r['plex_available'])
            self.assertEqual(r['library_album_id'],'group')
            self.assertNotEqual(r['availability_detail'],'Waiting for a library scan.')

    def test_history_still_deduplicates_but_request_results_are_preserved(self):
        rows=[{'artist':'Matchbox Twenty','album':'Mad Season','mbid':'group','request_key':key} for key in ('first','second')]
        with patch('harmonia.inventory.current',return_value=(self.plex,self.lidarr)):
            self.assertEqual(len(discover.availability(rows,apply_preferences=False)),1)
            result=discover.availability(rows,apply_preferences=False,deduplicate=False)
        self.assertEqual([r['request_key'] for r in result],['first','second'])
        self.assertTrue(all(r['availability']=='Available' for r in result))

    def test_missing_saved_plex_target_is_not_available(self):
        with patch('harmonia.inventory.current',return_value=({'checked_at':time.time(),'rows':[]},self.lidarr)):
            row=discover.availability([{'artist':'Matchbox Twenty','album':'Mad Season','mbid':'group'}],apply_preferences=False)[0]
        self.assertFalse(row['plex_present'])
        self.assertEqual(row['availability'],'Awaiting Plex')

    def test_partial_track_count_is_explicit(self):
        self.plex['rows']=self.plex['rows'][:12]
        with patch('harmonia.inventory.current',return_value=(self.plex,self.lidarr)):
            row=discover.availability([{'artist':'Matchbox Twenty','album':'Mad Season','mbid':'group'}],apply_preferences=False)[0]
        self.assertEqual(row['availability'],'Partially Available')
        self.assertTrue(row['plex_present'])
        self.assertIn('12/17',row['availability_detail'])
