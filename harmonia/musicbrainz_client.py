"""Central MusicBrainz routing for public search and optional mirror metadata."""

import threading
import time
from urllib.parse import urlsplit

from . import network
from requests import Session
from . import __version__

PUBLIC_BASE = "https://musicbrainz.org"
USER_AGENT = f"Harmonia/{__version__} (https://github.com/rstuke82/harmonia)"
_public_lock = threading.Lock()
_public_last = 0.0
_session = Session()


def _routed(data, source, base, reason=""):
    """Attach internal routing evidence without changing MusicBrainz fields."""
    if not isinstance(data, dict):
        return data
    return {
        **data,
        "_harmonia_route": {
            "source": source,
            "host": urlsplit(base).hostname or "",
            "reason": reason,
        },
    }


def _search_has_results(data, entity):
    key = {
        "artist": "artists",
        "recording": "recordings",
        "release": "releases",
        "release-group": "release-groups",
    }.get(entity)
    return bool(key and isinstance(data, dict) and data.get(key))


def probe_capabilities(base, release_group_id):
    """Probe optional mirror surfaces without assuming an indexed search node."""
    base = base.rstrip("/")
    found = []
    probes = [
        ("lookup", "release-group/" + release_group_id, {}),
        ("relationships", "release-group/" + release_group_id, {"inc": "artist-credits+releases"}),
        ("browse", "release", {"release-group": release_group_id, "limit": 1}),
    ]
    probes += [
        ("search_" + entity, entity, {"query": "test", "limit": 1})
        for entity in ("artist", "recording", "release", "release-group")
    ]
    for name, entity, params in probes:
        try:
            response = network.get(
                base + "/ws/2/" + entity,
                params={"fmt": "json", **params},
                headers={"User-Agent": USER_AGENT},
                timeout=(3, 10),
                _session=_session,
                _service="MusicBrainz",
            )
            if response.status_code < 400:
                response.json()
                found.append(name)
        except (network.RequestException, ValueError):
            pass
    return found


def _log(repo, message, level="INFO"):
    try:
        repo.add_log(level, "MusicBrainz", message)
    except Exception:
        pass


def _request(base, entity, params, public, timeout):
    global _public_last
    from . import jobs

    def checkpoint():
        if jobs.current():
            jobs.current().checkpoint()

    checkpoint()
    if public:
        # Serialize request starts, not network response waits. A cancelled slow
        # lookup must not hold the public service's rate-limit lock.
        with _public_lock:
            checkpoint()
            time.sleep(max(0, 1.1 - (time.monotonic() - _public_last)))
            checkpoint()
            _public_last = time.monotonic()
    try:
        response = network.get(
            base.rstrip("/") + "/ws/2/" + entity.lstrip("/"),
            params={"fmt": "json", **(params or {})},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            _session=_session,
            _service="MusicBrainz",
        )
    finally:
        checkpoint()
    response.raise_for_status()
    return response.json()


def get(repo, entity, params=None, timeout=(5, 20)):
    """Route indexed queries to public MB and direct/browse reads to the mirror."""
    from .musicbrainz_settings import settings

    preferences = settings(repo)
    if not preferences["enabled"]:
        raise ValueError("MusicBrainz lookups are disabled.")
    params = params or {}
    search = bool(params.get("query"))
    bare_entity = entity.strip("/").split("/")[0]
    direct = "/" in entity.strip("/")
    configured = preferences.get("server_url", PUBLIC_BASE).rstrip("/") or PUBLIC_BASE
    mirror = configured if configured != PUBLIC_BASE else ""
    capabilities = set(preferences.get("capabilities") or [])
    mirror_capability = (
        "search_" + bare_entity
        if search
        else "lookup"
        if direct and not params.get("inc")
        else "relationships"
        if direct
        else "browse"
    )
    if search and (not mirror or mirror_capability not in capabilities):
        _log(repo, "MusicBrainz search -> public")
        return _routed(_request(PUBLIC_BASE, entity, params, True, timeout), "public", PUBLIC_BASE)
    if not mirror:
        _log(repo, "MusicBrainz lookup -> public (no mirror configured)")
        return _routed(_request(PUBLIC_BASE, entity, params, True, timeout), "public", PUBLIC_BASE)
    if capabilities and mirror_capability not in capabilities:
        _log(repo, "MusicBrainz request -> public (capability unavailable on mirror)")
        return _routed(_request(PUBLIC_BASE, entity, params, True, timeout), "public", PUBLIC_BASE)
    _log(
        repo,
        ("MusicBrainz search" if search else "MusicBrainz lookup")
        + " -> configured mirror ("
        + (urlsplit(mirror).hostname or "configured host")
        + ")",
    )
    try:
        result = _request(mirror, entity, params, False, timeout)
        if search and not _search_has_results(result, bare_entity):
            _log(
                repo, "MusicBrainz mirror search returned no results -> public fallback", "WARNING"
            )
            return _routed(
                _request(PUBLIC_BASE, entity, params, True, timeout),
                "public_fallback",
                PUBLIC_BASE,
                "mirror_empty",
            )
        return _routed(result, "mirror", mirror)
    except (network.Timeout, network.ConnectionError):
        _log(repo, "MusicBrainz mirror failed -> public fallback", "WARNING")
    except network.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is None or status < 500:
            raise
        _log(repo, "MusicBrainz mirror failed -> public fallback", "WARNING")
    return _routed(
        _request(PUBLIC_BASE, entity, params, True, timeout),
        "public_fallback",
        PUBLIC_BASE,
        "mirror_failed",
    )
