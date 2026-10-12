import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harmonia import accounts, api, saved_views
from harmonia.storage import Repository
from harmonia.sync_projection import update

class SyncProjection(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.repo=Repository(Path(tmp.name))
        self.user={'id':'1','owner':True,'admin':True}
        self.ctx=accounts.as_user(self.user);self.ctx.__enter__();self.addCleanup(self.ctx.__exit__,None,None,None)
        p=patch('harmonia.accounts.root_repository',return_value=self.repo);p.start();self.addCleanup(p.stop)
        self.playlist={'source':'applemusic','source_id':'mix','plex_playlist_name':'Mix','last_synced':'2026-10-12T20:00:00+00:00'}
        self.cfg=SimpleNamespace(repository=self.repo,config={'playlists':[self.playlist]},
            mapping={'applemusic:mix':{'Resolved|Artist':'1'}},missing={'applemusic:mix':[]},
            match_metadata={'applemusic:mix':{'Resolved|Artist':{'provenance':'automatic'}}},ignored_tracks={})
        self.tracks=[{'title':'Resolved','artist':'Artist'},{'title':'Unmatched','artist':'Artist'}]
        self.library=[{'title':'Resolved','artist':'Artist','plex_id':'1'}]

    def payload(self,cfg,playlist):
        return {'key':'applemusic:mix','last_synced':playlist['last_synced'],'health':cfg.health.get('applemusic:mix'),
                'sync_error':self.repo.load_one('playlist_sync_state','applemusic:mix',{}).get('error','')}

    def publish(self,ok=True):
        with patch('harmonia.api._playlist_payload',side_effect=self.payload),patch('harmonia.inventory.current',side_effect=AssertionError('No inventory reload')),patch('harmonia.track_bridge.context',side_effect=AssertionError('No catalog rebuild')),patch('harmonia.matching.Matcher.match_track',side_effect=AssertionError('No repeated matching')):
            update(self.cfg,self.playlist,self.tracks,self.library,{'name':'Mix'},ok)

    def read(self):
        response=api.playlist_detail_route('applemusic:mix',progress_id='')
        return response,json.loads(response.body)

    def test_immediate_fresh_page_uses_the_sync_work_and_correct_status(self):
        self.publish()
        response,data=self.read()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['X-Saved-View-Stale'],'false')
        self.assertFalse(data['tracks'][0]['needs_attention'])
        self.assertTrue(data['tracks'][1]['needs_attention'])
        self.assertEqual(data['playlist']['attention_tracks'],1)
        with self.repo.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_failed_verification_never_looks_resolved(self):
        self.publish(False)
        _,data=self.read()
        self.assertTrue(data['playlist']['sync_error'])
        self.assertTrue(data['tracks'][0]['needs_attention'])
        # A later successful sync clears the failure marker.
        self.publish(True)
        _,data=self.read()
        self.assertFalse(data['tracks'][0]['needs_attention'])

    def test_cancel_queued_rebuild_and_prevent_running_worker_overwrite(self):
        response=api.playlist_detail_route('applemusic:mix',progress_id='')
        self.assertEqual(response.status_code,202)
        queued=json.loads(response.body)['job_id']
        self.publish()
        with self.repo.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(queued,)).fetchone()[0],'cancelled')
            # A preparation worker uses this guarded write; publishing detaches it.
            count=db.execute("UPDATE prepared_views SET data='{}' WHERE job_id=?",(queued,)).rowcount
            self.assertEqual(count,0)
        _,data=self.read();self.assertEqual(data['tracks'][0]['status'],'Automatic')

    def test_other_account_cannot_read_published_page(self):
        self.publish()
        with accounts.as_user({'id':'2','owner':False,'admin':False}):
            response=api.playlist_detail_route('applemusic:mix',progress_id='')
            self.assertEqual(response.status_code,202)

    def test_failed_write_keeps_list_ready_to_sync_despite_previous_healthy_check(self):
        self.repo.put('playlist_sync_state', 'applemusic:mix', {'error':'Verification failed'})
        self.cfg.health = {'applemusic:mix':{'healthy':True,'source_tracks':1,'unresolved':0,'ignored':0,'matched_in_library':1}}
        self.cfg.health_attempts = {}
        self.cfg._pending_settings_view = {}
        self.cfg._match_drafts_view = []
        self.cfg._schedules_view = {}
        self.cfg._server_next = None
        payload = api._playlist_payload(self.cfg, self.playlist)
        self.assertFalse(payload['fully_matched'])
        self.assertTrue(payload['ready_to_sync'])
