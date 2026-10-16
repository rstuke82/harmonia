import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from harmonia import fanart
from harmonia.storage import Repository


class FanartTests(unittest.TestCase):
    ARTIST='f4a31f0a-51dd-4fa7-986d-3095c40c5ed9'
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name))
        from harmonia.lidarr import state_put
        state_put(self.repo,'fanart','settings',{'api_key':'project-secret','client_key':'personal-secret'})

    def test_artist_lookup_uses_direct_api_and_keeps_keys_in_headers(self):
        response=Mock(status_code=200);response.json.return_value={'image_count':1,'artistthumb':[{'id':'1','url':'https://assets.fanart.tv/a.jpg','likes':'2'}]};response.raise_for_status.return_value=None
        with patch.object(fanart.network,'get',return_value=response) as get:data=fanart.lookup(self.repo,'artist',self.ARTIST)
        url=get.call_args.args[0];headers=get.call_args.kwargs['headers']
        self.assertEqual(url,'https://webservice.fanart.tv/v3.2/music/'+self.ARTIST);self.assertNotIn('secret',url)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertEqual(headers['api-key'],'project-secret');self.assertEqual(headers['client-key'],'personal-secret')
        self.assertEqual(fanart.artist_images(data,'portrait')[0]['url'],'https://assets.fanart.tv/a.jpg')

    def test_album_images_use_release_group_id_and_reject_untrusted_hosts(self):
        data={'albums':[{'release_group_id':'rgid','albumcover':[{'url':'https://assets.fanart.tv/good.jpg','likes':'5'},{'url':'https://example.test/bad.jpg','likes':'99'}]}]}
        self.assertEqual([row['url'] for row in fanart.album_images(data,'rgid')],['https://assets.fanart.tv/good.jpg'])

    def test_cache_avoids_repeated_requests(self):
        response=Mock(status_code=200);response.json.return_value={'artistbackground':[]};response.raise_for_status.return_value=None
        with patch.object(fanart.network,'get',return_value=response) as get:
            fanart.lookup(self.repo,'artist',self.ARTIST);fanart.lookup(self.repo,'artist',self.ARTIST)
        self.assertEqual(get.call_count,1)

    def test_lidarr_artwork_order_migrates_to_direct_fanart(self):
        from harmonia.metadata_sources import Preferences
        value=Preferences(source_order={'artist_portrait':['apple','lidarr','plex'],'artist_hero':['lidarr','plex'],'album_artwork':['lidarr','coverartarchive']})
        self.assertEqual(value.source_order['artist_portrait'],['apple','fanart','plex'])
        self.assertEqual(value.source_order['artist_hero'],['fanart','plex'])
        self.assertEqual(value.source_order['album_artwork'],['fanart','coverartarchive'])


if __name__=='__main__':unittest.main()
