"""Contract checks for reviewed Lidarr writes; all network calls are fakes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from fastapi import FastAPI, HTTPException
from harmonia import lidarr, jobs
from harmonia.storage import Repository

ALBUM = '11111111-1111-4111-8111-111111111111'
ARTIST = '22222222-2222-4222-8222-222222222222'


class FakeClient:
    def __init__(self, existing=False, artist_exists=False):
        self.calls = []
        self.artist = {'foreignArtistId': ARTIST, 'artistName': 'Artist', 'monitored': True}
        if artist_exists:
            self.artist.update(id=4, path='/existing/Artist', qualityProfileId=8, metadataProfileId=9)
        self.album = {'foreignAlbumId': ALBUM, 'title': 'Album', 'artist': self.artist, 'monitored': False,'anyReleaseOk':False,'releases':[{'id':1,'foreignReleaseId':'fixture-release','monitored':True,'status':'Official','country':['United States'],'media':[{'mediumFormat':'CD'}],'trackCount':12}]}
        self.existing = existing

    def options(self):
        return {'version': 'test', 'roots': [{'id': 1, 'path': '/music'}],
                'qualities': [{'id': 1, 'name': 'Lossless'}], 'metadata': [{'id': 1, 'name': 'Standard'}]}

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        if path == 'album/lookup':
            return [copy.deepcopy(self.album)]
        if method == 'GET' and path == 'album':
            return [{**copy.deepcopy(self.album), 'id': 7}] if self.existing else []
        if method == 'POST' and path == 'album':
            self.existing = True
            return {'id': 7}
        if method == 'GET' and path == 'command':
            return []
        if method == 'GET' and path == 'album/7':
            return {**copy.deepcopy(self.album), 'id':7, 'artistId':4}
        if method == 'GET' and path == 'track':
            return [{'id':1,'albumId':7}]
        if path == 'artist/4' and method == 'GET':
            return {**self.artist, 'tags': [4]}
        if method == 'POST' and path == 'command':
            return {'id':42,'status':'completed'}
        return {}


class LidarrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name))
        self.cfg = lidarr.Settings(enabled=True, url='http://lidarr:8686', api_key='secret-key',
            root_folder='/music', quality_profile_id=1, metadata_profile_id=1).model_dump()
        self.repo.save({'lidarr': {'settings': self.cfg}})
        self.defaults = lidarr.Defaults(**self.cfg).model_dump()
        self.client = FakeClient()
        self.patches = [patch.object(lidarr, 'repository', return_value=self.repo),
                        patch.object(lidarr, 'Client', return_value=self.client), patch.object(lidarr.time, 'sleep')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def preview(self):
        result = lidarr.prepare(self.repo, lidarr.Preview(album_id=ALBUM, **self.defaults))
        return result, lidarr.state_get(self.repo, 'lidarr_previews', result['preview_id'])

    def test_existing_search_deduplicates_and_never_adds_or_changes_monitoring(self):
        self.client.existing = True
        app = FastAPI()
        lidarr.register(app)
        endpoint = next(r.endpoint for r in app.routes if r.path == '/api/lidarr/search-existing')
        store = jobs.Store(self.repo)
        with patch('harmonia.api.job_store', return_value=store):
            first = endpoint(lidarr.ExistingSearch(album_id=ALBUM, source_title='Track', source_artist='Artist'))
            second = endpoint(lidarr.ExistingSearch(album_id=ALBUM, source_title='Other track', source_artist='Artist'))
        self.assertEqual(first['id'], second['id'])
        record = lidarr.state_get(self.repo, 'lidarr_requests', ALBUM)
        self.assertEqual(len(record['sources']), 2)
        result = lidarr.retry_search(first['payload'])
        self.assertTrue(result['search_requested'])
        writes = [(m,p) for m,p,_ in self.client.calls if m != 'GET']
        self.assertEqual(writes, [('POST','command')])
        self.assertFalse(self.client.album['monitored'])

    def test_lidarr_lookup_applies_saved_release_priority_before_limit(self):
        app = FastAPI()
        lidarr.register(app)
        endpoint = next(r.endpoint for r in app.routes if r.path == '/api/lidarr/search')
        rows = [{**self.client.album, 'title':f'Live {n}', 'albumType':'Album', 'secondaryTypes':['Live']} for n in range(50)]
        rows.append({**self.client.album, 'title':'Studio', 'albumType':'album', 'secondaryTypes':[]})
        with patch.object(self.client, 'call', return_value=rows):
            result = endpoint(lidarr.Lookup(query='Artist - Album'))
        self.assertEqual(result['rows'][0]['title'], 'Studio')
        self.assertEqual(len(result['rows']), 50)

    def test_existing_search_rejects_absent_album(self):
        app = FastAPI()
        lidarr.register(app)
        endpoint = next(r.endpoint for r in app.routes if r.path == '/api/lidarr/search-existing')
        with self.assertRaises(HTTPException) as error:
            endpoint(lidarr.ExistingSearch(album_id=ALBUM))
        self.assertEqual(error.exception.status_code, 409)

    def test_read_only_preview_then_selected_album_add(self):
        preview, payload = self.preview()
        self.assertFalse(preview['album_exists'])
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))
        self.assertNotIn('secret-key', str(preview) + str(payload))
        lidarr.execute(payload)
        writes = [c for c in self.client.calls if c[0] != 'GET']
        self.assertEqual(len(writes), 1)
        body = writes[0][2]['json']
        self.assertEqual(body['artist']['addOptions']['albumsToMonitor'], [ALBUM])
        self.assertEqual(body['artist']['addOptions']['monitor'], 'unknown')
        self.assertFalse(body['artist']['addOptions']['searchForMissingAlbums'])
        self.assertFalse(body['addOptions']['searchForNewAlbum'])
        self.assertEqual(body['artist']['monitorNewItems'], 'none')

    def test_existing_album_only_monitors_and_searches_selected(self):
        self.client.existing = True
        self.client.artist.update(id=4)
        self.defaults['search_now'] = True
        _, payload = self.preview()
        lidarr.execute(payload)
        writes = [c for c in self.client.calls if c[0] != 'GET']
        self.assertEqual([(m, p) for m, p, _ in writes], [('PUT', 'album/monitor'), ('POST', 'command')])
        self.assertEqual(writes[0][2]['json']['albumIds'], [7])
        self.assertEqual(writes[1][2]['json']['albumIds'], [7])

    def test_existing_artist_settings_are_preserved(self):
        self.client.artist.update(id=4, path='/existing/Artist', qualityProfileId=8, metadataProfileId=9)
        _, payload = self.preview()
        lidarr.execute(payload)
        body = next(c[2]['json'] for c in self.client.calls if c[:2] == ('POST', 'album'))
        self.assertEqual(body['artist']['qualityProfileId'], 8)
        self.assertEqual(body['artist']['metadataProfileId'], 9)
        self.assertEqual(body['artist']['path'], '/existing/Artist')
        self.assertNotIn('addOptions', body['artist'])

    def test_paused_artist_requires_review_in_lidarr(self):
        self.client.artist.update(id=4, monitored=False)
        with self.assertRaises(HTTPException):
            self.preview()
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))

    def test_confirm_is_idempotent_and_settings_changes_block_execution(self):
        preview, payload = self.preview()
        app = FastAPI()
        lidarr.register(app)
        endpoint = next(r.endpoint for r in app.routes if r.path == '/api/lidarr/add')
        with patch('harmonia.api.job_store', return_value=jobs.Store(self.repo)):
            first = endpoint(lidarr.Confirm(preview_id=preview['preview_id']))
            second = endpoint(lidarr.Confirm(preview_id=preview['preview_id']))
        self.assertEqual(first['id'], second['id'])
        self.repo.save({'lidarr': {'settings': {**self.cfg, 'api_key': 'changed'}}})
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            lidarr.execute(payload)
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))

    def test_key_stays_server_side_and_is_not_reused_for_changed_url(self):
        self.assertNotIn('api_key', lidarr.public_settings(self.cfg))
        same = lidarr.merged(lidarr.Settings(**{**self.cfg, 'api_key': ''}), self.repo)
        self.assertEqual(same['api_key'], 'secret-key')
        other = lidarr.merged(lidarr.Settings(**{**self.cfg, 'api_key': '', 'url': 'http://other:8686'}), self.repo)
        self.assertEqual(other['api_key'], '')

    def test_existing_artist_tags_are_appended(self):
        self.client.existing = True
        self.client.artist.update(id=4)
        self.defaults.update(tags=[7], tag_existing=True)
        self.client.options = lambda: {'version': 'test', 'roots': [{'id': 1, 'path': '/music'}], 'qualities': [{'id': 1, 'name': 'Lossless'}], 'metadata': [{'id': 1, 'name': 'None'}], 'tags': [{'id': 7, 'label': 'bridge'}]}
        preview, payload = self.preview()
        self.assertEqual(preview['metadata'], 'None')
        lidarr.execute(payload)
        update = next(c[2]['json'] for c in self.client.calls if c[:2] == ('PUT', 'artist/4'))
        self.assertEqual(update['tags'], [4, 7])

    def test_musicbrainz_persistent_cache(self):
        response = Mock()
        response.json.return_value = {'recordings': [{'id':'recording','title':'Track','artist-credit': [{'name': 'Artist'}],
            'releases': [{'release-group': {'id': ALBUM, 'title': 'Album', 'primary-type': 'Album'}}]}]}
        query = lidarr.Lookup(title='Track', artist='Artist', provider='musicbrainz')
        response.json.return_value['releases']=[{**r,'artist-credit':[{'name':'Artist'}]} for r in response.json.return_value['recordings'][0]['releases']]
        with patch('harmonia.musicbrainz_client._session.get', return_value=response) as get:
            result = lidarr.musicbrainz_search(self.repo, self.cfg, query)
            self.assertFalse(result['cached'])
            result = lidarr.musicbrainz_search(Repository(Path(self.tmp.name)), self.cfg, query)
            self.assertTrue(result['cached'])
            self.assertEqual(get.call_count, 2)
        self.assertEqual(result['rows'][0]['album_id'], ALBUM)

    def test_new_album_waits_without_starting_refresh(self):
        self.defaults['search_now'] = True
        _, payload = self.preview()
        original = self.client.call
        states = iter([[{'status':'started','body':{'name':'RefreshAlbum','albumId':7}}], [], []])
        def call(method,path,**kwargs):
            result = original(method,path,**kwargs)
            return next(states) if method == 'GET' and path == 'command' else result
        with patch.object(self.client,'call',side_effect=call):
            result = lidarr.execute(payload)
        self.assertEqual([c[2]['json']['name'] for c in self.client.calls if c[:2]==('POST','command')],['AlbumSearch'])
        self.assertEqual(sum(c[:2]==('GET','command') for c in self.client.calls),3)
        self.assertTrue(result['search_requested'])
        body = next(c[2]['json'] for c in self.client.calls if c[:2]==('POST','album'))
        self.assertFalse(body['addOptions']['searchForNewAlbum'])

    def test_running_search_wait_is_not_a_request_error(self):
        self.defaults['search_now'] = True
        _, payload = self.preview()
        from harmonia.lidarr_requests import server_id
        lidarr.state_put(self.repo, 'lidarr_requests', ALBUM, {'server':server_id(self.cfg),'status':'added'})
        def running(client, body, label, existing_id=None, accepted=None):
            accepted(42)
            raise ValueError('command 42 still started after 120s')
        with patch.object(lidarr, 'run_album_command', side_effect=running):
            lidarr.finish_search(self.repo, self.client, payload, 7)
        record=lidarr.state_get(self.repo,'lidarr_requests',ALBUM)
        self.assertEqual(record['status'],'search_pending')
        self.assertEqual(record['error'],'')
        self.assertIn('still started',record['search_wait_detail'])
        self.assertEqual(record['search_command_id'],42)

    def test_preview_and_queued_request_keep_album_artwork(self):
        self.client.album['images']=[{'coverType':'cover','remoteUrl':'https://example.org/album.jpg'}]
        preview,payload=self.preview()
        self.assertEqual(payload['artwork'],'https://example.org/album.jpg')
        app=FastAPI();lidarr.register(app)
        endpoint=next(r.endpoint for r in app.routes if r.path=='/api/lidarr/add')
        with patch('harmonia.api.job_store',return_value=jobs.Store(self.repo)):
            endpoint(lidarr.Confirm(preview_id=preview['preview_id']))
        self.assertEqual(lidarr.state_get(self.repo,'lidarr_requests',ALBUM)['artwork'],payload['artwork'])

    def test_empty_inventory_cover_does_not_erase_request_artwork(self):
        from harmonia import lidarr_requests
        lidarr.state_put(self.repo,'lidarr_requests',ALBUM,{
            'server':lidarr_requests.server_id(self.cfg),'status':'search_pending',
            'title':'Album','artist':'Artist','artwork':'https://example.org/album.jpg',
            'error':'command 42 still started after 120s'})
        self.repo.save({'inventory':{'lidarr':{'rows':[{'album_id':ALBUM,'title':'Album','artist':'Artist','artwork':''}]}}})
        app=FastAPI();lidarr_requests.register(app)
        endpoint=next(r.endpoint for r in app.routes if r.path=='/api/lidarr/requests')
        with patch('harmonia.accounts.users',return_value={}):
            row=endpoint.__wrapped__(compute_availability=False)[0]
        self.assertEqual(row['artwork'],'https://example.org/album.jpg')
        self.assertEqual(row['error'],'')
        self.assertIn('still started',row['search_wait_detail'])

    def test_failed_search_is_partial_and_preserves_exact_exception(self):
        self.defaults['search_now'] = True
        _, payload = self.preview()
        original = self.client.call
        def call(method,path,**kwargs):
            result = original(method,path,**kwargs)
            if method == 'POST' and path == 'command':
                return {'id':42,'status':'failed','message':'Failed','exception':'Specific upstream failure'}
            return result
        with patch.object(self.client,'call',side_effect=call):
            result=lidarr.execute(payload)
        self.assertTrue(result['partial_success'])
        self.assertIn('Specific upstream failure',result['error'])
        self.assertEqual(result['summary'],'Album added; search failed')
        self.assertEqual(sum(c[:2]==('POST','album') for c in self.client.calls),1)

    def test_album_request_deduplicates_across_tracks(self):
        from harmonia.lidarr_requests import register, server_id
        app=FastAPI();lidarr.register(app);register(app)
        add=next(r.endpoint for r in app.routes if r.path=='/api/lidarr/add')
        retry=next(r.endpoint for r in app.routes if r.path=='/api/lidarr/requests/{album}/retry-search')
        with patch('harmonia.api.job_store',return_value=jobs.Store(self.repo)):
            first,_=self.preview()
            j1=add(lidarr.Confirm(preview_id=first['preview_id'],source_title='One',source_artist='Artist'))
            second,_=self.preview()
            j2=add(lidarr.Confirm(preview_id=second['preview_id'],source_title='Two',source_artist='Artist'))
            self.assertEqual(j1['id'],j2['id'])
            record=lidarr.state_get(self.repo,'lidarr_requests',ALBUM)
            self.assertEqual(len(record['sources']),2)
            jobs.Store(self.repo).update(j1['id'],status='completed')
            from harmonia.lidarr_requests import save
            save(self.repo,ALBUM,lidarr_id=7,status='search_failed')
            retried=retry(ALBUM)
            again=retry(ALBUM)
            self.assertEqual(retried['id'],again['id'])
            self.assertEqual(retried['action'],'lidarr_search')
        self.client.existing=True
        self.client.calls.clear()
        result=lidarr.retry_search({'album_id':ALBUM,'server':server_id(self.cfg)})
        self.assertTrue(result['search_requested'])
        writes=[c for c in self.client.calls if c[0]!='GET']
        self.assertEqual([c[1] for c in writes],['command'])
        self.assertEqual(writes[0][2]['json']['name'],'AlbumSearch')


if __name__ == '__main__':
    unittest.main()
