import asyncio
import os
import unittest
from unittest.mock import patch
from starlette.requests import Request
from starlette.responses import Response
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from harmonia.browser_security import BrowserSecurity,CSP,https_only,secure_cookie,trusted_proxies
from harmonia import api

async def call(app,path='/api/private',scheme='http',client='198.51.100.4',headers=()):
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET','scheme':scheme,'path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','headers':[(b'host',b'test'),*headers],'client':(client,1234),'server':('test',80)}
    sent=[]
    async def receive():return {'type':'http.request','body':b'','more_body':False}
    async def send(message):sent.append(message)
    await app(scope,receive,send)
    start=next(m for m in sent if m['type']=='http.response.start')
    return start['status'],dict(start['headers']),b''.join(m.get('body',b'') for m in sent)

class BrowserHardening(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'HARMONIA_HTTPS_ONLY':'false','HARMONIA_TRUSTED_PROXIES':'127.0.0.1,::1'})
        env.start();self.addCleanup(env.stop)
    async def target(self,scope,receive,send):
        await Response('ok',headers={'Cache-Control':'public, max-age=100'},status_code=403)(scope,receive,send)
    def test_sensitive_errors_downloads_and_cookie_responses_are_not_cached(self):
        app=BrowserSecurity(self.target)
        for path in ('/api/private','/api/backups/download','/api/auth/start','/api/health'):
            status,headers,_=asyncio.run(call(app,path))
            self.assertEqual(status,403);self.assertEqual(headers[b'cache-control'],b'no-store')
            self.assertEqual(headers[b'x-content-type-options'],b'nosniff');self.assertEqual(headers[b'x-frame-options'],b'DENY')
        async def cookies(scope,receive,send):
            await Response('ok',headers={'Set-Cookie':'session=value','Cache-Control':'public'})(scope,receive,send)
        self.assertEqual(asyncio.run(call(BrowserSecurity(cookies),'/'))[1][b'cache-control'],b'no-store')
    def test_static_cache_preserved_and_https_hsts_only(self):
        app=BrowserSecurity(self.target)
        headers=asyncio.run(call(app,'/assets/index.js'))[1]
        self.assertEqual(headers[b'cache-control'],b'public, max-age=100');self.assertNotIn(b'strict-transport-security',headers)
        self.assertEqual(asyncio.run(call(app,scheme='https'))[1][b'strict-transport-security'],b'max-age=31536000')
        self.assertIn("script-src 'self'",CSP);self.assertIn("frame-ancestors 'none'",CSP);self.assertIn('frame-src https://embed.music.apple.com',CSP)
    def test_https_only_rejects_spoofed_forwarding_but_accepts_trusted_proxy(self):
        with patch.dict(os.environ,{'HARMONIA_HTTPS_ONLY':'true','HARMONIA_TRUSTED_PROXIES':'10.0.0.2'}):
            app=ProxyHeadersMiddleware(BrowserSecurity(self.target),trusted_hosts=trusted_proxies())
            headers=[(b'x-forwarded-proto',b'https'),(b'x-forwarded-for',b'203.0.113.7')]
            self.assertEqual(asyncio.run(call(app,headers=headers))[0],400)
            status,response,_=asyncio.run(call(app,client='10.0.0.2',headers=headers))
            self.assertEqual(status,403);self.assertIn(b'strict-transport-security',response)
    def test_trusted_proxy_client_address_used_for_rate_limits(self):
        seen=[]
        async def target(scope,receive,send):
            seen.append(scope['client'][0]);await Response('ok')(scope,receive,send)
        app=ProxyHeadersMiddleware(target,trusted_hosts='10.0.0.2')
        headers=[(b'x-forwarded-for',b'203.0.113.7')]
        asyncio.run(call(app,client='10.0.0.2',headers=headers));asyncio.run(call(app,headers=headers))
        self.assertEqual(seen,['203.0.113.7','198.51.100.4'])
    def test_cookie_modes_and_invalid_configuration(self):
        request=Request({'type':'http','scheme':'http','path':'/','headers':[(b'host',b'test')]})
        self.assertFalse(secure_cookie(request))
        with patch.dict(os.environ,{'HARMONIA_HTTPS_ONLY':'true'}):self.assertTrue(secure_cookie(request))
        for value in ('*','0.0.0.0/0','::/0','proxy.example.com'):
            with patch.dict(os.environ,{'HARMONIA_TRUSTED_PROXIES':value}):
                with self.assertRaises(ValueError):trusted_proxies()
        with patch.dict(os.environ,{'HARMONIA_HTTPS_ONLY':'typo'}):
            with self.assertRaises(ValueError):https_only()
    def test_startup_passes_explicit_trust_to_uvicorn(self):
        with patch('uvicorn.run') as run,patch.dict(os.environ,{'HARMONIA_TRUSTED_PROXIES':'10.0.0.2'}):api.run()
        self.assertTrue(run.call_args.kwargs['proxy_headers']);self.assertEqual(run.call_args.kwargs['forwarded_allow_ips'],'10.0.0.2')
    def test_app_auth_errors_have_headers_and_docs_disabled(self):
        status,headers,_=asyncio.run(call(api.app,'/api/account'))
        self.assertEqual(status,401);self.assertEqual(headers[b'cache-control'],b'no-store');self.assertIn(b'content-security-policy',headers)
        self.assertIsNone(api.app.docs_url);self.assertIsNone(api.app.openapi_url);self.assertIsNone(api.app.redoc_url)

    def test_unhandled_failures_receive_safe_no_store_security_headers(self):
        captured=[]
        async def broken(scope,receive,send):raise RuntimeError('private secret')
        async def run():
            scope={'type':'http','scheme':'https','path':'/api/private'}
            async def receive():return {'type':'http.request','body':b''}
            async def send(message):captured.append(message)
            with self.assertRaises(RuntimeError):await BrowserSecurity(broken)(scope,receive,send)
        asyncio.run(run())
        start=captured[0];headers=dict(start['headers'])
        self.assertEqual(start['status'],500);self.assertEqual(headers[b'cache-control'],b'no-store')
        self.assertIn(b'content-security-policy',headers)
        self.assertNotIn(b'private secret',captured[1]['body'])
