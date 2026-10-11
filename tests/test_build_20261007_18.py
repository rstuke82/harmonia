import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playlist_bridge import jobs,api,admin_activity,accounts
from playlist_bridge.storage import Repository

class InternalPolling(unittest.TestCase):
    def test_polling_hidden_for_admin_member_and_filtered_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository(Path(tmp));store=jobs.Store(repo)
            hidden=store.enqueue('request_status',{});visible=store.enqueue('lidarr_search',{'album_id':'fixture'})
            owner={'id':'owner','name':'Owner','admin':True}
            with patch('playlist_bridge.api.job_store',return_value=store),patch('playlist_bridge.admin_activity.stores',return_value=[(owner,store)]):
                for user in (owner,{'id':'member','admin':False}):
                    with accounts.as_user(user):self.assertEqual([r['id'] for r in api.list_jobs()],[visible])
                for worker in ('','downloads'):
                    result=admin_activity.history(worker=worker)
                    self.assertEqual(result['total'],1)
                    self.assertEqual([r['id'] for r in result['rows']],[visible])
            self.assertEqual(store.get(hidden)['action'],'request_status')
            self.assertEqual(jobs.worker_for('request_status'),'downloads')

if __name__=='__main__':unittest.main()
