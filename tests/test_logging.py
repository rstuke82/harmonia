import time
import unittest
import requests
from playlist_bridge.diagnostics import redact, service_failure

class DiagnosticsTests(unittest.TestCase):
    def test_console_log_format_includes_timestamp_and_timezone(self):
        from playlist_bridge.console_logging import uvicorn_log_config
        config=uvicorn_log_config()
        for name in ('default','access'):
            self.assertIn('%(asctime)s',config['formatters'][name]['fmt'])
            self.assertIn('%z',config['formatters'][name]['datefmt'])

    def test_secret_redaction(self):
        text = redact('X-Api-Key: abc api_key=def Authorization: Bearer xyz token=ghi https://user:pass@example.com')
        for secret in ('abc','def','xyz','ghi','user:pass'):
            self.assertNotIn(secret, text)

    def test_service_errors_are_distinct(self):
        for error, expected in ((requests.Timeout(), 'timed out'), (requests.ConnectionError(), 'Connection failed'), (ValueError(), 'invalid JSON')):
            self.assertIn(expected, service_failure('MusicBrainz', error, time.monotonic()).detail)
        for code in (403,429,503):
            response = requests.Response()
            response.status_code = code
            error = requests.HTTPError(response=response)
            detail = service_failure('MusicBrainz', error, time.monotonic()).detail
            self.assertIn(str(code), detail)
            self.assertEqual('Rate limited' in detail, code == 429)

    def test_api_logs_keep_reason_and_request_id(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import HTTPException
        from starlette.requests import Request
        from playlist_bridge import api
        async def exercise():
            request = Request({'type':'http', 'method':'POST', 'path':'/api/lidarr/search',
                'headers':[], 'scheme':'http', 'server':('test',80), 'query_string':b'',
                'route':SimpleNamespace(path='/api/lidarr/search')})
            async def fail(request):
                return await api.diagnostic_http_error(request, HTTPException(502, 'MusicBrainz returned HTTP 503'))
            with patch.object(api, '_record_log') as log:
                response = await api.log_operations(request, fail)
            self.assertEqual(response.status_code, 502)
            message = log.call_args.args[2]
            self.assertIn('MusicBrainz returned HTTP 503', message)
            self.assertIn(response.headers['X-Request-ID'], message)
        asyncio.run(exercise())

    def test_lidarr_timeout_identifies_read_only_step(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        from playlist_bridge.lidarr import Client
        with patch('playlist_bridge.lidarr.requests.request', side_effect=requests.Timeout('upstream timed out')), patch('playlist_bridge.api._record_log') as log:
            with self.assertRaises(HTTPException) as raised:
                Client({'url':'http://lidarr:8686','api_key':'private-key'}).call('GET','album/lookup',params={'term':'lidarr:example-id'})
        self.assertEqual(raised.exception.status_code,504)
        self.assertIn('GET /api/v1/album/lookup [lidarr:example-id]',raised.exception.detail)
        self.assertIn('upstream timed out',raised.exception.detail)
        self.assertIn('no album was added or changed',raised.exception.detail)
        self.assertEqual(log.call_args.args[0],'ERROR')

    def test_lidarr_response_error_is_preserved_and_redacted(self):
        from unittest.mock import patch, Mock
        from fastapi import HTTPException
        from playlist_bridge.lidarr import Client
        response=Mock(status_code=500,ok=False,text='SkyHook lookup failed; api_key=private-key')
        with patch('playlist_bridge.lidarr.requests.request',return_value=response), patch('playlist_bridge.api._record_log') as log:
            with self.assertRaises(HTTPException) as raised:
                Client({'url':'http://lidarr:8686','api_key':'private-key'}).call('GET','album/lookup')
        self.assertIn('SkyHook lookup failed',raised.exception.detail)
        self.assertNotIn('private-key',raised.exception.detail)
        self.assertEqual(log.call_args.args[0],'ERROR')

    def test_access_levels_hide_success_but_preserve_failures(self):
        import logging
        from unittest.mock import patch
        from playlist_bridge import console_logging
        for status,expected in ((200,None),(404,logging.WARNING),(502,logging.ERROR)):
            record=logging.LogRecord('uvicorn.access',logging.INFO,'',0,'%s',('client','GET','/api/jobs','1.1',status),None)
            with patch.object(console_logging,'enabled',False):
                accepted=console_logging.AccessFilter().filter(record)
            self.assertEqual(accepted,expected is not None)
            if expected is not None:self.assertEqual(record.levelno,expected)

    def test_musicbrainz_logs_cache_at_info_and_error_body_at_error(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from playlist_bridge import lidarr
        from playlist_bridge.storage import Repository
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite')
            response=requests.Response();response.status_code=503;response._content=b'{"error":"Server busy"}'
            with patch('playlist_bridge.musicbrainz_client._session.get',return_value=response),patch('playlist_bridge.api._record_log') as log:
                from fastapi import HTTPException
                with self.assertRaises(HTTPException):lidarr.musicbrainz_search(repo,{},lidarr.Lookup(title='Song',artist='Artist'))
                self.assertTrue(any(c.args[0]=='INFO' and 'cache=miss' in c.args[2] for c in log.call_args_list))
                self.assertTrue(any(c.args[0]=='ERROR' and 'Server busy' in c.args[2] for c in log.call_args_list))
