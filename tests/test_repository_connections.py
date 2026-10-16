"""Repository reads avoid repeated credential-tree scans without relaxing SQLite guards."""

import os
import stat
from types import SimpleNamespace
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from harmonia import storage
from harmonia.storage import Repository


class RepositoryConnections(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.repo = Repository(self.root / "config")
        self.repo.put("example", "item", {"value": 1})

    def test_normal_reads_do_not_rescan_credentials_or_chmod(self):
        with (
            patch.object(
                self.repo, "secure_files", side_effect=AssertionError("No credential tree scan")
            ),
            patch.object(storage.os, "chmod") as chmod,
        ):
            for _ in range(20):
                self.assertEqual(self.repo.load_one("example", "item"), {"value": 1})
            chmod.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_changed_database_mode_is_repaired_once(self):
        self.repo.path.chmod(0o644)
        original = os.chmod
        with patch.object(storage.os, "chmod", wraps=original) as chmod:
            self.repo.load_one("example", "item")
            self.repo.load_one("example", "item")
        self.assertEqual(chmod.call_count, 1)
        self.assertEqual(self.repo.path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX links")
    def test_database_and_sidecar_symlinks_are_rejected_before_connect(self):
        external = self.root / "outside"
        external.write_text("preserved")
        for suffix in ("", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = self.repo.path.with_name(self.repo.path.name + suffix)
                saved = path.read_bytes() if path.exists() else None
                path.unlink(missing_ok=True)
                path.symlink_to(external)
                try:
                    with patch.object(storage.sqlite3, "connect") as connect:
                        with self.assertRaisesRegex(RuntimeError, "symlinks or hard links"):
                            self.repo.load("example")
                        connect.assert_not_called()
                finally:
                    path.unlink()
                    if saved is not None:
                        path.write_bytes(saved)
                        path.chmod(0o600)
        self.assertEqual(external.read_text(), "preserved")

    @unittest.skipUnless(os.name == "posix", "POSIX links")
    def test_hard_link_is_refused_without_touching_external_mode(self):
        external = self.root / "outside"
        external.write_text("preserved")
        external.chmod(0o644)
        target = self.repo.directory / "harmonia.db-wal"
        target.unlink(missing_ok=True)
        os.link(external, target)
        with self.assertRaisesRegex(RuntimeError, "symlinks or hard links"):
            self.repo.load("example")
        self.assertEqual(external.read_text(), "preserved")
        self.assertEqual(external.stat().st_mode & 0o777, 0o644)

    def test_repository_cache_retains_more_than_sixteen_accounts(self):
        storage._repository.cache_clear()
        self.addCleanup(storage._repository.cache_clear)
        folders = [self.root / str(i) for i in range(32)]
        repos = [storage.get_repository(folder) for folder in folders]
        for folder, repo in zip(folders, repos):
            self.assertIs(storage.get_repository(folder), repo)
        self.assertEqual(storage._repository.cache_info().maxsize, 128)

    def test_concurrent_access_keeps_distinct_account_state(self):
        folders = [self.root / "users" / str(i) for i in range(20)]

        def write(pair):
            index, folder = pair
            repo = storage.get_repository(folder)
            repo.put("account", "id", index)
            return repo.load_one("account", "id")

        with ThreadPoolExecutor(max_workers=5) as executor:
            self.assertEqual(list(executor.map(write, enumerate(folders))), list(range(20)))


class SQLiteSidecarChanges(unittest.TestCase):
    setUp = RepositoryConnections.setUp

    def _inspect_with(self, suffix, snapshots):
        target = self.repo.path.with_name(self.repo.path.name + suffix)
        original = Path.lstat
        calls = []

        def inspect(path):
            if path == target:
                calls.append(path)
                value = snapshots[min(len(calls) - 1, len(snapshots) - 1)]
                if isinstance(value, Exception):
                    raise value
                return value
            return original(path)

        with patch.object(Path, "lstat", inspect):
            self.repo._check_database_files()
        return len(calls)

    def _snapshot(self, links, mode=stat.S_IFREG | 0o600):
        return SimpleNamespace(st_mode=mode, st_nlink=links, st_ino=123, st_uid=10001, st_gid=10001)

    def test_unlinked_sidecar_recreated_or_removed_is_rechecked(self):
        for suffix in ("-wal", "-shm"):
            for final in (self._snapshot(1), FileNotFoundError()):
                with self.subTest(suffix=suffix, final=final):
                    self.assertEqual(self._inspect_with(suffix, [self._snapshot(0), final]), 2)

    def test_persistently_unlinked_regular_sidecar_is_tolerated_without_chmod(self):
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix), patch.object(storage.os, "chmod") as chmod:
                self.assertEqual(self._inspect_with(suffix, [self._snapshot(0)]), 3)
                chmod.assert_not_called()

    def test_nonregular_zero_link_sidecar_is_rejected_with_details(self):
        with self.assertRaisesRegex(
            RuntimeError, r"harmonia.db-wal: mode=prw-------, links=0, inode=123, owner=10001:10001"
        ):
            self._inspect_with("-wal", [self._snapshot(0, stat.S_IFIFO | 0o600)])

    def test_main_database_is_not_exempted(self):
        with self.assertRaisesRegex(RuntimeError, "harmonia.db:.*links=0"):
            self._inspect_with("", [self._snapshot(0), self._snapshot(1)])

    def test_unsafe_sidecar_is_never_retried_into_acceptance(self):
        for snapshot in (self._snapshot(2), self._snapshot(1, stat.S_IFLNK | 0o777)):
            with self.subTest(snapshot=snapshot):
                with self.assertRaisesRegex(RuntimeError, "symlinks or hard links"):
                    self._inspect_with("-shm", [snapshot, self._snapshot(1)])

    def test_unlinked_sidecar_replaced_by_symlink_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "mode=l.*links=1"):
            self._inspect_with("-wal", [self._snapshot(0), self._snapshot(1, stat.S_IFLNK | 0o777)])
