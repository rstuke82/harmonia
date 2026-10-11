import asyncio
import ast
import concurrent.futures
import uuid
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from fastapi import HTTPException
from playlist_bridge import accounts,jobs,redirect_policy as policy
import test_30 as multi

class Redirects(unittest.TestCase):
    def test_service_redirect_cannot_forward_plex_token(self):
        response=Mock(status_code=302,headers={'Location':'http://other/private'})
        with patch.object(policy.requests,'get',return_value=response) as get:
            with self.assertRaises(HTTPException):policy.get('http://plex/image',headers={'X-Plex-Token':'SECRET'})
        self.assertEqual(get.call_count,1);self.assertFalse(get.call_args.kwargs['allow_redirects']);response.close.assert_called_once()
    def test_same_origin_redirect_retains_service_token(self):
        first=Mock(status_code=302,headers={'Location':'/new'});last=Mock(status_code=200)
        with patch.object(policy.requests,'get',side_effect=[first,last]) as get:
            self.assertIs(policy.get('http://plex/image',headers={'X-Plex-Token':'SECRET'}),last)
        self.assertEqual(get.call_args.args[0],'http://plex/new');self.assertEqual(get.call_args.kwargs['headers']['X-Plex-Token'],'SECRET');first.close.assert_called_once()
    def test_external_rejects_http_userinfo_and_private_or_mixed_dns(self):
        for url in ('http://external/image','https://user:pass@external/image'):
            with self.assertRaises(HTTPException):policy.external_get(url,{})
        for addresses in (['127.0.0.1'],['169.254.169.254'],['::1'],['8.8.8.8','10.0.0.1']):
            with patch.object(policy.socket,'getaddrinfo',return_value=[(None,None,None,None,(ip,443)) for ip in addresses]),patch.object(policy.requests,'Session') as session:
                with self.assertRaises(HTTPException):policy.external_get('https://external/image',{})
                session.assert_not_called()
    def test_external_pins_public_ip_verifies_original_tls_name_strips_credentials(self):
        session=Mock();response=Mock(status_code=200);session.get.return_value=response
        with patch.object(policy.socket,'getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',443))]),patch.object(policy.requests,'Session',return_value=session):
            self.assertIs(policy.external_get('https://external/image',{'X-Plex-Token':'SECRET','Authorization':'SECRET'}),response)
        self.assertFalse(session.trust_env);headers=session.get.call_args.kwargs['headers'];self.assertNotIn('SECRET',str(headers));self.assertEqual(headers['Host'],'external')
        adapter=session.mount.call_args.args[1]
        with patch.object(adapter.poolmanager,'connection_from_host') as connection:
            adapter.get_connection_with_tls_context(None,True)
        self.assertEqual(connection.call_args.args[0],'8.8.8.8');self.assertEqual(connection.call_args.kwargs['pool_kwargs'],{'server_hostname':'external','assert_hostname':'external'})
        response.close();session.close.assert_called_once()
    def test_redirect_budget_closes_every_response(self):
        responses=[Mock(status_code=302,headers={'Location':'/next'}) for _ in range(4)]
        with patch.object(policy.requests,'get',side_effect=responses) as get:
            with self.assertRaises(HTTPException):policy.get('http://plex/image')
        self.assertEqual(get.call_count,4)
        for response in responses:response.close.assert_called_once()
    def test_service_calls_explicitly_disable_automatic_redirects(self):
        for filename in ('accounts.py','artwork_sync.py','plex_client.py'):
            tree=ast.parse((Path(__file__).parents[1]/'playlist_bridge'/filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='requests' and node.func.attr in ('get','post','put','delete','request'):
                    self.assertTrue(any(k.arg=='allow_redirects' and isinstance(k.value,ast.Constant) and k.value.value is False for k in node.keywords),filename)

class Quotas(unittest.TestCase):
    setUp=multi.MultiUser.setUp
    http=multi.MultiUser.http
    def test_approval_queue_limit_and_duplicate_admission(self):
        user={**self.a,'auto_approve':False};accounts.put('accounts',user['id'],user)
        ids=[str(uuid.uuid4()) for _ in range(26)]
        for album in ids[:25]:self.assertEqual(asyncio.run(self.http('/api/requests-user/add','POST',user,body={'album_id':album}))[0],202)
        self.assertEqual(asyncio.run(self.http('/api/requests-user/add','POST',user,body={'album_id':ids[0]}))[0],202)
        self.assertEqual(asyncio.run(self.http('/api/requests-user/add','POST',user,body={'album_id':ids[-1]}))[0],429)
        self.assertEqual(len(self.repo.load('pending_requests')),25)
    def test_job_limit_is_atomic_and_duplicates_cancelled_slots_recover(self):
        def enqueue(i):
            with accounts.as_user(self.a):
                store=jobs.Store(accounts.personal_repository())
                try:return store.enqueue('musicbrainz_lookup',{'i':i})
                except HTTPException as error:self.assertEqual(error.status_code,429);return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:ids=list(pool.map(enqueue,range(35)))
        self.assertEqual(sum(bool(i) for i in ids),25)
        with accounts.as_user(self.a):
            store=jobs.Store(accounts.personal_repository());active=[i for i in ids if i];job=store.get(active[0])
            self.assertEqual(store.enqueue(job['action'],job['payload']),active[0]);store.cancel(active[0]);self.assertTrue(store.enqueue('musicbrainz_lookup',{'i':100}))
    def test_existing_explicit_auto_approval_is_preserved(self):
        self.assertTrue(accounts.users()[self.a['id']]['auto_approve'])

class DependencyLock(unittest.TestCase):
    def test_bootstrap_checks_included_lock_and_rejects_cycles(self):
        import tempfile
        from playlist_bridge.bootstrap import _parse_requirements_file
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'requirements.txt').write_text('-r requirements.lock\n');(root/'requirements.lock').write_text('requests==2.34.2\n')
            parsed=_parse_requirements_file(root/'requirements.txt');self.assertEqual(parsed[0]['name'],'requests');self.assertEqual(parsed[0]['operator'],'==')
            (root/'requirements.lock').write_text('-r requirements.txt\n')
            with self.assertRaises(ValueError):_parse_requirements_file(root/'requirements.txt')
    def test_container_health_probe_works_without_allowing_remote_http(self):
        from test_browser_security import call,BrowserHardening
        from playlist_bridge.browser_security import BrowserSecurity
        import os
        app=BrowserSecurity(BrowserHardening.target)
        # Use a normal ASGI target without authentication side effects.
        async def target(scope,receive,send):
            from starlette.responses import Response
            await Response('ok')(scope,receive,send)
        app=BrowserSecurity(target)
        with patch.dict(os.environ,{'HARMONIA_HTTPS_ONLY':'true'}):
            self.assertEqual(asyncio.run(call(app,'/api/health',client='127.0.0.1'))[0],200)
            self.assertEqual(asyncio.run(call(app,'/api/health',client='198.51.100.1'))[0],400)
