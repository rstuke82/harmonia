import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from harmonia.storage import Repository
from harmonia import lidarr_downloads as downloads
from harmonia.lidarr_requests import server_id


class FakeClient:
    key = "secret"

    def __init__(self):
        self.calls = []
        self.items = [
            {
                "id": 4,
                "albumId": 7,
                "downloadId": "download",
                "title": "Album release",
                "status": "downloading",
                "size": 100,
                "sizeleft": 25,
            }
        ]

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "queue":
            return {"records": self.items, "totalRecords": len(self.items)}
        if path == "album/7":
            return {"id": 7, "foreignAlbumId": "album"}
        if path == "album":
            return [
                {
                    "id": 7,
                    "foreignAlbumId": "album",
                    "statistics": {"trackCount": 10, "trackFileCount": 10},
                }
            ]
        if path == "command/8":
            return {"status": "completed"}
        if path == "command":
            return {"id": 8}
        return None


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name) / "bridge.sqlite")
        self.cfg = {"url": "http://lidarr", "api_key": "secret", "enabled": True}
        self.repo.save(
            {
                "lidarr_requests": {
                    "album": {
                        "server": server_id(self.cfg),
                        "lidarr_id": 7,
                        "title": "Album",
                        "status": "search_completed",
                    }
                }
            }
        )
        self.client = FakeClient()
        self.patch = patch("harmonia.lidarr.Client", return_value=self.client)
        self.patch.start()
        self.logs = patch("harmonia.api._record_log")
        self.log = self.logs.start()
        downloads._last.clear()
        downloads._catalog.clear()

    def tearDown(self):
        self.patch.stop()
        self.logs.stop()
        self.tmp.cleanup()

    def action(self, action, **kwargs):
        return downloads.perform(
            self.repo, self.cfg, "album", downloads.Action(action=action, **kwargs)
        )

    def test_cancel_does_not_delete_album_or_search(self):
        self.action("cancel", queue_id=4, download_id="download", confirmed=True)
        writes = [c for c in self.client.calls if c[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ("DELETE", "queue/4"))
        self.assertEqual(writes[0][2]["params"]["skipRedownload"], "true")
        self.assertEqual(writes[0][2]["params"]["blocklist"], "false")

    def test_replace_uses_lidarr_blocklist_and_redownload(self):
        self.action("replace", queue_id=4, download_id="download", confirmed=True)
        params = self.client.calls[-1][2]["params"]
        self.assertEqual(params["blocklist"], "true")
        self.assertEqual(params["skipRedownload"], "false")

    def test_stale_identity_and_shared_download_rejected(self):
        for kwargs in (
            {"queue_id": 4, "download_id": "stale", "confirmed": True},
            {"queue_id": 4, "download_id": "download", "confirmed": False},
        ):
            with self.assertRaises(HTTPException):
                self.action("cancel", **kwargs)
        self.client.items.append({**self.client.items[0], "id": 5, "albumId": 9})
        with self.assertRaises(HTTPException):
            self.action("cancel", queue_id=4, download_id="download", confirmed=True)
        self.assertFalse(any(c[0] != "GET" for c in self.client.calls))

    def test_search_rejects_existing_download_and_active_command(self):
        with self.assertRaises(HTTPException):
            self.action("search")
        self.client.items = []
        self.action("search")
        self.assertEqual(self.repo.load("lidarr_requests")["album"]["search_command_id"], 8)

    def test_unknown_submission_blocks_repeat(self):
        self.client.items = []
        original = self.client.call

        def fail(method, path, **kwargs):
            if method == "POST":
                raise HTTPException(504, "Timeout")
            return original(method, path, **kwargs)

        self.client.call = fail
        with self.assertRaises(HTTPException):
            self.action("search")
        with self.assertRaises(HTTPException) as error:
            self.action("search")
        self.assertEqual(error.exception.status_code, 409)

    def test_progress_and_import_are_persisted_not_plex_availability(self):
        downloads.snapshot(self.repo, self.cfg)
        state = self.repo.load("lidarr_requests")["album"]
        self.assertEqual(state["downloads"][0]["percent"], 75)
        self.client.items = []
        downloads._last.clear()
        downloads.snapshot(self.repo, self.cfg)
        self.assertEqual(
            self.repo.load("lidarr_requests")["album"]["download_status"], "Imported into Lidarr"
        )

    def test_direct_lookup_prevents_false_removed_status_during_catalog_lag(self):
        original = self.client.call

        def lagging(method, path, **kwargs):
            if path == "album":
                return []
            return original(method, path, **kwargs)

        self.client.call = lagging
        downloads.snapshot(self.repo, self.cfg, force=True)
        state = self.repo.load("lidarr_requests")["album"]
        self.assertFalse(state["removed_from_lidarr"])
        self.assertEqual(state["download_status"], "Downloading")
        self.assertIn(("GET", "album/7", {"quiet": True}), self.client.calls)

    def test_failed_finalization_is_not_automatically_requeued(self):
        from harmonia.lidarr_requests import save
        from harmonia.jobs import Store

        self.client.items = []
        save(
            self.repo,
            "album",
            download_status="Imported into Lidarr",
            finalize_status="failed",
            finalize_error="Previous failure",
        )
        downloads.snapshot(self.repo, self.cfg, force=True)
        self.assertEqual(
            Store(self.repo)._rows("SELECT * FROM jobs WHERE action='album_finalize'"), []
        )

    def test_import_failure_preserves_reason_and_unknown_size(self):
        result = downloads.describe(
            {
                "id": 1,
                "status": "completed",
                "trackedDownloadState": "importFailed",
                "statusMessages": [{"title": "Import failed", "messages": ["Permission denied"]}],
            }
        )
        self.assertEqual(result["status"], "Import blocked")
        self.assertIsNone(result["percent"])
        self.assertIn("Permission denied", result["error"])
