"""Small saved Library models for focused matching tests."""

from collections import defaultdict
from unittest.mock import patch
from harmonia.library import link_targets


def model_from_inventory(inventory, links=None):
    lidarr = inventory.get("lidarr", {})
    plex = inventory.get("plex", {})
    tracks = defaultdict(list)
    grouped = defaultdict(list)
    for track in lidarr.get("tracks", []):
        tracks[str(track.get("album_id"))].append(track)
    for track in plex.get("rows", []):
        grouped[str(track.get("plex_album_id") or "")].append(track)
    rows = {
        str(a["album_id"]): {**a, "in_lidarr": True, "library_state": "In Library"}
        for a in lidarr.get("rows", [])
    }
    for pid, ts in grouped.items():
        if not pid:
            continue
        first = ts[0]
        compatible = [
            r
            for r in rows.values()
            if str(r.get("title", "")).casefold() == str(first.get("album", "")).casefold()
            and str(r.get("artist", "")).casefold()
            == str(first.get("album_artist") or first.get("artist") or "").casefold()
        ]
        linked = str(compatible[0]["album_id"]) if len(compatible) == 1 else None
        rows["plex:" + pid] = {
            "album_id": "plex:" + pid,
            "plex_only": True,
            "plex_album_id": pid,
            "linked_lidarr_album_id": linked,
            "title": first.get("album"),
            "artist": first.get("album_artist") or first.get("artist"),
            "artwork": "/api/library/plex-albums/" + pid + "/image",
            "library_state": "In Plex",
            "track_count": len(ts),
        }
        if linked and not rows[linked].get("artwork"):
            rows[linked]["artwork"] = rows["plex:" + pid]["artwork"]
    return {
        "_rows_by_album": rows,
        "_tracks_by_album": tracks,
        "_plex_tracks_by_album": grouped,
        "_plex_by_id": {str(t.get("plex_id")): t for t in plex.get("rows", [])},
        "_ctx": {
            "links": {},
            "tracks": lidarr.get("tracks", []),
            "pending": False,
            "scope": "scope",
        },
        "_album_links": links or {},
        "_plex": plex,
        "_lidarr": lidarr,
    }


def library_targets(inventory):
    with patch(
        "harmonia.library.cached_link_library", return_value=model_from_inventory(inventory)
    ):
        return link_targets()
