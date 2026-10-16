"""FastAPI backend for Harmonia."""

from __future__ import annotations
from .browser_security import BrowserSecurity
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextvars import ContextVar

from .saved_views import saved as saved_page, COMMON as PAGE_DATA, PLAYLIST_LIST

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from starlette.concurrency import run_in_threadpool
import contextlib
import os
import threading
import time
import uuid
import json
import hashlib
import copy
from collections import Counter
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Depends
from .route_access import require_route_access
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import network

from . import __version__, __build__, __release_name__
from . import jobs
from datetime import timezone
from .bootstrap import _ensure_requirements

from .apple_music_source import AppleMusicAPI
from .config import Config
from .matching import Matcher
from .plex_client import PlexAPI
from .process_lock import ProcessLock
from .spotify_source import SpotifyAPI
from .sync_engine import Syncer
from .text_utils import repair_text
from .diagnostics import PlexDiagnosticError, plex_error, validate_plex_config, redact
from .notifications import WebhookNotifier, WebhookSettings


@contextlib.asynccontextmanager
async def lifespan(app):
    _ensure_requirements()
    from .login_security import initialize_setup, recover_setup
    from .accounts import root_repository

    recover_setup(root_repository())
    config = Config()
    from .factory_reset import recover

    recover(config.repository)
    initialize_setup(root_repository())
    config = Config()
    config.repository.add_log("INFO", "startup", f"Harmonia {__version__} started")
    from .console_logging import configure

    configure(config.repository)
    manager = jobs.Manager(config.repository)
    manager.start()
    app.state.jobs = manager
    try:
        yield
    finally:
        await __import__("asyncio").to_thread(manager.close)


app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(require_route_access)],
    title="Harmonia API",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlaylistAnalyzeRequest(BaseModel):
    url: str


class PlaylistAddRequest(BaseModel):
    url: str
    analysis_id: Optional[str] = None
    name: str = Field(default="", max_length=200)
    sync_mode: Literal["inherit", "disabled"] = "inherit"
    trigger_label: str = Field(default="", max_length=100)


class PlaylistUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    restore_source_name: bool = False


class PlexDiscoverRequest(BaseModel):
    url: str
    token: str = ""


class PlexSettingsRequest(BaseModel):
    url: str
    token: str = ""
    music_library_key: str


class MissingCandidateRequest(BaseModel):
    artists: list[dict[str, str] | str] = Field(default_factory=list, max_length=50)
    title: str
    artist: str
    album: str = ""
    query: str = ""
    duration: float | None = None
    limit: int = Field(default=10, ge=1, le=50)


class MissingMatchRequest(BaseModel):
    title: str
    artist: str
    album: str = ""
    plex_id: str
    playlist_keys: Optional[List[str]] = None
    replace_playlist_key: Optional[str] = None
    provenance: Literal["manual", "automatic"] = "manual"


class NotificationSettingsRequest(BaseModel):
    enabled: bool = False
    url: str = Field(default="", max_length=2048)
    allow_private: bool = False
    notify_sync_failures: bool = True
    notify_lost_matches: bool = True
    notify_unresolved: bool = True
    notify_source_changes: bool = True
    notify_successful_syncs: bool = False
    notify_sync_all_summary: bool = True


def _config(read_only=False, namespaces=None) -> Config:
    return Config(read_only=read_only, namespaces=namespaces)


def _playlist_key(playlist: dict) -> str:
    return f"{playlist.get('source', '')}:{playlist.get('source_id', '')}"


def _has_pending_playlist_changes(playlist, key, drafts):
    """Readiness means unsynced durable work, never cached inventory evidence."""
    return bool(playlist.get("ready_to_sync")) or any(v.get("playlist_key") == key for v in drafts)


def _playlist_payload(config: Config, playlist: dict) -> dict:
    key = _playlist_key(playlist)
    if not hasattr(config, "_pending_settings_view"):
        config._pending_settings_view = config.repository.load("pending_playlist_settings")
    if not hasattr(config, "_match_drafts_view"):
        # Drafts already claimed by a running sync are no longer pending user
        # work. If that job fails, match_queue.available() exposes them again.
        from .match_queue import available

        config._match_drafts_view = available(config.repository)
    if not hasattr(config, "_schedules_view"):
        config._schedules_view = config.repository.load("playlist_schedules")
        from .accounts import root_repository

        with root_repository().connect() as db:
            server = db.execute(
                "SELECT next_run,enabled FROM schedules WHERE action='sync' AND scope='all'"
            ).fetchone()
        config._server_next = server[0] if server and server[1] else None
    sync_state = config.repository.load_one("playlist_sync_state", key, {})
    pending = config._pending_settings_view.get(key, {})
    playlist = {**playlist, **{k: v for k, v in pending.items() if k in ("favorite", "auto_sync")}}
    drafts = config._match_drafts_view
    missing = config.missing.get(key, [])
    counts = Counter(
        Syncer(config)._get_match_provenance(key, search_key)
        for search_key in config.mapping.get(key, {})
    )
    return {
        "match_counts": {name: counts.get(name, 0) for name in ("automatic", "manual", "legacy")},
        "fully_matched": bool(
            not sync_state.get("error")
            and config.health.get(key)
            and config.health[key].get("source_tracks", 0) > 0
            and config.health[key].get("unresolved") == 0
            and config.health[key].get("ignored", 0) == 0
            and config.health[key].get("matched_in_library")
            == config.health[key].get("source_tracks")
        ),
        "key": key,
        "name": (
            __import__("harmonia.source_names", fromlist=["clean"]).clean(
                playlist.get("plex_playlist_name", "")
            )
            if not playlist.get("custom_name") and playlist.get("source_name")
            else playlist.get("plex_playlist_name", "")
        ),
        "source": playlist.get("source", ""),
        "source_url": playlist.get("source_url", ""),
        "source_id": playlist.get("source_id", ""),
        "plex_playlist_id": playlist.get("plex_playlist_id", ""),
        "favorite": playlist.get("favorite", False) is True,
        "auto_sync": playlist.get("auto_sync", True) is not False,
        "last_synced": playlist.get("last_synced"),
        "ready_to_sync": bool(sync_state.get("error"))
        or _has_pending_playlist_changes(playlist, key, drafts),
        "settings_pending": bool(pending),
        "source_name": playlist.get("source_name", ""),
        "source_owner": playlist.get("source_owner", ""),
        "custom_name": playlist.get("custom_name", ""),
        "schedule": (
            {"mode": "inherit", "next_run": config._server_next}
            if config._schedules_view.get(key, {}).get("mode", "inherit") == "inherit"
            else config._schedules_view[key]
        ),
        "added_at": playlist.get("added_at"),
        "last_match_attempt": playlist.get("last_match_attempt"),
        "saved_matches": len(config.mapping.get(key, {})),
        "unresolved": len(missing),
        "lost": sum(1 for track in missing if track.get("status") == "lost"),
        "ignored": len(config.ignored_tracks.get(key, {})),
        "health": config.health.get(key),
        "health_attempt": config.health_attempts.get(key),
        "sync_error": sync_state.get("error", ""),
    }


def _source_for_url(url: str):
    normalized = Config._normalize_url_input(url)
    lower = normalized.lower()
    if lower.startswith("text:"):
        from .text_playlists import TextSource

        return "text", normalized, TextSource()
    from .playlist_urls import canonical_playlist

    try:
        source, canonical = canonical_playlist(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return source, canonical, SpotifyAPI() if source == "spotify" else AppleMusicAPI()


_request_log_id = ContextVar("request_log_id", default="")


def _record_log(level, operation, message, config=None, *, _console=True):
    import logging

    context = jobs.current()
    try:
        config = config or _config(read_only=True, namespaces=[])
        message = redact(message, config)
        if level in ("ERROR", "WARNING"):
            from .diagnostics import concise_error

            short = concise_error(message)
            if short != message:
                _record_log("DEBUG", operation, message, config, _console=_console)
                message = short
        if _request_log_id.get() and not message.startswith(f"[{_request_log_id.get()}]"):
            message = f"[{_request_log_id.get()}] {message}"
        if context:
            message = f"[job {context.id}] {message}"
        if _console:
            logging.getLogger("uvicorn.error").log(
                getattr(logging, level, logging.INFO), "%s: %s", operation, message
            )
        service = (
            operation
            if operation
            in {
                "Plex",
                "Lidarr",
                "MusicBrainz",
                "Apple Music",
                "iTunes",
                "Spotify",
                "Fanart.tv",
                "Last.fm",
            }
            else None
        )
        config.repository.add_log(
            level,
            operation,
            message,
            _console=False,
            request_id=_request_log_id.get() or None,
            job_id=context.id if context else None,
            worker=jobs.worker_for(context.action) if context else None,
            service=service,
        )
        if context:
            context.store.event(context.id, message, operation)
    except Exception:
        logging.getLogger("uvicorn.error").error(
            "Could not persist diagnostic event (%s)", operation
        )


@app.exception_handler(StarletteHTTPException)
async def diagnostic_http_error(request, exc):
    request.state.failure_detail = str(exc.detail)
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def log_operations(request, call_next):
    started = time.monotonic()
    request_id = uuid.uuid4().hex[:12]
    log_token = _request_log_id.set(request_id)
    try:
        response = await call_next(request)
    except Exception as exc:
        import traceback

        route = getattr(request.scope.get("route"), "path", "unmatched route")
        await run_in_threadpool(
            _record_log,
            "ERROR",
            route,
            f"[{request_id}] {request.method} failed after {time.monotonic() - started:.2f}s: {type(exc).__name__}: {exc}",
        )
        await run_in_threadpool(_record_log, "DEBUG", route, traceback.format_exc())
        _request_log_id.reset(log_token)
        raise
    route = request.scope.get("route")
    path = getattr(route, "path", "")
    from . import reset_guard

    if (
        not reset_guard.restart_required
        and path.startswith("/api/")
        and path not in ("/api/health", "/api/settings/logs")
    ):
        level = (
            "ERROR"
            if response.status_code >= 500
            else "WARNING"
            if response.status_code >= 400
            else "INFO"
            if request.method != "GET"
            else "DEBUG"
        )
        detail = getattr(request.state, "failure_detail", "")
        if (
            response.status_code == 404
            and detail == "Plex image unavailable"
            and path.startswith("/api/library/")
        ):
            level = "DEBUG"
        from . import console_logging

        if level != "DEBUG" or console_logging.enabled:
            await run_in_threadpool(
                _record_log,
                level,
                path,
                f"[{request_id}] {request.method} HTTP {response.status_code} in {time.monotonic() - started:.2f}s"
                + (f" — {detail}" if detail else ""),
            )
    _request_log_id.reset(log_token)
    response.headers["X-Request-ID"] = request_id
    if request.url.path.startswith("/assets/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


def _capture(callable_obj, *args, **kwargs):
    # Thread-local capture avoids redirecting unrelated requests' stdout.
    previous = getattr(jobs._local, "output", None)
    buffer = []
    jobs._local.output = buffer
    try:
        result = callable_obj(*args, **kwargs)
        output = redact("\n".join(buffer), _config(read_only=True, namespaces=[]))
        return result, output
    finally:
        jobs._local.output = previous


_analysis_cache = {}
_analysis_lock = threading.Lock()
ANALYSIS_TTL = 600


def _analysis_fingerprint(config):
    data = [
        config.config.get("plex"),
        config.ignored_tracks,
        config.artist_aliases,
        config.mapping,
        config.match_metadata,
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _health_plex(config):
    settings = validate_plex_config(config)
    return PlexAPI(
        settings["url"],
        settings["token"],
        settings["music_library_key"],
        settings.get("music_library_name", ""),
        strict_errors=True,
    )


def _validated_plex_libraries(url: str, token: str) -> List[dict]:
    base_url = str(url or "").strip().rstrip("/")
    plex_token = str(token or "").strip()
    if not base_url or not plex_token:
        raise HTTPException(status_code=400, detail="Plex URL and token are required")

    try:
        response = network.get(
            f"{base_url}/identity",
            headers={"X-Plex-Token": plex_token},
            timeout=8,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not connect to Plex: {exc}") from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Plex connection failed (HTTP {response.status_code}). Check the URL and token.",
        )

    try:
        libraries = Config._get_plex_music_libraries(base_url, plex_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not libraries:
        raise HTTPException(status_code=400, detail="No Plex music libraries were found")

    return libraries


@app.get("/api/health")
def health():
    config = _config(read_only=True, namespaces=[])
    from .updates import stored_status

    playlists = config.config.get("playlists", [])
    unresolved = 0
    lost = 0
    for playlist in playlists:
        tracks = config.missing.get(_playlist_key(playlist), [])
        unresolved += len(tracks)
        lost += sum(1 for track in tracks if track.get("status") == "lost")

    return {
        "status": "ok",
        "version": __version__,
        "release_name": __release_name__,
        "update": stored_status(config.repository),
        "build": __build__,
        "playlists": len(playlists),
        "favorites": sum(1 for p in playlists if p.get("favorite") is True),
        "unresolved": unresolved,
        "lost": lost,
        "plex_configured": bool(
            config.config.get("plex", {}).get("url") and config.config.get("plex", {}).get("token")
        ),
    }


@app.get("/api/playlists")
@saved_page("playlists", PLAYLIST_LIST)
def list_playlists():
    config = _config(read_only=True, namespaces=[])
    return [_playlist_payload(config, playlist) for playlist in config.config.get("playlists", [])]


@app.patch("/api/playlists/{playlist_key:path}", status_code=202)
def update_playlist(playlist_key: str, request: PlaylistUpdateRequest):
    from .queued_settings import enqueue

    return enqueue([playlist_key], request.model_dump(exclude_none=True))


def analyze_playlist(request: PlaylistAnalyzeRequest):
    config = _config(read_only=True)
    source_type, url, source_api = _source_for_url(request.url)
    playlist_id = Config._extract_id(url, source_type)
    if not playlist_id:
        raise HTTPException(status_code=400, detail="Could not extract playlist ID")

    if config.find_playlist(url):
        raise HTTPException(status_code=409, detail="Playlist is already registered")

    try:
        jobs.progress("Reading source playlist")
        (tracks, metadata), source_log = _capture(
            source_api.get_playlist_tracks,
            url,
            fetch_artwork=False,
        )
        from .source_observations import validate as validate_source

        validate_source(
            config.repository,
            f"{source_type}:{playlist_id}",
            tracks,
            metadata,
            config.source_snapshots.get(f"{source_type}:{playlist_id}"),
        )
        jobs.target("job", repair_text(metadata.get("name", "Source playlist")))
        jobs.progress("Matching source tracks against Plex")
        syncer = Syncer(config)
        syncer.plex = _health_plex(config)
        match_result, match_log = _capture(
            syncer._match_source_tracks,
            tracks,
            f"api-analyze:{source_type}:{playlist_id}",
            record_provenance=False,
            mark_new_matches=False,
        )
        matched, unmatched, _mapping, _library, stats = match_result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    analysis_id = str(uuid.uuid4())
    with _analysis_lock:
        for key in list(_analysis_cache):
            if _analysis_cache[key]["expires"] < time.monotonic():
                del _analysis_cache[key]
        while len(_analysis_cache) >= 10:
            del _analysis_cache[next(iter(_analysis_cache))]
        _analysis_cache[analysis_id] = {
            "expires": time.monotonic() + ANALYSIS_TTL,
            "url": url,
            "fingerprint": _analysis_fingerprint(config),
            "tracks": tracks,
            "metadata": metadata,
            "result": match_result,
        }
    return {
        "analysis_id": analysis_id,
        "expires_in": ANALYSIS_TTL,
        "source": source_type,
        "source_id": playlist_id,
        "url": url,
        "name": repair_text(metadata.get("name", "Unknown Playlist")),
        "description": repair_text(metadata.get("description", "")),
        "source_tracks": len(tracks),
        "matched": len(matched),
        "unresolved": len(unmatched),
        "ignored": len(stats.get("ignored_tracks", [])),
        "log": (source_log + match_log).strip(),
    }


def add_playlist(request: PlaylistAddRequest):
    with ProcessLock():
        config = _config()
        source_type, url, source_api = _source_for_url(request.url)
        playlist_id = Config._extract_id(url, source_type)
        if not playlist_id:
            raise HTTPException(status_code=400, detail="Could not extract playlist ID")
        if config.find_playlist(url):
            raise HTTPException(status_code=409, detail="Playlist is already registered")

        try:
            jobs.progress("Connecting to Plex")
            plex = _health_plex(config)
            cached = None
            if request.analysis_id:
                with _analysis_lock:
                    cached = copy.deepcopy(_analysis_cache.get(request.analysis_id))
                if (
                    not cached
                    or cached["expires"] < time.monotonic()
                    or cached["url"] != url
                    or cached["fingerprint"] != _analysis_fingerprint(config)
                ):
                    raise HTTPException(
                        409,
                        "Analysis expired or matching settings changed. Analyze again or choose Add Now.",
                    )
            syncer = Syncer(config)
            syncer.plex = plex
            mapping_key = f"{source_type}:{playlist_id}"
            if cached:
                jobs.progress("Reusing analyzed source tracks and matches")
                tracks, metadata = cached["tracks"], cached["metadata"]
                from .source_observations import validate as validate_source

                complete = validate_source(
                    config.repository,
                    mapping_key,
                    tracks,
                    metadata,
                    config.source_snapshots.get(mapping_key),
                )
                jobs.target("job", repair_text(metadata.get("name", "Source playlist")))
                matched, unmatched, mapping, _library, _stats = cached["result"]
                lookup = {str(t.get("plex_id")): t for t in _library or []}
                for search_key, plex_id in mapping.items():
                    syncer._set_match_provenance(
                        mapping_key,
                        search_key,
                        "automatic",
                        matched_track=lookup.get(str(plex_id)),
                        plex_id=plex_id,
                    )
            else:
                jobs.progress("Reading source playlist and artwork")
                tracks, metadata = source_api.get_playlist_tracks(url, fetch_artwork=True)
                jobs.target("job", repair_text(metadata.get("name", "Source playlist")))
                from .source_observations import validate as validate_source

                complete = validate_source(
                    config.repository,
                    mapping_key,
                    tracks,
                    metadata,
                    config.source_snapshots.get(mapping_key),
                )
                jobs.progress("Matching tracks against the Plex library")
                matched, unmatched, mapping, _library, _stats = syncer._match_source_tracks(
                    tracks, mapping_key, mark_new_matches=False
                )
            if not tracks:
                raise HTTPException(
                    422,
                    "Source returned no tracks. Check that the playlist is public and its URL is correct.",
                )
            from .source_history import record

            record(config.repository, mapping_key, tracks, complete)
            syncer._store_unmatched(mapping_key, unmatched)
            config.mapping[mapping_key] = mapping

            if not matched:
                raise HTTPException(
                    status_code=422,
                    detail="No source tracks matched Plex, so the playlist was not created",
                )

            source_name = repair_text(metadata.get("name", "Unknown Playlist"))
            destination = request.name.strip() or source_name
            if any(
                p.get("plex_playlist_name", "").casefold() == destination.casefold()
                for p in config.config.get("playlists", [])
            ):
                jobs.output(
                    "Another playlist has this name. A separate Plex playlist will be created; rename it in Playlist Details if needed."
                )
            jobs.progress(
                f"Creating Plex playlist with {len(matched)} matched tracks; {len(unmatched)} unresolved"
            )
            plex_playlist_id = syncer._build_new_plex_playlist(
                destination,
                metadata.get("description", ""),
                matched,
                metadata.get("image_url", ""),
            )
            if not plex_playlist_id:
                raise HTTPException(status_code=502, detail="Plex playlist creation failed")

            jobs.progress("Saving playlist registration and source snapshot", check=False)
            playlist = config.add_playlist(
                url,
                source_type,
                destination,
                plex_playlist_id,
            )
            playlist["source_name"] = source_name
            playlist["custom_name"] = request.name.strip()
            playlist["source_owner"] = metadata.get("owner") or metadata.get("curator") or ""
            from .lidarr import state_put

            state_put(
                config.repository,
                "playlist_schedules",
                mapping_key,
                {"mode": request.sync_mode, "days": [0], "hour": 2, "minute": 0},
            )
            from datetime import datetime

            playlist["added_at"] = datetime.now(timezone.utc).isoformat()
            # Preserve the registration even if Plex accepted only part of the update.
            # This gives the user a repair target instead of creating duplicates on retry.
            verification_error = None
            try:
                actual = plex.get_playlist_items(str(plex_playlist_id))
                from .verification import compare, describe, track_details

                verification = compare(matched, actual)
                if not verification["ok"]:
                    verification_error = describe(verification)
                    for detail in track_details(matched, actual, _library or []):
                        jobs.output(detail)
                elif verification["duplicates_collapsed"]:
                    jobs.output(
                        f"⚠ Plex collapsed {verification['duplicates_collapsed']} repeated occurrences; completed with a server limitation."
                    )
            except Exception as exc:
                verification_error = f"Could not verify the new Plex playlist: {exc}"
            if not verification_error:
                playlist["last_synced"] = datetime.now(timezone.utc).isoformat()
                from .playlist_description import render

                plex.update_playlist_metadata(
                    str(plex_playlist_id),
                    destination,
                    render(
                        metadata.get("description", ""),
                        playlist,
                        len(tracks),
                        len(matched),
                        len(unmatched),
                        len(_stats.get("ignored_tracks", [])),
                    ),
                )
            syncer._save_source_snapshot(mapping_key, tracks)
            config.save()
            if not verification_error:
                from .sync_health import persist_sync_health

                persist_sync_health(config, playlist, tracks, matched, unmatched, _stats, actual)
            if request.analysis_id:
                with _analysis_lock:
                    _analysis_cache.pop(request.analysis_id, None)
            result = _playlist_payload(config, playlist)
            if verification_error:
                ctx = jobs.current()
                if ctx:
                    ctx.store.update(ctx.id, result=result)
                raise HTTPException(
                    status_code=502,
                    detail=f"Playlist was created and registered, but {verification_error} Open it in Playlists and sync to repair; do not add it again.",
                )
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc


def sync_all():
    with ProcessLock():
        config = _config()
        result, log = _capture(Syncer(config).sync_all)
        return {"summary": result, "log": log}


def sync_favorites():
    with ProcessLock():
        config = _config()
        result, log = _capture(Syncer(config).sync_favorites)
        return {"summary": result, "log": log}


def sync_one(playlist_key: str):
    with ProcessLock():
        config = _config()
        playlist = next(
            (p for p in config.config.get("playlists", []) if _playlist_key(p) == playlist_key),
            None,
        )
        if playlist is None:
            raise HTTPException(status_code=404, detail="Playlist not found")
        result, log = _capture(Syncer(config).sync_playlist, playlist)
        return {
            "summary": result,
            "log": log,
            "health": config.repository.load("health").get(playlist_key),
        }


def _health_call(fn, *args, **kwargs):
    return fn(*args, **kwargs), ""


def playlist_health(playlist_key: str, plex_library=None):
    """Read-only source/Plex comparison. Only health results/history are persisted."""
    config = _config(read_only=True)
    playlist = next(
        (p for p in config.config.get("playlists", []) if _playlist_key(p) == playlist_key),
        None,
    )
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")

    jobs.progress("Connecting to Plex")
    stage = "Plex connection"
    _record_log(
        "INFO",
        "health",
        f"Checking health: {playlist.get('plex_playlist_name', playlist_key)}",
        config,
    )
    try:
        syncer = Syncer(config)
        plex = _health_plex(config)
        syncer.plex = plex
        jobs.progress("Reading source playlist")
        stage = "source playlist"
        source_type, source_url, source_api = _source_for_url(playlist.get("source_url", ""))
        (observed_source_tracks, metadata), source_log = _health_call(
            source_api.get_playlist_tracks,
            source_url,
            fetch_artwork=False,
        )
        mapping_key = _playlist_key(playlist)
        from .source_observations import validate as validate_source

        validate_source(
            config.repository,
            mapping_key,
            observed_source_tracks,
            metadata,
            config.source_snapshots.get(mapping_key),
        )
        from .playlist_overlays import apply as apply_playlist_overlay

        source_tracks = apply_playlist_overlay(
            config.repository, mapping_key, observed_source_tracks
        )

        jobs.progress("Checking Plex library matches")
        stage = "Plex library"
        if plex_library is None:
            plex_library, library_log = _health_call(plex.search_library, "")
        else:
            library_log = ""
        if not plex_library:
            raise HTTPException(
                status_code=502,
                detail="The selected Plex music library returned no tracks. Check the library selection in Settings → Plex.",
            )

        match_result, match_log = _health_call(
            syncer._match_source_tracks,
            source_tracks,
            mapping_key,
            plex_library=plex_library,
            record_provenance=False,
            mark_new_matches=False,
        )
        matched_ids, unmatched, _mapping, _library, stats = match_result

        jobs.progress("Comparing destination playlist")
        stage = "Plex destination playlist"
        plex_playlist_id = str(playlist.get("plex_playlist_id", ""))
        plex_items, playlist_log = (
            _health_call(plex.get_playlist_items, plex_playlist_id)
            if plex_playlist_id
            else ([], "")
        )
        actual_ids = [
            str(item.get("plex_id")) for item in plex_items if item.get("plex_id") is not None
        ]

        expected = Counter(str(value) for value in matched_ids)
        actual = Counter(actual_ids)
        from .verification import compare

        verification = compare(matched_ids, plex_items)
        if verification["duplicates_collapsed"]:
            expected = actual.copy()
        missing_from_plex = sum((expected - actual).values())
        extra_in_plex = sum((actual - expected).values())

        source_changes = syncer._source_change_report(mapping_key, observed_source_tracks)
        source_added = 0 if source_changes.get("baseline") else len(source_changes.get("added", []))
        source_removed = (
            0 if source_changes.get("baseline") else len(source_changes.get("removed", []))
        )
        ignored = len(stats.get("ignored_tracks", []))
        unresolved = len(unmatched)
        plex_count = len(actual_ids)

        lookup = {str(t.get("plex_id")): t for t in [*plex_items, *plex_library]}

        def differences(counts):
            return [
                {
                    "plex_id": key,
                    "title": lookup.get(key, {}).get("title", key),
                    "artist": lookup.get(key, {}).get("artist", ""),
                    "count": count,
                }
                for key, count in counts.items()
            ]

        result = {
            "key": mapping_key,
            "name": (
                __import__("harmonia.source_names", fromlist=["clean"]).clean(
                    playlist.get("plex_playlist_name", "")
                )
                if not playlist.get("custom_name") and playlist.get("source_name")
                else playlist.get("plex_playlist_name", "")
            ),
            "source": source_type,
            "source_tracks": len(source_tracks),
            "plex_playlist_tracks": plex_count,
            "matched_in_library": len(matched_ids),
            "unresolved": unresolved,
            "lost": sum(1 for track in unmatched if track.get("status") == "lost"),
            "ignored": ignored,
            "missing_from_plex_playlist": missing_from_plex,
            "extra_in_plex_playlist": extra_in_plex,
            "source_added_since_last_sync": source_added,
            "source_removed_since_last_sync": source_removed,
            "healthy": (
                unresolved == 0
                and missing_from_plex == 0
                and extra_in_plex == 0
                and source_added == 0
                and source_removed == 0
                and verification["ok"]
            ),
            "duplicates_collapsed": verification["duplicates_collapsed"],
            "read_only": True,
            "drift_details": {
                "missing_from_plex": differences(expected - actual),
                "extra_in_plex": differences(actual - expected),
                "unresolved": unmatched,
                "source_added": source_changes.get("added", []),
                "source_removed": source_changes.get("removed", []),
            },
            "source_preview": [
                {
                    k: t.get(k, "")
                    for k in ("title", "artist", "album", "source_id", "duration", "artists")
                }
                for t in source_tracks
            ],
            "log": "\n".join(
                part for part in [source_log, library_log, match_log, playlist_log] if part
            ).strip(),
        }
        config.repository.save_health(mapping_key, result)
        config.repository.record_health_attempt(mapping_key)
        _record_log(
            "INFO",
            "health",
            f"Health check completed: {playlist.get('plex_playlist_name', playlist_key)}",
            config,
        )
        return result
    except Exception as exc:
        if isinstance(exc, HTTPException):
            message = str(exc.detail)
        elif isinstance(exc, PlexDiagnosticError) or stage.startswith("Plex"):
            message = plex_error(exc)
        else:
            message = "Could not read the source playlist. Check that its URL is correct and the playlist is public, then retry."
        message = redact(f"Health check failed — {stage}: {message}", config)
        config.repository.record_health_attempt(playlist_key, message)
        _record_log("ERROR", "health", message, config)
        raise HTTPException(status_code=502, detail=message) from exc


def playlist_detail(playlist_key: str, report=lambda message, percent: None):
    config = _config(read_only=True)
    playlist = next(
        (p for p in config.config.get("playlists", []) if _playlist_key(p) == playlist_key), None
    )
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    try:
        report("Reading saved source tracks", 5)
        observed_tracks = config.source_snapshots.get(playlist_key, [])
        metadata = {"name": playlist.get("name") or playlist.get("source_name") or "Playlist"}
        from .playlist_overlays import apply as apply_playlist_overlay

        tracks = apply_playlist_overlay(config.repository, playlist_key, observed_tracks)
        from .inventory import current as saved_inventory

        plex, _ = saved_inventory(config.repository, config)
        library = {str(t.get("plex_id")): t for t in plex.get("rows", [])}
        syncer = Syncer(config)
        report("Resolving saved matches", 50)
        from .track_bridge import context as bridge_context, source_status

        bridge = bridge_context(config, list(library.values()))
        blocked_artists = (
            {
                Matcher._normalize_match_text(v.get("name", ""))
                for v in config.repository.load("blocked_artists").values()
            }
            if playlist.get("source") == "text"
            else set()
        )
        rows = []
        for index, track in enumerate(tracks):
            if index % 10 == 0:
                report(
                    f"Comparing tracks {index + 1} / {len(tracks)}",
                    50 + int(45 * (index + 1) / max(1, len(tracks))),
                )
            search_key = f"{track.get('title', '')}|{track.get('artist', '')}"
            plex_id = config.mapping.get(playlist_key, {}).get(search_key)
            match = library.get(str(plex_id)) if plex_id is not None else None
            status = (
                syncer._get_match_provenance(playlist_key, search_key).capitalize()
                if match
                else ("LOST" if plex_id else "Unresolved")
            )
            if not match and any(
                t.get("status") == "lost" and syncer._same_missing_identity(t, track)
                for t in config.missing.get(playlist_key, [])
            ):
                status = "LOST"
            if (
                syncer._find_ignored_track_key(playlist_key, track)
                or Matcher._normalize_match_text(track.get("artist", "")) in blocked_artists
            ):
                status = "Ignored"
            rows.append(
                {
                    **track,
                    "index": index,
                    "status": status,
                    "match": match,
                    "plex_id": plex_id,
                    "catalog_status": source_status(bridge, track),
                }
            )
        report("Playlist details ready", 100)
        payload = _playlist_payload(config, playlist)
        from .playlist_summary import summarize

        payload = summarize(payload, rows)
        return {
            "playlist": payload,
            "metadata": metadata,
            "tracks": rows,
            "source_refresh_required": playlist_key not in config.source_snapshots,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=redact("Could not load playlist details: " + str(exc), config)
        ) from exc


# A separate, bounded pool keeps slow external I/O away from lightweight routes.
DETAIL_TIMEOUT = 60
_detail_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="playlist-detail")
_detail_slots = threading.BoundedSemaphore(2)

_detail_progress = {}
_detail_progress_lock = threading.Lock()


@app.get("/api/detail-progress/{progress_id}")
def detail_progress(progress_id: str):
    from .accounts import owner_key

    progress_id = owner_key() + ":" + progress_id
    with _detail_progress_lock:
        return _detail_progress.get(progress_id, {"events": [], "percent": 0})


@app.get("/api/playlists/{playlist_key:path}/detail")
@saved_page("playlist details", PAGE_DATA)
def playlist_detail_route(playlist_key: str, progress_id: str = Query(default="", max_length=100)):
    return playlist_detail(playlist_key)


@app.get("/api/settings/plex")
def get_plex_settings():
    config = _config(read_only=True, namespaces=[])
    plex = config.config.get("plex", {})
    token = str(plex.get("token", ""))
    return {
        "url": plex.get("url", ""),
        "token_configured": bool(token),
        "token_hint": (f"••••{token[-4:]}" if len(token) >= 4 else ("••••" if token else "")),
        "music_library_key": str(plex.get("music_library_key", "")),
        "music_library_name": plex.get("music_library_name", ""),
    }


@app.post("/api/settings/plex/discover")
def discover_plex_libraries(request: PlexDiscoverRequest):
    config = _config(read_only=True, namespaces=[])
    existing = config.config.get("plex", {})
    token = request.token.strip() or str(existing.get("token", "")).strip()
    libraries = _validated_plex_libraries(request.url, token)
    return {"libraries": libraries}


@app.put("/api/settings/plex")
def put_plex_settings(request: PlexSettingsRequest):
    with ProcessLock():
        config = _config()
        existing = config.config.get("plex", {})
        token = request.token.strip() or str(existing.get("token", "")).strip()
        libraries = _validated_plex_libraries(request.url, token)
        selected = next(
            (library for library in libraries if library["key"] == str(request.music_library_key)),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=400, detail="Select a valid Plex music library")

        config.config["plex"] = {
            "url": request.url.strip().rstrip("/"),
            "token": token,
            "music_library_key": selected["key"],
            "music_library_name": selected["name"],
        }
        config.save()
        return {
            "url": config.config["plex"]["url"],
            "token_configured": True,
            "token_hint": f"••••{token[-4:]}" if len(token) >= 4 else "••••",
            "music_library_key": selected["key"],
            "music_library_name": selected["name"],
            "connected": True,
        }


@app.get("/api/missing")
@saved_page("missing tracks", PAGE_DATA)
def missing_tracks(scope: Literal["all", "favorites"] = "all"):
    config = _config(read_only=True, namespaces=[])
    playlists = config.config.get("playlists", [])
    if scope == "favorites":
        playlists = [p for p in playlists if p.get("favorite") is True]
    syncer = Syncer(config)
    rows = syncer.collect_all_missing_tracks_deduped(playlists=playlists)
    # Index registered manual mappings once; never infer manual from identity alone.
    manual = {}

    def identity(track):
        return tuple(
            repair_text(track.get(field, "")).casefold().strip() for field in ("title", "artist")
        )

    for playlist in config.config.get("playlists", []):
        key = _playlist_key(playlist)
        for search_key, plex_id in config.mapping.get(key, {}).items():
            if not plex_id or syncer._get_match_provenance(key, search_key) != "manual":
                continue
            title, _, artist = search_key.partition("|")
            track = {"title": title, "artist": artist}
            if any(syncer._same_missing_identity(t, track) for t in config.missing.get(key, [])):
                continue
            if syncer._find_ignored_track_key(key, track):
                continue
            manual.setdefault(identity(track), []).append(
                {"key": key, "name": playlist.get("plex_playlist_name", key)}
            )
    for row in rows:
        row["manual_matches_elsewhere"] = list(
            {m["key"]: m for m in manual.get(identity(row), [])}.values()
        )
        members = []
        for playlist in playlists:
            key = _playlist_key(playlist)
            matches = [
                t for t in config.missing.get(key, []) if syncer._same_missing_identity(t, row)
            ]
            if matches:
                members.append(
                    {
                        "key": key,
                        "name": playlist.get("plex_playlist_name", key),
                        "source": playlist.get("source"),
                        "favorite": bool(playlist.get("favorite")),
                        "statuses": list(
                            {"LOST" if t.get("status") == "lost" else "Missing" for t in matches}
                        ),
                        "count": len(matches),
                        "last_checked": playlist.get("last_match_attempt")
                        or playlist.get("last_synced"),
                    }
                )
        row["memberships"] = members
        row["playlist_count"] = len(members)
        row["last_checked"] = max(
            (m["last_checked"] for m in members if m["last_checked"]), default=None
        )
    return rows


@app.post("/api/missing/candidates")
def missing_candidates(request: MissingCandidateRequest):
    started = time.monotonic()
    cpu_started = time.thread_time()
    config = _config(read_only=True, namespaces=["artist_aliases"])
    from .library import playable_tracks

    library = playable_tracks()
    inventory_elapsed = time.monotonic() - started
    source = {
        "title": request.title,
        "artist": request.artist,
        "album": request.album,
        "duration": request.duration,
    }
    import re

    query_tokens = re.findall(r"\w+", Matcher._normalize_match_text(request.query or ""))
    if query_tokens:
        filtered = []
        for track in library:
            candidate_tokens = set(
                re.findall(
                    r"\w+",
                    Matcher._normalize_match_text(
                        " ".join(str(track.get(k) or "") for k in ("title", "artist", "album"))
                    ),
                )
            )
            if all(token in candidate_tokens for token in query_tokens):
                filtered.append(track)
        library = filtered
    from .candidate_ranking import rank

    result, scored = rank(source, library, request.limit)
    _record_log(
        "INFO",
        "Candidate search",
        f"{request.title} — {request.artist}: returned {len(result)} candidates from {len(library)} library tracks; inventory={inventory_elapsed:.2f}s, ranking={time.monotonic() - started - inventory_elapsed:.2f}s ({scored} fully scored), CPU={time.thread_time() - cpu_started:.2f}s",
        config,
    )
    for row in result[:5]:
        _record_log(
            "DEBUG",
            "Candidate search",
            f"{row['title']} — {row['artist']} ({row['album']}): score={row['score']}, identity={row['identity_score']}, album penalty={row['album_penalty']}, version penalty={row['title_variant_penalty']}, release penalty={row['release_intent_penalty']}",
            config,
        )
    return result


def save_missing_match(request: MissingMatchRequest):
    with ProcessLock():
        config = _config()
        syncer = Syncer(config)
        playlists = config.config.get("playlists", [])
        if request.playlist_keys is not None:
            wanted = set(request.playlist_keys)
            playlists = [p for p in playlists if _playlist_key(p) in wanted]

        source_track = {
            "title": request.title,
            "artist": request.artist,
            "album": request.album,
        }
        affected_playlists = syncer._playlists_containing_missing_track(
            source_track,
            playlists,
        )

        from .library import playable_tracks

        library = playable_tracks()
        selected = next(
            (track for track in library if str(track.get("plex_id")) == request.plex_id),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="Plex track not found")

        target = None
        if request.replace_playlist_key:
            target = next(
                (
                    p
                    for p in config.config.get("playlists", [])
                    if _playlist_key(p) == request.replace_playlist_key
                ),
                None,
            )
            if target is None:
                raise HTTPException(status_code=404, detail="Playlist not found")
            if target not in affected_playlists and any(
                syncer._same_missing_identity(t, source_track)
                for t in config.missing.get(request.replace_playlist_key, [])
            ):
                affected_playlists.append(target)

        if request.provenance == "automatic":
            automatic_id = Matcher.match_track(source_track, library, {})
            if str(automatic_id) != request.plex_id:
                raise HTTPException(
                    409,
                    "The automatic candidate has changed. Retry automatic matching before applying it.",
                )
        jobs.progress("Saving " + request.provenance + " match")
        affected = syncer._apply_global_missing_match(source_track, selected, affected_playlists)

        if request.provenance == "automatic":
            for affected_playlist in affected_playlists:
                affected_key = _playlist_key(affected_playlist)
                for search_key, value in config.mapping.get(affected_key, {}).items():
                    title, _, artist = search_key.partition("|")
                    if str(value) == request.plex_id and syncer._same_missing_identity(
                        {"title": title, "artist": artist}, source_track
                    ):
                        syncer._set_match_provenance(
                            affected_key,
                            search_key,
                            "automatic",
                            matched_track=selected,
                            plex_id=request.plex_id,
                        )
            config.save()
        if target is not None:
            key = request.replace_playlist_key
            search_key = f"{request.title}|{request.artist}"
            config.mapping.setdefault(key, {})[search_key] = request.plex_id
            syncer._set_match_provenance(
                key, search_key, request.provenance, matched_track=selected, plex_id=request.plex_id
            )
            config.save()
            if target not in affected_playlists:
                affected_playlists.append(target)
                affected += 1

        sync_results = []
        combined_log = []
        for index, playlist in enumerate(affected_playlists):
            jobs.target(
                _playlist_key(playlist),
                playlist.get("plex_playlist_name", ""),
                index + 1,
                len(affected_playlists),
            )
            jobs.progress("Syncing affected playlist: " + playlist.get("plex_playlist_name", ""))
            result, log = _capture(syncer.sync_playlist, playlist)
            jobs.activity(
                mode="failed" if result.get("errors") else "completed",
                stage="Playlist update finished",
            )
            sync_results.append(
                {
                    "key": _playlist_key(playlist),
                    "name": (
                        __import__("harmonia.source_names", fromlist=["clean"]).clean(
                            playlist.get("plex_playlist_name", "")
                        )
                        if not playlist.get("custom_name") and playlist.get("source_name")
                        else playlist.get("plex_playlist_name", "")
                    ),
                    "summary": result,
                    "ok": not result.get("errors"),
                }
            )
            ctx = jobs.current()
            if ctx:
                ctx.store.update(
                    ctx.id,
                    result={
                        "affected": affected,
                        "total": len(affected_playlists),
                        "synced_playlists": len(sync_results),
                        "playlists": sync_results,
                    },
                )
            if log:
                combined_log.append(log)

        if any(p.get("summary", {}).get("errors") for p in sync_results):
            ctx = jobs.current()
            if ctx:
                ctx.store.update(
                    ctx.id,
                    result={
                        "affected": affected,
                        "synced_playlists": len(affected_playlists),
                        "playlists": [
                            {**p, "ok": not p.get("summary", {}).get("errors")}
                            for p in sync_results
                        ],
                    },
                )
            raise HTTPException(
                502,
                "Manual match was saved, but a playlist update failed. Review the output and sync that playlist again.",
            )
        return {
            "affected": affected,
            "synced_playlists": len(affected_playlists),
            "playlists": sync_results,
            "log": "\n".join(combined_log).strip(),
        }


@app.get("/api/settings/notifications")
def get_notification_settings():
    config = _config(read_only=True, namespaces=[])
    return WebhookSettings.from_dict(config.config.get("notifications")).to_dict()


@app.put("/api/settings/notifications")
def put_notification_settings(request: NotificationSettingsRequest):
    with ProcessLock():
        config = _config()
        settings = WebhookSettings.from_dict(request.model_dump())
        if settings.url or settings.enabled:
            from .webhook_transport import destination

            destination(settings.url, settings.allow_private)
        config.config["notifications"] = settings.to_dict()
        config.save()
        return settings.to_dict()


@app.post("/api/settings/notifications/test")
def test_notification():
    config = _config(read_only=True, namespaces=[])
    settings = WebhookSettings.from_dict(config.config.get("notifications"))
    if not settings.enabled or not settings.url:
        raise HTTPException(
            status_code=400, detail="Webhook notifications are not enabled/configured"
        )
    try:
        sent = WebhookNotifier(settings).send(
            "test",
            "Harmonia webhook test",
            {"version": __version__},
        )
        return {"sent": sent}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Notification delivery failed. Check the server URL and connection.",
        ) from None


@app.get("/api/settings/logs")
def get_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    level: Optional[Literal["DEBUG", "INFO", "WARNING", "ERROR"]] = None,
    action: Optional[str] = None,
    worker: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    job_id: Optional[str] = None,
    since: Optional[str] = None,
):
    config = _config(read_only=True, namespaces=[])
    debug = bool(config.repository.load("console_settings").get("debug", False))
    if level == "DEBUG" and not debug:
        return {
            "entries": [],
            "retention": 100000,
            "debug": False,
            "facets": config.repository.log_facets(),
        }
    rows = config.repository.logs(limit, level, action, worker, q, job_id, since)
    if not debug and not level:
        rows = [row for row in rows if row.get("level") != "DEBUG"]
    for row in rows:
        row["message"] = redact(row["message"], config)
    return {
        "entries": rows,
        "retention": 100000,
        "debug": debug,
        "facets": config.repository.log_facets(),
    }


class RemoveRequest(BaseModel):
    playlist_keys: List[str] = Field(min_length=1)
    delete_plex: bool = False


class JobRequest(BaseModel):
    submission_id: str = Field(default="", pattern=r"^(|[0-9a-f-]{36})$")
    action: Literal[
        "file_audit",
        "local_scan",
        "sync",
        "health",
        "analyze",
        "add",
        "fix_match",
        "track_match",
        "remove",
        "backup",
        "check_updates",
        "ignore_batch",
        "availability",
        "plex_scan",
        "lidarr_scan",
        "reconcile",
        "retry_missing",
        "database_optimize",
        "database_compact",
        "database_cleanup",
        "database_rebuild",
    ]
    payload: dict = Field(default_factory=dict)


class StartupViewsRequest(BaseModel):
    startup: Literal["never", "outdated", "always"]


@app.put("/api/tasks/startup-views")
def startup_views_settings(request: StartupViewsRequest):
    from .accounts import actor

    if actor() and not actor().get("admin"):
        raise HTTPException(403, "Administrator access required")
    from .lidarr import state_put

    state_put(job_store().repository, "task_startup", "page_views", request.startup)
    return {"startup": request.startup}


class ScheduleRequest(BaseModel):
    action: Literal[
        "lastfm_refresh",
        "file_audit",
        "local_scan",
        "sync",
        "health",
        "backup",
        "check_updates",
        "availability",
        "plex_scan",
        "lidarr_scan",
        "library_metadata_all",
        "reconcile",
        "retry_missing",
        "database_optimize",
    ]
    scope: Literal["all", "favorites", "automatic"] = "all"
    hours: Literal[0, 1, 3, 6, 12, 24, 168]
    startup: Literal["never", "missing", "outdated", "always"] | None = None
    start_time: str = Field(default="00:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def job_store():
    from .accounts import personal_repository

    return jobs.Store(personal_repository())


def validated_payload(action, payload):
    from .accounts import actor

    user = actor()
    if user and not user.get("admin") and user.get("can_playlists", True) is False:
        raise HTTPException(403, "Playlist changes are disabled for this account.")
    if (
        user
        and not user.get("admin")
        and action
        not in (
            "sync",
            "add",
            "analyze",
            "remove",
            "track_match",
            "fix_match",
            "ignore_batch",
            "health",
        )
    ):
        raise HTTPException(403, "Administrator access required for this job")
    if action == "ignore_batch":
        batch = BulkIgnoreRequest(**payload)
        if any(not t.universal and not t.playlist_keys for t in batch.tracks):
            raise ValueError("Select playlists or universal ignore for every track")
        return batch.model_dump()
    if action in (
        "file_audit",
        "local_scan",
        "backup",
        "check_updates",
        "availability",
        "plex_scan",
        "lidarr_scan",
        "reconcile",
        "retry_missing",
        "database_optimize",
        "database_compact",
        "database_rebuild",
    ):
        return {}
    if action == "database_cleanup":
        return {
            "job_days": max(7, min(3650, int(payload.get("job_days", 90)))),
            "log_days": max(1, min(3650, int(payload.get("log_days", 30)))),
        }
    if action == "remove":
        return RemoveRequest(**payload).model_dump()
    if action == "track_match":
        from .track_routes import ApplyRequest

        return ApplyRequest(**payload).model_dump()
    if action in ("add", "analyze", "fix_match"):
        model = {
            "add": PlaylistAddRequest,
            "analyze": PlaylistAnalyzeRequest,
            "fix_match": MissingMatchRequest,
        }[action]
        data = model(**payload).model_dump()
        if action == "add" and user and not user.get("admin"):
            data["sync_mode"] = "inherit"
        if action == "add":
            source, url, _source_api = _source_for_url(data["url"])
            if not Config._extract_id(url, source):
                raise ValueError("Enter a valid Spotify or Apple Music playlist URL")
            if _config(read_only=True, namespaces=[]).find_playlist(url):
                raise HTTPException(409, "Playlist is already registered")
        return data
    scope = payload.get("scope", "all")
    if scope in ("favorites", "automatic"):
        raise ValueError("Use Sync Playlists or select playlists explicitly")
    if scope not in jobs.SCOPES:
        raise ValueError("Choose all or selected playlists")
    keys = payload.get("playlist_keys", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ValueError("playlist_keys must be a list of playlist ids")
    if scope == "selected" and not keys:
        raise ValueError("Select at least one playlist")
    if action == "health":
        if user and not user.get("admin") and scope != "selected":
            raise HTTPException(403, "Select your own playlists for a health check")
        if scope == "selected":
            registered = {
                _playlist_key(p)
                for p in _config(read_only=True, namespaces=[]).config.get("playlists", [])
            }
            if any(key not in registered for key in keys):
                raise HTTPException(404, "Playlist not found")
    return {"scope": scope, **({"playlist_keys": keys} if scope == "selected" else {})}


@app.post("/api/jobs", status_code=202)
def queue_job(request: JobRequest):
    from .accounts import actor, as_user, is_owner

    user = actor()
    if (
        user
        and user.get("admin")
        and not is_owner(user)
        and request.action
        in (
            "file_audit",
            "local_scan",
            "backup",
            "check_updates",
            "availability",
            "lidarr_scan",
            "database_optimize",
            "database_compact",
            "database_cleanup",
            "database_rebuild",
        )
    ):
        with as_user({**user, "server_context": True}):
            return queue_job(request)
    if (
        user
        and user.get("admin")
        and not is_owner(user)
        and request.action == "health"
        and request.payload.get("scope", "all") == "all"
    ):
        with as_user({**user, "server_context": True}):
            return queue_job(request)
    try:
        payload = validated_payload(request.action, request.payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    store = job_store()
    if not request.submission_id:
        return store.get(store.enqueue(request.action, payload))
    with store.repository.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute(
            "SELECT value FROM state WHERE namespace='job_submissions' AND key=?",
            (request.submission_id,),
        ).fetchone()
        if saved:
            job_id = json.loads(saved[0])["job_id"]
        else:
            job_id = store.enqueue(request.action, payload, db=db)
            db.execute(
                "INSERT INTO state VALUES('job_submissions',?,?)",
                (request.submission_id, json.dumps({"job_id": job_id})),
            )
    return store.get(job_id)


@app.get("/api/jobs")
def list_jobs(background: bool = False):
    from .accounts import actor
    from .admin_activity import listing

    return (
        listing(summary=True, background=background)
        if actor() and actor().get("admin")
        else job_store().summaries(background)
    )


@app.post("/api/jobs/cancel-pending")
def cancel_pending_jobs():
    from .accounts import actor, root_repository
    from .admin_activity import stores

    user = actor()
    targets = list(stores()) if user and user.get("admin") else [(user, job_store())]
    cancelled = []
    for owner, store in targets:
        ids = store.cancel_pending()
        if ids:
            cancelled.append(
                {"owner_id": str(owner["id"]) if owner else "server", "count": len(ids)}
            )
    total = sum(row["count"] for row in cancelled)
    if user and user.get("admin"):
        root_repository().add_log(
            "INFO", "Admin cancellation", f"{user['id']} stopped {total} pending jobs"
        )
    return {
        "cancelled": total,
        "accounts": cancelled,
        "message": f"Stopped {total} pending job" + ("" if total == 1 else "s") + ".",
    }


def activity_store(job_id):
    from .admin_activity import resolve

    return resolve(job_id)


@app.get("/api/jobs/submission/{submission_id}")
def submitted_job(submission_id: str):
    store = job_store()
    reference = store.repository.load("job_submissions").get(submission_id, {})
    row = store.get(reference.get("job_id", ""))
    if not row:
        from .accounts import actor, root_repository

        if actor() and actor().get("admin"):
            server = jobs.Store(root_repository())
            reference = server.repository.load("job_submissions").get(submission_id, {})
            row = server.get(reference.get("job_id", ""))
    if not row:
        raise HTTPException(
            404, "Submission not found yet. Check Activity before submitting again."
        )
    return row


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    row = activity_store(job_id).get(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return row


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, after: int = Query(default=0, ge=0)):
    if not activity_store(job_id).get(job_id):
        raise HTTPException(404, "Job not found")
    return activity_store(job_id).events(job_id, after)


@app.get("/api/jobs/{job_id}/live")
def live_job(job_id: str, after: int = Query(default=0, ge=0)):
    store = activity_store(job_id)
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    from .admin_activity import job as described

    return {"job": described(job_id, row=job, store=store), "events": store.events(job_id, after)}


@app.get("/api/jobs/{job_id}/log")
def download_job_log(job_id: str):
    from starlette.responses import StreamingResponse

    store = activity_store(job_id)
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    def lines():
        yield f"{job['action']} · {job['status']}\nStarted: {job['started_at']}\n"
        after = 0
        while True:
            rows = store.events(job_id, after)
            if not rows:
                break
            for row in rows:
                yield f"{row['created_at']}  {row['message']}\n"
            after = rows[-1]["id"]
        if job.get("error"):
            yield f"Error: {job['error']}\n"

    return StreamingResponse(
        lines(),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="harmonia-{job["id"]}.log"'},
    )


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    row = activity_store(job_id).cancel(job_id)
    from .accounts import actor, root_repository

    if actor() and actor().get("admin"):
        root_repository().add_log(
            "INFO", "Admin cancellation", f"{actor()['id']} requested cancellation of job {job_id}"
        )
    if not row:
        raise HTTPException(404, "Job not found")
    return row


@app.get("/api/schedules")
def list_schedules():
    return [
        s
        for s in job_store().schedules()
        if not (s["action"] in ("sync", "health") and s["scope"] != "all")
    ]


@app.post("/api/schedules")
def create_schedule(request: ScheduleRequest):
    try:
        return job_store().save_schedule(request.model_dump())
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: str, request: ScheduleRequest):
    if not any(s["id"] == schedule_id for s in job_store().schedules()):
        raise HTTPException(404, "Schedule not found")
    try:
        return job_store().save_schedule(request.model_dump(), schedule_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    store = job_store()
    task = next((t for t in store.schedules() if t["id"] == schedule_id), None)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["action"] == "check_updates":
        raise HTTPException(422, "The update check is a built-in task")
    store.save_schedule({**task, "hours": 0}, schedule_id)
    return {"disabled": True}


@app.post("/api/schedules/{schedule_id}/run", status_code=202)
def run_schedule(schedule_id: str):
    store = job_store()
    schedule = next((s for s in store.schedules() if s["id"] == schedule_id), None)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    if schedule["action"] in ("sync", "health") and schedule["scope"] != "all":
        raise HTTPException(410, "This task has been retired")
    from .tasks import payload

    data = payload(schedule)
    if schedule["action"] == "sync":
        from .sync_policy import eligible

        keys = [
            _playlist_key(p)
            for p in eligible(
                store.repository, _config(read_only=True, namespaces=[]).config.get("playlists", [])
            )
        ]
        # The server task may still have work in other accounts.
        data = {"scope": "selected", "playlist_keys": keys}
    from .accounts import scheduled_members

    scheduled_members(schedule["action"], schedule_id)
    return store.get(store.enqueue(schedule["action"], data))


def _health_batch(playlists, config):
    """Three read-only checks at a time; persist each completion and drain on cancel."""
    ctx = jobs.current()
    results = []
    cancelled = False
    remaining = iter(playlists)

    # Load one account-scoped inventory before starting concurrent playlist
    # comparisons. Previously each worker downloaded the full Plex music
    # library, so three healthy connections could time out together on large
    # libraries. The scheduled Plex scan remains responsible for refreshing
    # the persistent snapshot.
    from .library_cache import reuse
    from .availability import preferences

    jobs.progress("Loading shared Plex library inventory")
    with reuse(preferences(job_store().repository).cache_minutes):
        plex_library = _health_plex(config).search_library("")
    if not plex_library:
        raise ValueError(
            "The saved Plex music library inventory is empty. Run Scan Plex Library and check the selected music library."
        )

    def perform(playlist):
        jobs._local.context = ctx
        jobs.target(
            _playlist_key(playlist),
            playlist.get("plex_playlist_name", ""),
            playlists.index(playlist) + 1,
            len(playlists),
        )
        try:
            if ctx:
                ctx.checkpoint()
            result = playlist_health(_playlist_key(playlist), plex_library=plex_library)
            jobs.activity(mode="completed", stage="Health check complete")
            return {
                "key": _playlist_key(playlist),
                "name": playlist.get("plex_playlist_name"),
                "ok": True,
                "result": result,
            }
        except Exception as exc:
            jobs.activity(mode="failed", stage=redact(getattr(exc, "detail", str(exc)), config))
            return {
                "key": _playlist_key(playlist),
                "name": playlist.get("plex_playlist_name"),
                "ok": False,
                "error": redact(getattr(exc, "detail", str(exc)), config),
            }
        finally:
            jobs._local.context = None
            jobs._local.target = None

    # Keep provider/Plex pressure bounded; the UI and library work must remain
    # responsive while a large health check is running.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="playlist-health") as pool:
        pending = set()
        while True:
            try:
                if ctx:
                    ctx.checkpoint()
            except jobs.Cancelled:
                cancelled = True
            while not cancelled and len(pending) < 3:
                playlist = next(remaining, None)
                if playlist is None:
                    break
                from contextvars import copy_context

                pending.add(pool.submit(copy_context().run, perform, playlist))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    results.append(future.result())
                except jobs.Cancelled:
                    cancelled = True
            if ctx:
                ctx.store.update(
                    ctx.id,
                    result={"playlists": results, "total": len(playlists)},
                    progress=f"Health: {len(results)} of {len(playlists)} finished",
                )
    if cancelled:
        raise jobs.Cancelled()
    if any(not r["ok"] for r in results):
        raise ValueError(
            f"{sum(not r['ok'] for r in results)} of {len(results)} playlists failed. See job results and logs."
        )
    return {"playlists": results, "total": len(playlists)}


def execute_job(action, payload):
    if action == "musicbrainz_lookup":
        from .lidarr import execute_lookup

        return execute_lookup(payload)
    if action == "artwork_sync":
        from .artwork_sync import pending

        return pending(_config(read_only=True, namespaces=[]))
    if action == "prepare_views":
        from .saved_views import execute

        return execute(payload)
    if action == "request_status":
        from .lidarr_downloads import snapshot
        from .lidarr import config
        from .accounts import root_repository

        snapshot(root_repository(), config(root_repository()))
        return {"summary": "Request download status updated."}
    if action.startswith("database_"):
        from .database_maintenance import execute

        return execute(action, payload)
    if action in ("file_audit", "file_audit_duplicates"):
        from .file_audit import execute

        return execute(action == "file_audit_duplicates")
    if action in ("library_metadata", "library_metadata_all"):
        from .file_tools import refresh_metadata, refresh_all_metadata

        return (
            refresh_all_metadata(payload)
            if action == "library_metadata_all"
            else refresh_metadata(payload)
        )
    if action == "lastfm_refresh":
        from .discover import refresh_lastfm

        return refresh_lastfm()
    if action in ("local_scan", "local_catalog", "artist_metadata"):
        return {
            "summary": "Local catalog retired. Use Library for Lidarr albums and File Tools for path mappings."
        }
    if action == "playlist_description":
        from .description_refresh import execute

        return execute(payload)
    if action == "recreate":
        from .recreate import execute

        return execute(payload)
    if action in ("plex_scan", "lidarr_scan"):
        from .inventory import scan

        return scan(action.split("_")[0])
    if action == "lidarr_webhook":
        from .api_v1 import execute_lidarr_webhook

        return execute_lidarr_webhook(payload)
    if action == "album_finalize":
        from .album_finalize import execute

        return execute(payload)
    if action == "release_change":
        from .release_change import execute

        return execute(payload)
    if action == "reconcile":
        from .inventory import reconcile

        return reconcile()
    if action == "text_playlist":
        from .text_playlists import execute

        return execute(payload)
    if action == "playlist_settings":
        from .queued_settings import execute

        return execute()
    if action == "retry_missing":
        from .availability import execute

        return execute(payload)

    if action == "availability":
        from .availability import execute

        return execute(payload)
    if action == "sync":
        from .match_queue import available, execute as apply_edits
        from .sync_policy import eligible

        config = _config(read_only=True, namespaces=[])
        selected = config.config.get("playlists", [])
        if payload.get("scope") == "selected":
            selected = [p for p in selected if _playlist_key(p) in payload.get("playlist_keys", [])]
        elif jobs.current() and jobs.current().store.get(jobs.current().id).get("schedule_id"):
            selected = eligible(config.repository, selected)
        keys = {_playlist_key(p) for p in selected}
        drafts = [r for r in available(job_store().repository) if r["playlist_key"] in keys]
        from .library_cache import reuse
        from .availability import preferences

        with reuse(preferences(job_store().repository).cache_minutes, refresh=bool(drafts)):
            if drafts:
                apply_edits({"changes": drafts, "save_only": True})
            return _execute_job(action, payload)
    return _execute_job(action, payload)


def _execute_job(action, payload):
    if action == "ignore_batch":
        request = BulkIgnoreRequest(**payload)
        results = []
        for index, track in enumerate(request.tracks):
            jobs.progress(
                f"Ignoring track {index + 1} of {len(request.tracks)}: {track.title}",
                completed=index,
                total=len(request.tracks),
            )
            try:
                result = ignore_missing(track)
                results.append({"title": track.title, "artist": track.artist, "ok": True, **result})
            except Exception as exc:
                message = redact(getattr(exc, "detail", str(exc)))
                _record_log("ERROR", "Bulk ignore", f"{track.title} — {track.artist}: {message}")
                results.append(
                    {"title": track.title, "artist": track.artist, "ok": False, "error": message}
                )
        failed = sum(not row["ok"] for row in results)
        return {
            "tracks": results,
            "partial_success": bool(failed),
            "summary": f"{len(results) - failed} tracks ignored; {failed} failed. Plex changes apply on the next sync.",
        }
    if action == "ignore":
        return ignore_missing(IgnoreRequest(**payload))
    if action == "match_batch":
        from .match_queue import execute
        from .library_cache import reuse

        with reuse(refresh=True):
            return execute(payload)
    if action == "lidarr_search":
        from .lidarr import retry_search

        return retry_search(payload)
    if action == "lidarr_add":
        from .lidarr import execute

        return execute(payload)
    if action == "check_updates":
        from .updates import check

        jobs.progress("Checking the published release image")
        result = check(True)
        if result.get("error"):
            raise ValueError(result["error"])
        return result
    if action in ("backup", "restore_backup"):
        from . import backups

        with ProcessLock():
            repo = job_store().repository
            if action == "restore_backup":
                return backups.restore(repo, payload["name"])
            ctx = jobs.current()
            kind = "daily" if ctx and ctx.store.get(ctx.id).get("schedule_id") else "manual"
            return backups.create(repo, kind)

    if action == "remove":
        return remove_selected(RemoveRequest(**payload))
    if action == "track_match":
        from .track_routes import apply_preview

        return apply_preview(payload["preview_id"])
    if action in ("analyze", "add", "fix_match"):
        jobs.target("job", payload.get("url") or payload.get("title") or "Playlist")
    if action == "analyze":
        return analyze_playlist(PlaylistAnalyzeRequest(**payload))
    if action == "add":
        return add_playlist(PlaylistAddRequest(**payload))
    if action == "fix_match":
        return save_missing_match(MissingMatchRequest(**payload))
    config = _config(read_only=True, namespaces=[])
    scope = payload.get("scope", "all")
    playlists = config.config.get("playlists", [])
    if scope == "selected":
        playlists = [p for p in playlists if _playlist_key(p) in payload.get("playlist_keys", [])]
    elif scope in ("favorites", "automatic"):
        raise ValueError(
            "Scoped sync tasks have been retired. Use Sync Playlists or select playlists explicitly."
        )
    if action == "sync" and payload.get("playlist_schedule"):
        if (
            config.repository.load("playlist_schedules")
            .get(payload["playlist_schedule"], {})
            .get("mode")
            != "custom"
        ):
            return {"summary": "Custom schedule was disabled before this run started."}
    if (
        action == "sync"
        and jobs.current()
        and jobs.current().store.get(jobs.current().id).get("schedule_id")
    ):
        from .sync_policy import eligible

        playlists = eligible(config.repository, playlists)
    if not playlists:
        return {"summary": "No playlists match this job scope.", "playlists": [], "total": 0}
    ctx = jobs.current()
    if ctx:
        ctx.store.update(ctx.id, result={"playlists": [], "total": len(playlists)})
    if action == "health":
        return _health_batch(playlists, config)
    results = []
    for index, playlist in enumerate(playlists):
        jobs.target(
            _playlist_key(playlist),
            playlist.get("plex_playlist_name", ""),
            index + 1,
            len(playlists),
        )
        jobs.progress(f"{index + 1} of {len(playlists)}: {playlist.get('plex_playlist_name', '')}")
        try:
            result = (
                sync_one(_playlist_key(playlist))
                if action == "sync"
                else playlist_health(_playlist_key(playlist))
            )
            if (
                action == "sync"
                and isinstance(result.get("summary"), dict)
                and result["summary"].get("errors")
            ):
                from .library_cache import invalidate

                invalidate()
                raise ValueError("Playlist sync reported errors; see the sync log")
            results.append(
                {
                    "key": _playlist_key(playlist),
                    "name": playlist.get("plex_playlist_name"),
                    "ok": True,
                    "result": result,
                }
            )
            jobs.activity(mode="completed", stage="Playlist completed")
            jobs.output("✓ Playlist completed: " + playlist.get("plex_playlist_name", ""))
        except Exception as exc:
            message = redact(getattr(exc, "detail", str(exc)), config)
            results.append(
                {
                    "key": _playlist_key(playlist),
                    "name": playlist.get("plex_playlist_name"),
                    "ok": False,
                    "error": message,
                }
            )
            jobs.activity(mode="failed", stage=message)
            jobs.output("✗ " + playlist.get("plex_playlist_name", "") + ": " + message)
        ctx = jobs.current()
        if ctx:
            ctx.store.update(ctx.id, result={"playlists": results, "total": len(playlists)})
    if any(not r["ok"] for r in results):
        raise ValueError(
            f"{sum(not r['ok'] for r in results)} of {len(results)} playlists failed. See job results and logs."
        )
    return {"playlists": results, "total": len(playlists)}


# Compatibility URLs now return 202 + a durable job; poll /api/jobs/{id}.
@app.post("/api/playlists/analyze", status_code=202)
def queue_analysis(request: PlaylistAnalyzeRequest):
    return queue_job(JobRequest(action="analyze", payload=request.model_dump()))


@app.post("/api/playlists", status_code=202)
def queue_add(request: PlaylistAddRequest):
    return queue_job(JobRequest(action="add", payload=request.model_dump()))


@app.post("/api/sync/all", status_code=202)
def queue_all():
    return queue_job(JobRequest(action="sync"))


@app.post("/api/sync/favorites", status_code=202)
def queue_favorites():
    return queue_job(JobRequest(action="sync", payload={"scope": "favorites"}))


@app.post("/api/sync/automatic", status_code=202)
def queue_automatic():
    return queue_job(JobRequest(action="sync", payload={"scope": "automatic"}))


@app.post("/api/sync/{playlist_key:path}", status_code=202)
def queue_one(playlist_key: str):
    return queue_job(
        JobRequest(action="sync", payload={"scope": "selected", "playlist_keys": [playlist_key]})
    )


@app.post("/api/playlists/{playlist_key:path}/health", status_code=202)
def queue_health(playlist_key: str):
    return queue_job(
        JobRequest(action="health", payload={"scope": "selected", "playlist_keys": [playlist_key]})
    )


@app.post("/api/missing/match", status_code=202)
def queue_match(request: MissingMatchRequest):
    return queue_job(JobRequest(action="fix_match", payload=request.model_dump()))


class IgnoreRequest(BaseModel):
    title: str
    artist: str
    album: str = ""
    universal: bool = False
    playlist_keys: List[str] = Field(default_factory=list)


class BulkIgnoreRequest(BaseModel):
    tracks: List[IgnoreRequest] = Field(min_length=1, max_length=500)


@app.post("/api/missing/ignore", status_code=202)
def queue_ignore(request: IgnoreRequest):
    if not request.universal and not request.playlist_keys:
        raise HTTPException(422, "Select playlists or choose universal ignore")
    store = job_store()
    return store.get(store.enqueue("ignore", request.model_dump()))


def ignore_missing(request: IgnoreRequest):
    if not request.universal and not request.playlist_keys:
        raise HTTPException(422, "Select playlists or choose universal ignore")
    with ProcessLock():
        config = _config()
        syncer = Syncer(config)
        if not request.universal:
            existing = {_playlist_key(p) for p in config.config.get("playlists", [])}
            if set(request.playlist_keys) - existing:
                raise HTTPException(
                    409,
                    "One or more selected playlists were removed. Refresh the Missing page before retrying.",
                )
        track = request.model_dump(include={"title", "artist", "album"})
        affected = 0
        if request.universal:
            syncer._ignore_track("__global__", track)
        for playlist in config.config.get("playlists", []):
            key = _playlist_key(playlist)
            if not request.universal and key not in request.playlist_keys:
                continue
            # A sync may have resolved it while the ignore request was waiting.
            if not request.universal:
                syncer._ignore_track(key, track)
            for search_key in list(config.mapping.get(key, {})):
                title, _, artist = search_key.partition("|")
                if syncer._same_missing_identity({"title": title, "artist": artist}, track):
                    config.mapping[key].pop(search_key, None)
                    syncer._remove_match_provenance(key, search_key)
            remaining = []
            for t in config.missing.get(key, []):
                if syncer._same_missing_identity(t, track):
                    if not request.universal:
                        syncer._ignore_track(key, t)
                    else:
                        search_key = f"{t.get('title', '')}|{t.get('artist', '')}"
                        config.mapping.get(key, {}).pop(search_key, None)
                        syncer._remove_match_provenance(key, search_key)
                    affected += 1
                else:
                    remaining.append(t)
            config.missing[key] = remaining
        config.save()
        _record_log(
            "INFO",
            "ignore",
            f"Ignored {request.title} in {affected} unresolved occurrences",
            config,
        )
        return {"affected": affected, "universal": request.universal}


@app.get("/api/ignored")
@saved_page("ignored tracks", PAGE_DATA)
def list_ignored():
    config = _config(read_only=True, namespaces=[])
    return [
        {
            "playlist_key": key,
            "ignore_key": identity,
            **track,
            "playlist_name": next(
                (
                    p.get("plex_playlist_name") or p.get("name") or "Unnamed playlist"
                    for p in config.config.get("playlists", [])
                    if _playlist_key(p) == key
                ),
                "Removed playlist",
            ),
        }
        for key, bucket in config.ignored_tracks.items()
        for identity, track in bucket.items()
    ]


class RestoreIgnoreRequest(BaseModel):
    playlist_key: str
    ignore_key: str


@app.post("/api/ignored/restore")
def restore_ignore(request: RestoreIgnoreRequest):
    with ProcessLock():
        config = _config()
        config.ignored_tracks.get(request.playlist_key, {}).pop(request.ignore_key, None)
        config.save()
    return {"restored": True}


@app.delete("/api/settings/logs")
def clear_logs():
    job_store().repository.clear_logs()
    return {"cleared": True}


@app.get("/api/search")
def search_library(q: str = Query(min_length=1, max_length=200)):
    config = _config(read_only=True, namespaces=[])
    query = q.casefold().strip()
    terms = query.replace(" - ", " ").split()

    def matches(text):
        return all(term in text.casefold() for term in terms)

    playlists = []
    tracks = []
    health = config.repository.load("health")
    for p in config.config.get("playlists", []):
        key = _playlist_key(p)
        name = p.get("plex_playlist_name", key)
        if matches(name):
            playlists.append({"key": key, "name": name})
        snapshot = config.source_snapshots.get(key, [])
        source = health.get(key, {}).get("source_preview") or (
            snapshot if isinstance(snapshot, list) else []
        )
        candidates = [*source, *config.missing.get(key, [])]
        for search_key in config.mapping.get(key, {}):
            title, _, artist = search_key.partition("|")
            candidates.append({"title": title, "artist": artist})
        seen = set()
        for track in candidates:
            identity = (track.get("title", ""), track.get("artist", ""))
            if identity in seen:
                continue
            seen.add(identity)
            if matches(" ".join(str(track.get(k, "")) for k in ("title", "artist", "album"))):
                tracks.append({**track, "playlist_key": key, "playlist_name": name})
    from .inventory import current

    plex, lidarr = current(config.repository, config)

    def contains(t):
        return matches(
            " ".join(str(t.get(k, "")) for k in ("title", "artist", "album", "album_artist"))
        )

    import hashlib

    artists = {}
    for a in lidarr.get("artists", []):
        name = str(a.get("name") or a.get("artistName") or "")
        if name and matches(name):
            artists[name.casefold()] = {"id": str(a.get("id")), "name": name}
    from .lidarr import config as search_lidarr_config
    from .accounts import root_repository

    if not search_lidarr_config(root_repository()).get("enabled"):
        for t in plex.get("rows", []):
            name = t.get("album_artist") or t.get("artist") or ""
            if name and matches(name) and name.casefold() not in artists:
                aid = "plex:" + str(
                    t.get("plex_artist_id")
                    or hashlib.sha256(name.casefold().encode()).hexdigest()[:16]
                )
                artists[name.casefold()] = {"id": aid, "name": name}
    return {
        "artists": list(artists.values())[:100],
        "playlists": playlists[:100],
        "tracks": tracks[:200],
        "track_count": len(tracks),
        "cached": True,
        "plex_tracks": [t for t in plex.get("rows", []) if contains(t)][:100],
        "lidarr_albums": [t for t in lidarr.get("rows", []) if contains(t)][:100],
        "inventory_checked_at": {
            "plex": plex.get("checked_at"),
            "lidarr": lidarr.get("checked_at"),
        },
    }


def remove_selected(request):
    results = []
    with ProcessLock():
        config = _config()
        selected = set(request.playlist_keys)
        targets = [p for p in config.config["playlists"] if _playlist_key(p) in selected]
        if selected != set(_playlist_key(p) for p in targets):
            raise HTTPException(
                409, "One or more playlists are no longer registered. Refresh the list."
            )
        plex = _health_plex(config) if request.delete_plex else None
        for index, playlist in enumerate(targets, 1):
            key = _playlist_key(playlist)
            jobs.target(key, playlist.get("plex_playlist_name", key), index, len(targets))
            jobs.progress(
                "Deleting Plex playlist and registration"
                if plex
                else "Removing registration; leaving Plex untouched"
            )
            try:
                if plex:
                    plex_id = str(playlist.get("plex_playlist_id", ""))
                    if not plex_id:
                        raise ValueError("This registration has no Plex playlist ID")
                    if any(
                        str(p.get("plex_playlist_id", "")) == plex_id
                        and _playlist_key(p) not in selected
                        for p in config.config["playlists"]
                    ):
                        raise ValueError(
                            "Another unselected registration uses this Plex playlist. Remove from Harmonia only or select every registration."
                        )
                    response = network.delete(
                        f"{plex.base_url}/playlists/{plex_id}", headers=plex.headers, timeout=15
                    )
                    if response.status_code not in (200, 204, 404):
                        raise ValueError(
                            f"Plex deletion failed (HTTP {response.status_code}); registration retained."
                        )
                remaining = [p for p in config.config["playlists"] if _playlist_key(p) != key]
                with config.repository.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "UPDATE state SET value=? WHERE namespace='runtime' AND key='playlists'",
                        (json.dumps(remaining),),
                    )
                    if playlist.get("source") == "text":
                        db.execute(
                            "DELETE FROM state WHERE namespace='text_sources' AND key=?",
                            (playlist["source_id"],),
                        )
                    for namespace in (
                        "mapping",
                        "missing",
                        "match_metadata",
                        "source_snapshots",
                        "playlist_overlays",
                        "ignored_tracks",
                        "health",
                        "health_attempts",
                        "playlist_schedules",
                        "pending_playlist_settings",
                    ):
                        db.execute(
                            "DELETE FROM state WHERE namespace=? AND key=?", (namespace, key)
                        )
                config.config["playlists"] = remaining
                results.append(
                    {
                        "key": key,
                        "name": playlist.get("plex_playlist_name"),
                        "ok": True,
                        "removed": True,
                        "plex_deleted": bool(plex),
                    }
                )
                jobs.output(
                    "✓ Removed playlist"
                    + (" from Harmonia and Plex" if plex else " from Harmonia; Plex untouched")
                )
            except Exception as exc:
                results.append(
                    {
                        "key": key,
                        "name": playlist.get("plex_playlist_name"),
                        "ok": False,
                        "error": redact(str(exc), config),
                    }
                )
            jobs.activity(
                mode="completed" if results[-1]["ok"] else "failed",
                stage="Playlist removed" if results[-1]["ok"] else results[-1]["error"],
            )
            if jobs.current():
                jobs.current().store.update(
                    jobs.current().id, result={"playlists": results, "total": len(targets)}
                )
        if any(not row["ok"] for row in results):
            raise ValueError(
                "Some removals failed. Review the per-playlist results; failed registrations were retained where possible."
            )
    return {"playlists": results, "total": len(targets)}


@app.delete("/api/playlists/{playlist_key:path}")
def remove_playlist(playlist_key: str):
    with ProcessLock():
        config = _config()
        playlists = config.config.get("playlists", [])
        if not any(_playlist_key(p) == playlist_key for p in playlists):
            raise HTTPException(404, "Playlist not found")
        # One transaction removes only this registration and its scoped state.
        with config.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE state SET value=? WHERE namespace='runtime' AND key='playlists'",
                (json.dumps([p for p in playlists if _playlist_key(p) != playlist_key]),),
            )
            for namespace in (
                "mapping",
                "missing",
                "match_metadata",
                "source_snapshots",
                "playlist_overlays",
                "ignored_tracks",
                "health",
                "health_attempts",
                "playlist_schedules",
                "pending_playlist_settings",
            ):
                db.execute(
                    "DELETE FROM state WHERE namespace=? AND key=?", (namespace, playlist_key)
                )
        return {"deleted": True, "plex_playlist_untouched": True}


@app.post("/api/missing/automatic")
def retry_automatic(request: MissingCandidateRequest):
    started, cpu_started = time.monotonic(), time.thread_time()
    config = _config(read_only=True, namespaces=["artist_aliases"])
    from .library import playable_tracks

    library = playable_tracks()
    inventory_elapsed = time.monotonic() - started
    source = request.model_dump(include={"title", "artist", "album", "duration", "artists"})
    # Deliberately omit saved mappings so the unchanged matcher gets a fresh try.
    candidate_id = Matcher.match_track(source, library, {})
    candidate = (
        next((t for t in library if str(t.get("plex_id")) == str(candidate_id)), None)
        if candidate_id is not None
        else None
    )
    if candidate:
        score = Matcher.score_candidate(source, candidate)
        candidate = {
            **candidate,
            "plex_id": str(candidate_id),
            "score": round(score["adjusted_score"], 1),
        }
    outcome = (
        f"{candidate['title']} — {candidate['artist']} ({candidate.get('album', '')}), score {candidate['score']}"
        if candidate
        else "no acceptable automatic match"
    )
    _record_log(
        "INFO",
        "Automatic matching",
        f"{request.title} — {request.artist}: {outcome}; inventory={inventory_elapsed:.2f}s, matching={time.monotonic() - started - inventory_elapsed:.2f}s, CPU={time.thread_time() - cpu_started:.2f}s",
        config,
    )
    return {"candidate": candidate}


def _register_routes():
    from .track_routes import register as register_tracks

    register_tracks(app)
    from .settings_routes import register as register_settings

    register_settings(app)
    from .updates import register as register_updates

    register_updates(app)
    from .lidarr import register as register_lidarr

    register_lidarr(app)
    from .match_queue import register as register_match_queue

    register_match_queue(app)
    from .console_logging import register as register_console

    register_console(app)
    from .previews import register as register_previews

    register_previews(app)

    from .musicbrainz_settings import register as register_musicbrainz

    register_musicbrainz(app)
    from .metadata_sources import register as register_metadata_sources

    register_metadata_sources(app)
    from .fanart import register as register_fanart

    register_fanart(app)

    from .lidarr_requests import register as register_lidarr_requests

    register_lidarr_requests(app)

    from .availability import register as register_availability

    register_availability(app)

    from .inventory import register as register_inventory

    register_inventory(app)
    from .playlist_schedules import register as register_playlist_schedules

    register_playlist_schedules(app)

    from .library import register as register_library

    register_library(app)

    from .shared_playlists import register as register_shared

    register_shared(app)

    from .recreate import register as register_recreate

    register_recreate(app)

    from .discover import register as register_discover

    register_discover(app)

    from .accounts import install as install_accounts

    install_accounts(app)

    from .user_preferences import register as register_user_preferences

    register_user_preferences(app)

    from .source_history import register as register_source_history

    register_source_history(app)

    from .import_users import register as register_import_users

    register_import_users(app)

    from .api_v1 import register as register_api_v1

    register_api_v1(app)

    from .text_playlists import register as register_text_playlists

    register_text_playlists(app)

    from .playlist_overlays import register as register_playlist_overlays

    register_playlist_overlays(app)


_register_routes()


@app.get("/api/admin/missing")
@saved_page("administrator missing tracks", PAGE_DATA | {"all_account_missing"})
def administrator_missing_tracks():
    from .accounts import users, as_user

    rows = []
    for user_id, user in users().items():
        if user.get("disabled") or user.get("pending_login"):
            continue
        with as_user({k: v for k, v in user.items() if k != "server_context"}):
            for row in missing_tracks.__wrapped__():
                rows.append(
                    {**row, "user_id": str(user_id), "user_name": user.get("name") or str(user_id)}
                )
    return rows


class AdministratorMatchRequest(MissingMatchRequest):
    target_user_id: str


@app.post("/api/admin/missing/match", status_code=202)
def administrator_match(request: AdministratorMatchRequest):
    from .accounts import users, as_user, actor
    from .match_queue import stage_match, available

    administrator = actor()
    user = users().get(request.target_user_id)
    if not user or user.get("disabled") or user.get("pending_login"):
        raise HTTPException(404, "Active user not found.")
    with as_user({k: v for k, v in user.items() if k != "server_context"}):
        # An administrator explicitly confirms this choice for the target user.
        staged = stage_match(request.model_copy(update={"provenance": "manual"}))
        store = job_store()
        with store.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [r for r in available(store.repository, db) if r["id"] in staged["ids"]]
            if not rows:
                raise HTTPException(409, "No missing occurrences remain for this user.")
            job_id = store.enqueue("match_batch", {"changes": rows}, db=db)
            for row in rows:
                db.execute(
                    "UPDATE state SET value=? WHERE namespace='match_drafts' AND key=?",
                    (json.dumps({**row, "job_id": job_id}), row["id"]),
                )
        from .accounts import root_repository

        root_repository().add_log(
            "INFO",
            "Administrator matching",
            f"Administrator {administrator['id']} queued a manual match for user {request.target_user_id}: {request.title}",
        )
        return {
            **store.get(job_id),
            "message": f"Manual match and sync queued for {user.get('name') or request.target_user_id}.",
        }


WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def mount_frontend(application, directory):
    if directory.exists():
        application.mount("/", StaticFiles(directory=directory, html=True), name="web")


mount_frontend(app, WEB_DIST)


def run():
    import uvicorn
    from .console_logging import uvicorn_log_config
    from .browser_security import trusted_proxies, https_only

    https_only()  # Validate configuration before accepting traffic.

    port = int(os.environ.get("HARMONIA_PORT") or "8173")
    if not 1 <= port <= 65535:
        raise ValueError("HARMONIA_PORT must be between 1 and 65535")

    uvicorn.run(
        "harmonia.api:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxies(),
        log_config=uvicorn_log_config(),
    )


@app.middleware("http")
async def reset_request_guard(request, call_next):
    from fastapi.responses import JSONResponse
    from . import reset_guard

    if not request.url.path.startswith("/api/") or request.url.path == "/api/health":
        return await call_next(request)
    if not reset_guard.enter_request():
        return JSONResponse(
            {"detail": "Application reset is in progress or requires a restart."}, status_code=503
        )
    try:
        return await call_next(request)
    finally:
        reset_guard.leave_request()


# Outermost application middleware covers auth/reset rejection responses too.
app.add_middleware(BrowserSecurity)

if __name__ == "__main__":
    run()
