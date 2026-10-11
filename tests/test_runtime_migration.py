"""Regression guards for the web-only runtime and removed CLI dependencies."""
import ast
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from playlist_bridge import api, runtime_output
from playlist_bridge.atomic_files import _atomic_write_json
from playlist_bridge.config import Config
from playlist_bridge.diagnostics import PlexDiagnosticError
from playlist_bridge.sync_engine import Syncer

ROOT = Path(__file__).resolve().parents[1]
REMOVED_MODULES = {'legacy', 'cli', 'cli_syncer'}


class RuntimeMigration(unittest.TestCase):
    def test_no_source_or_test_imports_removed_modules(self):
        violations = []
        for folder in ('playlist_bridge', 'tests'):
            for path in (ROOT / folder).glob('*.py'):
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.ImportFrom):
                        forbidden = node.module in REMOVED_MODULES or node.module in {
                            'playlist_bridge.' + name for name in REMOVED_MODULES
                        } or (node.module in (None, 'playlist_bridge') and any(
                            a.name in REMOVED_MODULES for a in node.names
                        ))
                    elif isinstance(node, ast.Import):
                        forbidden = any(a.name in {'playlist_bridge.' + name for name in REMOVED_MODULES}
                                        for a in node.names)
                    else:
                        continue
                    if forbidden:
                        violations.append(f'{path.relative_to(ROOT)}:{node.lineno}')
        self.assertEqual(violations, [])

    def test_unused_compatibility_helpers_are_removed(self):
        from playlist_bridge import inventory
        import playlist_bridge
        self.assertFalse(hasattr(inventory, 'queue_changed_matches'))
        self.assertFalse(hasattr(playlist_bridge, '_release_name'))

    def test_web_starts_without_removed_modules(self):
        with tempfile.TemporaryDirectory() as data:
            result = subprocess.run(
                [sys.executable, '-c',
                 'import json, sys; import playlist_bridge.api; '
                 'print(json.dumps([n for n in sys.modules if n in '
                 '("playlist_bridge.legacy", "playlist_bridge.cli", '
                 '"playlist_bridge.cli_syncer")]))'],
                cwd=ROOT, env={**os.environ, 'HARMONIA_DATA_DIR': data},
                text=True, capture_output=True, timeout=30, check=True,
            )
        self.assertEqual(json.loads(result.stdout), [])

    def test_web_uses_canonical_runtime_classes(self):
        for module, name in (
            ('config', 'Config'), ('matching', 'Matcher'),
            ('plex_client', 'PlexAPI'), ('spotify_source', 'SpotifyAPI'),
            ('apple_music_source', 'AppleMusicAPI'), ('process_lock', 'ProcessLock'),
            ('sync_engine', 'Syncer'),
        ):
            canonical = getattr(importlib.import_module('playlist_bridge.' + module), name)
            self.assertIs(getattr(api, name), canonical)

    def test_cli_files_and_interactive_methods_are_removed(self):
        for path in ('sync.py', 'playlist_bridge/legacy.py',
                     'playlist_bridge/cli.py', 'playlist_bridge/cli_syncer.py'):
            self.assertFalse((ROOT / path).exists(), path)
        for name in ('setup_plex', '_prompt_for_music_library', 'ensure_plex_music_library'):
            self.assertFalse(hasattr(Config, name), name)
        for name in ('add_source', 'resolve_missing_interactive', '_find_album_playlist_coverage',
                     'find_albums_without_playlist_tracks', 'find_albums_with_playlist_tracks',
                     '_artist_alias_owner'):
            self.assertFalse(hasattr(Syncer, name), name)
        self.assertTrue(callable(Config._get_plex_music_libraries))
        self.assertTrue(callable(Syncer.sync_playlist))
        self.assertTrue(callable(Syncer.sync_all))
        self.assertTrue(callable(Syncer.sync_favorites))
        self.assertTrue(callable(Syncer._build_new_plex_playlist))

    def test_runtime_cannot_prompt_on_standard_input(self):
        calls = []
        for path in (ROOT / 'playlist_bridge').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'input':
                    calls.append(f'{path.name}:{node.lineno}')
        self.assertEqual(calls, [])

    def test_missing_plex_settings_fail_without_prompts_or_network(self):
        complete = {'url': 'http://plex:32400', 'token': 'private', 'music_library_key': '1'}
        for settings in ({}, {**complete, 'url': 'bad'},
                         {**complete, 'token': ''}, {**complete, 'music_library_key': ''}):
            with self.subTest(settings=settings):
                config = object.__new__(Config)
                config.config = {'plex': settings}
                syncer = Syncer(config)
                with patch('builtins.input', side_effect=AssertionError('No interactive setup')), \
                     patch('playlist_bridge.network.get', side_effect=AssertionError('No discovery')), \
                     patch('playlist_bridge.sync_engine.PlexAPI') as client:
                    with self.assertRaises(PlexDiagnosticError) as error:
                        syncer._get_plex()
                self.assertIn('Settings', str(error.exception))
                self.assertNotIn('private', str(error.exception))
                client.assert_not_called()
                self.assertIsNone(syncer.plex)

    def test_saved_plex_configuration_does_not_trigger_discovery_or_writes(self):
        settings = {'url': 'http://plex:32400', 'token': 'private', 'music_library_key': '1'}
        config = object.__new__(Config)
        config.config = {'plex': settings}
        with patch.object(Config, '_get_plex_music_libraries', side_effect=AssertionError('No discovery')), \
             patch.object(Config, 'save', side_effect=AssertionError('No writes')), \
             patch('builtins.input', side_effect=AssertionError('No prompt')):
            self.assertEqual(config.get_plex(), settings)
        self.assertEqual(config.config['plex'], settings)

    def test_web_launcher_keeps_web_and_version_only(self):
        from playlist_bridge import __main__, __version__
        with patch.object(sys, 'argv', ['harmonia', '--help']), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(__main__.main(), 0)
        self.assertIn('playlist_bridge web', out.getvalue())
        self.assertNotIn('sync.py', out.getvalue())
        with patch.object(sys, 'argv', ['harmonia', 'version']), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(__main__.main(), 0)
        self.assertEqual(out.getvalue().strip(), __version__)
        with patch.object(sys, 'argv', ['harmonia', 'web']), patch.object(api, 'run') as run:
            self.assertEqual(__main__.main(), 0)
        run.assert_called_once_with()

    def test_runtime_services_use_shared_job_output(self):
        for module in ('plex_client', 'spotify_source',
                       'apple_music_source', 'sync_engine'):
            self.assertIs(importlib.import_module('playlist_bridge.' + module).print, runtime_output.print)
        with patch('playlist_bridge.jobs.current', return_value=object()), \
             patch('playlist_bridge.jobs.output') as output, \
             patch('playlist_bridge.console_logging.web_mode', True), \
             patch('playlist_bridge.console_logging.enabled', False):
            runtime_output.print('saved', 'track', sep=':')
        output.assert_called_once_with('saved:track')

    def test_atomic_writer_failure_preserves_previous_state_and_cleans_tempfile(self):
        with tempfile.TemporaryDirectory() as data:
            path = Path(data) / 'state.json'
            path.write_text('{"original": true}\n')
            with patch('playlist_bridge.atomic_files.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    _atomic_write_json(path, {'replacement': True})
            self.assertEqual(json.loads(path.read_text()), {'original': True})
            self.assertEqual(list(Path(data).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
