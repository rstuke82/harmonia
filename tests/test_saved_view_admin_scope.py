import json,unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_30
from playlist_bridge import accounts,saved_views,jobs

class AdminScope(unittest.TestCase):
    setUp=test_30.MultiUser.setUp
    def prepare(self,server=True,change=None):
        user={**self.a,'admin':True,'owner':False}
        accounts.put('accounts',user['id'],user)
        name='F07 regression'
        self.addCleanup(lambda:saved_views._providers.pop(name,None))
        self.addCleanup(lambda:saved_views._readers.pop(name,None))
        with patch.object(saved_views,'root_repository',return_value=self.repo):
            @saved_views.saved(name,{'inventory'})
            def provider():return {'directory':str(accounts.personal_repository().directory),'server':bool(accounts.actor().get('server_context'))}
            with accounts.as_user({**user,'server_context':True} if server else user):
                response=provider();repo=accounts.personal_repository()
            ident=json.loads(response.body)['job_id'];store=jobs.Store(repo)
            if change:accounts.put('accounts',user['id'],{**user,**change})
            with patch.object(jobs,'current',return_value=SimpleNamespace(store=store,id=ident,action='prepare_views',user_id=user['id'])),patch.object(jobs,'progress'):
                result=saved_views.execute(store.get(ident)['payload'])
            with repo.connect() as db:
                row=db.execute('SELECT * FROM prepared_views WHERE job_id=?',(ident,)).fetchone()
                columns=[c[1] for c in db.execute('PRAGMA table_info(prepared_views)')];row=dict(zip(columns,row))
            return row,result,repo
    def test_delegated_admin_server_context_reconstructed(self):
        row,_,repo=self.prepare()
        data=json.loads(row['data']);self.assertEqual(data['directory'],str(self.repo.directory));self.assertTrue(data['server'])
    def test_personal_admin_context_not_elevated(self):
        row,_,repo=self.prepare(False)
        data=json.loads(row['data']);self.assertEqual(data['directory'],str(repo.directory));self.assertFalse(data['server']);self.assertNotEqual(str(repo.directory),str(self.repo.directory))
    def test_admin_revocation_discards_server_work(self):
        row,result,_=self.prepare(change={'admin':False})
        self.assertIsNone(row['data']);self.assertIn('permission changed',result['summary'])
    def test_disabled_account_discards(self):
        row,_,_=self.prepare(change={'disabled':True});self.assertIsNone(row['data'])
    def test_projection_identity_includes_scope(self):
        user={**self.a,'admin':True,'owner':False}
        self.assertNotEqual(saved_views.permissions(user),saved_views.permissions({**user,'server_context':True}))
