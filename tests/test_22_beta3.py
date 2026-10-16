import tempfile,unittest,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from contextlib import nullcontext
from harmonia.storage import Repository
from harmonia.jobs import Store
from harmonia import tasks,sync_policy,playlist_schedules,queued_settings,lidarr_downloads
from harmonia.lidarr import state_put
from harmonia.lidarr_requests import server_id
from harmonia.playlist_description import render,MARKER

class Beta3(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(Path(self.tmp.name));self.store=Store(self.repo);tasks.setup(self.store)
  self.playlists=[{'source':'spotify','source_id':str(i),'plex_playlist_id':str(i),'plex_playlist_name':'Heavy Rotation','auto_sync':i!=2} for i in [1,2,3]]
  self.repo.save({'runtime':{'playlists':self.playlists}})
 def test_upgrade_preserves_disabled_and_custom(self):
  state_put(self.repo,'playlist_schedules','spotify:3',{'mode':'custom','days':[0],'hour':2,'minute':0,'timezone':'UTC','next_run':'2099-01-01T00:00:00+00:00'})
  sync_policy.migrate(self.store);sync_policy.migrate(self.store)
  modes=self.repo.load('playlist_schedules')
  self.assertEqual([modes['spotify:'+str(i)]['mode'] for i in [1,2,3]],['inherit','disabled','custom'])
  self.assertEqual(len(sync_policy.eligible(self.repo,self.playlists)),1)
  self.assertTrue(next(s for s in self.store.schedules() if s['action']=='sync')['enabled'])
 def test_upgrade_scoped_schedules_retired_history_retained(self):
  with self.repo.connect() as db:
   db.execute("INSERT INTO schedules(id,name,action,scope,cron,timezone,enabled,next_run) VALUES('old','old','sync','automatic','0 */3 * * *','UTC',1,'2099-01-01')")
   db.execute("INSERT INTO schedules(id,name,action,scope,cron,timezone,enabled,next_run) VALUES('health-old','old','health','favorites','0 */1 * * *','UTC',1,'2099-01-01')")
  jid=self.store.enqueue('health',{'scope':'favorites'});self.store.update(jid,status='completed')
  sync_policy.migrate(self.store)
  items=self.store.schedules();server=next(s for s in items if s['action']=='sync' and s['scope']=='all')
  self.assertTrue(server['enabled']);self.assertEqual(server['cron'],tasks.expression(24,'02:00'))
  self.assertFalse(next(s for s in items if s['id']=='health-old')['enabled']);self.assertEqual(self.store.get(jid)['status'],'completed')
  self.assertTrue(self.repo.load('tasks')['sync_modes_v3']['notes'])
 def test_global_and_custom_due_same_tick(self):
  sync_policy.migrate(self.store)
  state_put(self.repo,'playlist_schedules','spotify:3',{'mode':'custom','days':[0],'hour':2,'minute':0,'timezone':'UTC','next_run':'2000-01-01T00:00:00+00:00'})
  with self.repo.connect() as db:db.execute("UPDATE schedules SET enabled=1,next_run='2000-01-01T00:00:00+00:00' WHERE action='sync' AND scope='all'")
  config=SimpleNamespace(config={'playlists':self.playlists})
  with patch('harmonia.api._config',return_value=config):
   self.store.due();playlist_schedules.due(self.store)
  with self.repo.connect() as db:jobs=[json.loads(r[0]) for r in db.execute("SELECT payload FROM jobs WHERE action='sync'")]
  self.assertEqual(len(jobs),2);self.assertEqual({tuple(j['playlist_keys']) for j in jobs},{('spotify:1',),('spotify:3',)})
 def test_rename_is_latest_value_and_keeps_identity(self):
  config=SimpleNamespace(config={'playlists':self.playlists},save=Mock())
  plex=SimpleNamespace(base_url='http://plex',headers={})
  with patch('harmonia.api._config',return_value=config),patch('harmonia.api.job_store',return_value=self.store),patch('harmonia.api._health_plex',return_value=plex),patch('harmonia.process_lock.ProcessLock',return_value=nullcontext()),patch('harmonia.network.put') as put:
   first=queued_settings.enqueue(['spotify:1'],{'name':'First'})
   last=queued_settings.enqueue(['spotify:1'],{'name':'Heavy Rotation — Ryan'})
   self.assertEqual(first['job_id'],last['job_id']);queued_settings.execute()
   self.assertEqual(put.call_args.kwargs['params'],{'title':'Heavy Rotation — Ryan'})
  self.assertEqual(self.playlists[0]['plex_playlist_id'],'1');self.assertEqual(self.playlists[1]['plex_playlist_name'],'Heavy Rotation')
  self.assertEqual(self.playlists[0]['custom_name'],'Heavy Rotation — Ryan');self.assertFalse(self.repo.load('pending_playlist_settings'))
 def test_description_replaces_summary(self):
  p={'source_url':'https://example.com/playlist','last_synced':'2026-09-24T12:00:00+00:00'}
  first=render('Source notes',p,100,90,8,2);second=render(first,p,100,95,3,2)
  self.assertEqual(second.count(MARKER),1);self.assertTrue(second.startswith('Source notes'))
  self.assertIn('95 matched',second);self.assertNotIn('90 matched',second)
 def test_health_scope_retains_selection(self):
  from harmonia.api import validated_payload
  with self.assertRaises(ValueError):validated_payload('health',{'scope':'favorites'})
  config=SimpleNamespace(config={'playlists':self.playlists})
  with patch('harmonia.api._config',return_value=config):
   self.assertEqual(validated_payload('health',{'scope':'selected','playlist_keys':['spotify:1']}),{'scope':'selected','playlist_keys':['spotify:1']})
 def test_manual_sync_includes_custom_and_health_retains_scope(self):
  from harmonia.api import _execute_job
  config=SimpleNamespace(config={'playlists':self.playlists},repository=self.repo)
  sync_policy.migrate(self.store)
  state_put(self.repo,'playlist_schedules','spotify:3',{'mode':'custom'})
  with patch('harmonia.api._config',return_value=config),patch('harmonia.api.sync_one',return_value={'summary':{}}) as sync:
   _execute_job('sync',{'scope':'all'})
   self.assertEqual(sync.call_count,3)
  with patch('harmonia.api._config',return_value=config),patch('harmonia.api._health_batch',return_value={}) as health:
   _execute_job('health',{'scope':'selected','playlist_keys':['spotify:1']})
   self.assertEqual(len(health.call_args.args[0]),1)
   _execute_job('health',{'scope':'all'})
   self.assertEqual(len(health.call_args.args[0]),3)
 def test_lidarr_removed_and_readded_relinked(self):
  cfg={'url':'http://lidarr','api_key':'secret','enabled':True}
  state_put(self.repo,'lidarr_requests','mbid',{'server':server_id(cfg),'lidarr_id':1546,'title':'The Power','status':'added','sources':[]})
  client=Mock();client.call.side_effect=lambda method,path,**kw:{'records':[]} if path=='queue' else []
  with patch('harmonia.lidarr.Client',return_value=client),patch('harmonia.api._record_log'):
   lidarr_downloads.snapshot(self.repo,cfg,force=True)
   r=self.repo.load('lidarr_requests')['mbid'];self.assertIsNone(r['lidarr_id']);self.assertEqual(r['lidarr_url'],'');self.assertEqual(r['status'],'not_in_lidarr')
   client.call.side_effect=lambda method,path,**kw:{'records':[]} if path=='queue' else [{'id':2000,'foreignAlbumId':'mbid','statistics':{}}]
   lidarr_downloads.snapshot(self.repo,cfg,force=True)
   self.assertEqual(self.repo.load('lidarr_requests')['mbid']['lidarr_id'],2000)

if __name__=='__main__':unittest.main()
