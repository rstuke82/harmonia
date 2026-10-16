"""Publish playlist pages from the source and library work a sync already did."""

from datetime import datetime, timezone


def update(config, playlist, tracks, library, metadata, ok):
    from .api import _playlist_key, _playlist_payload
    from .sync_engine import Syncer
    from .playlist_summary import summarize
    from .saved_views import publish

    key = _playlist_key(playlist)
    state = {
        "ok": bool(ok),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": ""
        if ok
        else "Last sync failed. Plex playlist contents could not be confirmed; review the sync job.",
    }
    config.repository.put("playlist_sync_state", key, state)
    if not ok:
        config.repository.record_health_attempt(key, state["error"])
    # Health may have been persisted independently after matching.
    config.health = config.repository.load("health")
    config.health_attempts = config.repository.load("health_attempts")
    lookup = {str(t.get("plex_id")): t for t in library}
    syncer = Syncer(config)
    from .matching import Matcher

    blocked = (
        {
            Matcher._normalize_match_text(v.get("name", ""))
            for v in config.repository.load("blocked_artists").values()
        }
        if playlist.get("source") == "text"
        else set()
    )
    rows = []
    for index, track in enumerate(tracks):
        search = f"{track.get('title', '')}|{track.get('artist', '')}"
        plex_id = config.mapping.get(key, {}).get(search)
        match = lookup.get(str(plex_id)) if plex_id is not None else None
        status = (
            syncer._get_match_provenance(key, search).capitalize()
            if match
            else ("LOST" if plex_id else "Unresolved")
        )
        if not match and any(
            t.get("status") == "lost" and syncer._same_missing_identity(t, track)
            for t in config.missing.get(key, [])
        ):
            status = "LOST"
        if (
            syncer._find_ignored_track_key(key, track)
            or Matcher._normalize_match_text(track.get("artist", "")) in blocked
        ):
            status = "Ignored"
        rows.append(
            {
                **track,
                "index": index,
                "plex_id": plex_id,
                "match": match,
                "status": status,
                "catalog_status": "",
            }
        )
    payload = summarize(_playlist_payload(config, playlist), rows)
    publish(
        config.repository,
        "playlist details",
        {"playlist_key": key},
        {
            "playlist": payload,
            "metadata": metadata,
            "tracks": rows,
            "source_refresh_required": False,
        },
    )
    # The list uses the same saved account state; no Library rebuild is needed.
    publish(
        config.repository,
        "playlists",
        {},
        [_playlist_payload(config, p) for p in config.config.get("playlists", [])],
    )
