import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from harmonia.storage import Repository
from harmonia.musicbrainz_settings import Preferences, settings, ordered
from harmonia import lidarr

class MusicBrainzTests(unittest.TestCase):
    @staticmethod
    def response(data):
        value=Mock();value.json.return_value=data;value.raise_for_status.return_value=None
        return value
    def test_order_and_secondary_types(self):
        rows=[{'type':'Album','secondary_types':['Live'],'title':'Live'}, {'type':'Single','title':'Single'}, {'type':'Album','title':'Studio'}]
        prefs=Preferences().model_dump()
        self.assertEqual([r['title'] for r in ordered(rows,prefs)],['Studio','Single','Live'])
        prefs.update(prefer_studio=False,release_priority=['Single','EP','Album','Other'])
        self.assertEqual(ordered(rows,prefs)[0]['title'],'Single')
        with self.assertRaises(ValueError):Preferences(release_priority=['Album']*4)

    def test_migrate_and_reorder_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite')
            repo.save({'lidarr':{'settings':{'musicbrainz_enabled':False,'cache_days':30}}})
            self.assertFalse(settings(repo)['enabled'])
            self.assertEqual(settings(repo)['cache_days'],30)
            prefs=Preferences().model_dump()
            repo.save({'musicbrainz_settings':{'preferences':prefs}})
            request=lidarr.Lookup(title='Track',artist='Artist',album='Album',provider='musicbrainz')
            import hashlib,time
            key=hashlib.sha256(('release-group'+'releasegroup:"Album" AND artist:"Artist"').encode()).hexdigest()
            lidarr.state_put(repo,'musicbrainz_cache',key,{'at':time.time(),'rows':[{'type':'Single'},{'type':'Album'}]})
            with patch('harmonia.musicbrainz_client.network.get') as get:
                result=lidarr.musicbrainz_search(repo,{},request)
                self.assertTrue(result['cached'])
                self.assertEqual(result['rows'][0]['type'],'Album')
                get.assert_not_called()
            with patch('harmonia.musicbrainz_client.network.get',return_value=self.response({'release-groups':[]})) as get, patch('harmonia.musicbrainz_client.time.sleep'):
                request.force_refresh=True
                result=lidarr.musicbrainz_search(repo,{},request)
                self.assertFalse(result['cached'])
                get.assert_called_once()

    def test_explicit_search_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'bridge.sqlite')
            for values, entity, query in [
                ({'album':'Armageddon'}, 'release-group', 'releasegroup:"Armageddon"'),
                ({'title':'Song','artist':'Artist','album':'Album'}, 'release-group', 'releasegroup:"Album" AND artist:"Artist"'),
                ({'artist':'Artist'}, 'release-group', 'artist:"Artist"'),
            ]:
                with patch('harmonia.musicbrainz_client.network.get',return_value=self.response({})) as get, patch('harmonia.musicbrainz_client.time.sleep'):
                    lidarr.musicbrainz_search(repo,{},lidarr.Lookup(**values, fields_search=True))
                    self.assertTrue(get.call_args.args[0].endswith('/'+entity))
                    self.assertEqual(get.call_args.kwargs['params']['query'],query)

    def test_settings_routes_precede_website_mount(self):
        from harmonia.api import app, mount_frontend
        from fastapi import FastAPI
        from starlette.routing import Mount
        from starlette.routing import Match
        # Verify actual registration order when a production mount is present.
        # Also exercise the production mounting helper with a disposable frontend.
        production = app
        fixture = FastAPI()
        fixture.router.routes = [route for route in app.routes if not (isinstance(route, Mount) and route.name == 'web')]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        directory = Path(folder.name)
        (directory/'index.html').write_text('<html>fixture</html>')
        mount_frontend(fixture, directory)
        for method,path in [('GET','/api/settings/musicbrainz'),('PUT','/api/settings/musicbrainz'),('POST','/api/settings/musicbrainz/test'),('GET','/api/settings/lidarr/tags')]:
            scope={'type':'http','method':method,'path':path,'root_path':''}
            for application in (production, fixture):
                first=next(route for route in application.routes if route.matches(scope)[0] == Match.FULL)
                self.assertEqual(first.path,path)
