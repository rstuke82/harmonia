from types import SimpleNamespace
from unittest.mock import patch,Mock
from fastapi import FastAPI,HTTPException
from playlist_bridge import accounts,library
from playlist_bridge.album_service_links import validate
from test_build_20261007_14 import AlbumReads,ALBUM

class AlbumServiceLinks(AlbumReads):
    def setUp(self):
        super().setUp()
        mock=patch('playlist_bridge.album_apple_artwork.requests.get',return_value=Mock(status_code=200,json=lambda:{'results':[{'wrapperType':'collection','collectionType':'Album','collectionId':123,'artworkUrl100':'https://is1-ssl.mzstatic.com/cover/100x100bb.jpg'}]}))
        mock.start();self.addCleanup(mock.stop)
        app=FastAPI();library.register(app)
        self.get=next(r.endpoint for r in app.routes if r.path.endswith('/service-links') and 'GET' in r.methods)
        self.put=next(r.endpoint for r in app.routes if r.path.endswith('/service-links') and 'PUT' in r.methods)
        self.reset=next(r.endpoint for r in app.routes if r.path.endswith('/service-links/{provider}'))
    def test_save_projects_to_album_and_reset_restores_automatic(self):
        with accounts.as_user(self.user):
            original=self.get(ALBUM)['apple']['url']
            result=self.put(ALBUM,SimpleNamespace(provider='apple',url='https://music.apple.com/us/album/record/123'))
            self.assertTrue(result['apple']['manual'])
            self.assertEqual(self.prepare(self.album,ALBUM)['itunes_url'],'https://music.apple.com/us/album/record/123')
            self.assertEqual(self.reset(ALBUM,'apple')['apple']['url'],original)
    def test_empty_link_suppresses_automatic_and_keeps_other_services(self):
        with accounts.as_user(self.user):
            self.put(ALBUM,SimpleNamespace(provider='spotify',url='https://open.spotify.com/album/abc'))
            result=self.put(ALBUM,SimpleNamespace(provider='apple',url=''))
            self.assertEqual(result['apple']['url'],'');self.assertTrue(result['apple']['manual'])
            self.assertEqual(result['spotify']['url'],'https://open.spotify.com/album/abc')
    def test_member_cannot_edit_and_unknown_album_not_saved(self):
        with accounts.as_user({'id':'member','admin':False}):
            with self.assertRaises(HTTPException) as error:self.put(ALBUM,SimpleNamespace(provider='apple',url=''))
            self.assertEqual(error.exception.status_code,403)
        with accounts.as_user(self.user):
            with self.assertRaises(HTTPException) as error:self.get('unknown')
            self.assertEqual(error.exception.status_code,404)
    def test_service_urls_reject_wrong_host_track_or_credentials(self):
        for provider,url in [('apple','https://music.apple.com.evil.test/us/album/1'),('spotify','https://open.spotify.com/track/abc'),('apple','http://music.apple.com/us/album/1'),('apple','https://user:pass@music.apple.com/us/album/1'),('musicbrainz','https://musicbrainz.org/artist/'+ALBUM)]:
            with self.subTest(url=url),self.assertRaises(HTTPException):validate(provider,url)
        self.assertEqual(validate('musicbrainz','https://musicbrainz.org/release-group/'+ALBUM),'https://musicbrainz.org/release-group/'+ALBUM)
