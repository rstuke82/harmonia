import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from playlist_bridge import api
from playlist_bridge.storage import Repository
from playlist_bridge.saved_views import _providers

class PlaylistDetailConsistency(unittest.TestCase):
    def test_current_rows_replace_stale_missing_counts_without_service_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository(Path(tmp))
            playlist={'source':'text','source_id':'example','name':'Example'}
            key='text:example'
            tracks=[{'title':'Found','artist':'Artist'},{'title':'Missing','artist':'Artist'},{'title':'Ignored','artist':'Artist'}]
            cfg=SimpleNamespace(repository=repo,config={'playlists':[playlist]},source_snapshots={key:tracks},mapping={key:{'Found|Artist':'1'}},missing={key:[{'title':str(i)} for i in range(5)]})
            syncer=SimpleNamespace(_get_match_provenance=lambda *a:'manual',_same_missing_identity=lambda *a:False,_find_ignored_track_key=lambda key,t:t['title']=='Ignored')
            fresh={'saved_matches':94,'unresolved':5,'lost':0,'last_synced':'2026-10-09T12:29:00+00:00','health':{'checked_at':'2026-10-09T14:00:00+00:00'}}
            with patch('playlist_bridge.api._config',return_value=cfg),patch('playlist_bridge.api.Syncer',return_value=syncer),patch('playlist_bridge.api._playlist_payload',return_value=fresh),patch('playlist_bridge.inventory.current',return_value=({'rows':[{'plex_id':'1','title':'Found'}]},{})),patch('playlist_bridge.track_bridge.context',return_value={}),patch('playlist_bridge.track_bridge.source_status',return_value=''):
                data=api.playlist_detail(key)
            self.assertEqual(data['playlist']['unresolved'],1)
            self.assertEqual(data['playlist']['ignored'],1)
            self.assertEqual(data['playlist']['match_counts'],{'manual':1})
            self.assertEqual(sum(t['needs_attention'] for t in data['tracks']),1)
            self.assertEqual(data['playlist']['last_synced'],'2026-10-09T12:29:00+00:00')
            self.assertEqual(data['playlist']['health']['checked_at'],'2026-10-09T14:00:00+00:00')
    def test_health_changes_invalidate_detail_projection(self):
        deps=_providers['playlist details'][1]
        self.assertTrue({'health','health_attempts'} <= deps)

    def test_health_missing_occurrences_are_actionable_but_old_checks_do_not_survive_sync(self):
        from playlist_bridge.playlist_summary import summarize
        rows=[{'status':'Manual','plex_id':'1','match':{'title':'Found'}} for _ in range(2)]
        payload={'last_synced':'2026-10-09T12:00:00+00:00','health':{'checked_at':'2026-10-09T13:00:00+00:00','drift_details':{'missing_from_plex':[{'plex_id':'1','count':1}]}}}
        result=summarize(payload,rows)
        self.assertEqual(result['unresolved'],0)
        self.assertEqual(result['attention_tracks'],1)
        payload['last_synced']='2026-10-09T14:00:00+00:00'
        self.assertEqual(summarize(payload,rows)['attention_tracks'],0)
