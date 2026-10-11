import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playlist_bridge import tasks, accounts, saved_views
from playlist_bridge.storage import Repository
from playlist_bridge.jobs import Store

class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name))
        self.store=Store(self.repo)
        tasks.setup(self.store)

    def write(self,key,value):
        with self.repo.connect() as db:db.execute("INSERT OR REPLACE INTO state VALUES('task_startup',?,?)",(key,json.dumps(value)))

    def startup(self,checked=None,configured=True):
        with patch.object(accounts,'root_repository',return_value=self.repo),patch.object(accounts,'users',return_value={}),patch.object(accounts,'member_stores',return_value=[]),patch.object(tasks,'startup_check',return_value=(configured,checked)),patch.object(saved_views,'startup_pages') as pages:
            ids=tasks.startup(self.store)
            return ids,pages.call_count

    def test_fresh_inventory_and_history_do_not_scan_on_restart(self):
        import time
        ids,pages=self.startup(time.time())
        self.assertEqual(ids,[])
        self.assertEqual(pages,1)

    def test_missing_inventory_queues_once_without_heavy_default_tasks(self):
        ids,_=self.startup()
        self.assertEqual({self.store.get(i)['action'] for i in ids},{'plex_scan','lidarr_scan','lastfm_refresh'})
        self.assertTrue(all(self.store.get(i)['payload']['trigger_label']=='At startup' for i in ids))
        self.assertEqual(self.startup()[0],[])

    def test_old_inventory_retained_until_scheduled_scan_but_history_refreshes(self):
        ids,_=self.startup(1)
        self.assertEqual([self.store.get(i)['action'] for i in ids],['lastfm_refresh'])
        self.write('plex_scan','outdated')
        self.assertEqual([self.store.get(i)['action'] for i in self.startup(1)[0]],['plex_scan'])

    def test_unconfigured_services_and_disabled_startup_skip(self):
        self.assertEqual(self.startup(configured=False)[0],[])
        for action in tasks.STARTUP_DEFAULTS:self.write(action,'never')
        self.write('page_views','never')
        self.assertEqual(self.startup(),([],0))

    def test_schedule_edits_preserve_startup_and_defaults_restore_it(self):
        schedule=next(s for s in self.store.schedules() if s['action']=='plex_scan')
        data={'action':'plex_scan','scope':'all','hours':3,'start_time':'02:30','startup':'always'}
        self.store.save_schedule(data,schedule['id'])
        self.store.save_schedule({k:v for k,v in data.items() if k!='startup'},schedule['id'])
        self.assertEqual(self.repo.load('task_startup')['plex_scan'],'always')
        with patch('playlist_bridge.api._config') as cfg:
            cfg.return_value.config={'playlists':[]}
            tasks.restore_defaults(self.store,'plex_scan')
        self.assertEqual(self.repo.load('task_startup')['plex_scan'],'missing')
        with self.assertRaises(ValueError):self.store.save_schedule(data|{'startup':'bad'},schedule['id'])

    def test_changed_service_identity_counts_as_missing_and_empty_scan_is_valid(self):
        from types import SimpleNamespace
        from playlist_bridge.inventory import identity
        cfg={'url':'http://plex','token':'secret','music_library_key':'1'}
        key=identity(SimpleNamespace(base_url=cfg['url'],music_library_key='1',headers={'X-Plex-Token':'secret','Accept':'application/json'}))
        with self.repo.connect() as db:db.execute("INSERT INTO state VALUES('inventory','plex',?)",(json.dumps({'identity':key,'checked_at':123,'rows':[]}),))
        with patch.object(accounts,'root_repository',return_value=self.repo),patch('playlist_bridge.storage.read_json',return_value={'plex':cfg}):
            self.assertEqual(tasks.startup_check(self.store,'plex_scan'),(True,123))
            with patch('playlist_bridge.inventory.identity',return_value='changed'):
                self.assertEqual(tasks.startup_check(self.store,'plex_scan'),(True,None))

    def test_warm_saved_page_stays_available_and_force_reuses_active_job(self):
        name='library directory v16'
        old=saved_views._readers.get(name);provider=saved_views._providers.get(name)
        self.addCleanup(lambda: saved_views._readers.pop(name,None) if old is None else saved_views._readers.__setitem__(name,old))
        self.addCleanup(lambda: saved_views._providers.pop(name,None) if provider is None else saved_views._providers.__setitem__(name,provider))
        @saved_views.saved(name,{'inventory'})
        def directory(view='full'):raise AssertionError('Startup/read must not execute preparation')
        with patch.object(saved_views,'personal_repository',return_value=self.repo),patch.object(saved_views,'root_repository',return_value=self.repo),patch.object(saved_views,'actor',return_value=None):
            saved_views.startup_pages(self.repo)
            response=directory(view='directory')
            self.assertEqual(response.status_code,202)
            with self.repo.connect() as db:db.execute("UPDATE prepared_views SET data=?",(json.dumps({'artists':[{'name':'Saved artist'}]}),))
            saved_views.startup_pages(self.repo,'always')
            response=directory(view='directory')
            self.assertEqual(json.loads(response.body)['artists'][0]['name'],'Saved artist')
            key=response.headers['x-saved-view']
            self.assertEqual(sum(j['payload'].get('key')==key for j in self.store.list()),1)

    def test_members_use_their_own_history_freshness_and_preserve_schedule(self):
        member_repo=Repository(Path(self.tmp.name)/'member');member_store=Store(member_repo)
        user={'id':'12','plex_token':'token','can_playlists':False}
        with patch.object(accounts,'root_repository',return_value=self.repo),patch.object(accounts,'users',return_value={}),patch.object(accounts,'member_stores',return_value=[(user,member_store)]),patch.object(tasks,'startup_check',side_effect=lambda target,action:(target is member_store,None)),patch.object(saved_views,'startup_pages'):
            original=self.store.schedules()
            tasks.startup(self.store)
            self.assertEqual([j['action'] for j in member_store.list()],['lastfm_refresh'])
            self.assertEqual(self.store.schedules(),original)
            tasks.startup(self.store)
            self.assertEqual(len(member_store.list()),1)
