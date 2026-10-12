import tempfile
import unittest
from pathlib import Path
from harmonia.storage import Repository
from harmonia.playlist_overlays import apply,entries
from harmonia.apple_music_source import AppleMusicAPI
from harmonia.sync_engine import Syncer

class SourceMetadataThroughOverlays(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name))
        self.track=AppleMusicAPI._track_from_item({'id':'one','name':'Song','artistName':'Singer & Guest','albumName':'Album','duration':210000})

    def test_unedited_source_survives_sync_snapshot_and_page_read(self):
        effective=apply(self.repo,'apple:playlist',[self.track])
        snapshot=Syncer._snapshot_entries(effective)
        self.repo.save({'source_snapshots':{'apple:playlist':snapshot}})
        displayed=apply(self.repo,'apple:playlist',self.repo.load('source_snapshots')['apple:playlist'])
        self.assertEqual(displayed[0]['duration'],210000)
        self.assertEqual(displayed[0]['artists'],[{'name':'Singer'},{'name':'Guest'}])

    def test_reordered_duplicate_occurrences_keep_their_source_metadata(self):
        source=[self.track,{**self.track,'source_id':'two','duration':245000},self.track]
        keys=[r['editor_id'] for r in entries(source)]
        self.repo.save({'playlist_overlays':{'apple:playlist':{'removed':[],'added':[],'order':[keys[2],keys[1],keys[0]]}}})
        displayed=apply(self.repo,'apple:playlist',source)
        self.assertEqual([r['source_id'] for r in displayed],['one','two','one'])
        self.assertEqual([r['duration'] for r in displayed],[210000,245000,210000])
        self.assertTrue(all(r['artists']==self.track['artists'] for r in displayed))

    def test_added_track_without_source_duration_does_not_borrow_original(self):
        original=entries([self.track])[0]
        self.repo.save({'playlist_overlays':{'apple:playlist':{'removed':[original['editor_id']],'added':[{'editor_id':'bridge:new','title':'Other recording','artist':'Singer','album':'Album','source_id':'two'}],'order':['bridge:new']}}})
        row=apply(self.repo,'apple:playlist',[self.track])[0]
        self.assertNotIn('duration',row)
        self.assertNotIn('artists',row)
