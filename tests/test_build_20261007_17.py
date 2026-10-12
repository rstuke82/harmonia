import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from harmonia import saved_views
from harmonia.storage import Repository

class DiscoverRevisions(unittest.TestCase):
    def test_playlist_health_does_not_invalidate_discover(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository(Path(tmp))
            with patch('harmonia.saved_views.root_repository',return_value=repo):
                before=saved_views.revision(repo,saved_views.DISCOVER)
                repo.save({'health_history':{'playlist':{'healthy':True}},'mapping':{'playlist':{}},'match_queue':{'track':{}}})
                self.assertEqual(before,saved_views.revision(repo,saved_views.DISCOVER))
                repo.save({'lastfm_recommendations':{'user':{'rows':[]}}})
                after=saved_views.revision(repo,saved_views.DISCOVER)
                self.assertNotEqual(before,after)
                repo.save({'inventory':{'lidarr':{'rows':[]}}})
                self.assertNotEqual(after,saved_views.revision(repo,saved_views.DISCOVER))

if __name__=='__main__':unittest.main()
