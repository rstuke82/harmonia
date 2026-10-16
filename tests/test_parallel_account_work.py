"""Service concurrency and reconciliation safety regressions."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harmonia.jobs import wait_reason, Store, Manager
from harmonia.storage import Repository
from harmonia.inventory import scan, queue_reconciliation
from harmonia.process_lock import ProcessLock


class Concurrency(unittest.TestCase):
    def test_independent_fetches_can_overlap(self):
        for first in ("plex_scan", "lidarr_scan", "lastfm_refresh"):
            for second in ("plex_scan", "lidarr_scan", "lastfm_refresh"):
                if first != second:
                    self.assertIsNone(
                        wait_reason(second, "owner", [{"action": first, "account": "owner"}])
                    )
        self.assertIsNone(
            wait_reason(
                "prepare_views",
                "owner",
                [
                    {"action": a, "account": "owner"}
                    for a in ("plex_scan", "lidarr_scan", "lastfm_refresh")
                ],
            )
        )

    def test_inventory_writers_and_same_service_remain_serial(self):
        for scan_action in ("plex_scan", "lidarr_scan"):
            self.assertIsNotNone(
                wait_reason(scan_action, "member", [{"action": scan_action, "account": "owner"}])
            )
            for writer in (
                "lidarr_webhook",
                "album_finalize",
                "release_change",
                "reconcile",
                "file_audit",
            ):
                self.assertIsNotNone(
                    wait_reason(scan_action, "owner", [{"action": writer, "account": "owner"}])
                )
                self.assertIsNotNone(
                    wait_reason(writer, "owner", [{"action": scan_action, "account": "owner"}])
                )
        both = [{"action": a, "account": "owner"} for a in ("plex_scan", "lidarr_scan")]
        self.assertIsNotNone(wait_reason("reconcile", "owner", both))

    def test_reconcile_and_account_edits_wait_in_both_directions(self):
        for edit in ("sync", "match_batch", "health", "text_playlist"):
            self.assertIsNotNone(
                wait_reason("reconcile", "owner", [{"action": edit, "account": "owner"}])
            )
            self.assertIsNotNone(
                wait_reason(edit, "owner", [{"action": "reconcile", "account": "owner"}])
            )
        self.assertIsNone(
            wait_reason("sync", "member", [{"action": "reconcile", "account": "owner"}])
        )

    def test_rebuild_blocks_scans_and_page_preparation(self):
        for action in ("plex_scan", "lidarr_scan", "lastfm_refresh", "prepare_views", "reconcile"):
            self.assertIsNotNone(
                wait_reason(action, "owner", [{"action": "database_rebuild", "account": "owner"}])
            )
            self.assertIsNotNone(
                wait_reason("database_rebuild", "owner", [{"action": action, "account": "owner"}])
            )

    def test_scan_publishes_and_defers_deduplicated_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory))
            store = Store(repo)
            client = SimpleNamespace(
                base_url="plex",
                music_library_key="1",
                headers={},
                machine_identifier="server",
                _fetch_library=lambda: [{"plex_id": "1"}],
            )
            with (
                patch("harmonia.api.job_store", return_value=store),
                patch("harmonia.api._config"),
                patch("harmonia.api._health_plex", return_value=client),
                patch("harmonia.accounts.member_stores", return_value=[]),
                patch(
                    "harmonia.inventory.reconcile",
                    side_effect=AssertionError("must not run inline"),
                ),
            ):
                result = scan("plex")
            self.assertEqual(repo.load_one("inventory", "plex")["rows"], [{"plex_id": "1"}])
            self.assertTrue(result["reconciliation"]["queued"])
            again = queue_reconciliation(repo)
            self.assertEqual(again["job_id"], result["reconciliation"]["job_id"])
            self.assertEqual(store.get(again["job_id"])["status"], "queued")

    def test_waiting_account_lock_does_not_fail_when_edit_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.lock"
            entered = threading.Event()
            failures = []

            def reconcile():
                try:
                    with ProcessLock(path, wait=True):
                        entered.set()
                except BaseException as exc:
                    failures.append(exc)

            with ProcessLock(path):
                thread = threading.Thread(target=reconcile)
                thread.start()
                self.assertFalse(entered.wait(0.15))
            self.assertTrue(entered.wait(2))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

    def test_scheduler_runs_both_scans_and_lastfm_before_any_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(Repository(Path(directory)))
            ids = {
                action: manager.store.enqueue(action, {})
                for action in ("plex_scan", "lidarr_scan", "lastfm_refresh")
            }
            writer = manager.store.enqueue("lidarr_webhook", {})
            started = {action: threading.Event() for action in ids}
            release = threading.Event()

            def execute(job, store, user):
                if job["action"] in started:
                    started[job["action"]].set()
                    release.wait(5)
                    store.update(job["id"], status="completed")

            with (
                patch("harmonia.saved_views.tick"),
                patch("harmonia.accounts.member_stores", return_value=[]),
                patch("harmonia.playlist_schedules.due"),
                patch.object(manager.store, "due"),
                patch("harmonia.api_v1.catalog_batch_wait_seconds", return_value=0),
                patch.object(manager, "execute", side_effect=execute),
            ):
                scheduler = threading.Thread(target=manager.loop)
                scheduler.start()
                try:
                    for action, event in started.items():
                        self.assertTrue(event.wait(3), action + " did not start concurrently")
                    self.assertEqual(manager.store.get(writer)["status"], "queued")
                    for job_id in ids.values():
                        self.assertEqual(manager.store.get(job_id)["status"], "running")
                finally:
                    manager.stop.set()
                    release.set()
                    scheduler.join(5)
                    for worker in manager.workers:
                        worker["thread"].join(5)
