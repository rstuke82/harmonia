import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harmonia import accounts
from harmonia.jobs import Store
from harmonia.metadata_sources import Preferences, SOURCE_DEFAULTS
from harmonia.storage import Repository


class Beta13(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.repo=Repository(self.path)

    def test_structured_diagnostics_and_job_lifecycle(self):
        self.repo.add_log('WARNING','Plex request','Timed out',worker='library',service='Plex',request_id='req-1')
        job=Store(self.repo).enqueue('plex_scan',{'trigger_label':'test'})
        Store(self.repo).claim(job)
        rows=self.repo.logs(worker='library')
        self.assertTrue(any(row['request_id']=='req-1' and row['service']=='Plex' for row in rows))
        self.assertTrue(any(row['job_id']==job and row['message'].startswith('Queued') for row in rows))
        self.assertTrue(any(row['job_id']==job and row['message'].startswith('Worker claimed') for row in rows))
        self.assertIn('library',self.repo.log_facets()['workers'])

    def test_debug_events_follow_debug_setting(self):
        self.repo.add_log('DEBUG','HTTP','hidden')
        self.assertFalse(any(row['level']=='DEBUG' for row in self.repo.logs()))
        self.repo.save({'console_settings':{'debug':True}})
        self.repo.add_log('DEBUG','HTTP','visible')
        self.assertTrue(any(row['level']=='DEBUG' and row['message']=='visible' for row in self.repo.logs()))
        self.repo.save({'console_settings':{'debug':False}})
        self.repo.clear_debug_logs()
        self.assertFalse(any(row['level']=='DEBUG' for row in self.repo.logs()))

    def test_metadata_defaults_no_longer_use_lidarr(self):
        self.assertEqual(SOURCE_DEFAULTS['release_date'],['musicbrainz','apple'])
        self.assertEqual(Preferences(source_order={'artist_hero':['lidarr','plex']}).source_order['artist_hero'],['fanart','plex'])
        self.assertNotIn('lidarr',SOURCE_DEFAULTS['album_artwork'])

    def test_first_owner_configures_plex_before_account_creation(self):
        profile={'id':11,'username':'Owner','email':'owner@example.test'}
        resource={'clientIdentifier':'machine','provides':'server','accessToken':'server-token','owned':True,
                  'connections':[{'local':True,'uri':'http://plex.local:32400'}]}
        libraries={'MediaContainer':{'Directory':[{'key':'7','type':'artist','title':'Music'}]}}
        with patch('harmonia.accounts.root_repository',return_value=self.repo),patch.object(
                accounts,'plex_json',side_effect=[profile,[resource],libraries,libraries]):
            from harmonia.login_security import initialize_setup
            initialize_setup(self.repo)
            grant=self.repo.load_one('installation','setup')['key_hash']
            user=accounts.verify('claim-token',setup_hash=grant)
        self.assertTrue(user['admin'])
        config=json.loads((self.path/'config.json').read_text())
        self.assertEqual(config['plex']['url'],'http://plex.local:32400')
        self.assertEqual(config['plex']['music_library_key'],'7')


if __name__=='__main__':
    unittest.main()
