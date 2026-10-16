"""Update checks can be disabled without contacting the registry."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from harmonia import accounts, jobs, route_access, updates
from harmonia.storage import Repository


class UpdateControls(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(Path(folder.name))

    def test_disabled_forced_check_never_contacts_registry(self):
        self.repo.put("update_settings", "enabled", False)
        with (
            patch.object(updates, "repository", return_value=self.repo),
            patch.object(updates.network, "get") as get,
        ):
            state = updates.check(force=True)
        self.assertFalse(state["enabled"])
        self.assertFalse(state["available"])
        get.assert_not_called()

    def test_registry_redirect_fetch_strips_credentials_and_is_bounded(self):
        def response(data=None, status=200, headers=None):
            return Mock(
                status_code=status,
                json=lambda: data,
                headers=headers or {},
                raise_for_status=Mock(),
            )

        replies = [
            response({"token": "PRIVATE_REGISTRY_TOKEN"}),
            response({"config": {"digest": "sha256:test"}}),
            response(status=307, headers={"Location": "https://storage.example/config"}),
        ]
        payload = {
            "config": {
                "Labels": {
                    "org.opencontainers.image.version": updates.__version__.rsplit(".", 1)[0]
                    + "."
                    + str(int(updates.__build__) + 1)
                }
            }
        }
        with (
            patch.object(updates, "repository", return_value=self.repo),
            patch.object(updates.network, "get", side_effect=replies) as get,
            patch(
                "harmonia.image_download.download",
                return_value=SimpleNamespace(content=json.dumps(payload).encode()),
            ) as download,
        ):
            result = updates.check(force=True)
        self.assertTrue(result["available"])
        self.assertEqual(get.call_count, 3)
        download.assert_called_once_with(
            "https://storage.example/config", external=True, max_bytes=1_000_000
        )
        self.assertNotIn("PRIVATE_REGISTRY_TOKEN", json.dumps(result))

    def test_schedule_does_not_queue_disabled_checks(self):
        store = jobs.Store(self.repo)
        self.repo.put("update_settings", "enabled", False)
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO schedules VALUES('update','Check for Updates','check_updates','all','30 */6 * * *','UTC',1,'2020-01-01T00:00:00+00:00')"
            )
        with patch.object(updates.network, "get") as get:
            store.due("2026-10-13T00:00:00+00:00")
        self.assertEqual(store.list(), [])
        get.assert_not_called()
        self.assertGreater(store.schedules()[0]["next_run"], "2026-10-13T00:00:00+00:00")

    def test_new_update_setting_routes_require_admin_browser_session(self):
        async def enter(user, method):
            request = Request(
                {
                    "type": "http",
                    "method": "PUT",
                    "path": "/api/settings/updates",
                    "query_string": b"",
                    "headers": [],
                    "route": SimpleNamespace(path="/api/settings/updates"),
                    "state": {"auth_method": method},
                }
            )
            with accounts.as_user(user):
                guard = route_access.require_route_access(request)
                await guard.__anext__()
                await guard.aclose()

        for user, method in (
            ({"id": "member", "admin": False}, "session"),
            ({"id": "admin", "admin": True}, "api_key"),
        ):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(enter(user, method))
            self.assertEqual(error.exception.status_code, 403)
        asyncio.run(enter({"id": "admin", "admin": True}, "session"))
