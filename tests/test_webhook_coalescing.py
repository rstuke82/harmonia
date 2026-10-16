import hashlib
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from harmonia import accounts, api_v1, jobs, login_security, import_users
from harmonia.storage import Repository


class Build84(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name))
        p = patch.object(accounts, "root_repository", return_value=self.repo)
        p.start()
        self.addCleanup(p.stop)

    def queue(self, event, album, at, download=""):
        with patch.object(api_v1.time, "time", return_value=at):
            return api_v1._queue_lidarr_event(
                self.repo,
                {
                    "eventType": event,
                    "album": {"id": album, "mbId": "mb-" + str(album)},
                    "artist": {"id": 7},
                    "downloadId": download,
                },
                "1",
            )

    def batch(self):
        return self.repo.load_one(api_v1.CATALOG_BATCH_NAMESPACE, api_v1.CATALOG_BATCH_KEY)

    def execution(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        refresh = stack.enter_context(
            patch(
                "harmonia.lidarr_catalog.refresh",
                return_value={"summary": "Catalog updated", "partial_success": False},
            )
        )
        imported = stack.enter_context(
            patch.object(
                api_v1, "_execute_import", return_value={"summary": "Imported", "pending": False}
            )
        )
        reconcile = stack.enter_context(patch("harmonia.inventory.reconcile", return_value={}))
        invalidation = stack.enter_context(patch("harmonia.library.invalidate"))
        stack.enter_context(patch("harmonia.library_cache.invalidate"))
        stack.enter_context(patch("harmonia.artwork_sync.queue_pending"))
        return refresh, imported, reconcile, invalidation

    def test_first_immediate_followup_combines_imports_and_catalog(self):
        first = self.queue("Download", 1, 1000, "one")
        second = self.queue("Download", 2, 1001, "two")
        third = self.queue("Retag", 2, 1002)
        fourth = self.queue("AlbumDownload", 3, 1003, "three")
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual({second["job_id"], third["job_id"], fourth["job_id"]}, {second["job_id"]})
        store = jobs.Store(self.repo)
        leading = store.get(first["job_id"])
        following = store.get(second["job_id"])
        with patch.object(api_v1.time, "time", return_value=1003):
            self.assertEqual(api_v1.catalog_batch_wait_seconds(self.repo, leading["payload"]), 0)
            self.assertEqual(api_v1.catalog_batch_wait_seconds(self.repo, following["payload"]), 30)
        refresh, imported, reconcile, invalidation = self.execution()
        store.claim(first["job_id"])
        api_v1.execute_lidarr_webhook(leading["payload"])
        store.update(first["job_id"], status="completed")
        self.assertEqual([c.args[0]["album_id"] for c in imported.call_args_list], [1])
        imported.reset_mock()
        reconcile.reset_mock()
        invalidation.reset_mock()
        store.claim(second["job_id"])
        result = api_v1.execute_lidarr_webhook(following["payload"])
        self.assertEqual([c.args[0]["album_id"] for c in imported.call_args_list], [2, 3])
        self.assertTrue(all(c.kwargs["defer"] for c in imported.call_args_list))
        refresh.assert_called_once()
        self.assertTrue(refresh.call_args.kwargs["defer_invalidate"])
        reconcile.assert_called_once()
        invalidation.assert_called_once()
        self.assertEqual(len(result["albums"]), 2)
        self.assertEqual(self.batch()["imports"], {})
        self.assertEqual(self.batch()["targets"], {})

    def test_duplicate_delivery_does_not_extend_quiet_window(self):
        self.queue("Download", 1, 1000, "one")
        second = self.queue("Download", 2, 1001, "two")
        repeat = self.queue("Download", 2, 1015, "two")
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(repeat["job_id"], second["job_id"])
        timing = self.repo.load_one(api_v1.CATALOG_BATCH_NAMESPACE, api_v1.WEBHOOK_TIMING_KEY)
        self.assertEqual(timing["quiet_until"], 1031)

    def test_arrival_while_batch_runs_keeps_newer_same_album_generation(self):
        first = self.queue("Download", 1, 1000, "one")
        store = jobs.Store(self.repo)
        payload = store.get(first["job_id"])["payload"]
        store.claim(first["job_id"])
        _, imported, _, _ = self.execution()
        imported.side_effect = lambda detail, **kw: self.queue("Download", 1, 1001, "new") or {}
        result = api_v1.execute_lidarr_webhook(payload)
        self.assertEqual(self.batch()["imports"]["1"]["detail"]["download_id"], "new")
        self.assertIsNotNone(result["follow_up_job_id"])
        queued = store._rows("SELECT * FROM jobs WHERE action='lidarr_webhook' AND status='queued'")
        self.assertEqual(len(queued), 1)

    def test_one_bad_album_does_not_discard_other_albums_or_retry_evidence(self):
        first = self.queue("Download", 1, 1000, "one")
        follow = self.queue("Download", 2, 1001, "two")
        self.queue("Download", 3, 1002, "three")
        store = jobs.Store(self.repo)
        _, imported, _, _ = self.execution()
        api_v1.execute_lidarr_webhook(store.get(first["job_id"])["payload"])
        store.update(first["job_id"], status="completed")
        imported.side_effect = (
            lambda detail, **kw: (_ for _ in ()).throw(ValueError("Album unavailable"))
            if detail["album_id"] == 2
            else {"pending": False}
        )
        result = api_v1.execute_lidarr_webhook(store.get(follow["job_id"])["payload"])
        self.assertTrue(result["partial_success"])
        self.assertEqual([row["ok"] for row in result["albums"]], [False, True])
        self.assertEqual(self.batch()["failed_imports"]["2"]["detail"]["download_id"], "two")
        self.assertIsNone(result["follow_up_job_id"])

    def test_old_import_backlog_migrates_and_receipts_point_to_batch(self):
        store = jobs.Store(self.repo)
        old = store.enqueue("lidarr_webhook", {"event": "download", "detail": {"album_id": 42}})
        other = store.enqueue("lidarr_webhook", {"event": "rename", "detail": {"artist_id": 7}})
        self.repo.put(
            "lidarr_webhook_receipts", "receipt", {"received_at": time.time(), "job_id": old}
        )
        result = api_v1.coalesce_queued_lidarr_webhooks(self.repo)
        self.assertEqual(result["collapsed"], 2)
        self.assertEqual(store.get(old)["status"], "cancelled")
        self.assertEqual(store.get(other)["status"], "cancelled")
        self.assertEqual(
            self.repo.load_one("lidarr_webhook_receipts", "receipt")["job_id"], result["job_id"]
        )
        self.assertIn("42", self.batch()["imports"])

    def test_many_ips_do_not_exhaust_other_clients_minute_allowance(self):
        with patch.object(login_security.time, "time", return_value=1000):
            for i in range(100):
                login_security.admit_start(self.repo, "ip-" + str(i))
            for _ in range(4):
                login_security.admit_start(self.repo, "ip-0")
            with self.assertRaises(HTTPException) as error:
                login_security.admit_start(self.repo, "ip-0")
            self.assertEqual(error.exception.status_code, 429)
            login_security.admit_start(self.repo, "legitimate-new-ip")
        self.assertNotIn("start:global", self.repo.load("auth_limits"))
        with patch.object(login_security.time, "time", return_value=1061):
            login_security.admit_start(self.repo, "ip-0")
        self.assertEqual(len(self.repo.load("auth_limits")), 1)

    def test_api_key_lookup_is_indexed_nonexpiring_and_revocation_is_immediate(self):
        token = "pb_live_test"
        digest = hashlib.sha256(token.encode()).hexdigest()
        for i in range(100):
            self.repo.put("accounts", str(i), {"id": str(i), "api_key_hash": "unrelated-" + str(i)})
        self.repo.put(
            "accounts",
            "owner",
            {
                "id": "owner",
                "api_key_hash": digest,
                "api_key_created_at": 1,
                "api_key_scopes": ["read"],
            },
        )
        request = Request(
            {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
        )
        with patch.object(accounts.time, "time", return_value=10**12):
            self.assertEqual(accounts.api_user(request)["id"], "owner")
        with self.repo.connect() as db:
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT key,value FROM state WHERE namespace='accounts' AND json_extract(value,'$.api_key_hash')=?",
                (digest,),
            ).fetchall()
        self.assertIn("accounts_api_key_hash", str(plan))
        self.repo.put(
            "accounts", "owner", {"id": "owner", "api_key_hash": digest, "disabled": True}
        )
        with self.assertRaises(HTTPException) as error:
            accounts.api_user(request)
        self.assertEqual(error.exception.status_code, 403)
        self.repo.put("accounts", "owner", {"id": "owner", "api_key_hash": "replaced"})
        with self.assertRaises(HTTPException) as error:
            accounts.api_user(request)
        self.assertEqual(error.exception.status_code, 401)

    def test_auth_me_uses_direct_session_and_account_lookup(self):
        self.repo.put("accounts", "1", {"id": "1", "name": "Owner"})
        self.repo.put(
            "sessions",
            hashlib.sha256(b"secret").hexdigest(),
            {"user_id": "1", "expires": time.time() + 100, "csrf": "csrf"},
        )
        app = FastAPI()
        accounts.install(app)
        endpoint = next(
            r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/auth/me"
        )
        request = Request(
            {"type": "http", "headers": [(b"cookie", (accounts.COOKIE + "=secret").encode())]}
        )
        original_load = self.repo.load

        def auth_load(namespace, *args, **kwargs):
            if namespace in ("accounts", "sessions"):
                raise AssertionError("Whole namespace auth load")
            return original_load(namespace, *args, **kwargs)

        with patch.object(self.repo, "load", side_effect=auth_load):
            self.assertEqual(endpoint(request)["user"]["id"], "1")

    def test_failed_playlist_keeps_inventory_for_later_playlists(self):
        from harmonia import api

        self.repo.put("inventory", "plex", {"checked_at": 123, "rows": [{"plex_id": "track"}]})
        config = SimpleNamespace(
            repository=self.repo,
            config={
                "plex": {},
                "playlists": [
                    {"source": "applemusic", "id": "first", "plex_playlist_name": "First"},
                    {"source": "applemusic", "id": "second", "plex_playlist_name": "Second"},
                ],
            },
        )
        calls = []

        def sync(key):
            calls.append(key)
            self.assertEqual(self.repo.load_one("inventory", "plex")["checked_at"], 123)
            return {"summary": {"errors": 1 if len(calls) == 1 else 0}}

        with (
            patch.object(api, "_config", return_value=config),
            patch.object(api, "sync_one", side_effect=sync),
            patch.object(api, "job_store", return_value=SimpleNamespace(repository=self.repo)),
        ):
            with self.assertRaisesRegex(ValueError, "1 of 2 playlists failed"):
                api._execute_job("sync", {})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.repo.load_one("inventory", "plex")["checked_at"], 123)

    def test_batched_import_updates_request_and_defers_shared_publication(self):
        from harmonia.lidarr_requests import server_id

        cfg = {"enabled": True, "url": "http://lidarr", "api_key": "key"}
        self.repo.put("lidarr_requests", "album-mbid", {"server": server_id(cfg), "lidarr_id": 2})
        with (
            patch("harmonia.lidarr.config", return_value=cfg),
            patch("harmonia.lidarr.Client") as client,
            patch("harmonia.album_finalize.execute", return_value={"pending": False}) as finalize,
        ):
            client.return_value.call.return_value = {"foreignAlbumId": "album-mbid"}
            api_v1._execute_import({"album_id": 2}, defer=True)
        self.assertEqual(
            self.repo.load_one("lidarr_requests", "album-mbid")["download_status"],
            "Imported into Lidarr",
        )
        self.assertEqual(
            finalize.call_args.kwargs, {"defer_reconcile": True, "defer_publication": True}
        )

    def xml_setup(self):
        self.repo.save_startup({"plex": {"url": "http://plex", "token": "token"}})
        return patch.object(
            accounts, "plex_json", return_value={"MediaContainer": {"machineIdentifier": "server"}}
        )

    def test_plex_user_import_parses_normal_xml_and_rejects_entities_and_dtd(self):
        valid = b'<MediaContainer><User id="2" email="member@example.test" title="Member"><Server machineIdentifier="server" allLibraries="1" /></User></MediaContainer>'
        empty = b"<MediaContainer />"

        def response(body):
            return SimpleNamespace(content=body, raise_for_status=lambda: None)

        with (
            self.xml_setup(),
            patch.object(
                import_users.network, "get", side_effect=[response(valid), response(empty)]
            ),
        ):
            self.assertEqual(import_users.eligible()[0]["id"], "2")
        for body in (
            b'<!DOCTYPE x [<!ENTITY x "bad">]><x>&x;</x>',
            b'<!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
            b"<broken",
        ):
            with (
                self.subTest(body=body),
                self.xml_setup(),
                patch.object(import_users.network, "get", return_value=response(body)),
                self.assertRaises(HTTPException) as error,
            ):
                import_users.eligible()
            self.assertEqual(error.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
