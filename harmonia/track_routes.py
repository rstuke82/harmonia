"""Track membership inspection and explicit, scoped match previews."""

import copy
import hashlib
import json
import threading
import time
import uuid
from collections import Counter
from typing import Literal
from pydantic import BaseModel, Field
from fastapi import HTTPException

_cache = {}
_lock = threading.Lock()


class TrackRequest(BaseModel):
    title: str
    artist: str
    album: str = ""


class PreviewRequest(TrackRequest):
    playlist_keys: list[str] = Field(min_length=1)
    mode: Literal["automatic", "manual"]
    plex_id: str | None = None


class ApplyRequest(BaseModel):
    preview_id: str


def inspect(config, track, playlist_keys=None):
    from .api import _playlist_key
    from .sync_engine import Syncer

    syncer = Syncer(config)
    memberships = []
    unknown = []
    for playlist in config.config.get("playlists", []):
        key = _playlist_key(playlist)
        if playlist_keys is not None and key not in playlist_keys:
            continue
        health = config.health.get(key, {})
        source = config.source_snapshots.get(key)
        historical = False
        if not isinstance(source, list):
            source = health.get("source_preview")
            historical = isinstance(source, list)
        if isinstance(source, list):
            from .playlist_overlays import apply

            source = apply(config.repository, key, source)
        source_known = isinstance(source, list)
        if not source_known:
            unknown.append(playlist.get("plex_playlist_name", key))
            source = []
        occurrences = [t for t in source if syncer._same_missing_identity(t, track)]
        # Old databases may have mappings/missing state without complete snapshots.
        if not source_known:
            candidates = list(config.missing.get(key, []))
            for search_key in config.mapping.get(key, {}):
                title, _, artist = search_key.partition("|")
                candidates.append({"title": title, "artist": artist})
            seen = set()
            for t in candidates:
                signature = (t.get("title"), t.get("artist"), t.get("album", ""))
                if signature not in seen and syncer._same_missing_identity(t, track):
                    occurrences.append(t)
                    seen.add(signature)
        if not occurrences:
            continue
        details = []
        for t in occurrences:
            search_key = f"{t.get('title', '')}|{t.get('artist', '')}"
            plex_id = config.mapping.get(key, {}).get(search_key)
            metadata = config.match_metadata.get(key, {}).get(search_key, {})
            status = syncer._get_match_provenance(key, search_key) if plex_id else "unresolved"
            if any(
                m.get("status") == "lost" and syncer._same_missing_identity(m, t)
                for m in config.missing.get(key, [])
            ):
                status = "lost"
            if syncer._find_ignored_track_key(key, t):
                status = "ignored"
            details.append(
                {
                    "source": t,
                    "search_key": search_key,
                    "plex_id": str(plex_id) if plex_id is not None else None,
                    "status": status,
                    "match": metadata.get("matched_track") if isinstance(metadata, dict) else None,
                }
            )
        memberships.append(
            {
                "key": key,
                "name": playlist.get("plex_playlist_name", key),
                "source": playlist.get("source"),
                "occurrences": len(occurrences),
                "count_exact": source_known and not historical,
                "source_basis": "historical health"
                if historical
                else "current saved source"
                if source_known
                else "legacy match state",
                "tracks": details,
                "statuses": dict(Counter(d["status"] for d in details)),
            }
        )
    return {
        "track": track,
        "memberships": memberships,
        "occurrences": sum(m["occurrences"] for m in memberships),
        "cached": True,
        "unknown_playlists": unknown,
    }


def fingerprint(config, rows):
    value = [rows, config.config.get("plex"), config.artist_aliases, config.ignored_tracks]
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def preview(request):
    from .api import _config
    from .matching import Matcher

    config = _config(
        read_only=True,
        namespaces=[
            "health",
            "source_snapshots",
            "mapping",
            "match_metadata",
            "missing",
            "ignored_tracks",
            "artist_aliases",
        ],
    )
    track = request.model_dump(include={"title", "artist", "album"})
    result = inspect(config, track)
    rows = [m for m in result["memberships"] if m["key"] in request.playlist_keys]
    if set(m["key"] for m in rows) != set(request.playlist_keys):
        raise HTTPException(409, "Playlist memberships changed. Reload Track Details.")
    from .library import playable_tracks

    library = playable_tracks()
    lookup = {str(t.get("plex_id")): t for t in library}
    if request.mode == "manual" and request.plex_id not in lookup:
        raise HTTPException(404, "Choose an existing Plex track")
    changes = []
    unchanged = []
    decisions = {}
    for membership in rows:
        for entry in membership["tracks"]:
            if entry["status"] == "ignored":
                unchanged.append(
                    {
                        "playlist": membership["name"],
                        "reason": "Ignored occurrence; restore its ignore rule first.",
                    }
                )
                continue
            identity = json.dumps(entry["source"], sort_keys=True)
            if identity not in decisions:
                decisions[identity] = (
                    Matcher.match_track(entry["source"], library, {})
                    if request.mode == "automatic"
                    else request.plex_id
                )
            chosen = decisions[identity]
            if chosen is None:
                unchanged.append(
                    {
                        "playlist": membership["name"],
                        "reason": "No confident automatic match. Saved match will remain.",
                    }
                )
                continue
            changes.append(
                {
                    "playlist_key": membership["key"],
                    "playlist_name": membership["name"],
                    "search_key": entry["search_key"],
                    "source": entry["source"],
                    "before": entry["status"],
                    "previous_plex_id": entry["plex_id"],
                    "plex_id": str(chosen),
                    "provenance": request.mode,
                    "candidate": lookup[str(chosen)],
                }
            )
    key = str(uuid.uuid4())
    public = {
        "preview_id": key,
        "mode": request.mode,
        "changes": changes,
        "unchanged": unchanged,
        "playlist_count": len(set(c["playlist_key"] for c in changes)),
        "occurrences": len(changes),
        "expires_in": 600,
    }
    with _lock:
        for old in list(_cache):
            if _cache[old]["expires"] < time.monotonic():
                del _cache[old]
        while len(_cache) >= 20:
            del _cache[next(iter(_cache))]
        from .accounts import owner_key

        _cache[key] = {
            "owner": owner_key(),
            "expires": time.monotonic() + 600,
            "public": public,
            "rows": rows,
            "track": track,
            "fingerprint": fingerprint(config, rows),
            "keys": request.playlist_keys,
        }
    return public


def apply_preview(preview_id):
    from .api import _config, _playlist_key, _capture
    from .process_lock import ProcessLock
    from .sync_engine import Syncer
    from . import jobs

    with ProcessLock():
        with _lock:
            cached = copy.deepcopy(_cache.get(preview_id))
        from .accounts import owner_key

        if not cached or cached.get("owner") != owner_key() or cached["expires"] < time.monotonic():
            raise ValueError("Match preview expired. Review a new preview.")
        config = _config()
        syncer = Syncer(config)
        rows = [
            m for m in inspect(config, cached["track"])["memberships"] if m["key"] in cached["keys"]
        ]
        if fingerprint(config, rows) != cached["fingerprint"]:
            raise ValueError("Track state changed after preview. Reload and preview again.")
        changes = cached["public"]["changes"]
        if not changes:
            return {
                "playlists": [],
                "total": 0,
                "unresolved": 0,
                "note": "No confident changes; existing matches retained.",
            }
        # Revalidate destination IDs before changing saved state.
        from .library import playable_tracks

        library = {str(t.get("plex_id")): t for t in playable_tracks()}
        if any(c["plex_id"] not in library for c in changes):
            raise ValueError("A preview candidate is no longer in Plex. Preview again.")
        for change in changes:
            key = change["playlist_key"]
            search_key = change["search_key"]
            config.mapping.setdefault(key, {})[search_key] = change["plex_id"]
            syncer._set_match_provenance(
                key,
                search_key,
                change["provenance"],
                matched_track=library[change["plex_id"]],
                plex_id=change["plex_id"],
            )
            config.missing[key] = [
                t
                for t in config.missing.get(key, [])
                if not syncer._same_missing_identity(t, change["source"])
            ]
        jobs.progress("Saving confirmed match changes")
        config.save()
        with _lock:
            _cache.pop(preview_id, None)
        selected = set(c["playlist_key"] for c in changes)
        playlists = [p for p in config.config["playlists"] if _playlist_key(p) in selected]
        results = []
        for index, p in enumerate(playlists, 1):
            jobs.target(_playlist_key(p), p.get("plex_playlist_name", ""), index, len(playlists))
            try:
                summary, log = _capture(syncer.sync_playlist, p)
                ok = not summary.get("errors")
                results.append(
                    {
                        "key": _playlist_key(p),
                        "name": p.get("plex_playlist_name"),
                        "ok": ok,
                        "error": None if ok else "Match saved; Plex sync failed. See output.",
                        "result": {
                            "summary": summary,
                            "health": config.repository.load("health").get(_playlist_key(p)),
                        },
                    }
                )
            except Exception as exc:
                from .diagnostics import redact

                results.append(
                    {
                        "key": _playlist_key(p),
                        "name": p.get("plex_playlist_name"),
                        "ok": False,
                        "error": redact(str(exc), config),
                    }
                )
            jobs.activity(
                mode="completed" if results[-1]["ok"] else "failed",
                stage="Playlist update finished",
            )
            if jobs.current():
                jobs.current().store.update(
                    jobs.current().id, result={"playlists": results, "total": len(playlists)}
                )
        if any(not row["ok"] for row in results):
            raise ValueError(
                "Match selections saved; one or more affected playlists failed to sync. Review individual results."
            )
        return {"playlists": results, "total": len(playlists), "occurrences_updated": len(changes)}


def register(app):
    @app.post("/api/tracks/details")
    def details(request: TrackRequest):
        from .api import _config

        return inspect(
            _config(
                read_only=True,
                namespaces=[
                    "health",
                    "source_snapshots",
                    "mapping",
                    "match_metadata",
                    "missing",
                    "ignored_tracks",
                    "artist_aliases",
                ],
            ),
            request.model_dump(),
        )

    @app.post("/api/tracks/preview")
    def make_preview(request: PreviewRequest):
        return preview(request)
