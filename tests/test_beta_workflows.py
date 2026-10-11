import contextlib
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from playlist_bridge import api, jobs, match_queue, console_logging
from playlist_bridge.storage import Repository


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name))
        self.store = jobs.Store(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_edits_sync_one_playlist_once(self):
        key = 'spotify:one'
        playlist = {'source': 'spotify', 'source_id': 'one', 'plex_playlist_name': 'One'}
        tracks = [{'title': f'Track {n}', 'artist': 'Artist', 'album': ''} for n in range(5)]
        changes = [{'playlist_key': key, 'playlist_name': 'One', 'search_key': f"{t['title']}|Artist",
                    'source': t, 'before': 'unresolved', 'previous_plex_id': None, 'plex_id': str(n), 'provenance': 'manual'} for n, t in enumerate(tracks)]
        match_queue.stage_changes(self.repo, changes)
        config = SimpleNamespace(config={'playlists': [playlist]}, mapping={}, missing={key: tracks},
                                 ignored_tracks={}, match_metadata={}, save=Mock())
        plex = Mock()
        plex.search_library.return_value = [{**t, 'plex_id': str(n)} for n, t in enumerate(tracks)]
        with patch.object(api, '_config', return_value=config), patch.object(api, '_health_plex', return_value=plex), patch.object(api, 'job_store', return_value=self.store), patch('playlist_bridge.process_lock.ProcessLock', return_value=contextlib.nullcontext()), patch.object(api, '_capture', return_value=({'errors': 0}, '')) as sync, patch.object(jobs, 'activity') as activity:
            result = match_queue.execute({'changes': match_queue.available(self.repo)})
        activity.assert_called_with(mode='completed', stage='Playlist completed')
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(result['matches_saved'], 5)
        self.assertEqual(result['total'], 1)
        self.assertEqual(len(config.mapping[key]), 5)
        self.assertFalse(config.missing[key])
        self.assertFalse(match_queue.available(self.repo))

    def test_saved_edits_survive_failed_sync_as_partial_success(self):
        key = 'spotify:one'
        playlist = {'source': 'spotify', 'source_id': 'one', 'plex_playlist_name': 'One'}
        tracks = [{'title': f'Track {n}', 'artist': 'Artist', 'album': ''} for n in range(5)]
        changes = [{'playlist_key': key, 'playlist_name': 'One', 'search_key': f"{t['title']}|Artist",
                    'source': t, 'before': 'unresolved', 'previous_plex_id': None, 'plex_id': str(n), 'provenance': 'manual'} for n, t in enumerate(tracks)]
        match_queue.stage_changes(self.repo, changes)
        config = SimpleNamespace(config={'playlists': [playlist]}, mapping={}, missing={key: tracks},
                                 ignored_tracks={}, match_metadata={}, save=Mock())
        plex = Mock()
        plex.search_library.return_value = [{**t, 'plex_id': str(n)} for n, t in enumerate(tracks)]
        with patch.object(api, '_config', return_value=config), patch.object(api, '_health_plex', return_value=plex), patch.object(api, 'job_store', return_value=self.store), patch('playlist_bridge.process_lock.ProcessLock', return_value=contextlib.nullcontext()), patch.object(api, '_capture', return_value=({'errors': 1}, '')) as sync, patch.object(jobs, 'activity') as activity:
            result = match_queue.execute({'changes': match_queue.available(self.repo)})
        self.assertEqual(activity.call_args.kwargs['mode'], 'failed')
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(result['matches_saved'], 5)
        self.assertEqual(result['total'], 1)
        self.assertEqual(len(config.mapping[key]), 5)
        self.assertFalse(config.missing[key])
        self.assertFalse(match_queue.available(self.repo))

        self.assertTrue(result['partial_success'])
        self.assertFalse(result['playlists'][0]['ok'])
        self.assertIn('Match edits saved', result['playlists'][0]['error'])

    def test_verification_names_absent_tracks_and_accepts_duplicate_collapse(self):
        from playlist_bridge.verification import compare, track_details
        expected = ['1','2','1']
        actual = [{'plex_id':'1'}]
        library = [{'plex_id':'1','title':'Repeated','artist':'Artist'}, {'plex_id':'2','title':'Missing Song','artist':'Other','album':'Album'}]
        self.assertFalse(compare(expected, actual)['ok'])
        details = track_details(expected, actual, library)
        self.assertTrue(any('Missing Song — Other' in line and 'entire track absent' in line for line in details))
        self.assertTrue(any('Repeated' in line and 'occurrence difference' in line for line in details))
        self.assertTrue(compare(expected, [{'plex_id':'1'},{'plex_id':'2'}])['ok'])

    def test_latest_draft_replaces_choice_without_duplicate(self):
        change = {'playlist_key': 'spotify:one', 'search_key': 'Title|Artist', 'plex_id': '1'}
        match_queue.stage_changes(self.repo, [change])
        match_queue.stage_changes(self.repo, [{**change, 'plex_id': '2'}])
        rows = match_queue.available(self.repo)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['plex_id'], '2')

    def test_draft_returns_after_cancelled_job(self):
        match_queue.stage_changes(self.repo, [{'playlist_key': 'x', 'search_key': 't|a'}])
        row = match_queue.available(self.repo)[0]
        job_id = self.store.enqueue('match_batch', {'changes': [row]})
        with self.repo.connect() as db:
            import json
            db.execute("UPDATE state SET value=? WHERE namespace='match_drafts' AND key=?", (json.dumps({**row, 'job_id': job_id}), row['id']))
        self.assertFalse(match_queue.available(self.repo))
        self.store.update(job_id, status='cancelled')
        self.assertEqual(len(match_queue.available(self.repo)), 1)

    def test_ignore_queues_without_taking_active_sync_lock(self):
        with patch.object(api, 'job_store', return_value=self.store), patch.object(api, 'ProcessLock', side_effect=RuntimeError('Busy')) as lock:
            job = api.queue_ignore(api.IgnoreRequest(title='Track', artist='Artist', playlist_keys=['spotify:one']))
        self.assertEqual(job['action'], 'ignore')
        self.assertEqual(job['status'], 'queued')
        lock.assert_not_called()

    def test_console_polling_debug_filter_preserves_errors(self):
        filt = console_logging.AccessFilter()
        def record(status):
            return logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s', ('client', 'GET', '/api/jobs', '1.1', status), None)
        with patch.object(console_logging, 'enabled', False):
            self.assertFalse(filt.filter(record(200)))
            self.assertTrue(filt.filter(record(500)))
        with patch.object(console_logging, 'enabled', True):
            row = record(200)
            self.assertTrue(filt.filter(row))
            self.assertEqual(row.levelname, 'DEBUG')


if __name__ == '__main__':
    unittest.main()
