"""Latest-value-wins playlist setting changes, applied by the durable worker."""

import json
import uuid
from fastapi import HTTPException


def enqueue(keys, changes):
    from .api import job_store, _config, _playlist_key

    config = _config(read_only=True, namespaces=[])
    existing = {_playlist_key(p) for p in config.config.get("playlists", [])}
    if not keys or not set(keys) <= existing:
        raise HTTPException(409, "Playlist registrations changed. Refresh and try again.")
    name = changes.get("name")
    if name is not None:
        name = name.strip()
        if not name or len(name) > 200:
            raise HTTPException(422, "Enter a playlist name between 1 and 200 characters")
        changes = {"name": name}
    elif changes.get("restore_source_name"):
        changes = {"restore_source_name": True}
    else:
        raise HTTPException(422, "Choose a new name or restore the source name")
    store = job_store()
    with store.repository.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for key in keys:
            row = db.execute(
                "SELECT value FROM state WHERE namespace='pending_playlist_settings' AND key=?",
                (key,),
            ).fetchone()
            old = json.loads(row[0]) if row else {}
            old = changes.copy()
            old["revision"] = str(uuid.uuid4())
            db.execute(
                "INSERT OR REPLACE INTO state VALUES('pending_playlist_settings',?,?)",
                (key, json.dumps(old)),
            )
        jid = store.enqueue("playlist_settings", {}, db=db)
    return {"queued": True, "job_id": jid, "message": "Change queued; it will apply when safe."}


def execute():
    from .api import job_store, _config, _playlist_key
    from .process_lock import ProcessLock
    from . import jobs

    repo = job_store().repository
    with ProcessLock():
        config = _config()
        pending = repo.load("pending_playlist_settings")
        count = 0
        try:
            for p in config.config.get("playlists", []):
                key = _playlist_key(p)
                if key not in pending:
                    continue
                value = pending[key]
                # Old queued flag edits were consumed by the sync-mode migration.
                if "name" not in value and not value.get("restore_source_name"):
                    continue
                name = p.get("source_name") if value.get("restore_source_name") else value["name"]
                if not name:
                    raise ValueError("Refresh or sync this playlist first to load its source name.")
                from .api import _health_plex
                from . import network

                plex = _health_plex(config)
                jobs.progress("Renaming playlist: " + name)
                response = network.put(
                    f"{plex.base_url}/playlists/{p['plex_playlist_id']}",
                    headers=plex.headers,
                    params={"title": name},
                    timeout=20,
                )
                response.raise_for_status()
                p.setdefault("source_name", p.get("plex_playlist_name", ""))
                p["plex_playlist_name"] = name
                p["custom_name"] = "" if value.get("restore_source_name") else name
                config.save()
                count += 1
        finally:
            # A failed rename is visible in Activity and needs an explicit retry.
            with repo.connect() as db:
                for key, v in pending.items():
                    db.execute(
                        "DELETE FROM state WHERE namespace='pending_playlist_settings' AND key=? AND json_extract(value,'$.revision')=?",
                        (key, v["revision"]),
                    )
    return {"summary": f"Updated settings for {count} playlists."}
