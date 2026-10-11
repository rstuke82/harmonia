import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from playlist_bridge import jobs
from playlist_bridge.storage import Repository

class ActivityProgress(unittest.TestCase):
    def test_retarget_refreshes_index_and_clears_previous_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            store=jobs.Store(Repository(Path(directory)))
            jid=store.enqueue('sync',{'scope':'all'})
            with patch.object(jobs,'current',return_value=SimpleNamespace(store=store,id=jid)), patch.object(jobs._local,'target',None,create=True), patch.object(store,'event'):
                jobs.target('one','Old name',1,13)
                jobs.activity(mode='completed',completed=90,total=100,service='Plex')
                jobs.target('one','New name',7,13)
                lane=store.get(jid)['activity']['lanes']['one']
                self.assertEqual((lane['name'],lane['playlist_index'],lane['playlist_total']),('New name',7,13))
                self.assertEqual(lane['mode'],'working')
                self.assertIsNone(lane['completed'])
                self.assertIsNone(lane['total'])
                self.assertIsNone(lane['service'])
                jobs.target('two','Two',8,13)
                self.assertEqual(store.get(jid)['activity']['lanes']['one']['mode'],'working')
