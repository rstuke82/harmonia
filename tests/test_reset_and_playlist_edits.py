import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile
from fastapi import HTTPException
from playlist_bridge import accounts,api,reset_guard,factory_reset
from playlist_bridge.storage import Repository
from playlist_bridge.jobs import Store
from playlist_bridge.playlist_overlays import Edit,entries,apply
import test_beta11 as fixtures

def endpoint(path,method):
    return next(r.endpoint for r in api.app.routes if getattr(r,'path','')==path and method in r.methods)

class PlaylistEdits(unittest.TestCase):
    setUp=fixtures.Beta11API.setUp
    def seed(self):
        self.key='applemusic:pl.audit'
        self.raw=[{'title':'Wrong','artist':'Artist','album':'Album','source_id':'provider-id','duration':120,'artists':['Artist','Guest']}]
        self.repo.save({'runtime':{'playlists':[{'source':'applemusic','source_id':'pl.audit'}]},'source_snapshots':{self.key:self.raw}})
        return entries(self.raw)
    def save(self,rows,revision=0):
        with accounts.as_user(self.admin):
            return endpoint('/api/playlists/{key}/editor','PUT')(self.key,Edit(revision=revision,rows=rows))
    def test_corrections_keep_provider_metadata_across_sync(self):
        rows=self.seed();rows[0].update(title='Correct',artist='Correct artist',album='Correct album')
        self.save(rows)
        changed=apply(self.repo,self.key,self.raw)[0]
        self.assertEqual(changed['title'],'Correct');self.assertEqual(changed['artist'],'Correct artist')
        self.assertEqual(changed['duration'],120);self.assertEqual(changed['artists'],['Artist','Guest'])
        upstream=[{**self.raw[0],'duration':125}]
        self.assertEqual(apply(self.repo,self.key,upstream)[0]['title'],'Correct')
        self.assertEqual(apply(self.repo,self.key,upstream)[0]['duration'],125)
        with accounts.as_user(self.admin):
            current=endpoint('/api/playlists/{key}/editor','GET')(self.key)
        self.save(current['rows'],current['revision'])
        self.assertEqual(apply(self.repo,self.key,upstream)[0]['title'],'Correct')
    def test_concurrent_saves_conflict_without_lost_committed_edit(self):
        rows=self.seed();barrier=threading.Barrier(2);original=Repository.load_one
        def synchronized(repo,namespace,key,default=None):
            result=original(repo,namespace,key,default)
            if namespace=='playlist_overlays' and key==self.key:barrier.wait(timeout=5)
            return result
        def submit(index):
            try:self.save([*rows,{'editor_id':f'bridge:{index}','origin':'bridge','title':f'Addition {index}','artist':'Artist'}]);return 202
            except HTTPException as exc:return exc.status_code
        with patch.object(Repository,'load_one',synchronized),ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(results),[202,409])
        self.assertEqual(self.repo.load_one('playlist_overlays',self.key)['revision'],1)
        self.assertEqual(len(self.repo.load_one('playlist_overlays',self.key)['added']),1)
        self.assertEqual(len(Store(self.repo).list()),1)
    def test_job_failure_rolls_back_edit(self):
        rows=self.seed();rows[0]['title']='Correct'
        with patch.object(Store,'enqueue',side_effect=RuntimeError('Queue unavailable')):
            with self.assertRaises(RuntimeError):self.save(rows)
        self.assertFalse(self.repo.load_one('playlist_overlays',self.key))
    def test_duplicate_rows_rejected_and_new_revision_gets_own_sync(self):
        rows=self.seed()
        with self.assertRaises(HTTPException):self.save(rows+rows)
        first=self.save(rows);rows[0]['title']='Correct';second=self.save(rows,1)
        self.assertNotEqual(first['id'],second['id'])
    def test_duplicate_occurrence_edit_does_not_change_other_occurrence(self):
        self.seed();self.raw*=2
        self.repo.save({'source_snapshots':{self.key:self.raw}})
        rows=entries(self.raw);rows[1]['title']='Second correction';self.save(rows)
        result=apply(self.repo,self.key,self.raw)
        self.assertEqual([r['title'] for r in result],['Wrong','Second correction'])
        self.assertEqual([r['duration'] for r in result],[120,120])
    def test_new_recording_does_not_inherit_old_duration(self):
        rows=self.seed();rows[0]['recording_id']='different';self.save(rows)
        self.assertNotIn('duration',apply(self.repo,self.key,self.raw)[0])

class FactoryReset(unittest.TestCase):
    setUp=fixtures.Beta11API.setUp
    http=fixtures.Beta11API.http
    def personal(self):
        with accounts.as_user(self.member):return accounts.personal_repository()
    def clean_guard(self):
        reset_guard.restart_required=False
    def reset(self):
        self.addCleanup(self.clean_guard)
        return factory_reset.execute(self.repo)
    def test_reset_all_account_state_preserves_backup_and_user_media(self):
        member=self.personal();member.save({'runtime':{'playlists':[{'name':'Private'}]},'sessions':{'secret':{'token':'secret'}}})
        self.repo.save({'artwork_overrides':{'a':{'url':'private'}}})
        for repo in (self.repo,member):
            with repo.connect() as db:db.execute("INSERT INTO prepared_views(key,name,params,permissions,data) VALUES('old','library','{}','{}','{}')")
            cache=repo.directory/'plex-image-cache';cache.mkdir();(cache/'old.bin').write_bytes(b'private')
        media=member.directory/'song.flac';media.write_bytes(b'user media')
        result=self.reset()
        for repo in (self.repo,member):
            self.assertFalse(repo.load('runtime'));self.assertFalse(repo.load('sessions'))
            with repo.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM prepared_views').fetchone()[0],0)
            self.assertFalse((repo.directory/'plex-image-cache').exists())
        self.assertEqual(media.read_bytes(),b'user media')
        with ZipFile(self.repo.directory/'backups'/result['backup']) as z:
            self.assertIn('users/'+member.directory.name+'/playlist-bridge.db',z.namelist())
        self.assertTrue(reset_guard.restart_required)
    def test_disabled_member_jobs_and_jobs_beyond_list_limit_block_reset(self):
        member=self.personal();Store(member).enqueue('sync',{})
        self.repo.save({'accounts':{self.member['id']:{**self.member,'disabled':True}}})
        with self.assertRaises(HTTPException) as error:self.reset()
        self.assertEqual(error.exception.status_code,409)
        self.assertFalse((self.repo.directory/'backups').exists())
        with member.connect() as db:db.execute("UPDATE jobs SET status='completed'")
        for _ in range(205):
            job=Store(self.repo).enqueue('sync',{'unique':_});Store(self.repo).update(job,status='completed')
        Store(self.repo).enqueue('sync',{'pending':True})
        with self.assertRaises(HTTPException):self.reset()
    def test_http_barrier_and_restart_recovery(self):
        member=self.personal();member.save({'runtime':{'playlists':[{'name':'Old'}]}})
        self.addCleanup(self.clean_guard)
        with patch('playlist_bridge.factory_reset.finish',side_effect=OSError('Interrupted')):
            with self.assertRaises(OSError):self.reset()
        self.assertTrue((self.repo.directory/factory_reset.MARKER).exists())
        self.assertFalse(reset_guard.enter_request())
        factory_reset.recover(self.repo)
        self.assertFalse(member.load('runtime'));self.assertFalse((self.repo.directory/factory_reset.MARKER).exists())
        self.assertTrue(reset_guard.enter_request());reset_guard.leave_request()
    def test_other_requests_and_live_worker_block_reset(self):
        reset_guard.enter_request();reset_guard.enter_request()
        try:
            with self.assertRaises(HTTPException):self.reset()
        finally:reset_guard.leave_request();reset_guard.leave_request()
        with self.assertRaises(HTTPException):factory_reset.execute(self.repo,SimpleNamespace(workers=[{'thread':SimpleNamespace(is_alive=lambda:True)}]))
    def test_failed_backup_does_not_clear_data(self):
        member=self.personal();member.save({'runtime':{'playlists':[{'name':'Keep'}]}})
        with patch('playlist_bridge.backups.create',side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):self.reset()
        self.assertEqual(member.load('runtime')['playlists'][0]['name'],'Keep')
    def test_http_reset_blocks_subsequent_api_until_restart(self):
        self.addCleanup(self.clean_guard)
        status,result=asyncio.run(self.http('/api/database/factory-reset','POST',user=self.admin,body={'confirmation':'RESET HARMONIA'}))
        self.assertEqual(status,200);self.assertTrue(result['reset'])
        status,_=asyncio.run(self.http('/api/auth/me'))
        self.assertEqual(status,503)
        factory_reset.recover(self.repo)

if __name__=='__main__':unittest.main()
