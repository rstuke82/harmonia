"""Regression checks for notification delivery, redaction and API-key reads."""

import hashlib
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from harmonia import accounts, api, diagnostics, network, webhook_transport
from harmonia.notifications import WebhookNotifier, WebhookSettings
from harmonia.storage import Repository


class NotificationSecurity(unittest.TestCase):
    def addresses(self, address):
        return [(2, 1, 6, "", (address, 443))]

    def test_invalid_destinations_never_send(self):
        for url in (
            "file:///etc/passwd",
            "https://u:p@example.com/hook",
            "https://example.com/#fragment",
            "https://example.com:0/hook",
            "https://example.com:99999/hook",
            "https://example.com/ bad",
        ):
            with (
                self.subTest(url=url),
                patch.object(webhook_transport, "Session") as session,
            ):
                with self.assertRaises(HTTPException):
                    webhook_transport.post(url, {})
                session.assert_not_called()

    def test_private_addresses_require_explicit_permission_metadata_always_blocked(self):
        for address in ("127.0.0.1", "10.0.0.2", "::1", "169.254.169.254", "0.0.0.0"):
            with (
                self.subTest(address=address),
                patch.object(
                    webhook_transport.socket, "getaddrinfo", return_value=self.addresses(address)
                ),
            ):
                with self.assertRaises(HTTPException):
                    webhook_transport.destination("https://example.com/hook")
        with patch.object(
            webhook_transport.socket, "getaddrinfo", return_value=self.addresses("10.0.0.2")
        ):
            self.assertEqual(
                webhook_transport.destination("http://notify:8080/hook", True)[1], "10.0.0.2"
            )
        with patch.object(
            webhook_transport.socket, "getaddrinfo", return_value=self.addresses("169.254.169.254")
        ):
            with self.assertRaises(HTTPException):
                webhook_transport.destination("http://metadata/hook", True)

    def test_public_http_and_mixed_dns_rejected(self):
        for addresses in (
            self.addresses("8.8.8.8"),
            self.addresses("8.8.8.8") + self.addresses("10.0.0.2"),
        ):
            with patch.object(webhook_transport.socket, "getaddrinfo", return_value=addresses):
                with self.assertRaises(HTTPException):
                    webhook_transport.destination("http://example.com/hook")
        with patch.object(
            webhook_transport.socket, "getaddrinfo", return_value=self.addresses("8.8.8.8")
        ):
            with self.assertRaises(HTTPException):
                webhook_transport.destination("http://example.com/hook", True)

    def test_redirect_is_not_followed_and_transport_is_pinned(self):
        response = Mock(status_code=302)
        with (
            patch.object(
                webhook_transport.socket, "getaddrinfo", return_value=self.addresses("8.8.8.8")
            ),
            patch.object(webhook_transport, "Session") as factory,
            patch.object(network, "request", return_value=response) as send,
        ):
            session = factory.return_value.__enter__.return_value
            with self.assertRaises(HTTPException) as error:
                webhook_transport.post("https://example.com/secret-hook", {"message": "test"})
            self.assertEqual(error.exception.status_code, 502)
            self.assertFalse(send.call_args.kwargs["allow_redirects"])
            self.assertEqual(send.call_args.kwargs["timeout"], (5, 10))
            self.assertFalse(session.trust_env)
            adapter = session.mount.call_args.args[1]
            self.assertEqual(adapter.address, "8.8.8.8")
            self.assertEqual(adapter.hostname, "example.com")
            response.close.assert_called_once()

    def test_notifier_delivers_once_without_raw_response_errors(self):
        with patch.object(webhook_transport, "post") as send:
            self.assertTrue(
                WebhookNotifier(
                    WebhookSettings(enabled=True, url="https://example.com/secret")
                ).send("test", "message")
            )
            send.assert_called_once()
        config = SimpleNamespace(
            config={"notifications": {"enabled": True, "url": "https://example.com/secret"}}
        )
        with (
            patch.object(api, "_config", return_value=config),
            patch.object(
                webhook_transport,
                "post",
                side_effect=network.exceptions.ConnectionError("secret-url API_KEY_HIDDEN"),
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                api.test_notification()
            self.assertNotIn("secret-url", error.exception.detail)
            self.assertNotIn("API_KEY_HIDDEN", error.exception.detail)

    def test_configured_and_explicit_secrets_are_scrubbed(self):
        cfg = {
            "plex": {"token": "PLEX_PRIVATE"},
            "lidarr": {"api_key": "LIDARR_PRIVATE"},
            "lastfm": {"api_secret": "LASTFM_PRIVATE"},
            "fanart": {"api_key": "FANART_PRIVATE"},
            "notifications": {"url": "https://notify.example/PRIVATE_HOOK"},
        }
        text = diagnostics.redact(json.dumps(cfg) + " RAW_PRIVATE", cfg, secrets=("RAW_PRIVATE",))
        for secret in (
            "PLEX_PRIVATE",
            "LIDARR_PRIVATE",
            "LASTFM_PRIVATE",
            "FANART_PRIVATE",
            "PRIVATE_HOOK",
            "RAW_PRIVATE",
        ):
            self.assertNotIn(secret, text)

    def test_default_timeout_and_explicit_timeout(self):
        with (
            patch.object(network._requests, "request", return_value=Mock(status_code=200)),
            patch.object(network.jobs, "current", return_value=None),
        ):
            for supplied in ({}, {"timeout": None}, {"timeout": (2, 7)}):
                network.get("https://example.com/", **supplied)
                self.assertEqual(
                    network._requests.request.call_args.kwargs["timeout"],
                    network.normalize_timeout(supplied.get("timeout")),
                )


class ApiKeyReadLock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name))
        self.token = "pb_live_read_check"
        self.digest = hashlib.sha256(self.token.encode()).hexdigest()
        self.user = {"id": "1", "api_key_hash": self.digest, "api_key_last_used_at": time.time()}
        self.repo.put("accounts", "1", self.user)
        self.request = Request(
            {"type": "http", "headers": [(b"authorization", ("Bearer " + self.token).encode())]}
        )
        self.traces = []
        original = self.repo.connect

        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(self.traces.append)
                yield db

        self.original = original
        self.traced = traced

    def test_recent_key_uses_only_read_query(self):
        with (
            patch.object(accounts, "root_repository", return_value=self.repo),
            patch.object(self.repo, "connect", self.traced),
        ):
            self.assertEqual(accounts.api_user(self.request)["id"], "1")
        self.assertFalse(
            any("BEGIN IMMEDIATE" in value or value.startswith("UPDATE ") for value in self.traces)
        )

    def test_due_usage_update_changes_only_timestamp(self):
        self.repo.put(
            "accounts", "1", {**self.user, "api_key_last_used_at": 0, "can_playlists": False}
        )
        with (
            patch.object(accounts, "root_repository", return_value=self.repo),
            patch.object(self.repo, "connect", self.traced),
        ):
            accounts.api_user(self.request)
        self.assertTrue(any("BEGIN IMMEDIATE" in value for value in self.traces))
        self.assertFalse(self.repo.load_one("accounts", "1")["can_playlists"])

    def test_disabled_and_rotated_keys_fail_before_write_lock(self):
        for edits, code in (({"disabled": True}, 403), ({"api_key_hash": "rotated"}, 401)):
            self.repo.put("accounts", "1", {**self.user, **edits})
            self.traces.clear()
            with (
                patch.object(accounts, "root_repository", return_value=self.repo),
                patch.object(self.repo, "connect", self.traced),
            ):
                with self.assertRaises(HTTPException) as error:
                    accounts.api_user(self.request)
            self.assertEqual(error.exception.status_code, code)
            self.assertFalse(any("BEGIN IMMEDIATE" in value for value in self.traces))

    def test_disable_between_read_and_write_is_not_undone(self):
        self.repo.put("accounts", "1", {**self.user, "api_key_last_used_at": 0})
        count = 0

        @contextmanager
        def interleaved():
            nonlocal count
            count += 1
            if count == 2:
                self.repo.put("accounts", "1", {**self.user, "disabled": True})
            with self.original() as db:
                yield db

        with (
            patch.object(accounts, "root_repository", return_value=self.repo),
            patch.object(self.repo, "connect", interleaved),
        ):
            with self.assertRaises(HTTPException) as error:
                accounts.api_user(self.request)
        self.assertEqual(error.exception.status_code, 403)
        self.assertTrue(self.repo.load_one("accounts", "1")["disabled"])
