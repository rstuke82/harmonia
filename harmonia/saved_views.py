"""Durable page projections. HTTP reads never perform preparation or service I/O."""

import contextvars
import functools
import hashlib
import inspect
import json
import threading
import time
from fastapi.responses import JSONResponse
from .accounts import actor, personal_repository, root_repository, users, as_user

_providers = {}
_readers = {}
_lock = threading.RLock()
_last_tick = 0
_last_download = 0
_warming = contextvars.ContextVar("warming_saved_pages", default=False)
COMMON = {
    "playlist_sync_state",
    "health",
    "health_attempts",
    "runtime",
    "mapping",
    "missing",
    "match_metadata",
    "source_snapshots",
    "ignored_tracks",
    "artist_aliases",
    "inventory",
    "track_links",
    "plex_album_links",
    "blocked_albums",
    "blocked_artists",
    "user_preferences",
    "preferences",
    "match_queue",
    "health_history",
    "playlist_overlays",
    "accounts",
}
PLAYLIST_LIST = {
    "playlist_sync_state",
    "runtime",
    "mapping",
    "missing",
    "match_metadata",
    "ignored_tracks",
    "health",
    "health_attempts",
    "health_history",
    "pending_playlist_settings",
    "match_drafts",
    "playlist_schedules",
    "accounts",
}
LIBRARY = {
    "library_views_epoch",
    "library_album_apple_artwork",
    "library_album_service_links",
    "musicbrainz_settings",
    "runtime",
    "accounts",
    "inventory",
    "track_links",
    "plex_album_links",
    "blocked_albums",
    "blocked_artists",
    "local_artist_enrichment",
    "library_artist_catalog",
    "metadata_sources",
    "artwork_overrides",
    "fanart_cache",
    "file_audit",
    "file_issue_reviews",
    "library_artist_metadata",
    "library_lastfm_artist_metadata",
    "library_artist_links",
    "file_tools",
    "fanart",
    "lidarr",
}
DISCOVER = {
    "runtime",
    "accounts",
    "inventory",
    "track_links",
    "plex_album_links",
    "blocked_albums",
    "blocked_artists",
    "user_preferences",
    "preferences",
    "artist_aliases",
    "lidarr",
    "file_tools",
} | {
    "lastfm",
    "lastfm_cache",
    "lastfm_recommendations",
    "lastfm_history",
    "lastfm_history_manifest",
    "lastfm_history_manifests",
    "lastfm_history_pages",
    "lastfm_history_sync",
    "lastfm_feed_refresh",
    "lastfm_history_links",
    "lastfm_history_ignored",
    "lastfm_album_links",
    "album_links",
    "artwork_overrides",
}
DISCOVER |= LIBRARY
REQUESTS = DISCOVER | {"lidarr_requests", "user_requests", "request_jobs", "pending_requests"}


def permissions(user):
    values = {
        k: (user or {}).get(k)
        for k in (
            "admin",
            "owner",
            "can_request",
            "can_view_all_requests",
            "can_downloads",
            "can_manage_downloads",
            "can_playlists",
            "can_library_issues",
            "disabled",
        )
    }
    values["projection_scope"] = "server" if not user or user.get("server_context") else "account"
    return json.dumps(values, sort_keys=True)


def revision(repo, deps):
    result = ["library-artwork-fallback-v1"] if "artwork_overrides" in deps else []
    sources = [repo, root_repository()]
    if "all_account_missing" in deps:
        from .storage import get_repository

        sources.extend(
            get_repository(root_repository().directory / "users" / str(int(uid)))
            for uid, u in users().items()
            if not u.get("disabled")
            and not u.get("pending_login")
            and not u.get("owner", u.get("admin", False))
        )
    for source in dict.fromkeys(sources):
        with source.connect() as db:
            rows = db.execute(
                "SELECT namespace,generation FROM state_revisions WHERE namespace IN ("
                + ",".join("?" for _ in deps)
                + ") ORDER BY namespace",
                tuple(deps),
            ).fetchall()
        from .metadata_refresh import stable_generations

        rows = stable_generations(source, rows)
        result.append((str(source.directory), rows))
    return hashlib.sha256(json.dumps(result).encode()).hexdigest()


def enqueue(repo, row, stamp, force=False, priority=1):
    """One job per projection; queued work absorbs revisions and foreground demand."""
    from .jobs import Store

    with _lock, repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT attempt_revision,job_id FROM prepared_views WHERE key=?", (row["key"],)
        ).fetchone()
        job = (
            db.execute("SELECT status,payload FROM jobs WHERE id=?", (current[1],)).fetchone()
            if current
            else None
        )
        if job and job[0] in ("queued", "running", "cancelling"):
            if job[0] == "queued":
                payload = json.loads(job[1])
                payload.update(
                    revision=stamp, view_priority=min(priority, payload.get("view_priority", 2))
                )
                db.execute(
                    "UPDATE jobs SET payload=? WHERE id=? AND status='queued'",
                    (json.dumps(payload, sort_keys=True), current[1]),
                )
                db.execute(
                    "UPDATE prepared_views SET attempt_revision=? WHERE key=?", (stamp, row["key"])
                )
            return current[1]
        if not force and current and current[0] == stamp and not (job and job[0] == "interrupted"):
            return current[1]
        job_id = Store(repo).enqueue(
            "prepare_views",
            {
                "key": row["key"],
                "revision": stamp,
                "view_priority": priority,
                "trigger_label": "startup page warming" if priority == 2 else "page requested",
            },
            db=db,
        )
        db.execute(
            "UPDATE prepared_views SET attempt_revision=?,job_id=?,error=NULL WHERE key=?",
            (stamp, job_id, row["key"]),
        )
        return job_id


def retire_legacy_queue(repo):
    """Drain pre-fix automatic backlog without disturbing running or demanded work."""
    from .jobs import now

    retired = []
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for ident, encoded in db.execute(
            "SELECT id,payload FROM jobs WHERE action='prepare_views' AND status='queued'"
        ).fetchall():
            payload = json.loads(encoded)
            if "view_priority" in payload:
                continue
            message = "Obsolete background page preparation stopped; pages refresh when opened."
            db.execute(
                "UPDATE jobs SET status='cancelled',cancel_requested=1,finished_at=?,progress=? WHERE id=? AND status='queued'",
                (now(), message, ident),
            )
            db.execute(
                "UPDATE prepared_views SET attempt_revision=NULL,job_id=NULL WHERE key=? AND job_id=?",
                (payload.get("key"), ident),
            )
            db.execute(
                "INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)",
                (ident, now(), "lifecycle", message),
            )
            retired.append(ident)
    return retired


def load_row(repo, key):
    with repo.connect() as db:
        db.row_factory = __import__("sqlite3").Row
        row = db.execute("SELECT * FROM prepared_views WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def saved(name, deps=COMMON, enabled=None):
    deps = set(deps) | {"library_views_epoch"}

    def decorate(fn):
        signature = inspect.signature(fn, eval_str=True)
        _providers[name] = (fn, deps)

        @functools.wraps(fn)
        def read(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
            if enabled and not enabled(params):
                return fn(*args, **kwargs)
            force = bool(params.pop("refresh", False))
            params.pop("progress_id", None)
            user = actor()
            repo = personal_repository()
            permission = permissions(user)
            # Refresh request projections after the status/artwork/requester schema change.
            schema = (
                "requests-v2"
                if name in ("requests", "personal requests", "personal downloads")
                else name
            )
            identity = json.dumps(
                [schema, params, str((user or {}).get("id", "")), permission], sort_keys=True
            )
            key = hashlib.sha256(identity.encode()).hexdigest()
            started = time.monotonic()
            row = load_row(repo, key)
            if not row:
                with repo.connect() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO prepared_views(key,name,params,user_id,permissions) VALUES(?,?,?,?,?)",
                        (
                            key,
                            name,
                            json.dumps(params),
                            str((user or {}).get("id", "")),
                            permission,
                        ),
                    )
                row = load_row(repo, key)
            stamp = revision(repo, deps)
            stale = row["revision"] != stamp
            priority = 2 if _warming.get() else 0 if row["data"] is None or force else 1
            job_id = (
                enqueue(repo, row, stamp, force, priority=priority)
                if stale or force
                else row["job_id"]
            )
            headers = {
                "X-Saved-View": key,
                "X-Saved-View-Updated": str(row["updated_at"] or 0),
                "X-Saved-View-Stale": str(bool(stale or force)).lower(),
                "Server-Timing": f"saved-db;dur={(time.monotonic() - started) * 1000:.1f}",
            }
            if job_id and (stale or force):
                headers["X-Saved-View-Job"] = str(job_id)
            if row["error"] and row["attempt_revision"] == stamp and not force:
                headers["X-Saved-View-Error"] = "true"
            if row["data"] is None:
                if row["error"] and row["attempt_revision"] == stamp and not force:
                    return JSONResponse(
                        {
                            "detail": "Could not prepare this saved view. Refresh to retry; see Activity for details."
                        },
                        status_code=503,
                        headers=headers,
                    )
                headers["X-Saved-View-Pending"] = "true"
                return JSONResponse(
                    {"preparing": True, "job_id": job_id}, status_code=202, headers=headers
                )
            return JSONResponse(json.loads(row["data"]), headers=headers)

        _readers[name] = read
        read.__signature__ = signature
        return read

    return decorate


def execute(payload):
    from .jobs import current, progress

    repo = current().store.repository
    row = load_row(repo, payload["key"])
    if not row or row["name"] not in _providers:
        return {"summary": "Saved view no longer required."}
    user = users().get(row["user_id"]) if row["user_id"] else None
    scope = json.loads(row["permissions"]).get("projection_scope")
    if scope not in ("server", "account"):
        return {"summary": "Legacy saved view scope discarded; reopen the page."}
    if scope == "server" and row["user_id"]:
        if not user or not user.get("admin"):
            return {"summary": "Administrator permission changed; server view discarded."}
        user = {**user, "server_context": True}
    if row["user_id"] and (
        not user or user.get("disabled") or permissions(user) != row["permissions"]
    ):
        return {"summary": "Account permissions changed; saved view discarded."}
    fn, deps = _providers[row["name"]]
    with as_user(user):
        if str(personal_repository().directory) != str(repo.directory):
            return {"summary": "Saved view repository scope changed; view discarded."}
        stamp = revision(repo, deps)
        started = time.monotonic()
        import re

        label = re.sub(r" v[0-9_]+$", "", row["name"])
        progress("Preparing " + label + "…")
        try:
            data = fn(**json.loads(row["params"]))
            encoded = json.dumps(data, ensure_ascii=False)
            with repo.connect() as db:
                db.execute(
                    "UPDATE prepared_views SET data=?,revision=?,updated_at=?,error=NULL WHERE key=? AND job_id=?",
                    (encoded, stamp, time.time(), row["key"], current().id),
                )
            elapsed = time.monotonic() - started
            repo.add_log(
                "INFO",
                "Saved views",
                f"Prepared {row['name']} in {elapsed:.2f}s; {len(encoded)} bytes",
            )
            return {"summary": f"Saved {row['name']} ready in {elapsed:.2f}s"}
        except Exception as exc:
            with repo.connect() as db:
                db.execute(
                    "UPDATE prepared_views SET error=? WHERE key=? AND job_id=?",
                    (type(exc).__name__, row["key"], current().id),
                )
            raise


def tick(store):
    global _last_tick, _last_download
    now = time.monotonic()
    if now - _last_tick < 5:
        return
    _last_tick = now
    from .accounts import member_stores

    # Reads and explicit startup warming own preparation demand. Do not walk
    # all historical album/filter pages or continuously rebuild inactive views.
    for _, candidate in [(None, store), *member_stores()]:
        retire_legacy_queue(candidate.repository)
    if now - _last_download >= 30:
        _last_download = now
        from .lidarr_downloads import queue_plex_rechecks

        queue_plex_rechecks(store)
        from .lidarr import config

        if config(store.repository).get("enabled") and config(store.repository).get("url"):
            with store.repository.connect() as db:
                exists = db.execute(
                    "SELECT 1 FROM state WHERE namespace='lidarr_requests' LIMIT 1"
                ).fetchone()
                busy = db.execute(
                    "SELECT 1 FROM jobs WHERE action='request_status' AND status IN ('queued','running','cancelling') LIMIT 1"
                ).fetchone()
            if exists and not busy:
                store.enqueue("request_status", {})


def startup_pages(repo, mode="outdated"):
    """Warm only common landing pages, never the whole album/artist catalog."""
    user = actor() or {}
    names = [
        "Plex recently added v20261008_10",
        "library directory v17",
        "playlists",
        "discovery v18",
        "listening history v20261011_77",
    ]
    names.append("requests" if user.get("admin") or not user else "personal requests")
    if user.get("admin") or user.get("can_library_issues", False):
        names.append("library issues v20261008_14")
    for name in names:
        reader = _readers.get(name)
        if not reader:
            continue
        params = {}
        for key, param in inspect.signature(reader).parameters.items():
            value = param.default
            if value is inspect.Parameter.empty:
                break
            params[key] = getattr(value, "default", value)
        else:
            if name == "library directory v17":
                params["view"] = "directory"
            identity = json.dumps(
                [
                    name,
                    {k: v for k, v in params.items() if k not in ("refresh", "progress_id")},
                    str(user.get("id", "")),
                    permissions(actor()),
                ],
                sort_keys=True,
            )
            row = load_row(repo, hashlib.sha256(identity.encode()).hexdigest())
            if mode == "always" and "refresh" in params:
                params["refresh"] = True
            # Readers return immediately from SQLite and deduplicate preparation.
            if mode == "always" and row:
                enqueue(repo, row, revision(repo, _providers[name][1]), force=True, priority=2)
            else:
                token = _warming.set(True)
                try:
                    reader(**params)
                finally:
                    _warming.reset(token)


def publish(repo, name, params, data):
    """A completed operation owns its projection; stale workers cannot replace it."""
    if name not in _providers:
        return
    user = actor()
    permission = permissions(user)
    identity = json.dumps(
        [name, params, str((user or {}).get("id", "")), permission], sort_keys=True
    )
    key = hashlib.sha256(identity.encode()).hexdigest()
    stamp = revision(repo, _providers[name][1])
    encoded = json.dumps(data, ensure_ascii=False)
    with _lock, repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT job_id FROM prepared_views WHERE key=?", (key,)).fetchone()
        if old and old[0]:
            from .jobs import now

            db.execute(
                "UPDATE jobs SET status='cancelled',cancel_requested=1,finished_at=?,progress='Page updated directly by sync' WHERE id=? AND action='prepare_views' AND status='queued'",
                (now(), old[0]),
            )
        db.execute(
            "INSERT INTO prepared_views(key,name,params,user_id,permissions,data,revision,attempt_revision,updated_at,job_id,error) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL) ON CONFLICT(key) DO UPDATE SET data=excluded.data,revision=excluded.revision,attempt_revision=excluded.attempt_revision,updated_at=excluded.updated_at,job_id=NULL,error=NULL",
            (
                key,
                name,
                json.dumps(params),
                str((user or {}).get("id", "")),
                permission,
                encoded,
                stamp,
                stamp,
                time.time(),
            ),
        )
