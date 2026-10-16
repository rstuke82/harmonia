"""Observe bounded HTTP calls without exposing configured credentials."""

import requests as _requests
import math
import time
from urllib.parse import urlsplit, parse_qsl
from urllib3.exceptions import ReadTimeoutError
from . import jobs


def __getattr__(name):
    # Exception compatibility only. HTTP verbs must use the bounded helpers.
    if (
        name == "exceptions"
        or name == "Response"
        or (
            isinstance(getattr(_requests.exceptions, name, None), type)
            and issubclass(getattr(_requests.exceptions, name), _requests.RequestException)
        )
    ):
        return getattr(_requests, name, getattr(_requests.exceptions, name, None))
    raise AttributeError(f"Use Harmonia's bounded HTTP helpers instead of network.{name}")


DEFAULT_TIMEOUT = (5, 30)


def _read_timeout(exc):
    """Requests wraps response-body read timeouts as ConnectionError."""
    pending, seen = [exc], set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, ReadTimeoutError):
            return True
        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend((value.__cause__, value.__context__))
    return False


def normalize_timeout(value=None):
    """Bound connection establishment separately from response inactivity.

    These are Requests socket timeouts, not an overall request/job deadline.
    Service-specific read budgets remain intact; missing bounds use defaults.
    """
    if value is None:
        return DEFAULT_TIMEOUT
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError("HTTP timeout requires connection and read limits")
        bounds = tuple(
            default if bound is None else bound for bound, default in zip(value, DEFAULT_TIMEOUT)
        )
    else:
        bounds = (
            (min(DEFAULT_TIMEOUT[0], value), value)
            if isinstance(value, (int, float))
            else (value, value)
        )
    if any(
        isinstance(bound, bool)
        or not isinstance(bound, (int, float))
        or not math.isfinite(bound)
        or bound <= 0
        for bound in bounds
    ):
        raise ValueError("HTTP timeouts must be positive finite seconds")
    return bounds


def request(method, url, **kwargs):
    kwargs["timeout"] = normalize_timeout(kwargs.get("timeout"))
    session = kwargs.pop("_session", None)
    safe_path = kwargs.pop("_safe_path", None)
    log_body = kwargs.pop("_log_body", True)
    service_override = kwargs.pop("_service", None)
    quiet_statuses = set(kwargs.pop("_quiet_statuses", ()) or ())
    from .diagnostics import configured_secrets

    credentials = configured_secrets(
        {
            "headers": {**(getattr(session, "headers", {}) or {}), **(kwargs.get("headers") or {})},
            "params": kwargs.get("params") or {},
            "query": dict(parse_qsl(urlsplit(str(url)).query)),
            "json": kwargs.get("json") or {},
        }
    )
    for auth in (kwargs.get("auth"), getattr(session, "auth", None)):
        password = (
            auth[1]
            if isinstance(auth, tuple) and len(auth) == 2
            else getattr(auth, "password", None)
        )
        if isinstance(password, str) and password:
            credentials.append(password)
    for secret in list(credentials):
        if secret.lower().startswith("bearer "):
            credentials.append(secret.split(None, 1)[1])
    if (
        credentials
        or kwargs.get("auth")
        or getattr(session, "auth", None)
        or urlsplit(str(url)).username
    ):
        kwargs["allow_redirects"] = False
    host = (urlsplit(str(url)).hostname or "").lower()
    ctx = jobs.current()
    if ctx and hasattr(ctx, "deadline") and not ctx.safe_depth:
        ctx.checkpoint()
        remaining = max(0.001, ctx.deadline - time.monotonic())
        kwargs["timeout"] = tuple(min(bound, remaining) for bound in kwargs["timeout"])
    plex_host = (
        urlsplit(ctx.redaction_config.config.get("plex", {}).get("url", "")).hostname
        if ctx
        else None
    )
    service = (
        "Plex"
        if host == plex_host
        else "Spotify"
        if "spotify" in host
        else "Apple Music"
        if any(s in host for s in ("apple.com", "itunes", "mzstatic"))
        else "Last.fm"
        if host == "ws.audioscrobbler.com"
        else "MusicBrainz"
        if host == "musicbrainz.org"
        else "Fanart.tv"
        if host.endswith("fanart.tv")
        else "external source / artwork"
    )
    service = service_override or service
    from .diagnostics import redact

    path = redact(
        safe_path if safe_path is not None else urlsplit(str(url)).path, secrets=credentials
    )
    started = time.monotonic()
    from .console_logging import web_mode

    def report(level, message):
        if web_mode:
            from .api import _record_log

            _record_log(level, service, message)

    with jobs.waiting(service):
        attempts = 3 if str(method).upper() == "GET" else 1
        for attempt in range(1, attempts + 1):
            if ctx and hasattr(ctx, "deadline"):
                ctx.checkpoint()
                if not ctx.safe_depth:
                    remaining = max(0.001, ctx.deadline - time.monotonic())
                    kwargs["timeout"] = tuple(min(bound, remaining) for bound in kwargs["timeout"])
            try:
                response = (session.request if session is not None else _requests.request)(
                    method, url, **kwargs
                )
                break
            except _requests.exceptions.ChunkedEncodingError as exc:
                if attempt < attempts:
                    report(
                        "WARNING",
                        f"{method} {path} returned an incomplete response; retrying ({attempt}/{attempts - 1})",
                    )
                    time.sleep(0.35 * attempt)
                    continue
                from .diagnostics import service_failure

                report("ERROR", service_failure(service, exc, started).detail)
                raise
            except _requests.RequestException as exc:
                from .diagnostics import service_failure

                if isinstance(exc, _requests.ConnectionError) and _read_timeout(exc):
                    timeout = _requests.ReadTimeout(
                        "Upstream response timed out",
                        request=getattr(exc, "request", None),
                        response=getattr(exc, "response", None),
                    )
                    report("ERROR", service_failure(service, timeout, started).detail)
                    raise timeout from exc
                report("ERROR", service_failure(service, exc, started).detail)
                raise
        elapsed = time.monotonic() - started
        message = f"{method} {path} returned HTTP {response.status_code} in {elapsed:.2f}s"
        # Reading response.text here would consume a streamed image/webhook body
        # before its caller can enforce size, deadline and redirect policies.
        if response.status_code >= 400 and log_body and not kwargs.get("stream"):
            detail = response.text[:2000]
            message += " — " + redact(
                detail, ctx.redaction_config if ctx else None, secrets=credentials
            )
        report(
            "DEBUG"
            if response.status_code in quiet_statuses
            else "ERROR"
            if response.status_code >= 400
            else "DEBUG",
            message,
        )
        return response


def get(url, **kwargs):
    kwargs.setdefault("allow_redirects", True)
    return request("GET", url, **kwargs)


def post(url, **kwargs):
    return request("POST", url, **kwargs)


def put(url, **kwargs):
    return request("PUT", url, **kwargs)


def delete(url, **kwargs):
    return request("DELETE", url, **kwargs)


def patch(url, **kwargs):
    return request("PATCH", url, **kwargs)


def head(url, **kwargs):
    kwargs.setdefault("allow_redirects", False)
    return request("HEAD", url, **kwargs)


def options(url, **kwargs):
    return request("OPTIONS", url, **kwargs)
