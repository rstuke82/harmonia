import unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock
from harmonia import api
from harmonia.plex_client import PlexAPI

class DiscoverShelvesTests(unittest.TestCase):
    def test_album_date_comes_from_plex_album_not_track(self):
        client=object.__new__(PlexAPI);client.base_url='http://plex';client.music_library_key='1';client.music_library_name='Music';client.headers={};client.strict_errors=True
        tracks=Mock(status_code=200);tracks.json.return_value={'MediaContainer':{'Metadata':[{'ratingKey':'t','parentRatingKey':'a','title':'Song','addedAt':900}]}}
        albums=Mock(status_code=200);albums.json.return_value={'MediaContainer':{'Metadata':[{'ratingKey':'a','addedAt':100}]}}
        with patch('harmonia.network.get',side_effect=[tracks,albums,Mock(status_code=200,json=lambda:{'MediaContainer':{'Metadata':[]}})]) as get:
            rows=client._fetch_library()
        self.assertEqual(rows[0]['plex_album_added_at'],100)
        self.assertEqual(get.call_args_list[1].kwargs['params'],{'type':9})
        self.assertEqual(tracks.json.call_count,1);self.assertEqual(albums.json.call_count,1)

    def test_recent_albums_deduplicate_sort_and_use_album_artist(self):
        endpoint=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/library/recently-added').__wrapped__
        tracks=[{'plex_album_id':'a','album':'Compilation','artist':'Performer','album_artist':'Various Artists','plex_album_added_at':100,'plex_album_thumb':'/thumb'}, {'plex_album_id':'a','album':'Compilation','artist':'Other performer','plex_album_added_at':100}, {'plex_album_id':'b','album':'New album','artist':'Band','plex_album_added_at':200}, {'plex_album_id':'old','album':'Unknown date'}]
        with patch('harmonia.api._config',return_value=SimpleNamespace()),patch('harmonia.library_inventory.indexed',return_value={'plex':{'rows':tracks,'checked_at':300}}):
            value=endpoint()
        self.assertEqual([r['album_id'] for r in value['rows']],['plex:b','plex:a'])
        self.assertEqual(value['rows'][1]['artist'],'Various Artists')
        self.assertTrue(value['rows'][1]['artwork'].startswith('/api/library/plex-albums/a/image?v='))
        self.assertEqual(value['checked_at'],300)

    def test_search_words_in_any_order_without_artist_album_separator(self):
        repo=SimpleNamespace(load=lambda name:{})
        cfg=SimpleNamespace(repository=repo,config={'playlists':[]})
        inventory=({'rows':[]},{'rows':[{'album_id':'album','title':'Hybrid Theory','artist':'Linkin Park'}],'artists':[{'id':'1','artistName':'Linkin Park'}]})
        with patch.object(api,'_config',return_value=cfg),patch('harmonia.inventory.current',return_value=inventory),patch('harmonia.lidarr.config',return_value={'enabled':True}):
            result=api.search_library('Theory Linkin')
            artists=api.search_library('Park Linkin')
        self.assertEqual(result['lidarr_albums'][0]['album_id'],'album')
        self.assertEqual(artists['artists'],[{'id':'1','name':'Linkin Park'}])
