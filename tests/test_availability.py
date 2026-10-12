import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from contextlib import nullcontext
from harmonia.storage import Repository
from harmonia.jobs import Store
from harmonia import availability,library_cache,tasks

class AvailabilityTests(unittest.TestCase):
    def test_plex_library_response_is_decoded_once(self):
        from harmonia.plex_client import PlexAPI
        client=object.__new__(PlexAPI)
        client.base_url='http://plex';client.music_library_key='1';client.music_library_name='Music'
        client.headers={};client.strict_errors=True
        response=Mock(status_code=200)
        response.json.side_effect=[{'MediaContainer':{'Metadata':[]}},AssertionError('Plex response decoded twice')]
        with patch('harmonia.network.get',return_value=response):
            self.assertEqual(client._fetch_library(),[])
        self.assertEqual(response.json.call_count,1)

    @patch('harmonia.inventory.snapshot',return_value={'checked_at':1,'rows':[{'plex_id':'saved'}]})
    def test_cache_reuse_refresh_and_identity(self,_snapshot):
        library_cache.invalidate()
        client=SimpleNamespace(base_url='plex',music_library_key='1',headers={'token':'a'})
        fetch=Mock(return_value=[{'plex_id':'fresh'}])
        with library_cache.reuse(5):
            self.assertEqual(library_cache.load(client,fetch)[0]['plex_id'],'saved')
        self.assertEqual(library_cache.load(client,fetch)[0]['plex_id'],'saved');fetch.assert_not_called()
        with library_cache.reuse(5,refresh=True):library_cache.load(client,fetch)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(library_cache.load(client,fetch)[0]['plex_id'],'saved')
        client.headers={'token':'b'}
        _snapshot.return_value={}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):library_cache.load(client,fetch)
        self.assertEqual(fetch.call_count,1)

    def test_schedule_added_once_to_existing_install(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite');store=Store(repo)
            tasks.setup(store)
            with repo.connect() as db:db.execute("DELETE FROM schedules WHERE action='plex_scan'")
            tasks.setup(store);tasks.setup(store)
            found=[s for s in store.schedules() if s['action']=='plex_scan']
            self.assertEqual(len(found),1);self.assertEqual(found[0]['cron'],tasks.expression(1,'00:15'))
            store.save_schedule({'action':'plex_scan','scope':'all','hours':0})
            tasks.setup(store)
            self.assertFalse(next(s for s in store.schedules() if s['action']=='plex_scan')['enabled'])

    def test_manual_task_run_links_to_its_task_row(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite');store=Store(repo)
            with patch('harmonia.tasks.local_scan_ready',return_value=True):tasks.setup(store)
            job_id=store.enqueue('file_audit',{})
            store.claim(job_id)
            config=SimpleNamespace(config={'playlists':[]})
            with patch('harmonia.tasks.local_scan_ready',return_value=True),patch('harmonia.api._config',return_value=config):
                row=next(r for r in tasks.rows(store) if r['action']=='file_audit')
            self.assertEqual(row['active_job']['id'],job_id)
            self.assertEqual(row['active_origin'],'manual')
            self.assertIsNone(row['last_job'])
            from harmonia.jobs import now
            store.update(job_id,status='completed',finished_at=now())
            with patch('harmonia.tasks.local_scan_ready',return_value=True),patch('harmonia.api._config',return_value=config):
                row=next(r for r in tasks.rows(store) if r['action']=='file_audit')
            self.assertEqual(row['last_job']['id'],job_id)
            self.assertIsNone(row['active_job'])

    def test_scan_deduplicates_preserves_manual_and_scopes_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite');store=Store(repo)
            track={'title':'Song','artist':'Artist','album':'Album'}
            manual={'title':'Manual','artist':'Artist'};ignored={'title':'Ignored','artist':'Artist'}
            config=SimpleNamespace(config={'playlists':[{'key':'a','auto_sync':True},{'key':'b','auto_sync':False}]},missing={'a':[track,manual,ignored],'b':[track]},mapping={'a':{'Manual|Artist':'gone'}},save=Mock())
            syncer=Mock()
            syncer._find_ignored_track_key.side_effect=lambda key,t:t['title']=='Ignored'
            syncer._get_match_provenance.side_effect=lambda key,s:'manual' if s=='Manual|Artist' else 'automatic'
            syncer._same_missing_identity.side_effect=lambda a,b:a['title']==b['title']
            with patch('harmonia.api.job_store',return_value=store),patch('harmonia.api._config',return_value=config),patch('harmonia.api._playlist_key',side_effect=lambda p:p['key']),patch('harmonia.api._health_plex') as plex,patch('harmonia.process_lock.ProcessLock',return_value=nullcontext()),patch('harmonia.sync_engine.Syncer',return_value=syncer),patch('harmonia.matching.Matcher.match_track',return_value='7') as match,patch('harmonia.api._record_log'):
                plex.return_value.search_library.return_value=[{'plex_id':'7','title':'Song','artist':'Artist'}]
                result=availability.execute({})
            self.assertEqual(match.call_count,1)
            self.assertEqual(config.mapping['a']['Manual|Artist'],'gone')
            self.assertEqual([t['title'] for t in config.missing['a']],['Manual','Ignored'])
            self.assertTrue(config.config['playlists'][1]['ready_to_sync'])
            self.assertIsNone(result['sync_job_id']) # readiness follows configured schedule, never a second automatic path
            self.assertEqual(result['matches_found'],1)

    def test_unchanged_tracks_do_not_write(self):
        from harmonia.plex_client import PlexAPI
        client=object.__new__(PlexAPI)
        client.get_playlist_items=Mock(return_value=[{'plex_id':'1'},{'plex_id':'1'},{'plex_id':'2'}])
        with patch('harmonia.network.post') as post,patch('harmonia.network.put') as put:
            self.assertTrue(client.replace_playlist_tracks('p',['1','1','2']))
            post.assert_not_called();put.assert_not_called()
