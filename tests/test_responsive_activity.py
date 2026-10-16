"""Polling avoids large job blobs while retaining sync completion and safety signals."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from harmonia import accounts, jobs, api
from harmonia.matching import Matcher
from harmonia.storage import Repository


class ResponsiveActivity(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name))
        self.store = jobs.Store(self.repo)
        for name in ("root_repository", "personal_repository"):
            mocked = patch.object(accounts, name, return_value=self.repo)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_poll_summary_retains_playlist_completion_without_health_blobs(self):
        ident = self.store.enqueue("sync", {"scope": "all"})
        result = {
            "partial_success": True,
            "summary": "Done",
            "playlists": [
                {
                    "key": "mix",
                    "ok": True,
                    "result": {"health": {"source_preview": "DO_NOT_DECODE" * 10000}},
                }
            ],
        }
        self.store.update(ident, result=result)
        original = json.loads

        def decode(value, *args, **kwargs):
            self.assertNotIn("DO_NOT_DECODE", value, "Polling decoded a full job result")
            return original(value, *args, **kwargs)

        with patch.object(jobs.json, "loads", side_effect=decode):
            row = self.store.summaries()[0]
            self.assertTrue(self.store.contains(ident))
            self.assertFalse(self.store.cancel_requested(ident))
        self.assertEqual(row["result"]["playlists"], [{"key": "mix", "ok": True}])
        self.assertTrue(row["result"]["partial_success"])
        self.assertLess(len(json.dumps(row)), 2000)
        self.assertEqual(self.store.get(ident)["result"], result, "Full details must remain intact")

    def test_old_job_summary_needs_no_bulk_migration(self):
        ident = self.store.enqueue("sync", {})
        with self.repo.connect() as db:
            db.execute(
                "UPDATE jobs SET result=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "summary": "Old result",
                            "playlists": [
                                {"key": "old", "ok": False, "result": {"large": "PRIVATE DETAIL"}}
                            ],
                        }
                    ),
                    ident,
                ),
            )
        row = self.store.summaries()[0]
        self.assertEqual(row["result"]["summary"], "Old result")
        self.assertEqual(row["result"]["playlists"][0]["key"], "old")
        self.assertNotIn("PRIVATE DETAIL", json.dumps(row))
        self.assertIn("PRIVATE DETAIL", json.dumps(self.store.get(ident)))

    def test_cancelled_search_cannot_publish_a_stale_result_summary(self):
        ident = self.store.enqueue("musicbrainz_lookup", {})
        self.store.cancel(ident)
        self.store.update(ident, status="completed", result={"summary": "stale"})
        self.assertEqual(self.store.get(ident)["status"], "cancelled")
        self.assertIsNone(self.repo.load_one("job_result_summaries", ident))

    def test_cancellation_check_does_not_decode_saved_results(self):
        ident = self.store.enqueue("sync", {})
        with self.repo.connect() as db:
            db.execute("UPDATE jobs SET result=? WHERE id=?", ("invalid historical result", ident))
        with patch.object(self.store, "get", side_effect=AssertionError("Full result loaded")):
            context = jobs.Context(self.store, {"id": ident, "action": "sync"}, threading.Event())
            context.checkpoint()
            with self.repo.connect() as db:
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (ident,))
            with self.assertRaises(jobs.Cancelled):
                context.checkpoint()

    def test_polling_remains_account_scoped_and_background_is_separate(self):
        mine = self.store.enqueue("sync", {})
        page = self.store.enqueue("prepare_views", {"key": "one"})
        self.store.enqueue("request_status", {})
        member = Repository(self.repo.directory / "users" / "2")
        other = jobs.Store(member).enqueue("sync", {})
        with patch.object(api, "job_store", return_value=jobs.Store(member)):
            with accounts.as_user({"id": "2", "owner": False}):
                self.assertEqual([r["id"] for r in api.list_jobs()], [other])
                self.assertEqual(api.list_jobs(background=True), [])
        with accounts.as_user(None):
            with patch.object(api, "job_store", return_value=self.store):
                self.assertEqual([r["id"] for r in api.list_jobs()], [mine])
                self.assertEqual([r["id"] for r in api.list_jobs(background=True)], [page])

    def test_exact_identity_avoids_scoring_unrelated_catalog_tracks(self):
        source = {
            "title": "We Are Young (feat. Janelle Monáe)",
            "artist": "Fun.",
            "album": "Some Nights",
            "duration": 251000,
        }
        unrelated = [
            {
                "plex_id": str(i),
                "title": f"Unrelated piece {i}",
                "artist": "Other",
                "album": "Other",
                "duration": 200000,
            }
            for i in range(1000)
        ]
        target = {
            "plex_id": "wanted",
            "title": "We Are Young",
            "artist": "Fun",
            "album": "Some Nights",
            "duration": 251000,
        }
        with patch.object(Matcher, "score_candidate", wraps=Matcher.score_candidate) as score:
            self.assertEqual(Matcher._match_catalog_track(source, [*unrelated, target]), "wanted")
        self.assertLess(score.call_count, 10)

    def test_full_fuzzy_fallback_and_duration_conflicts_remain(self):
        source = {"title": "Completely unknown", "artist": "Unknown", "album": "Unknown"}
        rows = [
            {"plex_id": str(i), "title": f"Song {i}", "artist": "Other", "album": "Album"}
            for i in range(30)
        ]
        with patch.object(Matcher, "score_candidate", wraps=Matcher.score_candidate) as score:
            self.assertIsNone(Matcher._match_catalog_track(source, rows))
        self.assertEqual(
            score.call_count, len(rows), "No strong identities must retain full fallback"
        )
        feature = {
            "title": "Sun and Shine (feat. Eric Rachmany)",
            "artist": "HIRIE",
            "album": "Sun and Shine - Single",
            "duration": 204000,
        }
        other = {
            "plex_id": "wrong",
            "title": "Sun and Shine",
            "artist": "HIRIE",
            "album": "Sun and Shine",
            "duration": 244000,
        }
        self.assertIsNone(Matcher._match_catalog_track(feature, [other]))


class QuietPolling(unittest.TestCase):
    def test_disabled_debug_polling_does_not_load_configuration_or_write_logs(self):
        import asyncio
        from types import SimpleNamespace
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from harmonia import console_logging

        async def exercise():
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/jobs",
                    "headers": [],
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                    "route": SimpleNamespace(path="/api/jobs"),
                }
            )

            async def respond(request):
                return JSONResponse([])

            with (
                patch.object(console_logging, "enabled", False),
                patch.object(api, "_record_log") as log,
            ):
                response = await api.log_operations(request, respond)
                log.assert_not_called()
                self.assertEqual(response.status_code, 200)

        asyncio.run(exercise())


class ReusedMatchingIndex(unittest.TestCase):
    def test_saved_inventory_reuses_links_but_not_per_search_mutations(self):
        import time
        from types import SimpleNamespace
        from harmonia import library_inventory, track_bridge

        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(Path(folder))
            config = SimpleNamespace(
                repository=repo, config={"plex": {"url": "http://plex", "music_library_key": "1"}}
            )
            plex = {
                "plex_id": "p",
                "title": "Song",
                "artist": "Band",
                "album": "Album",
                "files": ["/music/song"],
            }
            catalog = {
                "id": "l",
                "title": "Song",
                "artist": "Band",
                "album": "Album",
                "file": "/music/song",
            }
            data = {
                "playable_tracks": {"p": plex},
                "lidarr": {"tracks": [catalog], "checked_at": time.time()},
                "_matching_lock": threading.Lock(),
            }
            with (
                patch.object(accounts, "root_repository", return_value=repo),
                patch(
                    "harmonia.lidarr.config", return_value={"enabled": True, "url": "http://lidarr"}
                ),
                patch.object(library_inventory, "indexed", return_value=data),
                patch.object(track_bridge, "links", wraps=track_bridge.links) as link,
            ):
                first = track_bridge.context(config, [plex])
                first["resolved"]["private"] = {"value": True}
                repo.put("catalog_source_matches", first["scope"], {"fresh": {"value": True}})
                second = track_bridge.context(config, [plex])
                self.assertEqual(link.call_count, 1)
                self.assertEqual(second["resolved"], {})
                self.assertEqual(second["source_links"], {"fresh": {"value": True}})
                self.assertEqual(second["links"]["l"]["plex_id"], "p")
                repo.put("file_tools", "settings", {"mappings": []})
                track_bridge.context(config, [plex])
                self.assertEqual(
                    link.call_count, 2, "Path mapping changes invalidate catalog links"
                )
                repo.put("track_links", first["scope"], {})
                track_bridge.context(config, [plex])
                self.assertEqual(link.call_count, 3, "Manual link changes invalidate catalog links")
                track_bridge.context(config, [dict(plex)])
                self.assertEqual(
                    link.call_count, 4, "A live/copied inventory retains its own link build"
                )
