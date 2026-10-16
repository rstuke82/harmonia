import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from harmonia import accounts,library,saved_views,library_inventory
from harmonia.jobs import Store
from harmonia.storage import Repository

ARTIST='11111111-1111-1111-1111-111111111111'
ALBUM='22222222-2222-2222-2222-222222222222'
OTHER='33333333-3333-3333-3333-333333333333'

class AlbumReads(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.repo=Repository(Path(tmp.name));self.user={'id':'audit','admin':True,'name':'Owner'}
        self.config=SimpleNamespace(repository=self.repo,config={'plex':{'url':'http://plex','token':'fixture','music_library_key':'1'}})
        self.lidarr={'identity':'fixture','checked_at':time.time(),'artists':[{'id':1,'name':'Artist','mbid':ARTIST,'path':'/private/artist'}],
            'rows':[{'album_id':ALBUM,'id':10,'artist_id':1,'artist':'Artist','artist_mbid':ARTIST,'title':'Partial','monitored':True,'statistics':{'trackFileCount':1,'trackCount':12}},
                    {'album_id':OTHER,'id':11,'artist_id':1,'artist':'Artist','artist_mbid':ARTIST,'title':'Other','statistics':{'trackFileCount':12,'trackCount':12}}],
            'tracks':[{'id':'t1','album_id':ALBUM,'title':'Song','artist':'Artist','album':'Partial','file':'/music/song.flac','track_number':1}]}
        self.plex={'checked_at':time.time(),'rows':[{'plex_id':'p1','plex_artist_id':'pa','plex_album_id':'pal','title':'Song','artist':'Artist','album':'Partial','files':['/music/song.flac'],'plex_album_thumb':'/library/metadata/pal/thumb'}]}
        for target,value in [('harmonia.accounts.root_repository',self.repo),('harmonia.saved_views.personal_repository',self.repo),('harmonia.api._config',self.config),('harmonia.lidarr.config',{'enabled':True,'url':'http://lidarr'} )]:
            mock=patch(target,return_value=value);mock.start();self.addCleanup(mock.stop)
        mock=patch('harmonia.inventory.current',return_value=(self.plex,self.lidarr));self.current=mock.start();self.addCleanup(mock.stop)
        mock=patch('harmonia.api._record_log');mock.start();self.addCleanup(mock.stop)
        self.repo.save({'accounts':{'audit':self.user},'metadata_sources':{'settings':{'source_order':{'album_artwork':['apple','coverartarchive']}}},'local_artist_enrichment':{ARTIST:{'albums':[{'title':'Partial','artwork':'https://example.org/apple.jpg','url':'https://example.org/album'}]}}})
        library.invalidate();self.addCleanup(library.invalidate)
        app=FastAPI();library.register(app)
        self.album=next(r.endpoint for r in app.routes if r.path=='/api/library/albums/{album_id}')
        self.artist=next(r.endpoint for r in app.routes if r.path=='/api/library/artists/{artist_id}')
        self.images=next(r.endpoint for r in app.routes if r.path=='/api/library/plex-albums/{plex_album_id}/image')

    def prepare(self,endpoint,*args):
        pending=endpoint(*args)
        self.assertEqual(pending.status_code,202)
        job=Store(self.repo).get(json.loads(pending.body)['job_id'])
        with patch('harmonia.jobs.current',return_value=SimpleNamespace(store=Store(self.repo),id=job['id'],action='prepare_views')),patch('harmonia.jobs.progress'):
            saved_views.execute(job['payload'])
        return json.loads(endpoint(*args).body)

    def test_album_uses_artist_state_artwork_and_enrichment(self):
        with accounts.as_user(self.user):
            detail=self.prepare(self.album,ALBUM)
            artist=self.prepare(self.artist,'1')
        self.assertTrue(artist['tracks_scanned'])
        card=next(r for r in artist['rows'] if r['album_id']==ALBUM)
        for key in ('library_state','status','artwork','itunes_url','musicbrainz_id'):
            self.assertEqual(detail[key],card[key],key)
        self.assertEqual(detail['library_state'],'Wanted')
        self.assertTrue(detail['artwork'].startswith('/api/library/plex-albums/pal/image?v='))
        self.assertEqual(detail['tracks'][0]['plex']['plex_id'],'p1')
        self.assertEqual(self.current.call_count,1)

    def test_prepared_http_reads_never_rebuild_inventory_or_links(self):
        with accounts.as_user(self.user):
            self.prepare(self.album,ALBUM)
            self.prepare(self.album,OTHER)
            with patch('harmonia.inventory.current',side_effect=AssertionError('Decoded inventory during HTTP read')),patch('harmonia.track_bridge.links',side_effect=AssertionError('Rebuilt links during HTTP read')),patch('harmonia.file_audit.report',side_effect=AssertionError('Rebuilt audit during HTTP read')):
                for _ in range(3):
                    response=self.album(ALBUM)
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(json.loads(response.body)['album_id'],ALBUM)
        self.assertEqual(self.current.call_count,1)

    def test_plex_artwork_wins_over_old_override_and_revision_rebuilds_it(self):
        with accounts.as_user(self.user):
            self.prepare(self.album,ALBUM)
            self.repo.save({'artwork_overrides':{'album:'+ALBUM+':cover':{'url':'https://example.org/manual.jpg','provider':'Manual'}}})
            stale=self.album(ALBUM)
            self.assertEqual(stale.headers['X-Saved-View-Stale'],'true')
            row=saved_views.load_row(self.repo,stale.headers['X-Saved-View'])
            job=Store(self.repo).get(row['job_id'])
            with patch('harmonia.jobs.current',return_value=SimpleNamespace(store=Store(self.repo),id=job['id'],action='prepare_views')),patch('harmonia.jobs.progress'):
                saved_views.execute(job['payload'])
            self.assertTrue(json.loads(self.album(ALBUM).body)['artwork'].startswith('/api/library/plex-albums/pal/image?v='))

    def test_member_projection_redacts_paths(self):
        member={'id':'2','admin':False};self.repo.save({'accounts':{'2':member}})
        with accounts.as_user(member):
            data=self.prepare(self.album,ALBUM)
            artist=self.prepare(self.artist,'1')
        self.assertNotIn('path',artist['artist'])
        self.assertEqual(data['issues'],[])
        self.assertNotIn('file',data['tracks'][0])
        self.assertNotIn('files',data['tracks'][0]['plex'])
        self.assertNotIn('local_path',data['tracks'][0])

    def test_image_index_reuse_and_revision_invalidation(self):
        response=SimpleNamespace(status_code=200,content=b'cover',headers={'Content-Type':'image/jpeg'},raise_for_status=lambda:None,iter_content=lambda **kw:iter([b'cover']),close=lambda:None)
        client=SimpleNamespace(base_url='http://plex',headers={},music_library_key='1')
        with accounts.as_user(self.user),patch('harmonia.api._health_plex',return_value=client),patch('harmonia.network.get',return_value=response),patch('harmonia.track_bridge.links',side_effect=AssertionError('Images must not rebuild track links')):
            self.images('pal');self.images('pal')
            self.assertEqual(self.current.call_count,1)
            self.repo.save({'inventory':{'fixture':{'changed':True}}})
            self.images('pal')
            self.assertEqual(self.current.call_count,2)
