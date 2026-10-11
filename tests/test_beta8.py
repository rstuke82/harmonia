import unittest
from unittest.mock import patch
from fastapi import HTTPException
from playlist_bridge import api
from playlist_bridge.diagnostics import concise_error

class Beta8Tests(unittest.TestCase):
    def test_batch_rejects_unscoped_and_empty_requests(self):
        for payload in ({'tracks':[]},{'tracks':[{'title':'Song','artist':'Artist'}]}):
            with self.assertRaises(ValueError):api.validated_payload('ignore_batch',payload)
    def test_batch_keeps_individual_scope_and_reports_partial_failure(self):
        payload={'tracks':[{'title':'One','artist':'Artist','playlist_keys':['spotify:one']},{'title':'Two','artist':'Artist','universal':True}]}
        value=api.validated_payload('ignore_batch',payload)
        with patch.object(api,'ignore_missing',side_effect=[{'affected':1},HTTPException(409,'Playlist removed')]) as ignore,patch.object(api,'_record_log'):
            result=api.execute_job('ignore_batch',value)
        self.assertEqual(ignore.call_args_list[0].args[0].playlist_keys,['spotify:one'])
        self.assertTrue(ignore.call_args_list[1].args[0].universal)
        self.assertTrue(result['partial_success'])
        self.assertEqual([r['ok'] for r in result['tracks']],[True,False])
        self.assertIn('Playlist removed',result['tracks'][1]['error'])
    def test_service_error_decodes_json_without_stack(self):
        message='Lidarr returned HTTP 503: {"message":"Search for \\u0027Pearl Jam\\u0027 failed.","description":"Long stack trace"}'
        short=concise_error(message)
        self.assertEqual(short,"Lidarr returned HTTP 503: Search for 'Pearl Jam' failed.")
        self.assertNotIn('stack',short)
    def test_partial_success_prefix_survives_shortening(self):
        message='Album added; search failed: {"message":"Metadata unavailable","description":"stack"}'
        self.assertEqual(concise_error(message),'Album added; search failed: Metadata unavailable')
