"""Regressions for reserved page preparation capacity."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from harmonia.jobs import Manager, worker_for, wait_reason, history_where
from harmonia.storage import Repository


class PagePreparation(unittest.TestCase):
    def test_pages_do_not_wait_for_service_jobs(self):
        self.assertEqual(worker_for("prepare_views"), "views")
        for action in (
            "health",
            "plex_scan",
            "lidarr_scan",
            "lastfm_refresh",
            "library_metadata_all",
            "sync",
        ):
            with self.subTest(action=action):
                self.assertIsNone(
                    wait_reason("prepare_views", "owner", [{"account": "owner", "action": action}])
                )
                self.assertIsNone(
                    wait_reason(action, "other", [{"account": "owner", "action": "prepare_views"}])
                )

    def test_one_reserved_worker_and_exclusive_maintenance_barriers(self):
        self.assertIsNotNone(
            wait_reason("prepare_views", "b", [{"account": "a", "action": "prepare_views"}])
        )
        for action in ("backup", "restore_backup", "database_compact", "database_cleanup"):
            self.assertIsNotNone(
                wait_reason("prepare_views", "a", [{"account": "b", "action": action}])
            )
            self.assertIsNotNone(
                wait_reason(action, "b", [{"account": "a", "action": "prepare_views"}])
            )
        self.assertIsNotNone(wait_reason("prepare_views", "a", [], exclusive_waiting=True))

    def test_long_jobs_keep_existing_resource_limits(self):
        running = [{"account": "owner", "action": "health"}]
        for action in ("lastfm_refresh", "library_metadata", "plex_scan"):
            self.assertIsNone(wait_reason(action, "other", running))
        self.assertIsNotNone(wait_reason("sync", "owner", running))

    def test_page_worker_history_can_be_filtered(self):
        _, actions = history_where("views")
        self.assertEqual(actions, ("prepare_views",))
        self.assertNotIn("prepare_views", history_where("metadata")[1])

    def test_scheduler_dispatches_page_while_health_is_still_running(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(Repository(Path(directory)))
            blocked = manager.store.enqueue("file_audit", {})
            page = manager.store.enqueue("prepare_views", {"key": "fixture"})
            release = threading.Event()
            health = threading.Thread(target=release.wait)
            health.start()
            manager.workers = [{"thread": health, "account": directory, "action": "health"}]
            dispatched = threading.Event()

            def execute(job, store, user):
                if job["id"] == page:
                    store.update(job["id"], status="completed")
                    dispatched.set()

            with (
                patch("harmonia.saved_views.tick"),
                patch("harmonia.accounts.member_stores", return_value=[]),
                patch("harmonia.playlist_schedules.due"),
                patch.object(manager.store, "due"),
                patch.object(manager, "execute", side_effect=execute),
            ):
                scheduler = threading.Thread(target=manager.loop)
                scheduler.start()
                try:
                    self.assertTrue(dispatched.wait(5), "Page was blocked behind Health")
                    self.assertTrue(health.is_alive())
                    self.assertEqual(manager.store.get(blocked)["status"], "queued")
                    self.assertEqual(manager.store.get(page)["status"], "completed")
                finally:
                    manager.stop.set()
                    release.set()
                    scheduler.join(5)
                    health.join(5)
                self.assertFalse(scheduler.is_alive())
