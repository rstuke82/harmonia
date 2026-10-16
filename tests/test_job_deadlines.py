"""Runtime budgets stop work safely without releasing a live writer's lock."""

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from harmonia import jobs, network, runtime_output
from harmonia.storage import Repository


class JobDeadlines(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = jobs.Store(Repository(Path(folder.name)))
        self.job_id = self.store.enqueue("sync", {})
        self.ctx = jobs.Context(self.store, self.store.get(self.job_id), Event())

    def test_all_supported_actions_have_finite_budget(self):
        for action in jobs.ACTIONS:
            self.assertGreater(jobs.RUNTIME_LIMITS[jobs.worker_for(action)], 0)
            self.assertLessEqual(jobs.RUNTIME_LIMITS[jobs.worker_for(action)], 21600)

    def test_expired_job_cannot_start_another_network_request(self):
        self.ctx.deadline = 0
        with (
            patch.object(jobs, "current", return_value=self.ctx),
            patch.object(network._requests, "request") as send,
        ):
            with self.assertRaises(jobs.JobTimedOut):
                network.get("https://example.com/")
        send.assert_not_called()

    def test_safe_unit_finishes_before_deadline_is_honored(self):
        with patch.object(jobs, "current", return_value=self.ctx):
            with jobs.safe_unit():
                self.ctx.deadline = 0
                self.ctx.checkpoint()
                self.assertEqual(self.ctx.safe_depth, 1)
            with self.assertRaises(jobs.JobTimedOut):
                self.ctx.checkpoint()
        self.assertEqual(self.ctx.safe_depth, 0)

    def test_safe_unit_exception_preserves_cancellation_boundary(self):
        with patch.object(jobs, "current", return_value=self.ctx):
            with self.assertRaises(ValueError):
                with jobs.safe_unit():
                    raise ValueError("writer failed")
        self.assertEqual(self.ctx.safe_depth, 0)

    def test_manager_reports_timeout_without_starting_work(self):
        manager = jobs.Manager(self.store.repository)
        self.ctx.deadline = 0
        with (
            patch.object(jobs, "Context", return_value=self.ctx),
            patch("harmonia.api._record_log"),
            patch("harmonia.api.execute_job") as run,
        ):
            manager._execute_account(self.store.get(self.job_id), self.store)
        run.assert_not_called()
        row = self.store.get(self.job_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Runtime limit", row["error"])

    def test_deadline_after_completion_retains_partial_result(self):
        manager = jobs.Manager(self.store.repository)

        def execute(*args):
            self.ctx.deadline = 0
            return {"playlists": [{"key": "one", "ok": True}]}

        with (
            patch.object(jobs, "Context", return_value=self.ctx),
            patch("harmonia.api._record_log"),
            patch("harmonia.api.execute_job", side_effect=execute),
        ):
            manager._execute_account(self.store.get(self.job_id), self.store)
        row = self.store.get(self.job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["result"]["playlists"], [{"key": "one", "ok": True}])

    def test_dead_worker_is_failed_but_live_writer_keeps_its_slot(self):
        from unittest.mock import Mock

        manager = jobs.Manager(self.store.repository)
        self.store.claim(self.job_id)
        worker = {"id": self.job_id, "store": self.store, "thread": Mock()}
        manager.workers = [worker]
        worker["thread"].is_alive.return_value = True
        manager._retire_finished_workers()
        self.assertEqual(len(manager.workers), 1)
        self.assertEqual(self.store.get(self.job_id)["status"], "running")
        worker["thread"].is_alive.return_value = False
        manager._retire_finished_workers()
        self.assertEqual(manager.workers, [])
        self.assertEqual(self.store.get(self.job_id)["status"], "failed")

    def test_runtime_events_share_severity_and_redaction(self):
        self.ctx.redaction_config.config["plex"] = {"token": "PRIVATE_TOKEN"}
        with (
            patch.object(jobs, "current", return_value=self.ctx),
            patch("harmonia.console_logging.web_mode", True),
            patch("harmonia.api._record_log") as record,
        ):
            runtime_output.emit("⚠ upstream token=PRIVATE_TOKEN")
        self.assertEqual(record.call_args.args[0], "WARNING")
        self.assertNotIn("PRIVATE_TOKEN", record.call_args.args[2])


class RuntimeCapture(unittest.TestCase):
    def test_buffer_and_event_receive_one_redacted_message(self):
        from harmonia import api
        from types import SimpleNamespace

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = Repository(Path(folder.name))
        store = jobs.Store(repo)
        job_id = store.enqueue("sync", {})
        ctx = jobs.Context(store, store.get(job_id), Event())
        config = SimpleNamespace(repository=repo, config={})
        with (
            patch.object(jobs, "current", return_value=ctx),
            patch.object(api, "_config", return_value=config),
            patch("harmonia.console_logging.web_mode", True),
        ):
            _, captured = api._capture(lambda: runtime_output.emit("saved track"))
        self.assertEqual(captured, "saved track")
        events = store.events(job_id)
        self.assertEqual(sum("saved track" in event["message"] for event in events), 1)
