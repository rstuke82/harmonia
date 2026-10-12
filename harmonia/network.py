"""Observe this module's HTTP calls without changing requests or its timeouts."""
import requests as _requests
import time
from urllib.parse import urlsplit
from . import jobs


def __getattr__(name):
    return getattr(_requests, name)


def request(method, url, **kwargs):
    quiet_statuses=set(kwargs.pop('_quiet_statuses',()) or ())
    if any(str(k).lower() in ('x-plex-token','x-api-key','authorization') for k in (kwargs.get('headers') or {})):
        kwargs['allow_redirects']=False
    host = (urlsplit(str(url)).hostname or '').lower()
    ctx = jobs.current()
    plex_host = urlsplit(ctx.redaction_config.config.get('plex', {}).get('url', '')).hostname if ctx else None
    service = ('Plex' if host == plex_host else 'Spotify' if 'spotify' in host
               else 'Apple Music' if any(s in host for s in ('apple.com','itunes','mzstatic'))
               else 'external source / artwork')
    started = time.monotonic()
    from .console_logging import web_mode
    def report(level, message):
        if web_mode:
            from .api import _record_log
            _record_log(level, service, message)
    with jobs.waiting(service):
        attempts=3 if str(method).upper()=='GET' else 1
        for attempt in range(1,attempts+1):
            try:
                response = _requests.request(method, url, **kwargs)
                break
            except _requests.exceptions.ChunkedEncodingError as exc:
                if attempt < attempts:
                    report('WARNING',f'{method} {urlsplit(str(url)).path} returned an incomplete response; retrying ({attempt}/{attempts-1})')
                    time.sleep(0.35*attempt)
                    continue
                from .diagnostics import service_failure
                report('ERROR', service_failure(service, exc, started).detail)
                raise
            except _requests.RequestException as exc:
                from .diagnostics import service_failure
                report('ERROR', service_failure(service, exc, started).detail)
                raise
        elapsed = time.monotonic() - started
        message = f'{method} {urlsplit(str(url)).path} returned HTTP {response.status_code} in {elapsed:.2f}s'
        if response.status_code >= 400:
            from .diagnostics import redact
            detail = response.text[:2000]
            for key, value in (kwargs.get('headers') or {}).items():
                if any(name in key.lower() for name in ('token', 'key', 'authorization')) and value:
                    detail = detail.replace(str(value), '[REDACTED]')
            message += ' — ' + redact(detail, ctx.redaction_config if ctx else None)
        report('DEBUG' if response.status_code in quiet_statuses else 'ERROR' if response.status_code >= 400 else 'DEBUG', message)
        return response


def get(url, **kwargs):
    kwargs.setdefault('allow_redirects', True)
    return request('GET', url, **kwargs)


def post(url, **kwargs):
    return request('POST', url, **kwargs)


def put(url, **kwargs):
    return request('PUT', url, **kwargs)


def delete(url, **kwargs):
    return request('DELETE', url, **kwargs)
