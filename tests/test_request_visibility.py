"""Request visibility and administrator matching remain account isolated."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from starlette.requests import Request
import test_account_isolation
from harmonia import accounts, api, route_access, saved_views
from harmonia.jobs import Store
from harmonia.lidarr_requests import server_id


class RequestVisibility(unittest.TestCase):
    def setUp(self):
        test_account_isolation.MultiUser.setUp(self)
        patched = patch("harmonia.saved_views.root_repository", return_value=self.repo)
        patched.start()
        self.addCleanup(patched.stop)

    http = test_account_isolation.MultiUser.http

    def endpoint(self, path):
        return next(r.endpoint for r in api.app.routes if getattr(r, "path", "") == path)

    def test_permission_is_saved_and_public_and_changes_cache_scope(self):
        status, raw = asyncio.run(
            self.http(
                "/api/users/11",
                "PUT",
                self.admin,
                body={"disabled": False, "can_request": True, "can_view_all_requests": True},
            )
        )
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)["can_view_all_requests"])
        self.assertTrue(accounts.users()["11"]["can_view_all_requests"])
        self.assertNotEqual(
            saved_views.permissions(self.a),
            saved_views.permissions({**self.a, "can_view_all_requests": True}),
        )

    def test_view_permission_reads_others_but_does_not_authorize_management(self):
        cfg = {"enabled": True, "url": "http://lidarr", "api_key": "key"}
        self.repo.put("lidarr", "settings", cfg)
        self.repo.put(
            "lidarr_requests",
            "other",
            {
                "server": server_id(cfg),
                "title": "Other album",
                "status": "search_completed",
                "requesters": [{"user_id": "12", "name": "Bob"}],
            },
        )
        own = self.endpoint("/api/requests-user").__wrapped__
        with patch("harmonia.discover.availability", return_value=[]):
            with accounts.as_user(self.a):
                self.assertEqual(own(), [])
            viewer = {**self.a, "can_view_all_requests": True}
            with accounts.as_user(viewer):
                rows = own()
                self.assertEqual([r["key"] for r in rows], ["other"])
                self.assertEqual(rows[0]["requesters"][0]["user_id"], "12")
                self.assertEqual(rows[0]["sources"], [])
        accounts.put("accounts", "11", viewer)
        for path, method, body in [
            ("/api/lidarr/requests/other", "DELETE", None),
            ("/api/requests-user/pending", "GET", None),
            ("/api/admin/missing", "GET", None),
            (
                "/api/admin/missing/match",
                "POST",
                {"title": "Song", "artist": "Artist", "plex_id": "1", "target_user_id": "12"},
            ),
        ]:
            status, raw = asyncio.run(self.http(path, method, viewer, body=body))
            self.assertEqual(status, 403, (path, raw))

    def test_admin_missing_keeps_identical_tracks_separate_and_filters_disabled(self):
        seen = []

        def rows():
            seen.append(accounts.actor()["id"])
            return [{"title": "Same song", "artist": "Same artist", "memberships": []}]

        with (
            accounts.as_user(self.admin),
            patch.object(api, "missing_tracks", SimpleNamespace(__wrapped__=rows)),
        ):
            result = api.administrator_missing_tracks.__wrapped__()
        self.assertEqual({r["user_id"] for r in result}, {"1", "11", "12"})
        self.assertEqual(len(result), 3)
        self.assertIsNone(accounts.actor())
        accounts.put("accounts", "12", {**self.b, "disabled": True})
        with (
            accounts.as_user(self.admin),
            patch.object(api, "missing_tracks", SimpleNamespace(__wrapped__=rows)),
        ):
            self.assertNotIn(
                "12", {r["user_id"] for r in api.administrator_missing_tracks.__wrapped__()}
            )

    def test_admin_match_queues_only_target_users_new_edits(self):
        from harmonia.match_queue import stage_changes

        seen = []

        def stage(request):
            seen.append(accounts.actor()["id"])
            self.assertEqual(request.provenance, "manual")
            return stage_changes(
                api.job_store().repository,
                [
                    {
                        "playlist_key": "spotify:shared-name",
                        "search_key": "Song|Artist",
                        "source": {"title": "Song", "artist": "Artist"},
                        "plex_id": "chosen",
                        "provenance": "manual",
                    }
                ],
            )

        with accounts.as_user(self.b):
            target = api.job_store().repository
            stage_changes(
                target,
                [
                    {
                        "playlist_key": "unrelated",
                        "search_key": "Other|Artist",
                        "source": {"title": "Other", "artist": "Artist"},
                        "plex_id": "old",
                        "provenance": "manual",
                    }
                ],
            )
        with (
            accounts.as_user(self.admin),
            patch("harmonia.match_queue.stage_match", side_effect=stage),
        ):
            job = api.administrator_match(
                api.AdministratorMatchRequest(
                    target_user_id="12", title="Song", artist="Artist", plex_id="chosen"
                )
            )
        self.assertEqual(seen, ["12"])
        self.assertIsNone(Store(self.repo).get(job["id"]))
        with accounts.as_user(self.a):
            self.assertIsNone(api.job_store().get(job["id"]))
        saved = Store(target).get(job["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(
            [r["playlist_key"] for r in saved["payload"]["changes"]], ["spotify:shared-name"]
        )
        self.assertEqual(len(target.load("match_drafts")), 2)
        self.assertIsNone(accounts.actor())

    def test_target_selector_requires_admin_browser_and_is_narrow(self):
        async def run(user, path, auth="session", uid="12"):
            req = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "query_string": ("user_id=" + uid).encode(),
                    "headers": [],
                    "state": {"auth_method": auth},
                    "route": SimpleNamespace(path=path),
                }
            )
            with accounts.as_user(user):
                dep = route_access.require_route_access(req)
                try:
                    await anext(dep)
                    self.assertEqual(accounts.actor()["id"], uid)
                finally:
                    await dep.aclose()
                self.assertEqual(accounts.actor()["id"], user["id"])

        asyncio.run(run(self.admin, "/api/missing/candidates"))
        for user, path, auth, uid in [
            (self.a, "/api/missing/candidates", "session", "12"),
            (self.admin, "/api/missing/candidates", "api_key", "12"),
            (self.admin, "/api/missing/ignore", "session", "12"),
            (self.admin, "/api/missing/candidates", "session", "nonexistent"),
        ]:
            with self.assertRaises(HTTPException):
                asyncio.run(run(user, path, auth, uid))

    def test_admin_projection_revision_changes_with_member_missing(self):
        deps = saved_views.COMMON | {"all_account_missing"}
        before = saved_views.revision(self.repo, deps)
        with accounts.as_user(self.b):
            api.job_store().repository.put("missing", "spotify:one", [{"title": "New"}])
        self.assertNotEqual(before, saved_views.revision(self.repo, deps))


if __name__ == "__main__":
    unittest.main()
