from playlist_bridge.config import Config
import unittest
from unittest.mock import patch
import test_30
from playlist_bridge import api, accounts
from playlist_bridge.track_routes import inspect
from playlist_bridge.playlist_overlays import entries

class HealthMembership(test_30.MultiUser):
    def config(self,source=None,health=None,overlay=None):
        repo=accounts.personal_repository()
        repo.save({'runtime':{'playlists':[{'source':'spotify','source_id':'one','plex_playlist_name':'One'}]},
            'source_snapshots':{} if source is None else {'spotify:one':source},
            'health':{'spotify:one':{'source_preview':health or []}},
            'playlist_overlays':{'spotify:one':overlay or {}}})
        return Config(read_only=True)
    def test_member_selected_health_and_foreign_key(self):
        with accounts.as_user(self.a):
            self.config([])
            self.assertEqual(api.validated_payload('health',{'scope':'selected','playlist_keys':['spotify:one']}),{'scope':'selected','playlist_keys':['spotify:one']})
            with self.assertRaises(api.HTTPException):api.validated_payload('health',{'scope':'selected','playlist_keys':['spotify:foreign']})
            with self.assertRaises(api.HTTPException):api.validated_payload('health',{'scope':'all'})
    def test_health_execution_retains_selection(self):
        with accounts.as_user(self.admin):
            config=self.config([])
            config.config['playlists'].append({'source':'spotify','source_id':'two'})
            with patch.object(api,'_config',return_value=config),patch.object(api,'_health_batch',return_value={}) as batch:
                api.execute_job('health',{'scope':'selected','playlist_keys':['spotify:one']})
            self.assertEqual([api._playlist_key(p) for p in batch.call_args.args[0]],['spotify:one'])
    def test_health_route_uses_post(self):
        routes=[r for r in api.app.routes if getattr(r,'path','')=='/api/playlists/{playlist_key:path}/health']
        self.assertEqual(len(routes),1);self.assertEqual(routes[0].methods,{'POST'})
    def test_current_source_replaces_old_health(self):
        with accounts.as_user(self.a):
            old={'title':'Old','artist':'Band'};new={'title':'New','artist':'Band'}
            config=self.config([new,new],[old])
            self.assertEqual(inspect(config,old)['occurrences'],0)
            self.assertEqual(inspect(config,new)['occurrences'],2)
    def test_empty_current_source_does_not_resurrect_health(self):
        with accounts.as_user(self.a):
            track={'title':'Old','artist':'Band'}
            self.assertEqual(inspect(self.config([],[track]),track)['occurrences'],0)
    def test_overlay_removed_and_added_membership(self):
        with accounts.as_user(self.a):
            old={'title':'Old','artist':'Band'};new={'title':'New','artist':'Band','editor_id':'bridge:one'}
            overlay={'removed':[entries([old])[0]['editor_id']],'added':[new]}
            config=self.config([old],[old],overlay)
            self.assertEqual(inspect(config,old)['occurrences'],0)
            self.assertEqual(inspect(config,new)['occurrences'],1)
    def test_historical_fallback_labeled(self):
        with accounts.as_user(self.a):
            track={'title':'Old','artist':'Band'}
            row=inspect(self.config(None,[track]),track)['memberships'][0]
            self.assertFalse(row['count_exact']);self.assertEqual(row['source_basis'],'historical health')
