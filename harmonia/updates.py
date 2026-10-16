"""Read-only GHCR release-image update checks, shared across browser sessions."""

import re
import os
import threading
import time
from datetime import datetime, timezone
import json
from urllib.parse import urljoin
from fastapi import HTTPException
from pydantic import BaseModel
from . import network
from . import __release_name__, __version__, __build__, __release_channel__

_lock = threading.Lock()
INTERVAL = 6 * 3600
REPO = "rstuke82/harmonia"


def channel(value):
    return value if value in ("stable", "beta") else "stable"


CHANNEL = channel(os.environ.get("HARMONIA_UPDATE_CHANNEL", __release_channel__))
IMAGE_TAG = "latest" if CHANNEL == "stable" else "beta"


def version(value):
    numbered = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\.(\d+)", str(value))
    if numbered:
        return tuple(map(int, numbered.groups()))
    build = re.fullmatch(r"(20\d{6})\.(\d+)", str(value))
    if build:
        return (int(build.group(1)), 0, 0, int(build.group(2)))
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-beta\.?(\d+))?", str(value))
    return (
        tuple(map(int, match.groups()[:3])) + (int(match[4]) if match[4] else 1000000,)
        if match
        else None
    )


def available_version(latest):
    # A semantic source release can share its exact image build number.
    parsed = version(latest)
    current = version(__build__ if re.fullmatch(r"20\d{6}\.\d+", str(latest)) else __version__)
    return bool(parsed and current and parsed > current)


def repository():
    from .runtime_paths import CONFIG_DIR
    from .storage import get_repository

    return get_repository(CONFIG_DIR)


def enabled(repo):
    return repo.load_one("update_settings", "enabled", True) is not False


def check(force=False):
    store = repository()
    if not enabled(store):
        return stored_status(store)
    if not _lock.acquire(False):
        return store.load("updates").get("latest", {})
    try:
        previous = store.load("updates").get("latest", {})
        if previous.get("repository") != REPO:
            previous = {}
        if previous.get("channel") == CHANNEL and time.time() - previous.get("checked_epoch", 0) < (
            60 if force else INTERVAL
        ):
            return previous
        state = {
            "checked_epoch": time.time(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "current_version": __version__,
            "repository": REPO,
            "channel": CHANNEL,
            "available": False,
            "interval_hours": 6,
        }
        try:
            token_response = network.get(
                "https://ghcr.io/token",
                params={"service": "ghcr.io", "scope": f"repository:{REPO}:pull"},
                timeout=6,
            )
            token_response.raise_for_status()
            headers = {
                "Authorization": "Bearer " + token_response.json()["token"],
                "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
            }
            base = f"https://ghcr.io/v2/{REPO}"
            response = network.get(base + "/manifests/" + IMAGE_TAG, headers=headers, timeout=6)
            response.raise_for_status()
            manifest = response.json()
            if "manifests" in manifest:
                platform = next(
                    m
                    for m in manifest["manifests"]
                    if m.get("platform", {}).get("architecture") == "amd64"
                    and m.get("platform", {}).get("os") == "linux"
                )
                response = network.get(
                    base + "/manifests/" + platform["digest"], headers=headers, timeout=6
                )
                response.raise_for_status()
                manifest = response.json()
            blob_url = base + "/blobs/" + manifest["config"]["digest"]
            response = network.get(blob_url, headers=headers, timeout=6)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ValueError("Missing image configuration redirect")
                from .image_download import download

                payload = json.loads(
                    download(
                        urljoin(blob_url, location), external=True, max_bytes=1_000_000
                    ).content
                )
            else:
                response.raise_for_status()
                payload = response.json()
                response.close()
            latest = (
                payload.get("config", {})
                .get("Labels", {})
                .get("org.opencontainers.image.version", "")
            )
            parsed = version(latest)
            if not parsed:
                raise ValueError("Image has no recognized version label")
            state.update(
                latest_version=latest,
                available=available_version(latest),
                url="https://github.com/rstuke82/harmonia",
            )
        except Exception:
            state.update(
                error="Could not check the published release image. Try again later.",
                latest_version=previous.get("latest_version"),
                available=bool(previous.get("available")),
            )
        store.save({"updates": {"latest": state}})
        return state
    finally:
        _lock.release()


def stored_status(repo):
    state = repo.load("updates").get("latest", {"available": False, "interval_hours": 6})
    if state.get("channel") != CHANNEL or state.get("repository") != REPO:
        state = {"available": False, "interval_hours": 6}
    state["channel"] = CHANNEL
    state["current_version"] = __version__
    state["release_name"] = __release_name__
    state["enabled"] = enabled(repo)
    state["available"] = state["enabled"] and available_version(state.get("latest_version", ""))
    if not state["enabled"]:
        state.pop("error", None)
    return state


class UpdateSettings(BaseModel):
    enabled: bool = True


def register(app):
    @app.get("/api/settings/updates")
    def settings():
        return {"enabled": enabled(repository())}

    @app.put("/api/settings/updates")
    def save_settings(value: UpdateSettings):
        repo = repository()
        repo.put("update_settings", "enabled", value.enabled)
        return stored_status(repo)

    @app.get("/api/updates")
    def status():
        return stored_status(repository())

    @app.post("/api/updates/check")
    def check_now():
        if not enabled(repository()):
            raise HTTPException(409, "Enable update checks before checking for updates.")
        from .api import job_store

        store = job_store()
        return store.get(store.enqueue("check_updates", {}))
