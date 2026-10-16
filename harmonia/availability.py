"""Scheduled availability discovery; respects manual selections and sync scope."""

import json
import time
from pydantic import BaseModel
from typing import Literal
from . import jobs


class Preferences(BaseModel):
    refresh_lidarr: bool = True
    cache_minutes: Literal[5, 15, 30, 60] = 15


def preferences(repo):
    from .accounts import root_repository

    repo = root_repository()
    return Preferences(**repo.load("availability_settings").get("preferences", {}))


def execute(payload):
    from .api import _config, _health_plex, _playlist_key, job_store, _record_log
    from .process_lock import ProcessLock
    from .sync_engine import Syncer
    from .matching import Matcher
    from .lidarr import config as lidarr_config
    from .library_cache import reuse

    started = time.monotonic()
    repo = job_store().repository
    prefs = preferences(repo)
    warnings = []
    matches = {}
    affected = set()
    decisions = {}
    checked = 0
    with ProcessLock(), reuse(prefs.cache_minutes):
        config = _config()
        syncer = Syncer(config)
        jobs.progress("Loading Plex inventory for missing-match retry")
        library = _health_plex(config).search_library("")
        lookup = {str(t["plex_id"]): t for t in library}
        playlists = {_playlist_key(p): p for p in config.config.get("playlists", [])}
        for key, playlist in playlists.items():
            pending = list(config.missing.get(key, []))
            for track in pending:
                jobs.progress(f"Checking availability: {track.get('title', '')}")
                search = f"{track.get('title', '')}|{track.get('artist', '')}"
                if syncer._find_ignored_track_key(key, track):
                    continue
                old = config.mapping.get(key, {}).get(search)
                provenance = syncer._get_match_provenance(key, search)
                signature = json.dumps(
                    {k: track.get(k, "") for k in ("title", "artist", "album")}, sort_keys=True
                )
                if old and str(old) in lookup:
                    candidate = str(old)
                elif provenance == "manual":
                    continue
                else:
                    if signature not in decisions:
                        decisions[signature] = Matcher.match_track(track, library, {})
                    candidate = decisions[signature]
                checked += 1
                if not candidate:
                    continue
                candidate = str(candidate)
                if candidate not in lookup:
                    continue
                config.mapping.setdefault(key, {})[search] = candidate
                if provenance != "manual":
                    syncer._set_match_provenance(
                        key, search, "automatic", matched_track=lookup[candidate], plex_id=candidate
                    )
                config.missing[key] = [
                    t
                    for t in config.missing.get(key, [])
                    if not syncer._same_missing_identity(t, track)
                ]
                matches[signature] = {
                    "title": track.get("title", ""),
                    "artist": track.get("artist", ""),
                    "album": track.get("album", ""),
                    "plex_id": candidate,
                }
                affected.add(key)
                playlist["ready_to_sync"] = True
            # Preserve readiness from a previous scan until a successful sync clears it.
        jobs.progress("Saving availability matches")
        config.save()
        job_id = None
        # Persist availability per requested source, independently from album import status.
        from .lidarr_requests import save
        from .lidarr_requests import server_id

        cfg = lidarr_config(repo)
        for key, record in repo.load("lidarr_requests").items():
            if record.get("server") != server_id(cfg):
                continue
            jobs.progress("Checking requested tracks in Plex")
            statuses = []
            for source in record.get("sources", []):
                candidates = [
                    t
                    for t in matches.values()
                    if t["title"] == source.get("title") and t["artist"] == source.get("artist")
                ]
                # Check requested tracks even when they are no longer in the missing list.
                track = {
                    "title": source.get("title", ""),
                    "artist": source.get("artist", ""),
                    "album": record.get("title", ""),
                }
                signature = json.dumps(track, sort_keys=True)
                if signature not in decisions:
                    decisions[signature] = Matcher.match_track(track, library, {})
                statuses.append({**source, "available": bool(candidates or decisions[signature])})
            save(
                repo,
                key,
                plex_tracks=statuses,
                plex_checked_at=time.time(),
                plex_available=bool(statuses) and all(t["available"] for t in statuses),
            )
    result = {
        "checked_occurrences": checked,
        "unique_lookups": len(decisions),
        "matches_found": len(matches),
        "affected_playlists": len(affected),
        "sync_job_id": job_id,
        "warnings": warnings,
        "partial_success": bool(warnings),
        "checked_at": time.time(),
        "seconds": round(time.monotonic() - started, 2),
        "summary": f"{len(matches)} tracks now available; {len(affected)} playlists ready to update.",
    }
    repo.save({"availability": {"latest": result}})
    _record_log(
        "INFO",
        "Media availability",
        result["summary"]
        + f" Scan completed in {result['seconds']}s; {len(decisions)} unique lookups.",
    )
    return result


def register(app):
    from .api import job_store

    @app.get("/api/settings/availability")
    def get():
        repo = job_store().repository
        return {
            "settings": preferences(repo).model_dump(),
            "latest": repo.load("availability").get("latest"),
        }

    @app.put("/api/settings/availability")
    def put(request: Preferences):
        job_store().repository.save(
            {"availability_settings": {"preferences": request.model_dump()}}
        )
        return get()

    @app.get("/api/lidarr/links")
    def links():
        from .lidarr import config

        cfg = config(job_store().repository)
        return {
            "downloads": cfg.get("url", "").rstrip("/") + "/activity/queue"
            if cfg.get("enabled")
            else ""
        }
