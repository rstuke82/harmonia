"""Admin-curated sources that users can opt into as their own Plex copies."""

import hashlib
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .accounts import actor, root_repository
from .lidarr import state_put


def register(app):
    class Source(BaseModel):
        url: str = Field(max_length=1000)
        name: str = Field(min_length=1, max_length=200)
        custom_name: bool = True

    class Lookup(BaseModel):
        url: str = Field(max_length=1000)

    @app.post("/api/shared-playlists/metadata")
    def metadata(body: Lookup):
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        from . import network
        from urllib.parse import urlsplit
        from bs4 import BeautifulSoup
        from .config import Config

        url = Config._normalize_url_input(body.url)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ("music.apple.com", "open.spotify.com")
            or parsed.username
            or parsed.password
        ):
            raise HTTPException(422, "Enter a public Spotify or Apple Music HTTPS playlist URL.")
        try:
            response = network.get(url, timeout=(5, 15), allow_redirects=False)
            response.raise_for_status()
            page = BeautifulSoup(response.content, "html.parser")
            tag = page.find("meta", property="og:title")
            name = tag.get("content", "").strip() if tag else ""
            if not name:
                raise ValueError("No playlist name found")
            from .source_names import clean

            name = clean(name)
            return {"name": name}
        except (network.RequestException, ValueError):
            raise HTTPException(502, "Could not fetch the source name. Enter a name manually.")

    @app.get("/api/shared-playlists")
    def listing():
        from .api import _config, _source_for_url
        from .config import Config
        from .source_names import clean

        config = _config(read_only=True, namespaces=[])
        registered = {(p["source"], p["source_id"]) for p in config.config.get("playlists", [])}
        from .api import job_store

        pending = {
            j.get("payload", {}).get("url")
            for j in job_store().list()
            if j["action"] == "add" and j["status"] in ("queued", "running", "cancelling")
        }
        rows = []
        for key, value in root_repository().load("shared_playlists").items():
            value = dict(value)
            # Older entries lack provenance: clean display only; leave stored custom text intact.
            if value.get("custom_name") is not True:
                value["name"] = clean(value.get("name"))
            try:
                source, url, _ = _source_for_url(value["url"])
                subscribed = (source, Config._extract_id(url, source)) in registered or value[
                    "url"
                ] in pending
            except Exception:
                subscribed = False
            rows.append({"id": key, **value, "subscribed": subscribed})
        return rows

    @app.get("/api/shared-playlists/{key}/preview")
    def preview(key: str):
        source = root_repository().load("shared_playlists").get(key)
        if not source:
            raise HTTPException(404, "Shared source not found")
        from .api import _source_for_url
        from .source_names import clean
        from .discover import availability
        import time

        cached = root_repository().load("shared_preview").get(key, {})
        if cached.get("expires", 0) < time.time():
            _, url, service = _source_for_url(source["url"])
            try:
                tracks, metadata = service.get_playlist_tracks(url, fetch_artwork=False)
            except Exception as exc:
                raise HTTPException(
                    502, "Could not load this source playlist. Try again later."
                ) from exc
            cached = {
                "tracks": tracks,
                "name": clean(metadata.get("name") or source["name"]),
                "expires": time.time() + 300,
            }
            state_put(root_repository(), "shared_preview", key, cached)
        tracks = cached["tracks"]
        albums = availability(
            [{"artist": t.get("artist", ""), "album": t.get("album", "")} for t in tracks],
            apply_preferences=False,
        )
        index = {(a["artist"], a["album"]): a["availability"] for a in albums}
        return {
            "name": cached["name"],
            "tracks": [
                {
                    **t,
                    "availability": index.get((t.get("artist", ""), t.get("album", "")), "Unknown"),
                }
                for t in tracks
            ],
        }

    @app.post("/api/shared-playlists")
    def publish(body: Source):
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Only an administrator can publish shared playlist sources.")
        from .api import _source_for_url

        _source_for_url(body.url)
        key = hashlib.sha256(body.url.encode()).hexdigest()
        state_put(root_repository(), "shared_playlists", key, body.model_dump())
        return {"id": key}

    @app.delete("/api/shared-playlists/{key}")
    def remove(key: str):
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        with root_repository().connect() as db:
            db.execute("DELETE FROM state WHERE namespace='shared_playlists' AND key=?", (key,))
        return {"removed": True}

    @app.post("/api/shared-playlists/{key}/subscribe", status_code=202)
    def subscribe(key: str):
        source = root_repository().load("shared_playlists").get(key)
        if not source:
            raise HTTPException(404, "Shared source not found")
        from .api import queue_job, JobRequest

        return queue_job(
            JobRequest(
                action="add",
                payload={
                    "url": source["url"],
                    "name": source["name"] if source.get("custom_name") else "",
                    "sync_mode": "inherit",
                },
            )
        )
