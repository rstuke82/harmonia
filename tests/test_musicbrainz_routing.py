import unittest
from unittest.mock import Mock, patch

import requests

from harmonia import musicbrainz_client as client
from harmonia.musicbrainz_settings import Preferences


class Repo:
    def __init__(self):self.logs=[]
    def add_log(self,level,source,message):self.logs.append((level,source,message))


def response(status=200, data=None):
    value=Mock(status_code=status)
    value.json.return_value=data or {'ok':True}
    if status >= 400:
        error=requests.HTTPError('failed');error.response=value;value.raise_for_status.side_effect=error
    return value


class MusicBrainzRoutingTests(unittest.TestCase):
    def setUp(self):
        client._public_last=0
        self.repo=Repo()

    def call(self,entity,params=None,mirror='https://mirror.example/'):
        with patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':mirror}), \
             patch.object(client.time,'sleep'):
            return client.get(self.repo,entity,params)

    def test_artist_and_recording_search_are_always_public(self):
        with patch.object(client._session,'get',side_effect=[response(),response()]) as get:
            self.call('artist',{'query':'artist:CAKE'})
            self.call('recording',{'query':'recording:The Distance'})
        self.assertEqual([c.args[0] for c in get.call_args_list],
                         ['https://musicbrainz.org/ws/2/artist','https://musicbrainz.org/ws/2/recording'])

    def test_known_artist_release_and_browse_use_mirror(self):
        with patch.object(client._session,'get',side_effect=[response(),response(),response()]) as get:
            self.call('artist/10ad7d60-32a0-4e09-9dc3-73e8e1e6e497')
            self.call('release/11111111-1111-1111-1111-111111111111',{'inc':'recordings+artist-credits'})
            self.call('release-group',{'artist':'10ad7d60-32a0-4e09-9dc3-73e8e1e6e497','limit':100})
        self.assertTrue(all(c.args[0].startswith('https://mirror.example/ws/2/') for c in get.call_args_list))
        self.assertEqual(get.call_args_list[1].kwargs['params']['inc'],'recordings+artist-credits')

    def test_no_mirror_uses_public_for_direct_lookup(self):
        with patch.object(client._session,'get',return_value=response()) as get:
            self.call('artist/10ad7d60-32a0-4e09-9dc3-73e8e1e6e497',mirror='')
        self.assertEqual(get.call_args.args[0],'https://musicbrainz.org/ws/2/artist/10ad7d60-32a0-4e09-9dc3-73e8e1e6e497')

    def test_mirror_timeout_connection_and_5xx_fall_back_to_public(self):
        failure5=response(503);error5=failure5.raise_for_status.side_effect
        for failure in (requests.Timeout(),requests.ConnectionError(),error5):
            with self.subTest(type=type(failure).__name__),patch.object(client._session,'get',side_effect=[failure,response()]) as get:
                self.call('artist/10ad7d60-32a0-4e09-9dc3-73e8e1e6e497')
                self.assertEqual(get.call_count,2)
                self.assertTrue(get.call_args_list[1].args[0].startswith(client.PUBLIC_BASE))

    def test_mirror_404_does_not_fall_back(self):
        with patch.object(client._session,'get',return_value=response(404)) as get:
            with self.assertRaises(requests.HTTPError):self.call('artist/00000000-0000-0000-0000-000000000000')
        self.assertEqual(get.call_count,1)

    def test_public_search_never_attempts_mirror_and_is_throttled(self):
        with patch.object(client._session,'get',return_value=response()) as get, \
             patch.object(client.time,'sleep') as sleep, \
             patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':'https://mirror.example'}):
            client.get(self.repo,'release-group',{'query':'releasegroup:Blue'})
        self.assertTrue(get.call_args.args[0].startswith(client.PUBLIC_BASE))
        sleep.assert_called_once()

    def test_public_fallback_uses_public_limiter(self):
        with patch.object(client._session,'get',side_effect=[requests.Timeout(),response()]), \
             patch.object(client.time,'sleep') as sleep, \
             patch('harmonia.musicbrainz_settings.settings',return_value={'enabled':True,'server_url':'https://mirror.example'}):
            client.get(self.repo,'release/11111111-1111-1111-1111-111111111111')
        sleep.assert_called_once()

    def test_mirror_url_normalization(self):
        self.assertEqual(Preferences(server_url='https://mirror.example/').server_url,'https://mirror.example')
        self.assertEqual(Preferences(server_url='').server_url,'')

