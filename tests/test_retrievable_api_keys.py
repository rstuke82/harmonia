"""Personal keys can be retrieved only through the owning browser session."""

import asyncio
import unittest

import test_api_permissions as fixtures
from harmonia import accounts, api
from harmonia.diagnostics import redact


class RetrievableKeys(unittest.TestCase):
    setUp = fixtures.Beta11API.setUp
    http = fixtures.Beta11API.http

    def create(self, user):
        return accounts.create_api_key(accounts.users()[user["id"]])["key"]

    def test_session_retrieval_survives_reload_and_idempotent_creation(self):
        key = self.create(self.member)
        for _ in range(2):
            code, value = asyncio.run(self.http("/api/account/api-key", user=self.member))
            self.assertEqual(code, 200)
            self.assertEqual(value["key"], key)
        self.assertEqual(self.create(self.member), key)
        self.assertNotIn("key", accounts.api_key_status(accounts.users()["2"]))

    def test_accounts_cannot_read_each_others_keys_or_retrieve_with_key_auth(self):
        member_key = self.create(self.member)
        owner_key = self.create(self.admin)
        for user, key, other in [
            (self.member, member_key, owner_key),
            (self.admin, owner_key, member_key),
        ]:
            code, value = asyncio.run(self.http("/api/account/api-key", user=user))
            self.assertEqual(code, 200)
            self.assertEqual(value["key"], key)
            self.assertNotIn(other, str(value))
            self.assertEqual(asyncio.run(self.http("/api/account/api-key", token=key))[0], 403)
        code, listing = asyncio.run(self.http("/api/users", user=self.admin))
        self.assertEqual(code, 200)
        self.assertNotIn(member_key, str(listing))
        self.assertNotIn(owner_key, str(listing))

    def test_legacy_hash_only_key_is_not_rotated_on_read(self):
        key = self.create(self.member)
        user = accounts.users()["2"]
        user.pop("api_key_value")
        self.repo.put("accounts", "2", user)
        code, value = asyncio.run(self.http("/api/account/api-key", user=self.member))
        self.assertEqual(code, 200)
        self.assertTrue(value["configured"])
        self.assertEqual(value["key"], "")
        self.assertEqual(accounts.create_api_key(user)["key"], "")
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=key))[0], 200)

    def test_regeneration_and_revocation_replace_or_remove_the_saved_value(self):
        first = self.create(self.member)
        second = accounts.create_api_key(accounts.users()["2"], replace=True)["key"]
        self.assertNotEqual(first, second)
        self.assertEqual(accounts.users()["2"]["api_key_value"], second)
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=first))[0], 401)
        accounts.revoke_api_key(accounts.users()["2"])
        self.assertNotIn("api_key_value", accounts.users()["2"])
        self.assertEqual(asyncio.run(self.http("/api/v1/status", token=second))[0], 401)

    def test_scope_edits_do_not_rotate_or_disclose_saved_key(self):
        key = self.create(self.member)
        result = accounts.set_api_key_scopes(accounts.users()["2"], ["read", "requests:write"])
        self.assertNotIn("key", result)
        self.assertEqual(accounts.users()["2"]["api_key_value"], key)

    def test_retrieval_response_is_not_cacheable_and_key_pattern_is_redacted(self):
        key = self.create(self.member)
        endpoint = next(
            route.endpoint
            for route in api.app.routes
            if getattr(route, "path", "") == "/api/account/api-key"
            and "GET" in getattr(route, "methods", set())
        )
        with accounts.as_user(accounts.users()["2"]):
            response = endpoint()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(redact("credential=" + key), "credential=[REDACTED]")
