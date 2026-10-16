"""Account-owned Discover preferences and artist exclusions."""

import hashlib
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .accounts import personal_repository
from .lidarr import state_put


def normalize(value):
    from .matching import Matcher

    return Matcher._normalize_match_text(value)


def preferences():
    return personal_repository().load("preferences").get("discover", {})


def blocked(row, *, artist_rules=None, album_rules=None):
    raw_credits = row.get("artists") or [
        {"name": row.get("artist", ""), "mbid": row.get("artist_mbid", "")}
    ]
    if not isinstance(raw_credits, list):
        raw_credits = [raw_credits]
    credits = [c if isinstance(c, dict) else {"name": str(c), "mbid": ""} for c in raw_credits if c]
    if artist_rules is None:
        artist_rules = personal_repository().load("blocked_artists").values()
    rules = [r for r in artist_rules if isinstance(r, dict)]
    artist_blocked = any(
        (r.get("mbid") and r["mbid"] == c.get("mbid"))
        or normalize(r.get("name", "")) == normalize(c.get("name", ""))
        for r in rules
        for c in credits
    )
    if album_rules is None:
        album_rules = personal_repository().load("blocked_albums").values()
    album_rules = [r for r in album_rules if isinstance(r, dict)]
    # Discover and Library use different field names for the same identity.
    row_mbid = row.get("mbid") or row.get("musicbrainz_id") or row.get("album_id")
    row_title = row.get("album") or row.get("title")
    album_blocked = any(
        (r.get("mbid") and r["mbid"] == row_mbid)
        or (
            normalize(r.get("artist")) == normalize(row.get("artist"))
            and normalize(r.get("album")) == normalize(row_title)
        )
        for r in album_rules
    )
    return artist_blocked or album_blocked


def history_ignored(row, rules):
    """History exclusions survive artwork changes and compilation credit shifts."""
    title = normalize(row.get("album", ""))
    credits = {
        normalize(name) for name in [row.get("artist", ""), *(row.get("artists") or [])] if name
    }
    for rule in rules:
        if row.get("mbid") and rule.get("mbid"):
            if row["mbid"] == rule["mbid"]:
                return True
            continue
        if title != normalize(rule.get("album", "")):
            continue
        previous = {
            normalize(name)
            for name in [rule.get("artist", ""), *(rule.get("artists") or [])]
            if name
        }
        if credits & previous:
            return True
    return False


def register(app):
    class Preferences(BaseModel):
        lastfm_username: str = Field(default="", max_length=100)
        hide_available: bool = False

    @app.get("/api/discover/preferences")
    def read():
        return Preferences(**preferences()).model_dump()

    @app.put("/api/discover/preferences")
    def save(body: Preferences):
        state_put(personal_repository(), "preferences", "discover", body.model_dump())
        return body.model_dump()

    class Artist(BaseModel):
        name: str = Field(min_length=1, max_length=250)
        mbid: str = Field(default="", max_length=36)

    @app.get("/api/discover/blocked-artists")
    def listing():
        return [{"id": k, **v} for k, v in personal_repository().load("blocked_artists").items()]

    @app.post("/api/discover/blocked-artists")
    def block(body: Artist):
        if not body.name.strip():
            raise HTTPException(422, "Enter an artist name.")
        key = hashlib.sha256(normalize(body.name).encode()).hexdigest()
        state_put(personal_repository(), "blocked_artists", key, body.model_dump())
        return {"id": key}

    @app.delete("/api/discover/blocked-artists/{key}")
    def unblock(key: str):
        with personal_repository().connect() as db:
            db.execute("DELETE FROM state WHERE namespace='blocked_artists' AND key=?", (key,))
        return {"removed": True}

    class Album(BaseModel):
        album: str = Field(min_length=1, max_length=500)
        artist: str = Field(min_length=1, max_length=250)
        mbid: str = Field(default="", max_length=36)

    @app.get("/api/discover/blocked-albums")
    def blocked_albums():
        return [
            {"id": key, **value}
            for key, value in personal_repository().load("blocked_albums").items()
        ]

    @app.post("/api/discover/blocked-albums")
    def block_album(body: Album):
        key = hashlib.sha256(
            (normalize(body.artist) + "|" + normalize(body.album)).encode()
        ).hexdigest()
        state_put(personal_repository(), "blocked_albums", key, body.model_dump())
        return {"id": key}

    @app.delete("/api/discover/blocked-albums/{key}")
    def unblock_album(key: str):
        with personal_repository().connect() as db:
            db.execute("DELETE FROM state WHERE namespace='blocked_albums' AND key=?", (key,))
        return {"removed": True}

    class IgnoredHistoryAlbum(Album):
        artists: list[str] = Field(default_factory=list, max_length=50)
        history_key: str = Field(default="", max_length=64)

    @app.get("/api/discover/history-ignored")
    def history_exclusions():
        return [
            {"id": key, **value}
            for key, value in personal_repository().load("lastfm_history_ignored").items()
        ]

    @app.post("/api/discover/history-ignored")
    def ignore_history_album(body: IgnoredHistoryAlbum):
        if not body.album.strip() or not body.artist.strip():
            raise HTTPException(422, "Enter an album and artist.")
        value = body.model_dump()
        key = hashlib.sha256(
            (body.mbid or (normalize(body.artist) + "|" + normalize(body.album))).encode()
        ).hexdigest()
        state_put(personal_repository(), "lastfm_history_ignored", key, value)
        return {"id": key, **value}

    @app.delete("/api/discover/history-ignored/{key}")
    def restore_history_album(key: str):
        with personal_repository().connect() as db:
            db.execute(
                "DELETE FROM state WHERE namespace='lastfm_history_ignored' AND key=?", (key,)
            )
        return {"restored": True}
