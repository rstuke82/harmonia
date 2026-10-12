import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch

from harmonia.album_finalize import _plex_folders,_scan_paths,_wait_for_plex_album
from harmonia.storage import Repository


class AlbumFinalizeTests(unittest.TestCase):
    def test_targeted_refresh_uses_parent_folder_strings(self):
        tracks=[{'file':'/lidarr/Artist/Album/01 Song.flac'},{'file':'/lidarr/Artist/Album/02 Song.flac'}]
        with patch('harmonia.file_tools.mapped',side_effect=lambda value,*args,**kwargs:value.replace('/lidarr','/plex')):
            self.assertEqual(_plex_folders(tracks,{}),['/plex/Artist/Album'])

    def test_targeted_refresh_updates_local_file_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            song=Path(directory)/'01 Song.flac';song.write_bytes(b'audio')
            repo=Repository(Path(directory))
            tracks=[{'file':'/lidarr/Artist/Album/01 Song.flac','title':'Song','artist':'Artist','album':'Album','album_id':'rgid'}]
            with patch('harmonia.file_tools.settings',return_value={'audit_enabled':True,'mappings':[]}), \
                 patch('harmonia.file_tools.mapped',return_value=str(song)), \
                 patch('harmonia.file_audit.scope',return_value='library'), \
                 patch('harmonia.file_audit.signature',return_value='signature'), \
                 patch('harmonia.local_library.read_tags',return_value={'title':'Song','artist':'Artist','album':'Album'}), \
                 patch('harmonia.jobs.progress'),patch('harmonia.jobs.output'):
                result=_scan_paths(repo,{},tracks)
            self.assertEqual(result,{'checked':1,'missing':0,'unmapped':0,'disabled':False})
            self.assertIn(str(song),repo.load('file_audit')['library']['files'])

    def test_targeted_refresh_rechecks_album_until_plex_indexes_it(self):
        plex=Mock()
        plex.search_library_subset.side_effect=[[],[{'album':'Album','album_artist':'Artist','plex_id':'1'}]]
        with patch('harmonia.album_finalize.time.sleep') as sleep,patch('harmonia.album_finalize.jobs.progress'),patch('harmonia.album_finalize.jobs.current',return_value=None):
            found=_wait_for_plex_album(plex,{'title':'Album'},{'artistName':'Artist'},True,(0,2,4))
        self.assertEqual(found[0]['plex_id'],'1');self.assertEqual(plex.search_library_subset.call_count,2);sleep.assert_called_once_with(2)

    def test_no_mapped_refresh_does_not_hold_worker_for_retries(self):
        plex=Mock();plex.search_library_subset.return_value=[]
        with patch('harmonia.album_finalize.time.sleep') as sleep:
            self.assertEqual(_wait_for_plex_album(plex,{'title':'Album'},{'artistName':'Artist'},False),[])
        sleep.assert_not_called();plex.search_library_subset.assert_called_once_with('Album')


if __name__=='__main__':unittest.main()
