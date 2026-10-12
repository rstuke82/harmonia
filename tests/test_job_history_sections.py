import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from harmonia import jobs, accounts, admin_activity, api
from harmonia.storage import Repository


class JobHistorySections(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name));self.store=jobs.Store(self.repo)
        with self.repo.connect() as db:
            for i in range(60):
                db.execute("INSERT INTO jobs(id,action,payload,status,progress,created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                           (f'done-{i:02}', 'sync','{}','failed' if i==59 else 'completed','Finished',f'2026-10-09T00:00:{i:02}',f'2026-10-09T00:01:{i:02}',f'2026-10-09T00:02:{i:02}'))
            for ident,status,created,action in [('q-old','queued','2026-10-08T01:00:00','sync'),('q-new','queued','2026-10-09T01:00:00','health'),('running','running','2026-10-08T00:00:00','sync'),('stopping','cancelling','2026-10-08T00:01:00','sync'),('hidden','queued','2026-10-08T00:00:00','prepare_views')]:
                db.execute('INSERT INTO jobs(id,action,payload,status,progress,created_at) VALUES(?,?,?,?,?,?)',(ident,action,'{}',status,'Active',created))

    def test_member_keeps_active_jobs_visible_beyond_completed_page(self):
        endpoint=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/job-history')
        with accounts.as_user({'id':'2','admin':False}),patch('harmonia.accounts.personal_repository',return_value=self.repo):
            result=endpoint(offset=50,limit=50,owner='',worker='',sections=True)
        self.assertEqual(result['total'],60)
        self.assertEqual(len(result['rows']),10)
        self.assertEqual(result['rows'][0]['id'],'done-09')
        self.assertEqual([r['id'] for r in result['active']],['running','stopping','q-old','q-new'])
        self.assertTrue(all(r['status'] not in ('queued','running','cancelling') for r in result['rows']))

    def test_admin_filters_active_and_completed_across_owner_stores(self):
        owner={'id':'owner','name':'Owner'};member={'id':'member','name':'Member'}
        with tempfile.TemporaryDirectory() as folder:
            other=jobs.Store(Repository(Path(folder)))
            other.enqueue('health',{'scope':'all'})
            with patch('harmonia.admin_activity.stores',return_value=[(owner,self.store),(member,other)]):
                result=admin_activity.history(offset=0,limit=1,owner='owner',worker='playlist',sections=True)
                self.assertEqual(result['total'],60)
                self.assertEqual(result['rows'][0]['id'],'done-59')
                self.assertEqual([r['id'] for r in result['active']],['running','stopping','q-old'])
                self.assertTrue(all(r['owner_id']=='owner' for r in result['active']+result['rows']))
                member_result=admin_activity.history(owner='member',sections=True)
                self.assertEqual(member_result['total'],0)
                self.assertEqual(len(member_result['active']),1)

    def test_completed_order_uses_finish_time_instead_of_queue_time(self):
        with self.repo.connect() as db:db.execute("UPDATE jobs SET finished_at='2026-10-10T00:00:00' WHERE id='done-00'")
        with patch('harmonia.admin_activity.stores',return_value=[({'id':'owner'},self.store)]):
            result=admin_activity.history(limit=1,sections=True)
        self.assertEqual(result['rows'][0]['id'],'done-00')
