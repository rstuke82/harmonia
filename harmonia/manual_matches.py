"""Personal saved manual selections, without contacting music providers."""

from pydantic import BaseModel, Field


def saved_manual_matches(config, query="", page=1, page_size=25):
    from .api import _playlist_key

    rows = []
    needle = query.casefold().strip()
    for playlist in config.config.get("playlists", []):
        key = _playlist_key(playlist)
        metadata = config.match_metadata.get(key, {})
        for search, plex_id in config.mapping.get(key, {}).items():
            record = metadata.get(search, {})
            if not isinstance(record, dict) or record.get("provenance") != "manual" or not plex_id:
                continue
            title, _, artist = search.partition("|")
            match = record.get("matched_track") or {}
            if not isinstance(match, dict):
                match = {}
            row = {
                "playlist_key": key,
                "playlist_name": playlist.get("plex_playlist_name") or key,
                "source": {"title": title, "artist": artist},
                "plex_id": str(plex_id),
                "match": {k: match[k] for k in ("title", "artist", "album") if k in match},
                "search_key": search,
                "updated_at": record.get("updated_at"),
            }
            if (
                needle
                and needle
                not in " ".join(
                    str(v) for v in [title, artist, row["playlist_name"], *row["match"].values()]
                ).casefold()
            ):
                continue
            rows.append(row)
    rows.sort(
        key=lambda r: (
            r["source"]["title"].casefold(),
            r["source"]["artist"].casefold(),
            r["playlist_name"].casefold(),
            r["playlist_key"],
        )
    )
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "rows": rows[start : start + page_size],
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
    }


class RemoveManualMatch(BaseModel):
    playlist_key: str = Field(min_length=1, max_length=1000)
    search_key: str = Field(min_length=1, max_length=2000)
    plex_id: str = Field(min_length=1, max_length=100)
    updated_at: str | None = None


def remove_manual_match(request):
    from fastapi import HTTPException
    from .config import Config
    from .api import _playlist_key
    from .process_lock import ProcessLock
    from .match_queue import draft_id

    try:
        with ProcessLock(wait=False):
            config = Config()
            playlist = next(
                (
                    p
                    for p in config.config.get("playlists", [])
                    if _playlist_key(p) == request.playlist_key
                ),
                None,
            )
            if playlist is None:
                raise HTTPException(404, "Playlist not found in your account.")
            mapping = config.mapping.get(request.playlist_key, {})
            metadata = config.match_metadata.get(request.playlist_key, {})
            record = metadata.get(request.search_key, {})
            if (
                not isinstance(record, dict)
                or record.get("provenance") != "manual"
                or str(mapping.get(request.search_key)) != request.plex_id
                or record.get("updated_at") != request.updated_at
            ):
                raise HTTPException(
                    409, "This saved match changed. Refresh the list before removing it."
                )
            queued = config.repository.load_one("match_drafts", draft_id(request.model_dump()))
            if queued:
                raise HTTPException(
                    409,
                    "This track has a pending match edit. Remove it from the Match Queue first.",
                )
            mapping.pop(request.search_key)
            metadata.pop(request.search_key)
            playlist["ready_to_sync"] = True
            config.save()
    except RuntimeError as exc:
        if "Another Harmonia process is already running" in str(exc):
            raise HTTPException(
                409, "A playlist job is running. Wait for it to finish, then try again."
            ) from exc
        raise
    return {"removed": True, "retry_on_next_sync": True}
