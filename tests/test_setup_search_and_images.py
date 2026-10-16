from harmonia import redirect_policy
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, PropertyMock
import requests
from fastapi import HTTPException
from harmonia import accounts, api, jobs, lidarr, image_download, member_album_search, plex_setup
from harmonia.storage import Repository
import test_account_isolation as multi_test


class MemberSearch(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.repo = Repository(self.path)
        self.repo.save_startup(
            {"plex": {"url": "http://plex", "token": "SECRET", "music_library_key": "1"}}
        )
        self.repo.save(
            {
                "lidarr": {
                    "settings": {"enabled": True, "url": "http://lidarr", "api_key": "PRIVATE"}
                }
            }
        )
        self.a = {
            "id": "2",
            "admin": False,
            "owner": False,
            "can_request": True,
            "can_playlists": False,
            "verified_at": 9999999999,
        }
        self.b = {**self.a, "id": "3"}
        for user in (self.a, self.b):
            self.repo.save({"accounts": {self.a["id"]: self.a, self.b["id"]: self.b}})
        for target, value in [
            ("harmonia.accounts.root_repository", self.repo),
            ("harmonia.discover.root_repository", self.repo),
        ]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("harmonia.config.CONFIG_FILE", self.path / "config.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self, path):
        return next(r.endpoint for r in api.app.routes if getattr(r, "path", "") == path)

    def start(self, user=None):
        with accounts.as_user(user or self.a):
            return self.endpoint("/api/requests-user/search-jobs")(
                member_album_search.Search(
                    artist="Artist", title="Song", recording_offset=25, lookup_session="attempt"
                )
            )["id"]

    def test_member_http_queue_and_admin_boundary(self):
        with patch.object(lidarr, "musicbrainz_search") as network:
            status, body = asyncio.run(
                multi_test.MultiUser.http(
                    self,
                    "/api/requests-user/search-jobs",
                    "POST",
                    self.a,
                    body={"artist": "Artist", "title": "Song"},
                )
            )
        self.assertEqual(status, 202)
        network.assert_not_called()
        with accounts.as_user(self.a):
            job = jobs.Store(accounts.personal_repository()).get(json.loads(body)["id"])
        self.assertTrue(job["payload"]["member_request"])
        self.assertEqual(job["payload"]["request_user_id"], "2")
        for method, path in (("GET", "/api/settings/lidarr"), ("POST", "/api/lidarr/search-jobs")):
            self.assertEqual(
                asyncio.run(multi_test.MultiUser.http(self, path, method, self.a, body={}))[0], 403
            )
        self.assertEqual(
            asyncio.run(
                multi_test.MultiUser.http(
                    self,
                    "/api/requests-user/search-jobs",
                    "POST",
                    self.a,
                    body={"recording_offset": -1},
                )
            )[0],
            422,
        )

    def test_job_status_and_cancel_cannot_cross_accounts(self):
        job_id = self.start()
        for path in ("/api/requests-user/search-jobs/{job_id}",):
            with accounts.as_user(self.b):
                with self.assertRaises(HTTPException) as error:
                    self.endpoint(path)(job_id)
                self.assertEqual(error.exception.status_code, 404)
        with accounts.as_user(self.b):
            with self.assertRaises(HTTPException):
                self.endpoint("/api/requests-user/search-jobs/{job_id}")(job_id)
        # Pick the DELETE route explicitly, since GET and DELETE share a path.
        cancel = next(
            r.endpoint
            for r in api.app.routes
            if getattr(r, "path", "") == "/api/requests-user/search-jobs/{job_id}"
            and "DELETE" in r.methods
        )
        with accounts.as_user(self.b):
            with self.assertRaises(HTTPException):
                cancel(job_id)
        with accounts.as_user(self.a):
            cancel(job_id)
            self.assertEqual(
                jobs.Store(accounts.personal_repository()).get(job_id)["status"], "cancelled"
            )

    def test_worker_uses_shared_preferences_and_private_filtered_results(self):
        job_id = self.start()
        with accounts.as_user(self.a):
            store = jobs.Store(accounts.personal_repository())
            store.repository.save({"blocked_artists": {"blocked": {"name": "Blocked"}}})
        ctx = SimpleNamespace(store=store, id=job_id, checkpoint=Mock())
        payload = store.get(job_id)["payload"]
        result = {
            "rows": [
                {
                    "album_id": "own",
                    "artist": "Artist",
                    "title": "Studio",
                    "type": "Album",
                    "secondary_types": [],
                    "other_release": False,
                },
                {
                    "album_id": "other",
                    "artist": "Guest",
                    "title": "Compilation",
                    "secondary_types": ["Compilation"],
                    "other_release": True,
                },
                {"album_id": "blocked", "artist": "Blocked", "title": "Hidden"},
            ],
            "next_recording_offset": 50,
            "lookup_host": "PRIVATE",
            "lookup_source": "mirror",
        }

        def search(repo, cfg, body, publish):
            self.assertIs(repo, self.repo)
            self.assertEqual(cfg["api_key"], "PRIVATE")
            self.assertIsNone(accounts.actor())
            self.assertEqual(body.recording_offset, 25)
            publish(result)
            return result

        with (
            patch.object(jobs, "current", return_value=ctx),
            patch.object(lidarr, "musicbrainz_search", side_effect=search),
        ):
            output = lidarr.execute_lookup(payload)
        self.assertEqual([r["album_id"] for r in output["rows"]], ["own", "other"])
        self.assertTrue(output["rows"][1]["other_release"])
        self.assertEqual(output["next_recording_offset"], 50)
        self.assertNotIn("PRIVATE", json.dumps(output))
        self.assertEqual(store.get(job_id)["result"], output)
        self.assertIsNone(accounts.actor())

    def test_revoked_permissions_and_disabled_integration_block_execution(self):
        job_id = self.start()
        with accounts.as_user(self.a):
            payload = jobs.Store(accounts.personal_repository()).get(job_id)["payload"]
        accounts.put("accounts", "2", {**self.a, "can_request": False})
        with patch.object(lidarr, "musicbrainz_search") as network:
            with self.assertRaises(HTTPException):
                lidarr.execute_lookup(payload)
            network.assert_not_called()
        self.repo.save({"lidarr": {"settings": {"enabled": False}}})
        with accounts.as_user(self.b):
            with self.assertRaises(HTTPException) as error:
                self.start(self.b)
            self.assertEqual(error.exception.status_code, 409)

    def test_preferences_do_not_expose_server_configuration(self):
        self.repo.save(
            {
                "musicbrainz_settings": {
                    "preferences": {
                        "server_url": "http://private-mirror",
                        "retries": 3,
                        "prefer_studio": False,
                    }
                }
            }
        )
        with accounts.as_user(self.a):
            prefs = self.endpoint("/api/requests-user/search-settings")()
        self.assertEqual(prefs["retries"], 0)
        self.assertFalse(prefs["prefer_studio"])
        self.assertNotIn("server_url", prefs)


class PlexSetupFallback(unittest.TestCase):
    def resource(self):
        return {
            "accessToken": "SECRET",
            "connections": [
                {"uri": "https://remote:32400", "local": False},
                {"uri": "http://local:32400", "local": True},
            ],
        }

    def test_unreachable_local_uses_remote_without_disabling_tls(self):
        libraries = {"MediaContainer": {"Directory": [{"type": "artist", "key": "7"}]}}
        with patch.object(
            accounts, "plex_json", side_effect=[HTTPException(503, "offline"), libraries]
        ) as call:
            url, music = plex_setup.music_connection(self.resource(), "token")
        self.assertEqual(url, "https://remote:32400")
        self.assertEqual(music["key"], "7")
        self.assertEqual(call.call_args_list[0].args[1], "http://local:32400/library/sections")
        self.assertNotIn("verify", call.call_args.kwargs)

    def test_duplicates_and_invalid_uris_not_requested(self):
        resource = self.resource()
        resource["connections"] = [
            {"uri": "file:///etc/passwd"},
            {"uri": "http://user:pass@host"},
            {"uri": "http://same"},
            {"uri": "http://same"},
        ]
        with patch.object(accounts, "plex_json", side_effect=HTTPException(503, "offline")) as call:
            with self.assertRaises(HTTPException) as error:
                plex_setup.music_connection(resource, "token")
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(call.call_count, 1)

    def test_reachable_without_music_and_all_offline_have_distinct_errors(self):
        with patch.object(
            accounts, "plex_json", return_value={"MediaContainer": {"Directory": []}}
        ):
            with self.assertRaises(HTTPException) as error:
                plex_setup.music_connection(self.resource(), "token")
            self.assertEqual(error.exception.status_code, 409)
        with patch.object(accounts, "plex_json", side_effect=HTTPException(503, "offline")):
            with self.assertRaises(HTTPException) as error:
                plex_setup.music_connection(self.resource(), "token")
            self.assertEqual(error.exception.status_code, 503)

    def test_connection_attempts_are_bounded(self):
        resource = {"connections": [{"uri": f"http://server{i}"} for i in range(20)]}
        with patch.object(accounts, "plex_json", side_effect=HTTPException(503, "offline")) as call:
            with self.assertRaises(HTTPException):
                plex_setup.music_connection(resource, "token")
        self.assertEqual(call.call_count, 4)


class BoundedImages(unittest.TestCase):
    def response(self, chunks, headers=None):
        return Mock(
            headers=headers or {"Content-Type": "image/png"}, iter_content=lambda **kw: iter(chunks)
        )

    def test_streamed_image_never_reads_unbounded_content(self):
        response = self.response([b"12", b"34"])
        type(response).content = PropertyMock(
            side_effect=AssertionError("buffered content accessed")
        )
        with patch.object(redirect_policy.network, "get", return_value=response) as get:
            image = image_download.download("https://image", max_bytes=4)
        self.assertEqual(image.content, b"1234")
        self.assertTrue(get.call_args.kwargs["stream"])
        response.close.assert_called_once()

    def test_oversized_header_rejected_before_body(self):
        response = self.response([])
        response.headers["Content-Length"] = "100"
        response.iter_content = Mock(side_effect=AssertionError("body read"))
        with patch.object(redirect_policy.network, "get", return_value=response):
            with self.assertRaises(HTTPException) as error:
                image_download.download("https://image", max_bytes=4)
        self.assertEqual(error.exception.status_code, 413)
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_actual_decoded_byte_limit_stops_reading_and_closes(self):
        seen = []

        def chunks(**kw):
            for chunk in (b"12", b"34", b"56", b"never"):
                seen.append(chunk)
                yield chunk

        response = self.response([])
        response.iter_content = chunks
        with patch.object(redirect_policy.network, "get", return_value=response):
            with self.assertRaises(HTTPException) as error:
                image_download.download("https://image", max_bytes=4, oversized_status=502)
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(seen, [b"12", b"34", b"56"])
        response.close.assert_called_once()

    def test_network_failure_releases_slot(self):
        slots = threading.BoundedSemaphore(1)
        with (
            patch.object(image_download, "_downloads", slots),
            patch.object(
                redirect_policy.network, "get", side_effect=requests.ConnectionError("offline")
            ),
        ):
            with self.assertRaises(requests.ConnectionError):
                image_download.download("https://image")
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_deadline_returns_while_stalled_reader_retains_bounded_slot(self):
        release = threading.Event()
        closed = threading.Event()
        slots = threading.BoundedSemaphore(1)
        response = self.response([])

        def chunks(**kw):
            release.wait(2)
            yield b"data"

        response.iter_content = chunks
        response.close.side_effect = closed.set
        with (
            patch.object(image_download, "_downloads", slots),
            patch.object(redirect_policy.network, "get", return_value=response),
        ):
            started = time.monotonic()
            with self.assertRaises(HTTPException) as error:
                image_download.download("https://image", seconds=0.03)
            self.assertEqual(error.exception.status_code, 504)
            self.assertLess(time.monotonic() - started, 0.5)
            with self.assertRaises(HTTPException) as error:
                image_download.download("https://other", seconds=0.03)
            self.assertEqual(error.exception.status_code, 503)
            release.set()
            self.assertTrue(closed.wait(2))
            for _ in range(100):
                if slots.acquire(blocking=False):
                    slots.release()
                    break
                time.sleep(0.001)
            else:
                self.fail("Reader failed to release its slot")
