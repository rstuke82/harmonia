"""Explicit recovery of a destination confirmed missing from the user's Plex."""

from fastapi import HTTPException
from pydantic import BaseModel
from . import jobs


def execute(payload):
    from .api import _config, _playlist_key, _health_plex, sync_one
    from .process_lock import ProcessLock
    from . import network

    with ProcessLock():
        config = _config()
        playlist = next(
            (p for p in config.config["playlists"] if _playlist_key(p) == payload["playlist_key"]),
            None,
        )
        if not playlist:
            raise ValueError("Playlist registration no longer exists.")
        plex = _health_plex(config)
        # Validate library access separately: a Plex 404 alone cannot prove deletion.
        plex.search_library("")
        destination_id = str(playlist.get("plex_playlist_id") or "").strip()
        if destination_id:
            response = network.get(
                f"{plex.base_url}/playlists/{destination_id}", headers=plex.headers, timeout=(5, 15)
            )
            if response.status_code != 404:
                response.raise_for_status()
                raise ValueError(
                    "The existing Plex playlist is accessible. Sync it instead of recreating it."
                )
        else:
            jobs.output(
                "The destination is already marked missing; retrying recreation from the source playlist."
            )
        key = payload["playlist_key"]
        # The confirmed destination is gone. Clearing only its ID lets the normal
        # source/Lidarr/Plex matching pipeline choose a currently playable seed,
        # create the replacement, preserve duplicate ordering, and sync once.
        # This also makes a failed attempt safely retryable later.
        playlist["plex_playlist_id"] = ""
        config.save()
        jobs.progress("Matching the source and recreating its Plex playlist")
    return sync_one(key)


def register(app):
    class Confirm(BaseModel):
        confirmed: bool = False

    @app.post("/api/playlists/{key:path}/recreate", status_code=202)
    def recreate(key: str, body: Confirm):
        from .api import _config, _playlist_key, job_store

        if not body.confirmed:
            raise HTTPException(422, "Confirm creation of a replacement Plex playlist.")
        if not any(
            _playlist_key(p) == key
            for p in _config(read_only=True, namespaces=[]).config["playlists"]
        ):
            raise HTTPException(404, "Playlist not found")
        store = job_store()
        return store.get(store.enqueue("recreate", {"playlist_key": key}))
