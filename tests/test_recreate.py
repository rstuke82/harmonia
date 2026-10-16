import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class Lock:
    def __enter__(self): return self
    def __exit__(self, *_): return False


class RecreateTests(unittest.TestCase):
    def test_missing_destination_reuses_normal_sync_matching(self):
        from harmonia.recreate import execute
        playlist = {
            'source': 'apple', 'source_id': 'favorite',
            'plex_playlist_id': '1202989', 'plex_playlist_name': 'Favorite Songs',
        }
        config = SimpleNamespace(config={'playlists': [playlist]}, save=Mock())
        plex = SimpleNamespace(base_url='http://plex', headers={}, search_library=Mock(return_value=[]))
        missing = Mock(status_code=404)
        with patch('harmonia.api._config', return_value=config), \
             patch('harmonia.api._health_plex', return_value=plex), \
             patch('harmonia.api.sync_one', return_value={'summary': 'rebuilt'}) as sync, \
             patch('harmonia.process_lock.ProcessLock', Lock), \
             patch('harmonia.network.get', return_value=missing):
            result = execute({'playlist_key': 'apple:favorite'})
        self.assertEqual(result, {'summary': 'rebuilt'})
        self.assertEqual(playlist['plex_playlist_id'], '')
        config.save.assert_called_once()
        sync.assert_called_once_with('apple:favorite')

    def test_retry_with_cleared_destination_does_not_require_old_mapping(self):
        from harmonia.recreate import execute
        playlist = {
            'source': 'spotify', 'source_id': 'mix',
            'plex_playlist_id': '', 'plex_playlist_name': 'Mix',
        }
        config = SimpleNamespace(config={'playlists': [playlist]}, save=Mock())
        plex = SimpleNamespace(base_url='http://plex', headers={}, search_library=Mock(return_value=[]))
        with patch('harmonia.api._config', return_value=config), \
             patch('harmonia.api._health_plex', return_value=plex), \
             patch('harmonia.api.sync_one', return_value={'summary': 'rebuilt'}), \
             patch('harmonia.process_lock.ProcessLock', Lock), \
             patch('harmonia.network.get') as get:
            execute({'playlist_key': 'spotify:mix'})
        get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
