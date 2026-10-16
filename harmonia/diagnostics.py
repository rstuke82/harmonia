"""Actionable, credential-free Plex diagnostics for web operations."""

from urllib.parse import urlsplit, quote, unquote
import re
import requests


class PlexDiagnosticError(RuntimeError):
    pass


def plex_error(exc, operation="contact Plex"):
    if isinstance(exc, PlexDiagnosticError):
        return str(exc)
    if isinstance(exc, requests.exceptions.SSLError):
        return "Plex TLS certificate could not be verified. Check the HTTPS URL and certificate in Settings → Plex."
    if isinstance(exc, requests.exceptions.Timeout):
        return "Plex timed out. Check the server URL in Settings → Plex and confirm the server is running and reachable from Harmonia."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Cannot connect to Plex. Check the server URL and port in Settings → Plex. In Docker, localhost refers to the container; use an address reachable from the container."
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "Plex rejected access. Check the Plex token and library permissions in Settings → Plex."
        if status == 404:
            return "Plex resource was not found (HTTP 404). Check the server URL, selected music library, and destination playlist in Settings → Plex."
        return f"Plex returned HTTP {status}. Check the server and reverse proxy before retrying."
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "The server returned an invalid Plex response. Check that Settings → Plex points to your Plex server, including the correct port."
    return f"Could not {operation}. Check Settings → Plex and try Test & Discover Libraries."


def validate_plex_config(config):
    plex = config.config.get("plex") or {}
    url = str(plex.get("url") or "").strip()
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise PlexDiagnosticError(
            "Plex server URL is missing or invalid. Set an http:// or https:// server URL and port in Settings → Plex."
        )
    if not plex.get("token"):
        raise PlexDiagnosticError("Plex token is missing. Configure it in Settings → Plex.")
    if not plex.get("music_library_key"):
        raise PlexDiagnosticError(
            "Plex music library is not selected. Use Test & Discover Libraries in Settings → Plex."
        )
    return {**plex, "url": url}


def configured_secrets(config):
    """Collect configured credentials without loading additional state."""
    found = []

    def walk(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                name = re.sub(r"[^a-z]", "", str(key).lower())
                if (
                    isinstance(child, str)
                    and child
                    and (
                        any(
                            part in name
                            for part in ("token", "apikey", "password", "secret", "authorization")
                        )
                        or name in ("userkey", "clientkey")
                        or (name == "url" and "notifications" in path)
                    )
                ):
                    found.append(child)
                else:
                    walk(child, path + (str(key),))
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, path)

    walk(getattr(config, "config", config) if config is not None else {})
    return found


def redact(message, config=None, *, secrets=()):
    text = str(message)
    values = configured_secrets(config) + [str(value) for value in secrets if value]
    variants = {
        variant
        for value in values
        for variant in (value, quote(value, safe=""), unquote(value))
        if variant
    }
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+", r"\1 [REDACTED]", text)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(
        r"(?i)((?:x-plex-token|x-api-key|api[_-]?key|access[_-]?token|token|authorization|password|secret)[\s\"\x27]*[:=][\s\"\x27]*)([^\s&,\"\x27}]+)",
        r"\1[REDACTED]",
        text,
    )
    return text[:16000]


def service_failure(service, exc, started):
    """Classify upstream failures without exposing request URLs, keys or bodies."""
    import time
    from fastapi import HTTPException

    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(exc, requests.exceptions.SSLError):
        reason = "TLS certificate verification failed. Check the server certificate."
    elif isinstance(exc, requests.exceptions.Timeout):
        reason = "Request timed out. Check connectivity and try again later."
    elif isinstance(exc, requests.exceptions.ConnectionError):
        reason = "Connection failed (DNS, network, or refused connection). Check connectivity from the Harmonia container."
    elif status == 429:
        reason = "Rate limited (HTTP 429). Wait before retrying."
    elif status in (401, 403):
        reason = (
            f"Access rejected (HTTP {status}). Check credentials or service access restrictions."
        )
    elif status:
        reason = f"Service returned HTTP {status}. Check the service status before retrying."
    elif isinstance(exc, ValueError):
        reason = "The service returned invalid JSON."
    else:
        reason = "The request could not be completed."
    return HTTPException(
        504 if isinstance(exc, requests.exceptions.Timeout) else 502,
        f"{service}: {reason} ({type(exc).__name__}, {time.monotonic() - started:.2f}s)",
    )


def concise_error(message):
    """Keep service messages at ERROR; reserve diagnostic bodies/stacks for DEBUG."""
    import json

    text = redact(message)
    start = text.find("{")
    if start >= 0:
        try:
            value = json.loads(text[start:])
            if isinstance(value, dict):
                text = text[:start] + str(
                    value.get("message")
                    or value.get("error")
                    or value.get("detail")
                    or "Service returned an error."
                )
        except (ValueError, TypeError):
            pass
    text = re.split(r'\n\s*(?:at |Traceback|File ")|--- End of stack trace', text)[0].strip()
    return text[:1200]
