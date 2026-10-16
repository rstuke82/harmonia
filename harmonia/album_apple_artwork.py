"""Saved artwork for an explicitly reviewed Apple album link."""

import re
import time
from urllib.parse import urlsplit
from . import network
from fastapi import HTTPException
from .lidarr import state_put


NAMESPACE = "library_album_apple_artwork"


def album_reference(url):
    from .album_service_links import validate

    url = validate("apple", url)
    if not url:
        return "", ""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if not re.fullmatch(r"[0-9]+", parts[-1]):
        raise HTTPException(422, "Use an Apple Music album link ending in its numeric album ID.")
    country = parts[0].lower() if re.fullmatch(r"[A-Za-z]{2}", parts[0]) else "us"
    return parts[-1], country


def refresh(repo, album_id, url):
    album, country = album_reference(url)
    result = {"url": url, "artwork": "", "checked_at": time.time(), "message": ""}
    if album:
        try:
            response = network.get(
                "https://itunes.apple.com/lookup",
                params={"id": album, "country": country, "entity": "album"},
                timeout=(5, 10),
                allow_redirects=False,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Unexpected lookup response")
            rows = response.json().get("results", [])
            match = next(
                (
                    r
                    for r in rows
                    if str(r.get("collectionId")) == album
                    and r.get("wrapperType") == "collection"
                    and r.get("collectionType") == "Album"
                ),
                {},
            )
            from .artist_library import image_url

            result["artwork"] = image_url(match.get("artworkUrl100", "")).replace(
                "100x100bb", "1200x1200bb"
            )
            if not result["artwork"]:
                result["message"] = (
                    "Apple artwork was not found for this album in its storefront. Save the link again to retry."
                )
        except (network.RequestException, ValueError, TypeError, AttributeError):
            result["message"] = (
                "The link was saved, but Apple artwork could not be loaded. Save the link again to retry."
            )
    state_put(repo, NAMESPACE, album_id, result)
    return result


def artwork(repo, album_id, automatic="", *, saved_links=None, saved_artwork=None):
    saved = (
        saved_links.get(str(album_id), {})
        if saved_links is not None
        else repo.load_one("library_album_service_links", str(album_id))
    ) or {}
    if "apple" not in saved:
        return automatic
    cached = (
        saved_artwork.get(str(album_id), {})
        if saved_artwork is not None
        else repo.load_one(NAMESPACE, str(album_id))
    ) or {}
    # Never display title-matched or previous-link art for a reviewed link.
    return (
        cached.get("artwork", "") if saved["apple"] and cached.get("url") == saved["apple"] else ""
    )
