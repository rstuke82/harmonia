from harmonia.sync_engine import Syncer
from harmonia.matching import Matcher
from harmonia.config import Config
import asyncio,json,time
from unittest.mock import patch
from test_30 import MultiUser
from harmonia import accounts, api
from harmonia.text_playlists import parse,Source,TextSource
from harmonia.track_bridge import links,match,_context
from harmonia.lidarr import state_put

class Beta4(MultiUser):
    def test_text_parser_order_and_review(self):
        rows=parse('1. CAKE — The Distance — Fashion Nugget\n2. Buddy Holly by Weezer\nA heading\nCAKE — The Distance — Fashion Nugget','artist-title')
        self.assertEqual(rows[0]['artist'],'CAKE');self.assertEqual(rows[1]['title'],'Buddy Holly');self.assertTrue(rows[2]['issue']);self.assertEqual(rows[0]['title'],rows[3]['title'])
        self.assertEqual(parse('The Distance - CAKE','title-artist')[0]['artist'],'CAKE')
    def test_text_source_private_and_edit_requires_owner(self):
        with accounts.as_user(self.a):state_put(accounts.personal_repository(),'text_sources','source',{'name':'Mine','tracks':[{'artist':'Artist','title':'Song'}],'revision':1})
        with accounts.as_user(self.b):
            with self.assertRaises(ValueError):TextSource().get_playlist_tracks('text:source')
        status,raw=asyncio.run(self.http('/api/playlists/text',method='POST',user=self.b,body={'name':'Hijack','source_id':'source','revision':1,'tracks':[{'title':'Song','artist':'Artist'}]}))
        self.assertEqual(status,404)
    def test_lidarr_paths_and_ambiguity(self):
        tracks=[{'id':'L','title':'Song','artist':'Artist','album':'Album','file':'/music/a.flac'}]
        plex=[{'plex_id':'P','title':'Song','artist':'Artist','album':'Album','files':['/music/a.flac']}]
        self.assertEqual(links(tracks,plex)['L']['method'],'file path')
        self.assertFalse(links([{**tracks[0],'file':''}],plex))
        self.assertFalse(links(tracks,[{**plex[0],'files':[]},{**plex[0],'plex_id':'Q','files':[]}]))
    def test_catalog_matching_requires_lidarr_and_respects_live(self):
        source={'title':'Song','artist':'Artist','album':'Album'}
        lid={**source,'id':'L','plex_id':'L','recording_id':'R'}
        token=_context.set({'tracks':[lid],'links':{'L':{'plex_id':'P'}},'pending':False})
        try:
            self.assertEqual(match(Matcher,source,[{**source,'plex_id':'P'}]),'P')
            self.assertIsNone(match(Matcher,{**source,'artist':'Other'},[]))
            _context.set({'tracks':[{**lid,'album':'Live in London'}],'links':{'L':{'plex_id':'P'}},'pending':False})
            self.assertIsNone(match(Matcher,source,[]))
        finally:_context.reset(token)
    def test_text_registration_without_playable_tracks_survives(self):
        from harmonia.text_playlists import execute
        with accounts.as_user(self.a),patch.object(api,'execute_job',return_value={'summary':'queued'}) as sync:
            execute({'name':'New list','tracks':[{'title':'Missing','artist':'Artist'}]})
            c=Config();self.assertEqual(c.config['playlists'][0]['source'],'text')
            self.assertEqual(c.config['playlists'][0]['plex_playlist_id'],'')
            url=c.config['playlists'][0]['source_url'];self.assertTrue(Config._extract_id(url,'text'))
            self.assertEqual(TextSource().get_playlist_tracks(url)[0][0]['title'],'Missing')
            self.assertEqual(sync.call_args.args[0],'sync')
    def test_text_sync_without_matches_never_touches_plex(self):
        from unittest.mock import Mock
        from harmonia.text_playlists import execute
        with accounts.as_user(self.a),patch.object(api,'execute_job',return_value={}):
            execute({'name':'Waiting','tracks':[{'title':'Song','artist':'Artist'}]})
            c=Config();p=c.config['playlists'][0];syncer=Syncer(c)
            plex=Mock();syncer.plex=plex
            tracks=[{'title':'Song','artist':'Artist','album':''}]
            stats={'new_matches':[],'lost_matches':[],'recovered_lost':[],'stale_mappings':[],'ignored_tracks':[]}
            with patch.object(syncer,'_get_plex',return_value=plex),patch.object(syncer,'_match_source_tracks',return_value=([],tracks,{},[],stats)):
                result=syncer.sync_playlist(p)
            self.assertFalse(result.get('errors'));plex.replace_playlist_tracks.assert_not_called();plex.get_playlist_items.assert_not_called()
            self.assertTrue(Config().missing)
