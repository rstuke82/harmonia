import tempfile
import unittest
from pathlib import Path
from harmonia.storage import Repository
from harmonia.lidarr_requests import server_id
from harmonia.lidarr_request_events import update, preserve_failure
from harmonia.api_v1 import _queue_lidarr_event


class RequestEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name))
        self.cfg = {"enabled": True, "url": "http://lidarr", "api_key": "secret"}
        self.repo.save(
            {
                "lidarr": {"settings": self.cfg},
                "lidarr_requests": {
                    "one": {"server": server_id(self.cfg), "lidarr_id": 1, "status": "added"},
                    "two": {"server": server_id(self.cfg), "lidarr_id": 2, "status": "added"},
                    "other-server": {"server": "other", "lidarr_id": 1, "status": "added"},
                },
            }
        )

    def row(self, key="one"):
        return self.repo.load("lidarr_requests")[key]

    def event(self, kind, download="A", **extra):
        return _queue_lidarr_event(
            self.repo,
            {"eventType": kind, "albums": [{"id": 1}], "downloadId": download, **extra},
            "admin",
        )

    def test_grab_updates_only_matching_server_and_has_no_job(self):
        result = self.event("Grab")
        self.assertEqual(result["updated"], 1)
        self.assertIsNone(result["job_id"])
        self.assertEqual(self.row()["download_status"], "Download grabbed")
        self.assertNotIn("download_status", self.row("two"))
        self.assertNotIn("download_status", self.row("other-server"))
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_failure_persists_through_empty_queue_then_retry_clears_it(self):
        self.event("Grab")
        self.event("ImportFailed", message="Cannot import secret")
        row = self.row()
        self.assertNotIn("secret", row["download_error"])
        status, error, failure = preserve_failure(row, [], False, "Waiting for download", "")
        self.assertEqual(status, "Import failed")
        self.assertTrue(error)
        self.assertTrue(failure)
        self.assertEqual(self.event("Grab", "B")["updated"], 0)
        self.assertEqual(self.row()["download_status"], "Import failed")
        self.assertTrue(self.row()["webhook_failure"])
        self.assertIsNone(
            preserve_failure(
                self.row(), [{"download_id": "B", "error": ""}], False, "Downloading", ""
            )[2]
        )

    def test_available_and_imported_requests_are_not_downgraded(self):
        from harmonia.lidarr_requests import save

        save(self.repo, "one", download_status="Imported into Lidarr")
        self.assertEqual(self.event("DownloadFailed")["updated"], 0)
        save(self.repo, "one", download_status="Waiting for download", plex_available=True)
        self.assertEqual(self.event("ImportFailed")["updated"], 0)

    def test_unknown_target_and_duplicate_failure(self):
        self.assertEqual(update(self.repo, {"artist": {"id": 99}}, "grab")["updated"], 0)
        self.assertEqual(self.event("DownloadFailed")["updated"], 1)
        self.assertEqual(self.event("DownloadFailed")["updated"], 0)

    def test_queue_retry_and_import_clear_failure(self):
        self.event("DownloadFailed")
        row = self.row()
        self.assertIsNone(
            preserve_failure(row, [{"download_id": "B", "error": ""}], False, "Downloading", "")[2]
        )
        self.assertEqual(
            preserve_failure(row, [], True, "Imported into Lidarr", ""),
            ("Imported into Lidarr", "", None),
        )

    def test_poll_started_before_webhook_cannot_overwrite_it(self):
        from harmonia.lidarr_requests import save

        old_event = self.row().get("webhook_checked_at")
        self.event("ImportFailed")
        self.assertFalse(
            save(
                self.repo,
                "one",
                _expected_webhook_at=old_event,
                download_status="Waiting for download",
                download_error="",
            )
        )
        self.assertEqual(self.row()["download_status"], "Import failed")

    def test_delayed_and_duplicate_grabs_do_not_recover_failure(self):
        self.event("Grab", "A")
        self.event("Grab", "B")
        self.event("ImportFailed", "B")
        for download in ("A", "B", "C", ""):
            self.assertEqual(self.event("Grab", download)["updated"], 0)
        row = self.row()
        self.assertEqual(row["webhook_download_id"], "B")
        self.assertEqual(row["download_status"], "Import failed")
        self.assertEqual(row["webhook_retired_download_ids"], ["A"])
        self.assertIsNotNone(
            preserve_failure(row, [{"download_id": "A", "error": ""}], False, "Downloading", "")[2]
        )

    def test_queue_retry_advances_identity_and_rejects_delayed_failure(self):
        from harmonia.lidarr_request_events import retry_identity_changes
        from harmonia.lidarr_requests import save

        self.event("Grab", "A")
        self.event("ImportFailed", "A")
        row = self.row()
        matches = [{"download_id": "B", "error": ""}]
        status, error, failure = preserve_failure(row, matches, False, "Downloading", "")
        save(
            self.repo,
            "one",
            download_status=status,
            download_error=error,
            webhook_failure=failure,
            **retry_identity_changes(row, matches),
        )
        self.assertEqual(self.row()["webhook_download_id"], "B")
        self.assertEqual(self.event("ImportFailed", "A")["updated"], 0)
        self.assertEqual(self.event("Grab", "A")["updated"], 0)
