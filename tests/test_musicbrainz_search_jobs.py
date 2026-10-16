import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from harmonia import api, jobs, lidarr
from harmonia.storage import Repository
from harmonia.song_albums import containing_albums
import test_account_isolation
import asyncio


class SearchJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name))
        self.store = jobs.Store(self.repo)
        self.body = lidarr.Lookup(
            artist="Artist", title="Song", provider="musicbrainz", fields_search=True
        )

    def endpoint(self, path, method):
        return next(
            r.endpoint
            for r in api.app.routes
            if getattr(r, "path", "") == path and method in getattr(r, "methods", set())
        )

    def test_submission_coalesces_retries_and_is_not_a_live_lookup(self):
        start = self.endpoint("/api/lidarr/search-jobs", "POST")
        with (
            patch.object(lidarr, "repository", return_value=self.repo),
            patch.object(lidarr, "enabled", return_value={}),
            patch.object(lidarr, "musicbrainz_search") as lookup,
        ):
            first = start(self.body)
            second = start(self.body.model_copy(update={"attempt": 2, "attempts": 3}))
        self.assertEqual(first, second)
        lookup.assert_not_called()
        self.assertEqual(jobs.worker_for("musicbrainz_lookup"), "lookup")

    def test_poll_and_cancel_reject_other_actions_or_repository(self):
        poll = self.endpoint("/api/lidarr/search-jobs/{job_id}", "GET")
        cancel = self.endpoint("/api/lidarr/search-jobs/{job_id}", "DELETE")
        jid = self.store.enqueue("musicbrainz_lookup", self.body.model_dump())
        other = self.store.enqueue("backup", {})
        with patch.object(lidarr, "repository", return_value=self.repo):
            self.assertEqual(poll(jid)["status"], "queued")
            for value in [other, "missing"]:
                with self.assertRaises(lidarr.HTTPException):
                    poll(value)
                with self.assertRaises(lidarr.HTTPException):
                    cancel(value)
            cancel(jid)
            self.assertEqual(poll(jid)["status"], "cancelled")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(lidarr, "repository", return_value=Repository(Path(directory))),
        ):
            with self.assertRaises(lidarr.HTTPException):
                poll(jid)

    def test_browse_reuses_pages_and_publishes_incremental_results(self):
        credit = [{"name": "Artist"}]
        records = [{"id": str(i), "title": "Song", "artist-credit": credit} for i in range(2)]
        get = Mock(
            return_value={
                "releases": [
                    {
                        "release-group": {
                            "id": "album",
                            "title": "Album",
                            "primary-type": "Album",
                            "artist-credit": credit,
                        }
                    }
                ]
            }
        )
        publish = Mock()
        prefs = {"cache_days": 7}
        containing_albums(self.repo, records, self.body, prefs, get, publish)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([len(c.args[0]) for c in publish.call_args_list], [1, 2])
        containing_albums(self.repo, records, self.body, prefs, get)
        self.assertEqual(get.call_count, 2)
        containing_albums(
            self.repo, records, self.body.model_copy(update={"force_refresh": True}), prefs, get
        )
        self.assertEqual(get.call_count, 4)

    def test_cancellation_stops_before_next_browse(self):
        jid = self.store.enqueue("musicbrainz_lookup", {})
        ctx = jobs.Context(self.store, self.store.get(jid), Event())
        self.store.cancel(jid)
        with patch.object(jobs, "current", return_value=ctx):
            get = Mock()
            with self.assertRaises(jobs.Cancelled):
                containing_albums(
                    self.repo,
                    [{"id": "r", "title": "Song", "artist-credit": [{"name": "Artist"}]}],
                    self.body,
                    {},
                    get,
                )
            get.assert_not_called()

    def test_incremental_job_result_retained(self):
        jid = self.store.enqueue("musicbrainz_lookup", {})
        ctx = jobs.Context(self.store, self.store.get(jid), Event())

        def lookup(repo, cfg, body, publish):
            publish({"rows": [{"album_id": "first"}]})
            self.assertEqual(self.store.get(jid)["result"]["rows"][0]["album_id"], "first")
            return {"rows": [{"album_id": "first"}, {"album_id": "second"}]}

        with (
            patch.object(jobs, "current", return_value=ctx),
            patch.object(lidarr, "enabled", return_value={}),
            patch.object(lidarr, "musicbrainz_search", side_effect=lookup),
        ):
            self.assertEqual(len(lidarr.execute_lookup(self.body.model_dump())["rows"]), 2)


class SearchAccess(unittest.TestCase):
    setUp = test_account_isolation.MultiUser.setUp
    http = test_account_isolation.MultiUser.http

    def test_members_cannot_read_or_cancel_server_searches(self):
        jid = jobs.Store(self.repo).enqueue("musicbrainz_lookup", {"artist": "Private search"})
        for method in ["GET", "DELETE"]:
            code, _ = asyncio.run(self.http("/api/lidarr/search-jobs/" + jid, method, self.a))
            self.assertEqual(code, 403)
        self.assertEqual(jobs.Store(self.repo).get(jid)["status"], "queued")

    def test_delegated_admin_uses_server_job_scope_and_csrf(self):
        delegated = {**self.a, "admin": True}
        from harmonia import accounts

        accounts.put("accounts", delegated["id"], delegated)
        path = "/api/lidarr/search-jobs"
        code, _ = asyncio.run(
            self.http(path, "POST", delegated, False, {"title": "Song", "artist": "Artist"})
        )
        self.assertEqual(code, 403)
        code, raw = asyncio.run(
            self.http(path, "POST", delegated, True, {"title": "Song", "artist": "Artist"})
        )
        self.assertEqual(code, 202)
        import json

        jid = json.loads(raw)["id"]
        self.assertIsNotNone(jobs.Store(self.repo).get(jid))
        code, _ = asyncio.run(self.http(path + "/" + jid, "GET", delegated))
        self.assertEqual(code, 200)
