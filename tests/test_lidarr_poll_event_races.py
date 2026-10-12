import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi import HTTPException
from harmonia.storage import Repository
from harmonia.lidarr_requests import server_id
from harmonia.lidarr_request_events import update
from harmonia import lidarr_downloads


class PollEventRaces(unittest.TestCase):
    def test_missing_catalog_branches_retain_newer_webhook(self):
        for recent in (False, True):
            with self.subTest(recent=recent), tempfile.TemporaryDirectory() as folder:
                repo=Repository(Path(folder))
                cfg={'enabled':True,'url':'http://lidarr','api_key':'secret'}
                repo.save({'lidarr':{'settings':cfg},'lidarr_requests':{'one':{
                    'server':server_id(cfg),'lidarr_id':1,'status':'added',
                    'updated_at':time.time() if recent else 0}}})
                client=Mock(key='secret')
                def call(method,path,**kwargs):
                    if path=='album':return []
                    if path=='album/1':
                        update(repo,{'albums':[{'id':1}],'downloadId':'B','message':'Broken'},'importfailed')
                        raise HTTPException(502,'Lidarr returned HTTP 404: absent')
                    raise AssertionError(path)
                client.call.side_effect=call
                with patch('harmonia.lidarr.Client',return_value=client),patch.object(lidarr_downloads,'queue',return_value=[]),patch('harmonia.api._record_log'):
                    lidarr_downloads.snapshot(repo,cfg,force=True)
                row=repo.load('lidarr_requests')['one']
                self.assertEqual(row['download_status'],'Import failed')
                self.assertEqual(row['download_error'],'Broken')
                self.assertEqual(row['lidarr_id'],1)
                self.assertFalse(row.get('removed_from_lidarr'))

    def test_missing_catalog_preserves_existing_failure(self):
        for recent in (False,True):
            with self.subTest(recent=recent),tempfile.TemporaryDirectory() as folder:
                repo=Repository(Path(folder));cfg={'enabled':True,'url':'http://lidarr','api_key':'secret'}
                repo.save({'lidarr':{'settings':cfg},'lidarr_requests':{'one':{
                    'server':server_id(cfg),'lidarr_id':1,'status':'added',
                    'download_status':'Import failed','download_error':'Broken',
                    'webhook_failure':{'kind':'importfailed','download_id':'A','message':'Broken'},
                    'webhook_download_id':'A','webhook_checked_at':1,
                    'updated_at':time.time() if recent else 0}}})
                client=Mock(key='secret')
                def call(method,path,**kwargs):
                    if path=='album':return []
                    raise HTTPException(502,'Lidarr returned HTTP 404: absent')
                client.call.side_effect=call
                with patch('harmonia.lidarr.Client',return_value=client),patch.object(lidarr_downloads,'queue',return_value=[]),patch('harmonia.api._record_log'):
                    lidarr_downloads.snapshot(repo,cfg,force=True)
                row=repo.load('lidarr_requests')['one']
                self.assertEqual(row['download_status'],'Import failed')
                self.assertTrue(row['webhook_failure'])
                self.assertEqual(row['lidarr_id'],1)
                self.assertFalse(row.get('removed_from_lidarr'))
