import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harmonia.discover import refresh_lastfm,history_tracks
from harmonia.jobs import wait_reason
from harmonia.lidarr import _discovery_title
from harmonia.lidarr_downloads import describe
from harmonia.storage import Repository
from harmonia.tasks import DEFINITIONS


class Build2026100411Tests(unittest.TestCase):
    def test_search_title_removes_release_type_labels_only(self):
        cases = {
            'Record (EP)': 'Record',
            'Record [Single Version]': 'Record',
            'Record - Single': 'Record',
            'Record EP': 'Record',
            'Single Ladies': 'Single Ladies',
            'Epilogue': 'Epilogue',
        }
        for value, expected in cases.items():
            self.assertEqual(_discovery_title(value), expected)

    def test_repository_uses_wal_for_concurrent_page_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory))
            with repo.connect() as db:
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_heavy_jobs_wait_for_each_other(self):
        running = [{'account': 'owner', 'action': 'health'}]
        self.assertIsNone(wait_reason('lidarr_scan', 'owner', running))
        self.assertIsNone(wait_reason('lastfm_refresh', 'owner', running))
        self.assertIsNone(wait_reason('lidarr_search', 'owner', running))

    def test_lastfm_refresh_is_scheduled_and_populates_saved_feeds(self):
        self.assertIn(('lastfm_refresh', 'all', 'Refresh Last.fm Discovery', 6, '00:45', 'metadata'), DEFINITIONS)
        with tempfile.TemporaryDirectory() as directory,patch('harmonia.discover.root_repository',return_value=Repository(Path(directory))),patch('harmonia.user_preferences.preferences', return_value={'lastfm_username': 'listener'}), \
             patch('harmonia.jobs.progress'), \
             patch('harmonia.discover.lastfm', side_effect=[
                 {'recenttracks': {'track': [{'date':{'uts':str(int(__import__('time').time())-100)}}, {'date':{'uts':str(int(__import__('time').time())-90)}}]}},
                 {'topartists': {'artist': []}},
                 {'artists': {'artist': []}},
             ]) as lookup:
            result = refresh_lastfm()
        self.assertIn('2 recent plays', result['summary'])
        self.assertIn('personalized album recommendations cached', result['summary'])
        self.assertTrue(all(call.kwargs['refresh'] for call in lookup.call_args_list))

    def test_lastfm_history_uses_selected_range_and_pagination(self):
        first={'recenttracks':{'track':[{'name':'One','date':{'uts':'1999999900'}}]*200,'@attr':{'totalPages':'2'}}}
        second={'recenttracks':{'track':[{'name':'Two','date':{'uts':'1999999901'}}],'@attr':{'totalPages':'2'}}}
        with tempfile.TemporaryDirectory() as directory,patch('harmonia.discover.root_repository',return_value=Repository(Path(directory))),patch('harmonia.jobs.progress'),patch('harmonia.discover.lastfm_settings',return_value={'history_days':180}), \
             patch('harmonia.discover.lastfm',side_effect=[first,second]) as lookup, \
             patch('harmonia.discover.time.time',return_value=2_000_000_000):
            tracks,truncated=history_tracks('listener')
        self.assertEqual(len(tracks),201);self.assertFalse(truncated)
        self.assertEqual([call.kwargs['page'] for call in lookup.call_args_list],[1,2])
        self.assertIn('from',lookup.call_args.kwargs)

    def test_repeated_lidarr_track_errors_are_summarized(self):
        result = describe({'id': 1, 'status': 'completed', 'trackedDownloadState': 'importFailed',
            'statusMessages': [{'title': 'One or more tracks expected in this release were not imported',
                                'messages': ['001-song.flac; Has fewer tracks than existing release',
                                             '002-song.flac; Has fewer tracks than existing release']}]})
        self.assertIn('fewer tracks than the existing release', result['error'])
        self.assertIn('2 track files rejected', result['error'])
        self.assertIn('001-song.flac', result['error_detail'])


if __name__ == '__main__':
    unittest.main()
