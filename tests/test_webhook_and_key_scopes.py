"""Focused authorization and webhook timing checks for build 80."""

import asyncio
import unittest
from unittest.mock import patch
from harmonia import accounts, api, api_v1, jobs
from harmonia.route_access import require_route_access
import test_api_permissions as legacy


# Probe registration deliberately uses an existing member prefix. It must remain
# admin-only despite its URL, and inherit the default route dependency.
@api.app.get("/api/discover/__build80_security_probe")
def security_probe():
    return {"protected": True}


class Build80(unittest.TestCase):
    setUp = legacy.Beta11API.setUp
    http = legacy.Beta11API.http

    def key(self, user):
        return accounts.create_api_key(user)["key"]

    def test_default_and_legacy_admin_keys_are_read_only_and_settings_are_session_only(self):
        token = self.key(self.admin)
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=token))[0], 200)
        for method, path, body in [
            ("GET", "/api/settings/plex", None),
            ("POST", "/api/jobs", {"action": "backup"}),
            ("POST", "/api/account/api-key/regenerate", None),
            ("PUT", "/api/account/api-key/scopes", {"scopes": ["webhooks:lidarr"]}),
            ("POST", "/api/v1/share", {"url": "https://music.apple.com/us/playlist/a/pl.a"}),
            ("POST", "/api/v1/webhooks/lidarr", {"eventType": "Test"}),
        ]:
            self.assertEqual(asyncio.run(self.http(path, method, token, body))[0], 403, path)
        self.assertEqual(asyncio.run(self.http("/api/settings/plex", user=self.admin))[0], 200)
        legacy = accounts.users()["1"]
        legacy.pop("api_key_scopes")
        accounts.put("accounts", "1", legacy)
        self.assertEqual(
            asyncio.run(self.http("/api/v1/webhooks/lidarr", "POST", token, {"eventType": "Test"}))[
                0
            ],
            403,
        )

    def test_explicit_scope_changes_apply_without_replacing_key_and_never_elevate_role(self):
        token = self.key(self.admin)
        status, value = asyncio.run(
            self.http(
                "/api/account/api-key/scopes",
                "PUT",
                body={"scopes": ["webhooks:lidarr"]},
                user=self.admin,
            )
        )
        self.assertEqual(status, 200, value)
        self.assertEqual(
            asyncio.run(self.http("/api/v1/webhooks/lidarr", "POST", token, {"eventType": "Test"}))[
                0
            ],
            202,
        )
        self.assertEqual(asyncio.run(self.http("/api/settings/plex", token=token))[0], 403)
        asyncio.run(
            self.http(
                "/api/account/api-key/scopes", "PUT", body={"scopes": ["read"]}, user=self.admin
            )
        )
        self.assertEqual(
            asyncio.run(self.http("/api/v1/webhooks/lidarr", "POST", token, {"eventType": "Test"}))[
                0
            ],
            403,
        )
        memberkey = self.key(self.member)
        self.assertEqual(
            asyncio.run(
                self.http(
                    "/api/account/api-key/scopes",
                    "PUT",
                    body={"scopes": ["webhooks:lidarr"]},
                    user=self.member,
                )
            )[0],
            403,
        )
        asyncio.run(
            self.http(
                "/api/account/api-key/scopes",
                "PUT",
                body={"scopes": ["playlists:write"]},
                user=self.member,
            )
        )
        status, value = asyncio.run(
            self.http(
                "/api/v1/share",
                "POST",
                memberkey,
                {"url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"},
            )
        )
        self.assertEqual(status, 202, value)
        self.assertEqual(
            asyncio.run(self.http("/api/jobs", "POST", memberkey, {"action": "backup"}))[0], 403
        )

    def test_every_route_inherits_dependency_and_new_member_prefix_route_fails_closed(self):
        for route in api.app.routes:
            if hasattr(route, "dependant"):
                self.assertTrue(
                    any(d.call is require_route_access for d in route.dependant.dependencies),
                    route.path,
                )
        path = "/api/discover/__build80_security_probe"
        if not any(getattr(route, "path", "") == path for route in api.app.routes):
            api.app.get(path)(security_probe)
        probe = next(route for route in api.app.routes if getattr(route, "path", "") == path)
        api.app.routes.remove(probe)
        api.app.routes.insert(0, probe)
        self.assertEqual(asyncio.run(self.http(path, user=self.member))[0], 403)
        self.assertEqual(asyncio.run(self.http(path, user=self.admin))[0], 200)
        self.assertEqual(asyncio.run(self.http(path, token=self.key(self.admin)))[0], 403)

    def test_scopes_preserve_disabled_accounts_and_current_permissions(self):
        self.key(self.member)
        saved = accounts.users()["2"]
        saved.update(disabled=True, can_request=False)
        accounts.put("accounts", "2", saved)
        with self.assertRaises(Exception):
            accounts.set_api_key_scopes(self.member, ["playlists:write"])
        self.assertTrue(accounts.users()["2"]["disabled"])
        self.assertFalse(accounts.users()["2"]["can_request"])

    def queue(self, kind, artist=7, album=None):
        body = {"eventType": kind, "artist": {"id": artist}}
        if album is not None:
            body["album"] = {"id": album}
        return api_v1._queue_lidarr_event(self.repo, body, "1")

    def test_first_catalog_event_is_immediate_and_followups_extend_quiet_window(self):
        with patch("harmonia.api_v1.time.time", return_value=1000):
            first = self.queue("ArtistAdd")
        with patch("harmonia.api_v1.time.time", return_value=1010):
            second = self.queue("Rename", album=42)
        with patch("harmonia.api_v1.time.time", return_value=1020):
            third = self.queue("Retag", album=42)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["job_id"], third["job_id"])
        store = jobs.Store(self.repo)
        leading = store.get(first["job_id"])
        following = store.get(second["job_id"])
        with patch("harmonia.api_v1.time.time", return_value=1020):
            self.assertEqual(api_v1.catalog_batch_wait_seconds(self.repo, leading["payload"]), 0)
            self.assertEqual(api_v1.catalog_batch_wait_seconds(self.repo, following["payload"]), 30)
        store.claim(first["job_id"])
        with patch(
            "harmonia.lidarr_catalog.refresh", return_value={"summary": "Scoped refresh"}
        ) as refresh:
            api_v1.execute_lidarr_webhook(leading["payload"])
            self.assertEqual([t["id"] for t in refresh.call_args.args[1]], [7])
            store.update(first["job_id"], status="completed")
            api_v1.execute_lidarr_webhook(following["payload"])
            self.assertEqual([t["id"] for t in refresh.call_args.args[1]], [42])
            calls = refresh.call_count
            api_v1.execute_lidarr_webhook(leading["payload"])
            self.assertEqual(refresh.call_count, calls)
        store.update(second["job_id"], status="completed")
        with patch("harmonia.api_v1.time.time", return_value=1100):
            fourth = self.queue("ArtistAdd", artist=9)
            self.assertEqual(
                api_v1.catalog_batch_wait_seconds(
                    self.repo, store.get(fourth["job_id"])["payload"]
                ),
                0,
            )

    def test_import_followup_waits_but_failure_status_updates_do_not_queue(self):
        with patch("harmonia.api_v1.time.time", return_value=1000):
            first = self.queue("Download", album=41)
        with patch("harmonia.api_v1.time.time", return_value=1001):
            second = self.queue("Download", album=42)
        store = jobs.Store(self.repo)
        with patch("harmonia.api_v1.time.time", return_value=1001):
            self.assertEqual(
                api_v1.catalog_batch_wait_seconds(self.repo, store.get(first["job_id"])["payload"]),
                0,
            )
            self.assertEqual(
                api_v1.catalog_batch_wait_seconds(
                    self.repo, store.get(second["job_id"])["payload"]
                ),
                30,
            )
            with patch(
                "harmonia.lidarr_request_events.update", return_value={"updated": True}
            ) as update:
                result = self.queue("DownloadFailed", album=42)
                update.assert_called_once()
                self.assertIsNone(result["job_id"])
