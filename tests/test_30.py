from harmonia.config import Config
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from harmonia import accounts, api, library, lidarr
from harmonia.storage import Repository
from harmonia.jobs import Store, Manager


class MultiUser(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.settings={'plex':{'url':'http://plex','token':'OWNER_SECRET','music_library_key':'1'}}
        (self.path/'config.json').write_text(json.dumps(self.settings))
        self.repo=Repository(self.path)
        for target,value in [('harmonia.accounts.root_repository',self.repo),('harmonia.discover.root_repository',self.repo)]:
            p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)
        p=patch('harmonia.config.CONFIG_FILE',self.path/'config.json');p.start();self.addCleanup(p.stop)
        self.repo.save({'lidarr':{'settings':{'enabled':True,'url':'http://lidarr','api_key':'test'}}})
        self.a={'id':'11','name':'Alice','admin':False,'plex_token':'ALICE_SECRET','can_request':True,'auto_approve':True,'verified_at':9999999999}
        self.b={'id':'12','name':'Bob','admin':False,'plex_token':'BOB_SECRET','can_request':False,'verified_at':9999999999}
        self.admin={'id':'1','name':'Owner','admin':True,'plex_token':'OWNER_SECRET'}
        for u in (self.a,self.b,self.admin):accounts.put('accounts',u['id'],u)

    def test_personal_state_and_credentials_are_isolated(self):
        self.repo.save({'runtime':{'playlists':[{'source_id':'owner'}]}})
        with accounts.as_user(self.a):
            c=Config();self.assertEqual(c.config['playlists'],[])
            self.assertEqual(c.config['plex']['token'],'ALICE_SECRET')
            c.config['playlists']=[{'source_id':'alice'}];c.save()
            store=api.job_store();jid=store.enqueue('sync',{'scope':'all'})
        with accounts.as_user(self.b):
            c=Config();self.assertEqual(c.config['playlists'],[])
            self.assertEqual(c.config['plex']['token'],'BOB_SECRET')
            self.assertIsNone(api.job_store().get(jid))
        self.assertEqual(json.loads((self.path/'config.json').read_text()),self.settings)
        self.assertEqual(self.repo.load('runtime')['playlists'],[{'source_id':'owner'}])

    def test_member_cannot_submit_admin_job(self):
        with accounts.as_user(self.a):
            for action in ('health','backup','plex_scan','lidarr_add','restore_backup','check_updates'):
                with self.assertRaises(Exception):api.validated_payload(action,{})

    def test_worker_uses_owner_credentials_and_resets_context(self):
        with accounts.as_user(self.a):
            store=api.job_store();jid=store.enqueue('sync',{'scope':'all'});job=store.claim()
        seen=[]
        with patch.object(api,'execute_job',side_effect=lambda *a:seen.append(Config().config['plex']['token'])):
            Manager(self.repo).execute(job,store,self.a)
        self.assertEqual(seen,['ALICE_SECRET']);self.assertIsNone(accounts.actor())
        self.assertEqual(store.get(jid)['status'],'completed')

    async def http(self,path,method='GET',user=None,csrf=True,body=None):
        headers=[(b'host',b'test'),(b'content-type',b'application/json')]
        if user:
            secret='session-'+user['id'];accounts.put('sessions',hashlib.sha256(secret.encode()).hexdigest(),{'user_id':user['id'],'csrf':'yes','expires':9999999999})
            headers.append((b'cookie',('bridge_session='+secret).encode()))
            if csrf:headers.append((b'x-bridge-csrf',b'yes'))
        sent=[];received=False
        async def receive():
            nonlocal received
            if not received:
                received=True;return {'type':'http.request','body':json.dumps(body or {}).encode(),'more_body':False}
            await asyncio.sleep(100)
        async def send(message):sent.append(message)
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','headers':headers,'client':('127.0.0.1',1),'server':('test',80)}
        await api.app(scope,receive,send)
        status=next(m['status'] for m in sent if m['type']=='http.response.start')
        data=b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body')
        return status,data

    def test_http_auth_admin_boundary_and_csrf(self):
        for path in ('/api/playlists','/api/jobs','/api/library','/api/settings/lidarr','/api/users'):
            status,_=asyncio.run(self.http(path));self.assertEqual(status,401,path)
        for path in ('/api/settings/lidarr','/api/users','/api/backups','/api/settings/plex'):
            status,_=asyncio.run(self.http(path,user=self.a));self.assertEqual(status,403,path)
        status,_=asyncio.run(self.http('/api/jobs',method='POST',user=self.a,csrf=False));self.assertEqual(status,403)
        status,body=asyncio.run(self.http('/api/health'));self.assertEqual(status,200);self.assertEqual(json.loads(body),{'status':'ok'})
        status,body=asyncio.run(self.http('/api/playlists',user=self.a));self.assertEqual(status,202);self.assertIn('job_id',json.loads(body))
        status,body=asyncio.run(self.http('/api/library',user=self.a));self.assertEqual(status,202);self.assertIn('job_id',json.loads(body))

    def test_pin_login_only_owner_can_initialize(self):
        self.repo.save({'accounts':{}},replace=True)
        responses=[{'id':11,'username':'Alice','email':'a@example.com'}, {'MediaContainer':{'machineIdentifier':'server'}}, [{'clientIdentifier':'server','provides':'server','accessToken':'token','owned':False}],{'MediaContainer':{'Directory':[{'key':'1','type':'artist'}]}}]
        with patch.object(accounts,'plex_json',side_effect=responses):
            with self.assertRaises(Exception):accounts.verify('token')
        responses[2][0]['owned']=True
        with patch.object(accounts,'plex_json',side_effect=responses):
            user=accounts.verify('token');self.assertTrue(user['admin'])
        self.assertNotIn('plex_token',accounts.public(user))

    def test_library_waits_for_both_scans(self):
        p={'rows':[{'plex_id':'1','title':'Song','artist':'Artist','album':'Album'}],'checked_at':1}
        self.assertEqual(library.compare(p,{})['rows'][0]['status'],'pending')
        result=library.compare(p,{'checked_at':2,'rows':[]})
        self.assertEqual(result['rows'][0]['status'],'attention')
        l={'checked_at':2,'rows':[{'id':1,'title':'Album','artist':'Artist','statistics':{'trackCount':10}}]}
        row=library.compare(p,l)['rows'][0]
        self.assertEqual(row['status'],'linked');self.assertEqual(row['completeness'],'incomplete')

    def test_library_directory_is_lightweight_and_private_cache_data_is_hidden(self):
        endpoint=next(route.endpoint for route in api.app.routes if getattr(route,'path','')=='/api/library').__wrapped__
        library.invalidate()
        with patch.object(api,'_record_log') as logged,accounts.as_user(self.admin):
            directory=endpoint(view='directory',q='',sort='name:asc',offset=0,limit=50,refresh=True)
            full=endpoint(view='full',q='',sort='name:asc',offset=0,limit=50,refresh=False)
            self.assertEqual(sum('Prepared Library view' in call.args[2] for call in logged.call_args_list),1)
            lidarr.state_put(self.repo,'blocked_albums','new-rule',{'artist':'Nobody','album':'Nothing'})
            endpoint(view='directory',q='',sort='name:asc',offset=0,limit=50,refresh=False)
            self.assertEqual(sum('Prepared Library view' in call.args[2] for call in logged.call_args_list),2)
        self.assertIn('artists',directory);self.assertIn('release_count',directory)
        self.assertNotIn('rows',directory);self.assertNotIn('errors',directory)
        self.assertFalse(any(key.startswith('_') for key in full))

    def test_artist_can_be_manually_linked_to_a_plex_inventory_artist(self):
        from harmonia.inventory import identity
        from harmonia.lidarr_requests import server_id
        from types import SimpleNamespace
        plex_client=SimpleNamespace(base_url='http://plex',music_library_key='1',headers={'X-Plex-Token':'OWNER_SECRET','Accept':'application/json'})
        lidarr_cfg=lidarr.config(self.repo)
        lidarr.state_put(self.repo,'inventory','plex',{'identity':identity(plex_client),'machine_identifier':'machine','checked_at':1,'rows':[
            {'plex_id':'track-1','title':'Song','artist':'Hed PE','album_artist':'Hed PE','album':'Album','plex_artist_id':'plex-42','plex_artist_thumb':'/thumb','plex_artist_art':'/art'},
            {'plex_id':'track-2','title':'Other','artist':'Hed PE','album_artist':'Hed PE','album':'Second','plex_artist_id':'plex-42'}]})
        lidarr.state_put(self.repo,'inventory','lidarr',{'identity':server_id(lidarr_cfg),'checked_at':1,'rows':[],'tracks':[],'artists':[
            {'id':'lidarr-7','name':'(həd) p.e.','mbid':'19516266-e5d9-4774-b749-812bb76a6559'}]})
        # Metadata routes were registered with the real repository resolver;
        # direct its runtime lookup at this isolated installation as well.
        with patch('harmonia.runtime_paths.CONFIG_DIR',self.path):
            status,body=asyncio.run(self.http('/api/library/artists/lidarr-7/links/plex/search',method='POST',user=self.admin,body={}))
            self.assertEqual(status,200);results=json.loads(body);self.assertEqual(results[0]['id'],'plex-42')
            self.assertEqual(results[0]['album_count'],2);self.assertEqual(results[0]['track_count'],2)
            status,_=asyncio.run(self.http('/api/library/artists/lidarr-7/links',method='PUT',user=self.admin,body={'provider':'plex','value':'plex-42'}))
            self.assertEqual(status,200)
            status,body=asyncio.run(self.http('/api/library/artists/lidarr-7/links',user=self.admin));link=json.loads(body)
            self.assertEqual(link['plex_artist_id'],'plex-42');self.assertEqual(link['plex_source'],'Harmonia override')
            library.invalidate()
            with accounts.as_user(self.admin):
                endpoint=next(route.endpoint for route in api.app.routes if getattr(route,'path','')=='/api/library').__wrapped__
                artist=endpoint(view='directory',q='',sort='name:asc',offset=0,limit=50,refresh=True)['artists'][0]
            self.assertEqual(artist['plex_artist_id'],'plex-42');self.assertIn('%2Flibrary%2Fmetadata%2Fplex-42',artist['plex_url'])

    def test_server_schedule_queues_each_member_with_personal_keys(self):
        for user in (self.a,self.b):
            with accounts.as_user(user):
                c=Config();c.config['playlists']=[{'source':'spotify','source_id':user['id']}];c.save()
        accounts.scheduled_members('sync','server-schedule')
        for user in (self.a,self.b):
            with accounts.as_user(user):
                jobs=api.job_store().list();self.assertEqual(len(jobs),1)
                self.assertEqual(jobs[0]['payload']['playlist_keys'],['spotify:'+user['id']])
        accounts.scheduled_members('sync','server-schedule')
        with accounts.as_user(self.a):self.assertEqual(len(api.job_store().list()),1)

    def test_backup_contains_and_restores_personal_databases(self):
        from harmonia import backups
        from zipfile import ZipFile
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[{'source_id':'saved'}];c.save()
        result=backups.create(self.repo)
        with ZipFile(backups.path_for(self.repo,result['backup'])) as z:
            self.assertIn('users/11/harmonia.db',z.namelist())
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[];c.save()
        backups.restore(self.repo,result['backup'])
        with accounts.as_user(self.a):self.assertEqual(Config().config['playlists'],[{'source_id':'saved'}])

    def test_musicbrainz_recording_release_date_fallback(self):
        from harmonia import lidarr
        from unittest.mock import Mock
        response=Mock(status_code=200)
        response.json.return_value={'recordings':[{'id':'recording','title':'Song','artist-credit':[{'name':'Artist'}],'releases':[{'date':'1995-05-01','release-group':{'id':'group','title':'Album','primary-type':'Album'}},{'date':'1998','release-group':{'id':'group','title':'Album','primary-type':'Album'}}]}]}
        response.json.return_value['releases']=[{**r,'artist-credit':[{'name':'Artist'}]} for r in response.json.return_value['recordings'][0]['releases']]
        with patch('harmonia.musicbrainz_client._session.get',return_value=response),patch('harmonia.musicbrainz_client.time.sleep'),patch.object(api,'_record_log'):
            result=lidarr.musicbrainz_search(self.repo,{},lidarr.Lookup(title='Song',artist='Artist',provider='musicbrainz'),test=True)
        self.assertEqual(result['rows'][0]['year'],'1995')

    def test_shared_settings_are_not_in_member_health(self):
        status,body=asyncio.run(self.http('/api/health',user=self.a))
        self.assertEqual(status,200)
        self.assertNotIn(b'OWNER_SECRET',body);self.assertNotIn(b'ALICE_SECRET',body)

    def test_member_inventory_never_inherits_owner_plex_snapshot(self):
        from harmonia.inventory import current,identity
        from types import SimpleNamespace
        from harmonia.lidarr import state_put
        client=SimpleNamespace(base_url='http://plex',music_library_key='1',headers={'X-Plex-Token':'OWNER_SECRET','Accept':'application/json'})
        state_put(self.repo,'inventory','plex',{'identity':identity(client),'rows':[{'title':'Private owner track'}],'checked_at':1})
        with accounts.as_user(self.a):
            config=Config(read_only=True,namespaces=[])
            plex,_=current(config.repository,config)
            self.assertFalse(plex)

    def test_lastfm_key_not_returned(self):
        self.repo.save({'lastfm':{'settings':{'api_key':'LASTFM_SECRET'}}})
        status,body=asyncio.run(self.http('/api/settings/lastfm',user=self.admin))
        self.assertEqual(status,200);self.assertNotIn(b'LASTFM_SECRET',body)
        status,_=asyncio.run(self.http('/api/settings/lastfm',user=self.a));self.assertEqual(status,403)

    def test_cross_account_job_endpoints_are_not_found(self):
        with accounts.as_user(self.a):
            store=api.job_store();jid=store.enqueue('sync',{'scope':'all'});store.event(jid,'Private Alice output')
        for suffix in ('','/events','/live','/log'):
            status,body=asyncio.run(self.http('/api/jobs/'+jid+suffix,user=self.b))
            self.assertEqual(status,404);self.assertNotIn(b'Private Alice',body)

    def test_revoked_library_access_is_rechecked(self):
        from fastapi import HTTPException
        member={**self.a,'verified_at':0}
        with patch.object(accounts,'plex_json',return_value={'MediaContainer':{'Directory':[]}}):
            with self.assertRaises(HTTPException):accounts.refresh_access(member)

    def test_member_requests_use_only_server_defaults(self):
        from unittest.mock import Mock
        album='7672bd54-417c-3dda-b16c-0f49265216bb'
        self.repo.save({'lidarr':{'settings':{'enabled':True,'url':'http://lidarr','api_key':'test','root_folder':'/server/music','quality_profile_id':7,'metadata_profile_id':3,'search_now':True}}})
        preview=next(r for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/preview')
        add=next(r for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/add')
        captured=[]
        def inspect(request):
            self.assertIsNone(accounts.actor());captured.append(request)
            return {'preview_id':'11111111-1111-1111-1111-111111111111'}
        with patch.object(preview,'endpoint',side_effect=inspect),patch.object(add,'endpoint',return_value={'id':'job'}):
            status,body=asyncio.run(self.http('/api/requests-user/add',method='POST',user=self.a,body={'album_id':album,'root_folder':'/malicious','quality_profile_id':99}))
        self.assertEqual(status,202,body)
        self.assertEqual(captured[0].root_folder,'/server/music');self.assertEqual(captured[0].quality_profile_id,7)
        self.assertNotIn(b'/server/music',body)
        self.assertEqual(self.repo.load('user_requests')['11:'+album]['user_id'],'11')
        status,_=asyncio.run(self.http('/api/requests-user/add',method='POST',user=self.b,body={'album_id':album}))
        self.assertEqual(status,403)
