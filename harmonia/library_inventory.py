"""Revision-scoped inventory indexes for Library preparation and image lookup."""

import hashlib
import json
import threading
import weakref
from collections import OrderedDict

_cache = OrderedDict()
_lock = threading.RLock()
_loading = weakref.WeakValueDictionary()
_epoch = 0


def clear():
    global _epoch
    with _lock:
        _cache.clear()
        _epoch += 1


def indexed(config):
    from .accounts import actor, root_repository
    from .inventory import current
    from .saved_views import revision
    from .lidarr import config as lidarr_config
    from .lidarr_requests import server_id

    root = root_repository()
    repo = config.repository
    user = actor() or {}
    credentials = hashlib.sha256(
        json.dumps(config.config.get("plex", {}), sort_keys=True).encode()
    ).hexdigest()
    cfg = lidarr_config(root)
    key = (
        str(repo.directory),
        str(root.directory),
        user.get("id", ""),
        credentials,
        server_id(cfg),
    )
    stamp = (
        revision(repo, {"inventory", "library_artist_links", "plex_album_links"}),
        revision(root, {"inventory", "library_artist_links", "plex_album_links"}),
    )
    with _lock:
        entry = _cache.get(key)
        if entry and entry[0] == stamp:
            _cache.move_to_end(key)
            return entry[1]
        loading = _loading.setdefault(key, threading.Lock())
    # Unrelated accounts must not wait for this inventory to decode/index.
    with loading:
        stamp = (
            revision(repo, {"inventory", "library_artist_links", "plex_album_links"}),
            revision(root, {"inventory", "library_artist_links", "plex_album_links"}),
        )
        with _lock:
            entry = _cache.get(key)
            epoch = _epoch
            if entry and entry[0] == stamp:
                _cache.move_to_end(key)
                return entry[1]
        plex, lidarr = current(repo, config)
        by_album = {}
        by_artist = {}
        by_name = {}
        by_identity = {}
        ambiguous_names = set()
        ambiguous_albums = set()
        for row in plex.get("rows", []):
            aid = str(row.get("plex_artist_id") or "")
            album = str(row.get("plex_album_id") or "")
            name = str(row.get("album_artist") or row.get("artist") or "").casefold()
            if aid:
                by_artist.setdefault(aid, row)
            if name:
                if name in by_name and str(by_name[name].get("plex_artist_id")) != aid:
                    ambiguous_names.add(name)
                by_name.setdefault(name, row)
            if album:
                by_album.setdefault(album, row)
                identity = (name, str(row.get("album") or "").casefold())
                if (
                    identity in by_identity
                    and str(by_identity[identity].get("plex_album_id")) != album
                ):
                    ambiguous_albums.add(identity)
                by_identity.setdefault(identity, row)
        for name in ambiguous_names:
            by_name.pop(name, None)
        for identity in ambiguous_albums:
            by_identity.pop(identity, None)
        p = config.config.get("plex", {})
        scope = (
            server_id(cfg)
            + ":"
            + hashlib.sha256(
                (p.get("url", "").rstrip("/") + "|" + str(p.get("music_library_key", ""))).encode()
            ).hexdigest()
        )
        data = {
            "_matching_lock": threading.Lock(),
            "plex": plex,
            "lidarr": lidarr,
            "playable_tracks": {str(p["plex_id"]): p for p in plex.get("rows", [])},
            "plex_albums": by_album,
            "plex_artists": by_artist,
            "plex_artist_names": by_name,
            "plex_album_names": by_identity,
            "albums": {str(a.get("album_id")): a for a in lidarr.get("rows", [])},
            "artists": {str(a.get("id")): a for a in lidarr.get("artists", [])},
            "artist_links": root.load("library_artist_links"),
            "album_links": root.load_one("plex_album_links", scope, {}),
        }
        with _lock:
            if epoch == _epoch:
                _cache[key] = (stamp, data)
                _cache.move_to_end(key)
                while len(_cache) > 4:
                    _cache.popitem(last=False)
        return data


def matching_context(config, plex):
    """Reuse mapped catalog links only for the identical saved account inventory."""
    from .track_bridge import context
    from .saved_views import revision
    import time

    data = indexed(config)
    saved = list(data["playable_tracks"].values())
    # Live verification and filtered/copied inputs retain their own link build.
    if len(saved) != len(plex) or any(left is not right for left, right in zip(saved, plex)):
        return context(config, plex, inventory=data["lidarr"])
    stamp = revision(config.repository, {"track_links", "file_tools"})
    with data["_matching_lock"]:
        cached = data.get("_matching_context")
        if cached is None or cached[0] != stamp:
            base = context(config, plex, inventory=data["lidarr"])
            if base is None:
                return None
            base = {
                key: value for key, value in base.items() if key not in ("resolved", "source_links")
            }
            data["_matching_context"] = (stamp, base)
        else:
            base = cached[1]
    scope = base.get("scope", "")
    return {
        **base,
        "resolved": {},
        "source_links": config.repository.load_one("catalog_source_matches", scope, {}),
        "pending": "tracks" not in data["lidarr"]
        or time.time() - data["lidarr"].get("checked_at", 0) > 86400,
    }
