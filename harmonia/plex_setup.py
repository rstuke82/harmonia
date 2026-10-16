"""Bounded first-run discovery over Plex's advertised server connections."""

from urllib.parse import urlsplit
from fastapi import HTTPException


def music_connection(resource, token, *, all_libraries=False):
    from .accounts import plex_json, headers

    connections = sorted(resource.get("connections") or [], key=lambda c: not bool(c.get("local")))
    urls = []
    for connection in connections:
        url = str(connection.get("uri") or "").rstrip("/")
        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            continue
        if url not in urls:
            urls.append(url)
    if not urls:
        raise HTTPException(409, "Plex did not provide a usable connection for your server.")
    reachable = False
    for url in urls[:4]:
        try:
            libraries = plex_json(
                "GET",
                url + "/library/sections",
                headers=headers(resource.get("accessToken") or token),
            )
        except HTTPException:
            continue
        reachable = True
        music = [
            s
            for s in libraries.get("MediaContainer", {}).get("Directory", [])
            if s.get("type") == "artist"
        ]
        if music:
            return url, music if all_libraries else music[0]
    if reachable:
        raise HTTPException(409, "No accessible Plex music library was found on the owned server.")
    raise HTTPException(
        503,
        "Could not reach your Plex server using its advertised connections. Check container networking and Plex remote access, then retry setup.",
    )
