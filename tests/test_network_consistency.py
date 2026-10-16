"""Bounded service calls, credential-safe logging, and safe retry behavior."""

import ast
import contextlib
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
import requests
from harmonia import network, musicbrainz_client


class NetworkConsistency(unittest.TestCase):
    def test_all_verbs_share_default_bounds(self):
        for verb in ("get", "post", "put", "delete", "patch", "head", "options"):
            with (
                self.subTest(verb=verb),
                patch.object(
                    network._requests, "request", return_value=Mock(status_code=200)
                ) as send,
            ):
                getattr(network, verb)("https://example.com/")
                self.assertEqual(send.call_args.args[0], verb.upper())
                self.assertEqual(send.call_args.kwargs["timeout"], (5, 30))

    def test_scalar_preserves_read_budget_and_caps_connect(self):
        for value, wanted in [
            (60, (5, 60)),
            (0.2, (0.2, 0.2)),
            ((2, 90), (2, 90)),
            ((None, None), (5, 30)),
            ((None, 10), (5, 10)),
            ((2, None), (2, 30)),
        ]:
            self.assertEqual(network.normalize_timeout(value), wanted)

    def test_unbounded_or_invalid_timeout_never_reaches_transport(self):
        for value in (0, -1, math.inf, math.nan, True, "20", (), (5,), (5, 0), (math.inf, 10)):
            with self.subTest(value=value), patch.object(network._requests, "request") as send:
                with self.assertRaises(ValueError):
                    network.get("https://example.com/", timeout=value)
                send.assert_not_called()

    def test_custom_session_is_bounded_and_private_options_removed(self):
        session = Mock(headers={}, auth=None)
        session.request.return_value = Mock(status_code=200)
        with patch.object(network._requests, "request") as raw:
            network.get("https://example.com/", _session=session, timeout=10)
        raw.assert_not_called()
        self.assertEqual(session.request.call_args.kwargs["timeout"], (5, 10))
        self.assertNotIn("_session", session.request.call_args.kwargs)

    def test_credentials_in_session_headers_params_and_auth_block_redirects(self):
        with requests.Session() as session:
            session.headers["Authorization"] = "Bearer PRIVATE"
            with patch.object(session, "request", return_value=Mock(status_code=302)) as send:
                network.get("https://example.com/", _session=session, allow_redirects=True)
            self.assertFalse(send.call_args.kwargs["allow_redirects"])
        for supplied in (
            {"params": {"api_key": "PRIVATE"}},
            {"auth": ("user", "PRIVATE")},
            {"headers": {"api-key": "PRIVATE"}},
        ):
            with patch.object(
                network._requests, "request", return_value=Mock(status_code=302)
            ) as send:
                network.get("https://example.com/", allow_redirects=True, **supplied)
                self.assertFalse(send.call_args.kwargs["allow_redirects"])
        with patch.object(network._requests, "request", return_value=Mock(status_code=302)) as send:
            network.get("https://example.com/?access_token=PRIVATE")
            self.assertFalse(send.call_args.kwargs["allow_redirects"])

    def test_stream_error_logging_does_not_read_or_close_body(self):
        response = Mock(status_code=503)
        type(response).text = PropertyMock(side_effect=AssertionError("stream must remain unread"))
        with patch.object(network._requests, "request", return_value=response):
            self.assertIs(network.get("https://example.com/image", stream=True), response)
        response.close.assert_not_called()

    def test_session_and_query_secrets_are_scrubbed_from_error_logs(self):
        ctx = SimpleNamespace(redaction_config=SimpleNamespace(config={}))
        with requests.Session() as session:
            session.headers["Authorization"] = "Bearer SESSION_PRIVATE"
            response = Mock(status_code=403, text="PARAM_PRIVATE SESSION_PRIVATE PASSWORD_PRIVATE")
            with (
                patch.object(session, "request", return_value=response),
                patch.object(network.jobs, "current", return_value=ctx),
                patch.object(network.jobs, "waiting", return_value=contextlib.nullcontext()),
                patch("harmonia.console_logging.web_mode", True),
                patch("harmonia.api._record_log") as log,
            ):
                network.get(
                    "https://example.com/",
                    _session=session,
                    params={"api_key": "PARAM_PRIVATE"},
                    auth=("user", "PASSWORD_PRIVATE"),
                )
            for secret in ("PARAM_PRIVATE", "SESSION_PRIVATE", "PASSWORD_PRIVATE"):
                self.assertNotIn(secret, str(log.call_args))
            self.assertIn("[REDACTED]", str(log.call_args))

    def test_writes_are_not_retried_when_response_is_lost(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with (
                self.subTest(method=method),
                patch.object(
                    network._requests,
                    "request",
                    side_effect=requests.exceptions.ChunkedEncodingError("response lost"),
                ) as send,
            ):
                with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                    network.request(method, "http://plex/playlists/1/items")
                self.assertEqual(send.call_count, 1)

    def test_reads_retry_only_incomplete_responses(self):
        for error in (requests.Timeout("slow"), requests.ConnectionError("offline")):
            with patch.object(network._requests, "request", side_effect=error) as send:
                with self.assertRaises(type(error)):
                    network.get("https://example.com/")
                self.assertEqual(send.call_count, 1)
        with (
            patch.object(
                network._requests,
                "request",
                side_effect=[
                    requests.exceptions.ChunkedEncodingError("short"),
                    Mock(status_code=200),
                ],
            ) as send,
            patch.object(network.time, "sleep"),
        ):
            network.get("https://example.com/")
            self.assertEqual(send.call_count, 2)

    def test_response_body_timeout_is_classified_as_timeout_without_retry(self):
        from urllib3.exceptions import ReadTimeoutError

        error = requests.ConnectionError(
            ReadTimeoutError(None, "https://example.com/secret", "read stalled")
        )
        with patch.object(network._requests, "request", side_effect=error) as send:
            with self.assertRaises(requests.ReadTimeout):
                network.get("https://example.com/")
            self.assertEqual(send.call_count, 1)

    def test_unbounded_requests_apis_are_not_exported(self):
        for name in ("Session", "session", "api"):
            with self.assertRaises(AttributeError):
                getattr(network, name)
        self.assertIs(network.Timeout, requests.Timeout)

    def test_musicbrainz_probes_use_shared_session_and_short_bounds(self):
        with patch.object(
            network, "get", return_value=Mock(status_code=200, json=lambda: {})
        ) as get:
            musicbrainz_client.probe_capabilities("http://mirror", "album")
        self.assertEqual(get.call_count, 7)
        for call in get.call_args_list:
            self.assertIs(call.kwargs["_session"], musicbrainz_client._session)
            self.assertEqual(call.kwargs["timeout"], (3, 10))

    def test_no_service_directly_calls_requests_http(self):
        for path in Path(network.__file__).parent.glob("*.py"):
            if path.name == "network.py":
                continue
            tree = ast.parse(path.read_text())
            aliases = {
                a.asname or a.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for a in node.names
                if a.name == "requests"
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in aliases
                    and node.func.attr
                    in {"get", "post", "put", "patch", "head", "delete", "options", "request"}
                ):
                    self.fail(f"{path.name}:{node.lineno} bypasses bounded HTTP transport")


if __name__ == "__main__":
    unittest.main()
