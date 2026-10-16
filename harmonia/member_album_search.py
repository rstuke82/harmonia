"""Permission-scoped member album lookups in the shared bounded lookup lane."""

from fastapi import HTTPException
from pydantic import BaseModel, Field
from .accounts import actor, as_user, personal_repository
from . import accounts
from . import jobs


class Search(BaseModel):
    artist: str = Field(default="", max_length=200)
    album: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    recording_offset: int = Field(default=0, ge=0, le=10000)
    force_refresh: bool = False
    lookup_session: str = Field(default="", max_length=64)


def permitted():
    user = actor()
    if not user or user.get("disabled") or not (user.get("can_request") or user.get("admin")):
        raise HTTPException(403, "Album requests are disabled for this account.")
    from .lidarr import enabled

    enabled(accounts.root_repository())
    return user


def project(result, user):
    from .user_preferences import blocked

    fields = (
        "album_id",
        "title",
        "artist",
        "year",
        "artwork",
        "exists",
        "type",
        "secondary_types",
        "query_score",
        "query_exact",
        "other_release",
    )
    with as_user(user):
        rows = [
            {k: row.get(k) for k in fields}
            | {
                "type": row.get("type") or "Other",
                "secondary_types": row.get("secondary_types") or [],
            }
            for row in result.get("rows", [])
            if not blocked(row)
        ]
    return {
        "rows": rows,
        "cached": bool(result.get("cached")),
        "provider": "MusicBrainz",
        "next_recording_offset": result.get("next_recording_offset"),
    }


def execute(payload):
    from .accounts import users

    context = jobs.current()
    user = users().get(str(payload.get("request_user_id", "")))
    with as_user(user):
        user = permitted()
        if context and personal_repository().directory != context.store.repository.directory:
            raise HTTPException(409, "Account permissions changed. Start a new search.")
    from .lidarr import Lookup, enabled, musicbrainz_search

    def publish(result):
        context.checkpoint()
        context.store.update(context.id, result=project(result, user))

    # Shared server preferences/cache contain public metadata only. Job ownership,
    # cancellation and exposed result projections remain in the member's store.
    with as_user(None):
        root = accounts.root_repository()
        result = musicbrainz_search(
            root, enabled(root), Lookup(**payload), publish=publish if context else None
        )
    if context:
        context.checkpoint()
    return project(result, user)


def install(app):
    @app.get("/api/requests-user/search-settings")
    def preferences():
        permitted()
        from .musicbrainz_settings import settings

        prefs = settings(accounts.root_repository())
        return {k: prefs[k] for k in ("release_priority", "prefer_studio")} | {
            "retries": 0 if prefs["server_url"] else prefs["retries"]
        }

    @app.post("/api/requests-user/search", status_code=202)
    @app.post("/api/requests-user/search-jobs", status_code=202)
    def start(body: Search):
        permitted()
        from .musicbrainz_settings import settings

        if not settings(accounts.root_repository())["enabled"]:
            raise HTTPException(409, "MusicBrainz lookup is disabled.")
        payload = body.model_dump() | {
            "provider": "musicbrainz",
            "fields_search": True,
            "member_request": True,
            "request_user_id": str(actor()["id"]),
        }
        store = jobs.Store(personal_repository())
        return {"id": store.enqueue("musicbrainz_lookup", payload)}

    def found(job_id):
        permitted()
        store = jobs.Store(personal_repository())
        job = store.get(job_id)
        if (
            not job
            or job["action"] != "musicbrainz_lookup"
            or not job["payload"].get("member_request")
        ):
            raise HTTPException(404, "Search not found.")
        return store, job

    @app.get("/api/requests-user/search-jobs/{job_id}")
    def status(job_id: str):
        _, job = found(job_id)
        result = {k: job.get(k) for k in ("id", "status", "progress", "error")}
        result["result"] = project(job["result"], actor()) if job.get("result") else None
        return result

    @app.delete("/api/requests-user/search-jobs/{job_id}")
    def cancel(job_id: str):
        store, job = found(job_id)
        store.cancel(job["id"])
        return {"cancelled": True}
