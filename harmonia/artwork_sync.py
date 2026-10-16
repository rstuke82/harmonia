"""Plex is the artwork authority; explicit Harmonia choices are sent to Plex."""

import hashlib
import time
from . import network
import threading
from fastapi import HTTPException


_choice_lock = threading.RLock()


def choose(config, target, ident, kind, url, provider):
    from .accounts import root_repository
    from .lidarr import state_put

    with _choice_lock:
        result = apply(config, target, ident, kind, url, provider)
        state_put(
            root_repository(),
            "artwork_overrides",
            target + ":" + ident + ":" + kind,
            {
                "url": url,
                "provider": provider,
                "sync_status": result["status"],
                "selected_at": time.time(),
            },
        )
        return result


def source(data, target, ident, hint=None):
    if target == "album":
        if ident.startswith("plex:"):
            return data["plex_albums"].get(ident[5:], {})
        row = data["albums"].get(ident, hint or {})
        saved = str(data["album_links"].get(ident, {}).get("plex_album_id") or "")
        return (
            data["plex_albums"].get(saved, {})
            if saved
            else data["plex_album_names"].get(
                (str(row.get("artist", "")).casefold(), str(row.get("title", "")).casefold()), {}
            )
        )
    if ident.startswith("plex:"):
        return data["plex_artists"].get(ident[5:], {})
    row = data["artists"].get(ident, {})
    saved = str(data["artist_links"].get(ident, {}).get("plex_artist_id") or "")
    return (
        data["plex_artists"].get(saved, {})
        if saved
        else data["plex_artist_names"].get(str(row.get("name", "")).casefold(), {})
    )


def image_url(target, ident, kind, row):
    path = row.get(
        "plex_album_thumb"
        if target == "album"
        else "plex_artist_art"
        if kind == "hero"
        else "plex_artist_thumb"
    )
    if not path:
        return ""
    version = hashlib.sha256(
        (str(path) + "|" + str(row.get("plex_artwork_updated_at", ""))).encode()
    ).hexdigest()[:16]
    if target == "album":
        return "/api/library/plex-albums/" + str(row["plex_album_id"]) + "/image?v=" + version
    return "/api/library/artists/" + ident + "/plex-image?kind=" + kind + "&v=" + version


def apply(config, target, ident, kind, url, provider):
    from .library_inventory import indexed

    data = indexed(config)
    row = source(data, target, ident)
    rating = str(row.get("plex_album_id" if target == "album" else "plex_artist_id") or "")
    if not rating:
        return {
            "status": "pending",
            "message": "Artwork saved. It will sync when the item is linked to Plex.",
        }
    if provider == "plex":
        return {"status": "synced", "message": "Using current Plex artwork."}
    if not url.startswith(("https://", "http://")):
        raise HTTPException(409, "Choose a direct artwork source to sync to Plex.")
    from .api import _health_plex, _config
    from .accounts import as_user

    # Harmonia administrators act through the configured server owner, while
    # target resolution above still respects the caller's saved visibility.
    with as_user(None):
        client = _health_plex(_config(read_only=True, namespaces=[]))
    if (
        data.get("plex", {}).get("machine_identifier")
        and data["plex"]["machine_identifier"] != client.machine_identifier
    ):
        raise HTTPException(
            409,
            "The Plex server association changed. Scan Plex and review this item before updating artwork.",
        )
    try:
        from .image_download import download

        image = download(url, headers={"User-Agent": "Harmonia"}, external=True)
        from PIL import Image
        from io import BytesIO

        try:
            with Image.open(BytesIO(image.content)) as decoded:
                decoded.verify()
        except Exception as exc:
            raise HTTPException(422, "The artwork source did not return a valid image.") from exc
        endpoint = "arts" if kind == "hero" else "posters"
        resource_url = client.base_url + "/library/metadata/" + rating + "/" + endpoint
        before = network.get(
            resource_url, headers=client.headers, timeout=(5, 20), allow_redirects=False
        )
        before.raise_for_status()
        existing = {
            str(r.get("ratingKey") or "")
            for r in before.json().get("MediaContainer", {}).get("Metadata", [])
        }
        response = network.post(
            resource_url,
            headers={
                **client.headers,
                "Content-Type": image.headers.get("Content-Type", "image/jpeg"),
            },
            data=image.content,
            timeout=(5, 30),
            allow_redirects=False,
        )
        response.raise_for_status()
        after = network.get(
            resource_url, headers=client.headers, timeout=(5, 20), allow_redirects=False
        )
        after.raise_for_status()
        resources = after.json().get("MediaContainer", {}).get("Metadata", [])
        uploaded = [
            r for r in resources if r.get("ratingKey") and str(r["ratingKey"]) not in existing
        ]
        if len(uploaded) != 1:
            raise HTTPException(
                502,
                "Plex accepted the upload but the new image could not be identified. Refresh and retry the artwork choice.",
            )
        selected = network.put(
            resource_url[:-1],
            headers=client.headers,
            params={"url": uploaded[0]["ratingKey"]},
            timeout=(5, 20),
            allow_redirects=False,
        )
        selected.raise_for_status()
        confirmed = network.get(
            resource_url, headers=client.headers, timeout=(5, 20), allow_redirects=False
        )
        confirmed.raise_for_status()
        if not any(
            str(r.get("ratingKey")) == str(uploaded[0]["ratingKey"])
            and str(r.get("selected")).lower() in ("1", "true")
            for r in confirmed.json().get("MediaContainer", {}).get("Metadata", [])
        ):
            raise HTTPException(
                502, "Plex did not retain the selected image. Refresh and retry the artwork choice."
            )
        response = network.get(
            client.base_url + "/library/metadata/" + rating,
            headers=client.headers,
            timeout=(5, 20),
            allow_redirects=False,
        )
        response.raise_for_status()
        items = response.json().get("MediaContainer", {}).get("Metadata", [])
        metadata = next((item for item in items if str(item.get("ratingKey")) == rating), None)
        field = "art" if kind == "hero" else "thumb"
        if not metadata or not str(metadata.get(field) or "").startswith("/"):
            raise HTTPException(
                502,
                "Plex accepted the upload but its current artwork could not be verified. Retry the artwork choice.",
            )
    except (network.RequestException, ValueError, TypeError) as exc:
        raise HTTPException(
            502,
            "Plex artwork could not be updated. Your Harmonia artwork choice was not changed; retry after checking Plex.",
        ) from exc
    # Merge only current image paths into each visible account inventory. Never
    # replace a fetched whole snapshot, or expand a member's Plex visibility.
    from .accounts import root_repository, member_stores
    import json

    key = "plex_album_id" if target == "album" else "plex_artist_id"
    path_key = (
        "plex_album_thumb"
        if target == "album"
        else "plex_artist_art"
        if kind == "hero"
        else "plex_artist_thumb"
    )
    root = root_repository()
    from .lidarr import state_put

    state_put(
        root,
        "artwork_plex_sync",
        target + ":" + ident + ":" + kind,
        {
            "machine_identifier": client.machine_identifier,
            "id_key": key,
            "plex_id": rating,
            "path_key": path_key,
            "path": metadata[field],
            "applied_at": time.time(),
        },
    )
    for repo in dict.fromkeys(
        [config.repository, root, *[store.repository for user, store in member_stores()]]
    ):
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute(
                "SELECT value FROM state WHERE namespace='inventory' AND key='plex'"
            ).fetchone()
            if not record:
                continue
            value = json.loads(record[0])
            if value.get("machine_identifier") != client.machine_identifier:
                continue
            changed = False
            for track in value.get("rows", []):
                if str(track.get(key) or "") == rating:
                    track[path_key] = metadata[field]
                    track["plex_artwork_updated_at"] = time.time()
                    changed = True
            if changed:
                db.execute(
                    "UPDATE state SET value=? WHERE namespace='inventory' AND key='plex'",
                    (json.dumps(value),),
                )
        from .plex_images import invalidate as invalidate_images

        invalidate_images(repo)
    from .library import invalidate

    invalidate()
    return {
        "status": "synced",
        "plex_id": rating,
        "message": "Artwork updated in Plex and Harmonia.",
    }


def pending(config):
    from .accounts import root_repository

    repo = root_repository()
    results = []
    for key, value in repo.load("artwork_overrides").items():
        if value.get("sync_status") != "pending":
            continue
        target, ident, kind = (
            key.split(":", 1)[0],
            key.rsplit(":", 1)[0].split(":", 1)[1],
            key.rsplit(":", 1)[1],
        )
        try:
            with _choice_lock:
                value = repo.load_one("artwork_overrides", key, {})
                if value.get("sync_status") != "pending":
                    continue
                result = choose(config, target, ident, kind, value["url"], value["provider"])
            results.append(result)
        except Exception as exc:
            from .jobs import output

            output("Artwork sync failed: " + str(getattr(exc, "detail", exc)))
            results.append({"status": "failed"})
    return {
        "summary": f"Artwork sync: {sum(r['status'] == 'synced' for r in results)} updated; {sum(r['status'] == 'failed' for r in results)} failed.",
        "partial_success": any(r["status"] == "failed" for r in results),
    }


def queue_pending(repo):
    if any(
        value.get("sync_status") == "pending" for value in repo.load("artwork_overrides").values()
    ):
        from .jobs import Store

        Store(repo).enqueue("artwork_sync", {})
