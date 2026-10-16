import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harmonia import accounts, api, api_v1, jobs
from harmonia.storage import Repository


class Beta11API(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / "config.json").write_text(json.dumps({"plex": {}}))
        self.repo = Repository(self.path)
        p = patch("harmonia.accounts.root_repository", return_value=self.repo)
        p.start()
        self.addCleanup(p.stop)
        p = patch("harmonia.config.CONFIG_FILE", self.path / "config.json")
        p.start()
        self.addCleanup(p.stop)
        self.admin = {
            "id": "1",
            "name": "Owner",
            "owner": True,
            "admin": True,
            "disabled": False,
            "verified_at": 9999999999,
            "can_playlists": True,
            "plex_token": "OWNER_TOKEN",
        }
        self.member = {
            "id": "2",
            "name": "Member",
            "admin": False,
            "disabled": False,
            "verified_at": 9999999999,
            "can_playlists": True,
            "plex_token": "MEMBER_TOKEN",
        }
        for user in (self.admin, self.member):
            accounts.put("accounts", user["id"], user)

    async def http(self, path, method="GET", token="", body=None, user=None):
        headers = [(b"host", b"test"), (b"content-type", b"application/json")]
        if token:
            headers.append((b"authorization", ("Bearer " + token).encode()))
        if user:
            secret = "session-" + user["id"]
            accounts.put(
                "sessions",
                __import__("hashlib").sha256(secret.encode()).hexdigest(),
                {"user_id": user["id"], "csrf": "yes", "expires": 9999999999},
            )
            headers.extend(
                [(b"cookie", ("bridge_session=" + secret).encode()), (b"x-bridge-csrf", b"yes")]
            )
        sent = []
        done = False

        async def receive():
            nonlocal done
            if not done:
                done = True
                return {
                    "type": "http.request",
                    "body": json.dumps(body or {}).encode(),
                    "more_body": False,
                }
            await asyncio.sleep(100)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        await api.app(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(raw or b"{}")

    def key(self, user, replace=False):
        current = accounts.users()[user["id"]]
        token = accounts.create_api_key(current, replace)["key"]
        scopes = ["read", "playlists:write", "requests:write"] + (
            ["webhooks:lidarr"] if current.get("admin") else []
        )
        accounts.set_api_key_scopes(current, scopes)
        return token

    def test_key_authentication_and_regeneration(self):
        first = self.key(self.member)
        status, body = asyncio.run(self.http("/api/v1/status", token=first))
        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["id"], "2")
        second = self.key(self.member, True)
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=first))[0], 401)
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=second))[0], 200)

    def test_plex_reauthentication_preserves_api_key(self):
        key = self.key(self.admin)
        before = accounts.users()["1"]
        profile = {"id": 1, "email": "owner@example.test", "username": "Owner", "thumb": ""}
        resources = [
            {
                "owned": True,
                "provides": "server",
                "clientIdentifier": "machine",
                "accessToken": "OWNER_TOKEN",
            }
        ]
        libraries = {"MediaContainer": {"Directory": [{"key": "1", "type": "artist"}]}}
        (self.path / "config.json").write_text(
            json.dumps(
                {"plex": {"url": "http://plex", "token": "SERVER", "music_library_key": "1"}}
            )
        )

        def plex(method, url, **kwargs):
            if url.endswith("/identity"):
                return {"MediaContainer": {"machineIdentifier": "machine"}}
            if url.endswith("/user"):
                return profile
            if url.endswith("/resources"):
                return resources
            if url.endswith("/library/sections"):
                return libraries
            raise AssertionError(url)

        with patch("harmonia.accounts.plex_json", side_effect=plex):
            accounts.verify("NEW_PLEX_TOKEN")
        after = accounts.users()["1"]
        self.assertEqual(after["api_key_hash"], before["api_key_hash"])
        self.assertEqual(after["api_key_value"], key)
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=key))[0], 200)

    def test_shared_playlist_url_queues_personal_add(self):
        token = self.key(self.member)
        status, body = asyncio.run(
            self.http(
                "/api/v1/share",
                method="POST",
                token=token,
                body={"url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"},
            )
        )
        self.assertEqual(status, 202, body)
        self.assertTrue(body["job_id"])
        with accounts.as_user(accounts.users()["2"]):
            job = api.job_store().get(body["job_id"])
        self.assertEqual(job["action"], "add")
        self.assertEqual(job["payload"]["trigger_label"], "iOS Shortcut / API")

    def test_playlist_permission_applies_to_api_key(self):
        member = accounts.users()["2"]
        member["can_playlists"] = False
        accounts.put("accounts", "2", member)
        token = accounts.create_api_key(member)["key"]
        status, _ = asyncio.run(
            self.http(
                "/api/v1/share",
                method="POST",
                token=token,
                body={"url": "https://music.apple.com/us/playlist/example/pl.123"},
            )
        )
        self.assertEqual(status, 403)

    def test_lidarr_test_and_duplicate_import(self):
        token = self.key(self.admin)
        status, body = asyncio.run(
            self.http(
                "/api/v1/webhooks/lidarr",
                method="POST",
                token=token,
                body={"eventType": "Test", "instanceName": "Lidarr"},
            )
        )
        self.assertEqual(status, 202)
        self.assertIsNone(body["job_id"])
        payload = {
            "eventType": "Download",
            "downloadId": "abc",
            "album": {"id": 42, "mbId": "11111111-1111-1111-1111-111111111111", "title": "Album"},
            "artist": {"id": 7, "name": "Artist"},
        }
        first = asyncio.run(
            self.http("/api/v1/webhooks/lidarr", method="POST", token=token, body=payload)
        )
        second = asyncio.run(
            self.http("/api/v1/webhooks/lidarr", method="POST", token=token, body=payload)
        )
        self.assertEqual(first[0], 202)
        self.assertEqual(first[1]["job_id"], second[1]["job_id"])
        self.assertTrue(second[1]["duplicate"])
        self.assertEqual(api.job_store().get(first[1]["job_id"])["action"], "lidarr_webhook")

    def test_member_key_cannot_send_lidarr_webhook(self):
        status, _ = asyncio.run(
            self.http(
                "/api/v1/webhooks/lidarr",
                method="POST",
                token=self.key(self.member),
                body={"eventType": "Test"},
            )
        )
        self.assertEqual(status, 403)

    def test_catalog_webhooks_dispatch_first_then_hold_followup(self):
        token = self.key(self.admin)
        artist = {"id": 7, "name": "Artist"}
        first = asyncio.run(
            self.http(
                "/api/v1/webhooks/lidarr",
                method="POST",
                token=token,
                body={"eventType": "ArtistAdd", "artist": artist},
            )
        )
        second = asyncio.run(
            self.http(
                "/api/v1/webhooks/lidarr",
                method="POST",
                token=token,
                body={
                    "eventType": "Rename",
                    "artist": artist,
                    "album": {"id": 42, "title": "Album"},
                },
            )
        )
        self.assertEqual(first[0], 202)
        self.assertNotEqual(first[1]["job_id"], second[1]["job_id"])
        active = jobs.Store(self.repo)._rows(
            "SELECT * FROM jobs WHERE action='lidarr_webhook' AND status='queued'"
        )
        self.assertEqual(len(active), 2)
        self.assertTrue(all(job["payload"]["event"] == "catalog_batch" for job in active))
        batch = self.repo.load_one(api_v1.CATALOG_BATCH_NAMESPACE, api_v1.CATALOG_BATCH_KEY, {})
        self.assertEqual(batch["count"], 2)
        self.assertEqual(batch["event_counts"], {"artistadd": 1, "rename": 1})
        self.assertEqual(
            api_v1.catalog_batch_wait_seconds(
                self.repo, jobs.Store(self.repo).get(first[1]["job_id"])["payload"]
            ),
            0,
        )
        self.assertGreater(
            api_v1.catalog_batch_wait_seconds(
                self.repo, jobs.Store(self.repo).get(second[1]["job_id"])["payload"]
            ),
            0,
        )

    def test_catalog_batch_scans_once_and_followup_only_scans_new_generation(self):
        token = self.key(self.admin)
        response = asyncio.run(
            self.http(
                "/api/v1/webhooks/lidarr",
                method="POST",
                token=token,
                body={"eventType": "Rename", "artist": {"id": 7}, "album": {"id": 42}},
            )
        )
        job = jobs.Store(self.repo).get(response[1]["job_id"])
        with (
            patch(
                "harmonia.lidarr_catalog.refresh", return_value={"summary": "Scoped refresh"}
            ) as refresh,
            patch("harmonia.inventory.scan", side_effect=AssertionError("Full scan forbidden")),
        ):
            result = api_v1.execute_lidarr_webhook(job["payload"])
            self.assertEqual(refresh.call_count, 1)
            self.assertIn("1 affected", result["summary"])
            self.assertEqual(refresh.call_args.args[1][0]["scope"], "album")
            self.assertEqual(refresh.call_args.args[1][0]["id"], 42)
            follow = jobs.Store(self.repo)._rows(
                "SELECT * FROM jobs WHERE action='lidarr_webhook' AND json_extract(payload,'$.follow_up')=1"
            )
            self.assertEqual(len(follow), 0)
            noop = api_v1.execute_lidarr_webhook(job["payload"])
            self.assertEqual(refresh.call_count, 1)
            self.assertIn("No newer", noop["summary"])

    def test_upgrade_cleanup_combines_catalog_and_download_backlog(self):
        store = jobs.Store(self.repo)
        rename = store.enqueue(
            "lidarr_webhook",
            {"event": "rename", "detail": {"artist_id": 7}, "trigger_label": "Lidarr webhook"},
        )
        add = store.enqueue(
            "lidarr_webhook",
            {"event": "artistadd", "detail": {"artist_id": 8}, "trigger_label": "Lidarr webhook"},
        )
        download = store.enqueue(
            "lidarr_webhook",
            {"event": "download", "detail": {"album_id": 42}, "trigger_label": "Lidarr webhook"},
        )
        result = api_v1.coalesce_queued_lidarr_webhooks(self.repo)
        self.assertEqual(result["collapsed"], 3)
        self.assertEqual(store.get(rename)["status"], "cancelled")
        self.assertEqual(store.get(add)["status"], "cancelled")
        self.assertEqual(store.get(download)["status"], "cancelled")
        batch = store.get(result["job_id"])
        self.assertEqual(batch["payload"]["event"], "catalog_batch")

    def test_import_completions_share_one_quiet_period_batch(self):
        from harmonia.lidarr_downloads import (
            queue_finalize,
            finalize_batch_wait_seconds,
            FINALIZE_BATCH_NAMESPACE,
            FINALIZE_BATCH_KEY,
        )

        first = queue_finalize(
            self.repo, "group-1", {"lidarr_id": 41, "server": "server", "track_file_count": 10}
        )
        second = queue_finalize(
            self.repo, "group-2", {"lidarr_id": 42, "server": "server", "track_file_count": 12}
        )
        self.assertEqual(first, second)
        row = jobs.Store(self.repo).get(first)
        self.assertTrue(row["payload"]["batch"])
        batch = self.repo.load_one(FINALIZE_BATCH_NAMESPACE, FINALIZE_BATCH_KEY, {})
        self.assertEqual(set(batch["albums"]), {"group-1", "group-2"})
        self.assertGreater(finalize_batch_wait_seconds(self.repo, row["payload"]), 0)

    def test_upgrade_cleanup_collapses_album_finalize_backlog(self):
        from harmonia.lidarr_downloads import coalesce_queued_album_finalizers

        store = jobs.Store(self.repo)
        old = [
            store.enqueue(
                "album_finalize", {"album_id": f"group-{n}", "lidarr_id": n, "server": "server"}
            )
            for n in range(1, 5)
        ]
        with self.repo.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (old[0],))
        result = coalesce_queued_album_finalizers(self.repo)
        self.assertEqual(result["collapsed"], 4)
        self.assertTrue(all(store.get(job_id)["status"] == "cancelled" for job_id in old))
        active = store.get(result["job_id"])
        self.assertEqual(active["status"], "queued")
        self.assertTrue(active["payload"]["batch"])

    def test_album_finalize_batch_drains_each_album_once(self):
        from harmonia import album_finalize
        from harmonia.lidarr import state_put
        from harmonia.lidarr_downloads import FINALIZE_BATCH_NAMESPACE, FINALIZE_BATCH_KEY

        state_put(
            self.repo,
            FINALIZE_BATCH_NAMESPACE,
            FINALIZE_BATCH_KEY,
            {
                "generation": 2,
                "processed_generation": 0,
                "count": 2,
                "last_received": 0,
                "albums": {
                    "group-1": {
                        "album_id": "group-1",
                        "lidarr_id": 41,
                        "server": "server",
                        "generation": 1,
                    },
                    "group-2": {
                        "album_id": "group-2",
                        "lidarr_id": 42,
                        "server": "server",
                        "generation": 2,
                    },
                },
            },
        )
        with (
            patch.object(album_finalize, "_execute", return_value={"pending": False}) as execute,
            patch("harmonia.inventory.reconcile") as reconcile,
        ):
            result = album_finalize.execute_batch({"batch": True})
        self.assertEqual(execute.call_count, 2)
        self.assertIn("Finalized 2 imported albums", result["summary"])
        reconcile.assert_called_once_with()
        batch = self.repo.load_one(FINALIZE_BATCH_NAMESPACE, FINALIZE_BATCH_KEY, {})
        self.assertEqual(batch["processed_generation"], 2)
        self.assertEqual(batch["albums"], {})

    def test_stop_pending_jobs_leaves_running_work_alone(self):
        store = jobs.Store(self.repo)
        first = store.enqueue("lidarr_scan", {})
        second = store.enqueue("plex_scan", {})
        running = store.enqueue("local_scan", {})
        with self.repo.connect() as db:
            db.execute(
                "UPDATE jobs SET status='running',started_at=? WHERE id=?", (jobs.now(), running)
            )
        status, result = asyncio.run(
            self.http("/api/jobs/cancel-pending", method="POST", user=self.admin)
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(store.get(first)["status"], "cancelled")
        self.assertEqual(store.get(second)["status"], "cancelled")
        self.assertEqual(store.get(running)["status"], "running")


if __name__ == "__main__":
    unittest.main()
