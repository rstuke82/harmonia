import hashlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from playlist_bridge import local_library as lib
from playlist_bridge.storage import Repository
from playlist_bridge.musicbrainz_settings import Preferences, ordered_releases

RID='11111111-1111-1111-1111-111111111111'
GID='22222222-2222-2222-2222-222222222222'
TID='33333333-3333-3333-3333-333333333333'
REC='44444444-4444-4444-4444-444444444444'

class LocalLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.music=self.base/'music';self.music.mkdir()
        self.repo=Repository(self.base/'state')
        p=patch.object(lib,'repo',return_value=self.repo);p.start();self.addCleanup(p.stop)
        lib.put('local_library_settings','current',{'enabled':True,'roots':[{'path':str(self.music),'plex_path':'/plex/music'}]})
    def test_scan_is_read_only_and_reuses_unchanged_tags(self):
        p=self.music/'test.wav'
        with wave.open(str(p),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\0'*16000)
        before=hashlib.sha256(p.read_bytes()).hexdigest()
        result=lib.scan();self.assertIn('1 local audio files',result['summary'])
        row=lib.snapshot()['tracks'][0];self.assertEqual(row['plex_file'],'/plex/music/test.wav')
        with patch.object(lib,'read_tags',side_effect=AssertionError('Unchanged file reread')):lib.scan()
        self.assertEqual(before,hashlib.sha256(p.read_bytes()).hexdigest())
    def test_failed_root_retains_previous_scan_and_skips_symlinks(self):
        target=self.base/'outside.mp3';target.write_bytes(b'not music')
        (self.music/'link.mp3').symlink_to(target)
        lib.scan();self.assertEqual(lib.snapshot()['tracks'],[])
        before=lib.snapshot();(self.music/'link.mp3').unlink();self.music.rmdir()
        with self.assertRaises(ValueError):lib.scan()
        self.assertEqual(before,lib.snapshot())
    def test_preferences_rank_editions_without_excluding(self):
        p=Preferences(preferred_countries=[' us ','XE'],preferred_formats=['Digital Media','CD']).model_dump()
        rows=[{'id':'jp','country':'JP','status':'Official','media':[{'format':'CD'}]}, {'id':'eu','country':'XE','status':'Official','media':[{'format':'Digital Media'}]}, {'id':'uscd','country':'US','status':'Official','media':[{'format':'CD'}]}, {'id':'us','country':'US','status':'Official','media':[{'format':'Digital Media'}]}]
        self.assertEqual([r['id'] for r in ordered_releases(rows,p)],['us','uscd','eu','jp'])
        with self.assertRaises(ValueError):Preferences(preferred_countries=['USA'])
    def test_reviewed_identity_does_not_survive_file_change(self):
        file=str(self.music/'track.flac');row={'id':lib.digest(file),'file':file,'title':'Song','artist':'Artist','album':'Album','album_artist':'Artist','signature':[12,123]}
        lib.put('local_inventory',lib.scope(lib.settings()),{'tracks':[row]})
        lib.put('local_track_links',row['id'],{'signature':[12,123],'release_id':RID,'release_track_id':TID,'recording_id':REC,'track_number':'1','disc_number':'1'})
        self.assertEqual(lib.resolved_tracks()[0]['recording_id'],REC)
        lib.put('local_inventory',lib.scope(lib.settings()),{'tracks':[{**row,'signature':[13,124]}]})
        self.assertFalse(lib.resolved_tracks()[0].get('recording_id'))
    def test_completeness_requires_identity_not_count(self):
        import time
        from types import SimpleNamespace
        file=str(self.music/'track.flac')
        row={'id':lib.digest(file),'file':file,'title':'Song','artist':'Artist','album':'Album','album_artist':'Artist','album_id':GID,'release_id':RID,'track_number':'1','disc_number':'1','recording_id':REC,'signature':[12,123]}
        release={'id':RID,'title':'Album','release-group':{'id':GID},'media':[{'position':1,'tracks':[{'id':TID,'position':1,'recording':{'id':'wrong'}}]}]}
        lib.put('local_inventory',lib.scope(lib.settings()),{'checked_at':time.time(),'tracks':[row]})
        lib.put('local_releases',RID,release)
        plex={'checked_at':time.time(),'rows':[{'plex_id':'p','files':[file],'title':'Song','artist':'Artist','album':'Album'}]}
        with patch('playlist_bridge.api._config',return_value=SimpleNamespace(repository=self.repo)),patch('playlist_bridge.inventory.current',return_value=(plex,{})):
            self.assertEqual(lib.native_view()['rows'][0]['status'],'Partial')
            release['media'][0]['tracks'][0]['recording']['id']=REC
            lib.put('local_releases',RID,release)
            self.assertEqual(lib.native_view()['rows'][0]['status'],'Available')

if __name__=='__main__':unittest.main()
