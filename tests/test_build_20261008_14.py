import tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from io import BytesIO
from PIL import Image
from fastapi import HTTPException
from playlist_bridge.storage import Repository
from playlist_bridge import artwork_sync
from playlist_bridge.edition_preferences import findings,ranker
from playlist_bridge.metadata_sources import order

class ArtworkAndEditions(unittest.TestCase):
    def setUp(self):
        transport=patch('playlist_bridge.redirect_policy.external_get',side_effect=lambda url,headers:__import__('requests').get(url,headers=headers,stream=True,timeout=(5,5),allow_redirects=False));transport.start();self.addCleanup(transport.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name));self.config=SimpleNamespace(repository=self.repo)
        self.track={'plex_album_id':'10','plex_artist_id':'20','plex_album_thumb':'/old-album','plex_artist_thumb':'/old-artist','plex_artist_art':'/old-art'}
        self.repo.save({'inventory':{'plex':{'machine_identifier':'machine','rows':[self.track]}}})
        self.data={'plex_albums':{'10':self.track},'plex_artists':{'20':self.track},'plex_album_names':{('band','record'):self.track},'plex_artist_names':{'band':self.track},'albums':{'group':{'artist':'Band','title':'Record'}},'artists':{'1':{'name':'Band'}},'album_links':{},'artist_links':{}}
        self.client=SimpleNamespace(base_url='http://plex',headers={'X-Plex-Token':'secret'},machine_identifier='machine')
        image=BytesIO();Image.new('RGB',(5,5)).save(image,'PNG');self.image=image.getvalue()
    def response(self,data=None,content=b''):
        return Mock(json=lambda:data,content=content,headers={'Content-Type':'image/png'},iter_content=lambda **kwargs:iter([content]))
    def test_plex_is_first_even_with_old_order_or_disabled_field(self):
        for field in ('artist_portrait','artist_hero','album_artwork'):
            self.assertEqual(order({'source_order':{field:['fanart','apple']},field:'none'},field)[0],'plex')
    def test_direct_and_reviewed_associations_avoid_wrong_album(self):
        self.data['album_links']={'group':{'plex_album_id':'missing'}}
        self.assertEqual(artwork_sync.source(self.data,'album','group'),{})
        self.assertEqual(artwork_sync.source(self.data,'album','plex:10')['plex_album_id'],'10')
    def test_album_portrait_and_background_upload_select_verify(self):
        for target,ident,kind,rating,path_field,metadata_field,resource in [('album','group','cover','10','plex_album_thumb','thumb','posters'),('artist','1','portrait','20','plex_artist_thumb','thumb','posters'),('artist','1','hero','20','plex_artist_art','art','arts')]:
            with self.subTest(kind=kind):
                responses=[self.response(content=self.image),self.response({'MediaContainer':{'Metadata':[{'ratingKey':'old'}]}}),self.response({'MediaContainer':{'Metadata':[{'ratingKey':'old'},{'ratingKey':'upload://new'}]}}),self.response({'MediaContainer':{'Metadata':[{'ratingKey':'upload://new','selected':True}]}}),self.response({'MediaContainer':{'Metadata':[{'ratingKey':rating,metadata_field:'/new-'+kind}]}})]
                with patch('playlist_bridge.library_inventory.indexed',return_value=self.data),patch('playlist_bridge.api._health_plex',return_value=self.client),patch('playlist_bridge.accounts.root_repository',return_value=self.repo),patch('playlist_bridge.accounts.member_stores',return_value=[]),patch('playlist_bridge.artwork_sync.requests.get',side_effect=responses),patch('playlist_bridge.artwork_sync.requests.post',return_value=self.response()) as post,patch('playlist_bridge.artwork_sync.requests.put',return_value=self.response()) as put:
                    result=artwork_sync.choose(self.config,target,ident,kind,'https://images/art.png','fanart')
                self.assertEqual(result['status'],'synced');self.assertTrue(post.call_args.args[0].endswith('/'+resource))
                self.assertEqual(post.call_args.kwargs['data'],self.image)
                self.assertEqual(put.call_args.kwargs['params'],{'url':'upload://new'})
                self.assertEqual(self.repo.load_one('inventory','plex')['rows'][0][path_field],'/new-'+kind)
    def test_failed_upload_does_not_save_choice(self):
        self.repo.save({'artwork_overrides':{'album:group:cover':{'url':'previous'}}})
        with patch('playlist_bridge.library_inventory.indexed',return_value=self.data),patch('playlist_bridge.api._health_plex',return_value=self.client),patch('playlist_bridge.artwork_sync.requests.get',side_effect=__import__('requests').ConnectionError()):
            with self.assertRaises(HTTPException):artwork_sync.choose(self.config,'album','group','cover','https://images/new.png','fanart')
        self.assertEqual(self.repo.load_one('artwork_overrides','album:group:cover')['url'],'previous')
    def test_unlinked_choice_waits_without_network(self):
        self.data['plex_album_names']={}
        with patch('playlist_bridge.library_inventory.indexed',return_value=self.data),patch('playlist_bridge.accounts.root_repository',return_value=self.repo),patch('playlist_bridge.artwork_sync.requests.get') as get:
            result=artwork_sync.choose(self.config,'album','group','cover','https://images/new.png','fanart')
        get.assert_not_called();self.assertEqual(result['status'],'pending')
    def test_release_issue_only_when_better_preference_available(self):
        self.repo.save({'musicbrainz_settings':{'preferences':{'preferred_countries':['US','XW'],'preferred_formats':['Digital Media','CD'],'prefer_official':True}}})
        a={'foreignReleaseId':'a','status':'Official','country':['United States'],'media':[{'mediumFormat':'Digital Media'}],'monitored':False}
        b={'foreignReleaseId':'b','status':'Official','country':['[Worldwide]'],'media':[{'mediumFormat':'CD'}],'monitored':True}
        inv={'checked_at':time.time(),'rows':[{'album_id':'group','title':'Record','artist':'Band','releases':[a,b]}]}
        with patch('playlist_bridge.lidarr.config',return_value={'url':'http://lidarr'}):
            self.assertFalse(findings(inv,self.repo)) # CD and worldwide are both acceptable
            b['media']=[{'mediumFormat':'Vinyl'}]
            result=findings(inv,self.repo);self.assertEqual(len(result),1);self.assertEqual(result[0]['evidence']['preferred_release'],'a')
            a['monitored']=True;b['monitored']=False;self.assertFalse(findings(inv,self.repo))
            a['monitored']=False;b['monitored']=True;b['country']=['United States'];b['media']=[{'mediumFormat':'Digital Media'}]
            self.assertFalse(findings(inv,self.repo)) # lexical tiebreak is not a preference difference
            del a['status'];self.assertFalse(findings(inv,self.repo)) # legacy/incomplete evidence
    def test_new_artwork_survives_an_older_inflight_scan(self):
        from playlist_bridge.inventory import publish
        started=time.monotonic()-30
        self.repo.save({'artwork_plex_sync':{'album:group:cover':{'machine_identifier':'machine','id_key':'plex_album_id','plex_id':'10','path_key':'plex_album_thumb','path':'/new-cover','applied_at':time.time()}}})
        with patch('playlist_bridge.accounts.root_repository',return_value=self.repo):
            result=publish(self.repo,'plex','identity',[dict(self.track)],started,machine_identifier='machine')
            self.assertEqual(result['rows'][0]['plex_album_thumb'],'/new-cover')
            fresh=publish(self.repo,'plex','identity',[{**self.track,'plex_album_thumb':'/changed-in-plex'}],time.monotonic(),machine_identifier='machine')
            self.assertEqual(fresh['rows'][0]['plex_album_thumb'],'/changed-in-plex')
    def test_release_snapshot_keeps_preferences_without_track_payload(self):
        from playlist_bridge.edition_preferences import release_summaries
        result=release_summaries([{'status':'Official','country':['United States'],'media':[{'mediumFormat':'CD','tracks':[{'title':'Huge catalog'}]}]}])
        self.assertEqual(result[0]['media'],[{'mediumFormat':'CD'}]);self.assertEqual(result[0]['status'],'Official')
    def test_pending_choice_is_sent_when_link_becomes_available(self):
        key='album:group:cover'
        self.repo.save({'artwork_overrides':{key:{'url':'https://image/cover.png','provider':'fanart','sync_status':'pending'}}})
        with patch('playlist_bridge.accounts.root_repository',return_value=self.repo),patch('playlist_bridge.artwork_sync.apply',return_value={'status':'synced'}) as apply:
            result=artwork_sync.pending(self.config)
        apply.assert_called_once();self.assertFalse(result['partial_success'])
        self.assertEqual(self.repo.load_one('artwork_overrides',key)['sync_status'],'synced')
