import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harmonia import accounts, api, tasks, saved_views
from harmonia.storage import Repository
from harmonia.jobs import Store


class Build86(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name))
        self.store = Store(self.repo)
        tasks.setup(self.store)
        self.playlists = [
            {"source": "applemusic", "source_id": name, "plex_playlist_name": name}
            for name in ("fresh", "old", "missing")
        ]
        self.now = time.time()
        self.fresh = self.now - 120
        self.old = self.now - 90000
        self.repo.put(
            "health",
            "applemusic:fresh",
            {"checked_at": datetime.fromtimestamp(self.fresh, timezone.utc).isoformat()},
        )
        self.repo.put(
            "health",
            "applemusic:old",
            {"checked_at": datetime.fromtimestamp(self.old, timezone.utc).isoformat()},
        )

    def startup(self, mode, playlists=None):
        for definition in tasks.DEFINITIONS:
            self.repo.put("task_startup", definition[0], "never")
        self.repo.put("task_startup", "health", mode)
        self.repo.put("task_startup", "page_views", "never")
        config = SimpleNamespace(
            config={"playlists": self.playlists if playlists is None else playlists},
            repository=self.repo,
        )
        with (
            patch.object(accounts, "users", return_value={}),
            patch.object(accounts, "member_stores", return_value=[]),
            patch.object(api, "_config", return_value=config),
            patch.object(saved_views, "startup_pages"),
            patch.object(
                tasks.time if hasattr(tasks, "time") else time, "time", return_value=self.now
            ),
        ):
            return [self.store.get(i) for i in tasks.startup(self.store)]

    def test_missing_only_queues_never_checked_playlist(self):
        jobs = self.startup("missing")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["payload"]["playlist_keys"], ["applemusic:missing"])
        self.assertEqual(self.startup("missing"), [])

    def test_outdated_queues_only_old_and_missing_checks(self):
        jobs = self.startup("outdated")
        self.assertEqual(
            jobs[0]["payload"]["playlist_keys"], ["applemusic:old", "applemusic:missing"]
        )

    def test_fresh_checks_survive_job_history_cleanup_and_failed_attempts(self):
        self.repo.put(
            "health_attempts",
            "applemusic:fresh",
            {"attempted_at": datetime.now(timezone.utc).isoformat(), "error": "failed retry"},
        )
        self.assertEqual(self.startup("missing", self.playlists[:1]), [])
        self.assertEqual(self.startup("outdated", self.playlists[:1]), [])

    def test_always_queues_all_and_never_queues_none(self):
        self.assertEqual(self.startup("never"), [])
        self.assertEqual(len(self.startup("always")[0]["payload"]["playlist_keys"]), 3)

    def test_running_or_failed_job_is_not_completed_health_evidence(self):
        for status in ("running", "failed"):
            self.store.update(
                self.store.enqueue("health", {}),
                status=status,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat() if status == "failed" else None,
            )
        config = SimpleNamespace(config={"playlists": self.playlists}, repository=self.repo)
        with patch.object(api, "_config", return_value=config):
            self.assertEqual(tasks.startup_check(self.store, "health"), (True, None))

    def test_threshold_boundary_and_invalid_timestamps(self):
        self.assertFalse(tasks.startup_needed("outdated", self.now - 86399, 24, self.now))
        self.assertTrue(tasks.startup_needed("outdated", self.now - 86400, 24, self.now))
        for value in ({}, {"checked_at": "bad"}, None):
            self.assertIsNone(tasks.health_checked_at(value))

    def test_disabled_schedule_uses_default_day_but_startup_still_works(self):
        schedule = next(s for s in self.store.schedules() if s["action"] == "health")
        with self.repo.connect() as db:
            db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (schedule["id"],))
        self.assertEqual(
            self.startup("outdated")[0]["payload"]["playlist_keys"],
            ["applemusic:old", "applemusic:missing"],
        )

    def test_account_scoped_evidence(self):
        other = Repository(self.repo.directory / "member")
        self.assertEqual(
            tasks.health_startup_keys(self.repo, self.playlists[:1], "missing", 24, self.now), []
        )
        self.assertEqual(
            tasks.health_startup_keys(other, self.playlists[:1], "missing", 24, self.now),
            ["applemusic:fresh"],
        )

    def test_last_completed_excludes_active_failed_and_skipped_runs(self):
        stamp = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        completed = self.store.enqueue("health", {})
        self.store.update(
            completed,
            status="completed",
            started_at=stamp(self.now - 300),
            finished_at=stamp(self.now - 200),
        )
        failed = self.store.enqueue("health", {})
        self.store.update(
            failed,
            status="failed",
            started_at=stamp(self.now - 100),
            finished_at=stamp(self.now - 50),
        )
        running = self.store.enqueue("health", {"trigger_label": "At startup"})
        self.store.update(running, status="running", started_at=stamp(self.now - 20))
        with patch.object(
            api, "_config", return_value=SimpleNamespace(config={"playlists": self.playlists})
        ):
            row = next(r for r in tasks.rows(self.store) if r["action"] == "health")
        self.assertEqual(row["last_job"]["id"], completed)
        self.assertEqual(row["active_job"]["id"], running)
        self.assertEqual(row["duration"], 100)
        self.assertEqual(row["active_origin"], "startup")

    def test_current_run_without_completed_history_shows_no_last_completed(self):
        active = self.store.enqueue("health", {})
        self.store.update(
            active, status="running", started_at=datetime.now(timezone.utc).isoformat()
        )
        with patch.object(
            api, "_config", return_value=SimpleNamespace(config={"playlists": self.playlists})
        ):
            row = next(r for r in tasks.rows(self.store) if r["action"] == "health")
        self.assertIsNone(row["last_job"])
        self.assertEqual(row["active_job"]["id"], active)

    def test_due_schedule_does_not_duplicate_selected_startup_health_job(self):
        active = self.store.enqueue(
            "health",
            {
                "scope": "selected",
                "playlist_keys": ["applemusic:missing"],
                "trigger_label": "At startup",
            },
        )
        with self.repo.connect() as db:
            db.execute("UPDATE schedules SET enabled=0 WHERE action!='health'")
            db.execute(
                "UPDATE schedules SET next_run=? WHERE action='health'",
                (datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat(),),
            )
        with patch.object(accounts, "scheduled_members"):
            self.store.due(datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        self.assertEqual([j["id"] for j in self.store.list() if j["status"] == "queued"], [active])
        self.assertTrue(any(j["status"] == "skipped" for j in self.store.list()))


if __name__ == "__main__":
    unittest.main()
