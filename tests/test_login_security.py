"""Attack and recovery regressions for protected setup and bounded PIN polling."""
import asyncio
import hashlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from playlist_bridge import accounts, login_security as security
from playlist_bridge.jobs import Store
from playlist_bridge.storage import Repository


class LoginSecurity(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.repo=Repository(Path(temp.name))
        patcher=patch.object(accounts,'root_repository',return_value=self.repo);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch('builtins.print');self.print=patcher.start();self.addCleanup(patcher.stop)
        self.app=FastAPI();accounts.install(self.app)

    def route(self,path):
        return next(r.endpoint for r in self.app.routes if getattr(r,'path',None)==path)

    def request(self,cookie='',ip='127.0.0.1'):
        return Request({'type':'http','method':'POST','scheme':'http','path':'/','headers':[(b'host',b'test'),(b'origin',b'http://test'),(b'cookie',cookie.encode())],'client':(ip,1),'server':('test',80),'query_string':b''})

    def key(self):
        security.initialize_setup(self.repo)
        return (self.repo.directory/'setup-key').read_text().strip()

    def plex(self,method,url,**kwargs):
        if url.endswith('/user'):return {'id':11,'email':'owner@test.invalid','username':'Owner'}
        if url.endswith('/resources'):return [{'owned':True,'provides':'server','accessToken':'SERVER','clientIdentifier':'machine','connections':[{'uri':'http://plex','local':True}]}]
        if url.endswith('/identity'):return {'MediaContainer':{'machineIdentifier':'machine'}}
        if url.endswith('/library/sections'):return {'MediaContainer':{'Directory':[{'key':'7','title':'Music','type':'artist'}]}}
        raise AssertionError(url)

    def test_library_choice_excludes_video_and_does_not_commit_until_selected(self):
        self.key();grant=self.repo.load_one('installation','setup')['key_hash']
        libraries={'MediaContainer':{'Directory':[
            {'key':'1','title':'Movies','type':'movie'},
            {'key':'7','title':'Music','type':'artist'},
            {'key':'8','title':'Lossless','type':'artist'}]}}
        def plex(method,url,**kwargs):
            return libraries if url.endswith('/library/sections') else self.plex(method,url,**kwargs)
        with patch.object(accounts,'plex_json',side_effect=plex):
            choice=accounts.verify('OWNER',setup_hash=grant,choose_library=True)
            self.assertEqual(choice,{'setup_libraries':[{'key':'7','title':'Music'},{'key':'8','title':'Lossless'}]})
            self.assertEqual(accounts.users(),{})
            self.assertEqual(Store(self.repo).list(),[])
            self.assertNotIn('SERVER',json.dumps(choice))
            for key in ('1','999'):
                with self.assertRaises(HTTPException) as error:
                    accounts.verify('OWNER',setup_hash=grant,choose_library=True,library_key=key)
                self.assertEqual(error.exception.status_code,403)
                self.assertEqual(accounts.users(),{})
            user=accounts.verify('OWNER',setup_hash=grant,choose_library=True,library_key='8')
        self.assertTrue(user['admin'])
        from playlist_bridge.storage import read_json
        self.assertEqual(read_json(self.repo.directory/'config.json')['plex']['music_library_key'],'8')
        self.assertEqual(len(Store(self.repo).list()),1)

    def test_library_selection_rejects_foreign_origin_before_plex(self):
        req=self.request()
        req.scope['headers']=[(k,v) for k,v in req.scope['headers'] if k!=b'origin']+[(b'origin',b'https://attacker.invalid')]
        with patch.object(accounts,'plex_json') as plex:
            with self.assertRaises(HTTPException) as error:self.route('/api/auth/poll')(req)
        self.assertEqual(error.exception.status_code,403)
        plex.assert_not_called()

    def test_key_created_once_private_and_not_in_application_logs(self):
        key=self.key();security.initialize_setup(self.repo)
        self.assertEqual(self.key(),key);self.assertGreaterEqual(len(key),40)
        self.assertEqual(self.print.call_count,1)
        if os.name=='posix':self.assertEqual((self.repo.directory/'setup-key').stat().st_mode & 0o777,0o600)
        self.assertNotIn(key,json.dumps(self.repo.logs()))
        self.assertNotIn(key,json.dumps(self.repo.load('installation')))

    def test_missing_or_wrong_key_never_contacts_plex(self):
        start=self.route('/api/auth/start')
        body_type=start.__annotations__['body'].__args__[0]
        with patch.object(accounts,'plex_json') as plex:
            for body in (None,body_type(setup_key='wrong')):
                with self.assertRaises(HTTPException) as error:start(self.request(),body)
                self.assertEqual(error.exception.status_code,403)
            with self.assertRaises(HTTPException):accounts.verify('some-other-owner')
        plex.assert_not_called();self.assertEqual(accounts.users(),{})

    def test_setup_grant_bound_to_pin_and_reuse_rejected(self):
        start=self.route('/api/auth/start');body_type=start.__annotations__['body'].__args__[0]
        key=self.key()
        with patch.object(accounts,'plex_json',return_value={'id':42,'code':'pin'}):
            response=start(self.request(),body_type(setup_key=key))
        pin_cookie=response.headers['set-cookie'].split(';')[0]
        saved=next(iter(self.repo.load('login_pins').values()))
        self.assertEqual(saved['setup_hash'],hashlib.sha256(key.encode()).hexdigest())
        with patch.object(accounts,'plex_json',side_effect=lambda method,url,**kw:{'authToken':'OWNER'} if '/pins/' in url else self.plex(method,url,**kw)):
            response=self.route('/api/auth/poll')(self.request(pin_cookie))
            self.assertEqual(json.loads(response.body),{'setup_libraries':[{'key':'7','title':'Music'}]})
            self.assertEqual(accounts.users(),{})
            self.assertTrue((self.repo.directory/'setup-key').exists())
            body_type=self.route('/api/auth/poll').__annotations__['body'].__args__[0]
            with patch.object(security.time,'time',return_value=security.time.time()+3):
                response=self.route('/api/auth/poll')(self.request(pin_cookie),body_type(library_key='7'))
        self.assertEqual(json.loads(response.body)['user']['admin'],True)
        self.assertFalse((self.repo.directory/'setup-key').exists())
        self.assertEqual(self.repo.load('login_pins'),{})
        self.assertEqual(len(Store(self.repo).list()),1)
        self.assertEqual(Store(self.repo).list()[0]['action'],'plex_scan')
        with self.assertRaises(HTTPException) as error:accounts.verify('OWNER',setup_hash=saved['setup_hash'])
        self.assertEqual(error.exception.status_code,409)
        self.assertTrue(self.repo.load_one('installation','initial_scan')['job_id'])

    def test_competing_claims_create_exactly_one_owner_and_scan(self):
        key=self.key();grant=hashlib.sha256(key.encode()).hexdigest()
        config={'plex':{'url':'http://plex','token':'SECRET','music_library_key':'7'}}
        def claim(i):
            try:security.commit_setup(self.repo,config,{'id':str(i),'owner':True,'admin':True},grant);return True
            except HTTPException as error:self.assertEqual(error.status_code,409);return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,(1,2)))
        self.assertEqual(results.count(True),1);self.assertEqual(len(accounts.users()),1)
        self.assertEqual(len(Store(self.repo).list()),1)
        self.assertEqual(json.loads((self.repo.directory/'config.json').read_text()),config)

    def test_committed_claim_recovers_config_and_scan_after_write_failure(self):
        grant=hashlib.sha256(self.key().encode()).hexdigest()
        config={'plex':{'url':'http://plex','token':'SECRET','music_library_key':'7'}}
        with patch.object(self.repo,'save_startup',side_effect=OSError('interrupted write')):
            with self.assertRaises(OSError):security.commit_setup(self.repo,config,{'id':'1','owner':True},grant)
        self.assertEqual(len(accounts.users()),1);self.assertEqual(Store(self.repo).list(),[])
        self.assertTrue(self.repo.load_one('installation','pending_config'))
        security.initialize_setup(self.repo);security.initialize_setup(self.repo)
        self.assertEqual(json.loads((self.repo.directory/'config.json').read_text()),config)
        self.assertEqual(len(Store(self.repo).list()),1)
        self.assertFalse(self.repo.load_one('installation','pending_config'))
        self.assertFalse((self.repo.directory/'setup-key').exists())

    def test_failed_plex_validation_does_not_consume_key_or_commit_config(self):
        grant=hashlib.sha256(self.key().encode()).hexdigest()
        with patch.object(accounts,'plex_json',side_effect=[{'email':'owner@test.invalid'},[],HTTPException(503,'offline')]):
            with self.assertRaises(HTTPException):accounts.verify('OWNER',setup_hash=grant)
        self.assertEqual(accounts.users(),{});self.assertFalse((self.repo.directory/'config.json').exists())
        self.assertFalse(self.repo.load_one('installation','setup')['consumed'])

    def test_configured_installations_need_no_key_and_existing_owner_not_rescanned(self):
        self.repo.save_startup({'plex':{'url':'http://plex','token':'SERVER','music_library_key':'7'}})
        security.initialize_setup(self.repo)
        self.assertIsNone(security.authorize_setup(self.repo,''))
        self.assertFalse((self.repo.directory/'setup-key').exists())
        with patch.object(accounts,'plex_json',side_effect=self.plex):
            owner=accounts.verify('OWNER');accounts.verify('OWNER')
        self.assertTrue(owner['admin']);self.assertEqual(len(Store(self.repo).list()),1)
        self.assertTrue(self.repo.load_one('installation','initial_scan')['job_id'])

    def pin(self,nonce='nonce',**extra):
        key=hashlib.sha256(nonce.encode()).hexdigest()
        accounts.put('login_pins',key,{'id':1,'code':'code','expires':9999999999,**extra})
        return key

    def test_repeated_and_concurrent_polls_share_one_upstream_check(self):
        self.pin();poll=self.route('/api/auth/poll');req=self.request('bridge_login=nonce')
        entered=threading.Event();release=threading.Event()
        def upstream(*a,**kw):entered.set();self.assertTrue(release.wait(5));return {}
        with patch.object(accounts,'plex_json',side_effect=upstream) as plex,ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(poll,req);self.assertTrue(entered.wait(5))
            for _ in range(20):self.assertEqual(json.loads(poll(req).body),{'pending':True})
            release.set();self.assertEqual(json.loads(first.result().body),{'pending':True})
            self.assertEqual(json.loads(poll(req).body),{'pending':True});self.assertEqual(plex.call_count,1)

    def test_poll_interval_and_failure_recovery(self):
        key=self.pin()
        with patch.object(security.time,'time',return_value=100):
            lease=security.claim_poll(self.repo,key,'ip');security.release_poll(self.repo,key,lease['lease'])
            self.assertIsNone(security.claim_poll(self.repo,key,'ip'))
        with patch.object(security.time,'time',return_value=102):self.assertTrue(security.claim_poll(self.repo,key,'ip'))
        key=self.pin('failure');req=self.request('bridge_login=failure')
        with patch.object(accounts,'plex_json',side_effect=HTTPException(503,'offline')):
            with self.assertRaises(HTTPException):self.route('/api/auth/poll')(req)
        self.assertEqual(self.repo.load_one('login_pins',key)['lease_until'],0)

    def test_ip_global_and_concurrent_limits_return_retry_after(self):
        key=self.pin()
        with patch.object(security.time,'time',return_value=100):
            for _ in range(60):security.claim_poll(self.repo,key,'same-ip')
            with self.assertRaises(HTTPException) as error:security.claim_poll(self.repo,key,'same-ip')
            self.assertEqual(error.exception.status_code,429);self.assertIn('Retry-After',error.exception.headers)
            for i in range(240):security.claim_poll(self.repo,key,'ip-'+str(i))
            with self.assertRaises(HTTPException) as error:security.claim_poll(self.repo,key,'new-ip')
            self.assertEqual(error.exception.status_code,429)
        with patch.object(security.time,'time',return_value=161):self.assertTrue(security.claim_poll(self.repo,key,'same-ip') is None) # existing live lease is still shared
        self.repo.save({'auth_limits':{},'login_pins':{}})
        with patch.object(security.time,'time',return_value=200):
            for i in range(security.MAX_ACTIVE_POLLS):self.assertTrue(security.claim_poll(self.repo,self.pin(str(i)),str(i)))
            with self.assertRaises(HTTPException) as error:security.claim_poll(self.repo,self.pin('extra'),'extra')
            self.assertEqual(error.exception.status_code,429)

    def test_expiry_and_stale_lease_release_cannot_overwrite_replacement(self):
        key=self.pin()
        with patch.object(security.time,'time',return_value=100):old=security.claim_poll(self.repo,key,'ip')
        with patch.object(security.time,'time',return_value=281):new=security.claim_poll(self.repo,key,'ip')
        security.release_poll(self.repo,key,old['lease'])
        self.assertEqual(self.repo.load_one('login_pins',key)['lease'],new['lease'])
        self.pin('expired',expires=1)
        with self.assertRaises(HTTPException) as error:security.claim_poll(self.repo,hashlib.sha256(b'expired').hexdigest(),'ip')
        self.assertEqual(error.exception.status_code,410)

    def test_initial_scan_status_is_owner_only_and_follows_manual_retry(self):
        grant=hashlib.sha256(self.key().encode()).hexdigest()
        security.commit_setup(self.repo,{'plex':{'url':'http://plex','token':'SECRET','music_library_key':'7'}},{'id':'1','owner':True},grant)
        status=self.route('/api/account/initial-library');store=Store(self.repo)
        initial=self.repo.load_one('installation','initial_scan')['job_id']
        with accounts.as_user({'id':'1','owner':True}):self.assertEqual(status()['job']['id'],initial)
        with accounts.as_user({'id':'2','owner':False,'admin':True}):self.assertIsNone(status()['job'])
        store.update(initial,status='failed',error='Plex unavailable')
        retry=store.enqueue('plex_scan',{})
        with accounts.as_user({'id':'1','owner':True}):
            self.assertEqual(status()['job']['id'],retry)
            self.assertEqual(status()['job']['status'],'queued')
            store.update(retry,status='completed')
            self.assertEqual(status()['job']['status'],'completed')

    def test_valid_setup_start_limit_and_poll_expiry_do_not_contact_upstream(self):
        start=self.route('/api/auth/start');body_type=start.__annotations__['body'].__args__[0]
        key=self.key()
        with patch.object(accounts,'plex_json',return_value={'id':42,'code':'pin'}) as plex:
            for _ in range(5):start(self.request(),body_type(setup_key=key))
            with self.assertRaises(HTTPException) as error:start(self.request(),body_type(setup_key=key))
            self.assertEqual(error.exception.status_code,429);self.assertEqual(plex.call_count,5)
            with self.assertRaises(HTTPException) as error:self.route('/api/auth/poll')(self.request('bridge_login=unknown'))
            self.assertEqual(error.exception.status_code,410);self.assertEqual(plex.call_count,5)

    def test_http_setup_payload_validation_and_secret_not_returned(self):
        async def post(body):
            sent=[];received=False
            async def receive():
                nonlocal received
                if not received:
                    received=True;return {'type':'http.request','body':json.dumps(body).encode(),'more_body':False}
                await asyncio.sleep(100)
            async def send(message):sent.append(message)
            scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http','path':'/api/auth/start','raw_path':b'/api/auth/start','query_string':b'','root_path':'','headers':[(b'host',b'test'),(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)}
            await self.app(scope,receive,send)
            start=next(m for m in sent if m['type']=='http.response.start')
            return start,b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body')
        key=self.key()
        with patch.object(accounts,'plex_json',return_value={'id':42,'code':'pin'}) as plex:
            response,_=asyncio.run(post({}));self.assertEqual(response['status'],403);plex.assert_not_called()
            response,_=asyncio.run(post({'setup_key':'x'*201}));self.assertEqual(response['status'],422);plex.assert_not_called()
            response,body=asyncio.run(post({'setup_key':key}));self.assertEqual(response['status'],200)
            self.assertNotIn(key.encode(),body);self.assertTrue(any(k==b'set-cookie' for k,v in response['headers']))
