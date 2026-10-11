import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('container_entrypoint', Path(__file__).parents[1] / 'scripts/container_entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class ContainerEntrypointTests(unittest.TestCase):
    def test_repairs_nested_state_without_rewriting_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'backups').mkdir()
            data = root / 'backups/state.db'
            data.write_bytes(b'preserved state')
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(entrypoint, 'UID', os.getuid() + 1), patch.object(entrypoint, 'GID', os.getgid() + 1), patch.object(entrypoint.os, 'fchown') as chown:
                    entrypoint.repair(fd, os.fstat(fd).st_dev)
                self.assertEqual(chown.call_count, 3)
                self.assertEqual(data.read_bytes(), b'preserved state')
                self.assertEqual(data.stat().st_mode & 0o777, 0o600)
                self.assertEqual((root / 'backups').stat().st_mode & 0o777, 0o700)
            finally:
                os.close(fd)

    def test_symlink_does_not_change_external_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / 'music.flac'
            external.write_bytes(b'untouched')
            external.chmod(0o644)
            config = root / 'config'
            config.mkdir()
            (config / 'link').symlink_to(external)
            fd = os.open(config, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(entrypoint.os, 'fchown'), self.assertRaisesRegex(ValueError, 'symlinks'):
                    entrypoint.repair(fd, os.fstat(fd).st_dev)
                self.assertEqual(external.read_bytes(), b'untouched')
                self.assertEqual(external.stat().st_mode & 0o777, 0o644)
            finally:
                os.close(fd)

    def test_hardlink_is_rejected_before_file_permissions_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / 'music.flac'
            external.write_bytes(b'untouched')
            external.chmod(0o644)
            config = root / 'config'
            config.mkdir()
            os.link(external, config / 'state.db')
            fd = os.open(config, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(entrypoint.os, 'fchown'), self.assertRaisesRegex(ValueError, 'Hard-linked'):
                    entrypoint.repair(fd, os.fstat(fd).st_dev)
                self.assertEqual(external.stat().st_mode & 0o777, 0o644)
            finally:
                os.close(fd)

    def test_repeated_startup_avoids_unnecessary_ownership_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # Native test runs use their current identity; Docker smoke tests use 10001.
                with patch.object(entrypoint, 'UID', os.getuid()), patch.object(entrypoint, 'GID', os.getgid()), patch.object(entrypoint.os, 'fchown') as chown:
                    entrypoint.repair(fd, os.fstat(fd).st_dev)
                    entrypoint.repair(fd, os.fstat(fd).st_dev)
                chown.assert_not_called()
            finally:
                os.close(fd)

    def test_rejects_unsafe_directory_choices(self):
        for value in ('/', '/music', '/app', '/tmp', '/music/nested', '/app/nested', 'relative', '/config/../music'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                entrypoint.open_directory(Path(value))


if __name__ == '__main__':
    unittest.main()
