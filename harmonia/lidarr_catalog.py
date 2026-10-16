"""Scoped, read-only Lidarr catalog updates from webhook targets."""

import time
from . import jobs
from .lidarr import Client, config, state_put
from .lidarr_requests import server_id
from .album_finalize import _album_row, _tracks
from .track_bridge import scan_tracks


def _artist_row(artist):
    images = [
        i for i in artist.get("images", []) if str(i.get("remoteUrl", "")).startswith("https://")
    ]

    def art(kinds):
        return next(
            (
                i["remoteUrl"]
                for kind in kinds
                for i in images
                if str(i.get("coverType", "")).casefold() == kind
            ),
            "",
        )

    return {
        "id": artist["id"],
        "name": artist.get("artistName", ""),
        "mbid": artist.get("foreignArtistId", ""),
        "monitored": artist.get("monitored"),
        "artwork": art(("poster", "cover", "logo", "fanart")),
        "portrait_artwork": art(("poster", "cover", "logo")),
        "hero_artwork": art(("fanart", "banner")),
        "images": [{k: i.get(k) for k in ("coverType", "remoteUrl", "url")} for i in images],
    }


def refresh(repo, targets, defer_invalidate=False):
    cfg = config(repo)
    if not cfg.get("enabled"):
        raise ValueError("Lidarr integration is disabled.")
    server = server_id(cfg)
    saved = repo.load_one("inventory", "lidarr", {})
    if saved and saved.get("identity") != server:
        raise ValueError(
            "Lidarr server changed. Run a scheduled or manual full Lidarr Library Scan first."
        )
    inventory = {
        **saved,
        "identity": server,
        "rows": list(saved.get("rows", [])),
        "tracks": list(saved.get("tracks", [])),
        "artists": list(saved.get("artists", [])),
        "checked_at": saved.get("checked_at", 0),
    }
    client = Client(cfg)
    failed = []
    done = []
    for target in targets:
        jobs.progress(f"Refreshing affected Lidarr {target.get('scope')}: {target.get('id')}")
        scope = target.get("scope")
        ident = target.get("id")
        kind = target.get("kind")
        try:
            if scope not in ("artist", "album") or not str(ident or "").isdigit():
                raise ValueError(
                    "Webhook has no usable album or artist ID; run a manual or scheduled full scan."
                )
            if kind == "albumdelete" and scope != "album":
                raise ValueError("Album deletion lacks an album ID; no artist data was removed.")
            if kind == "artistdelete" and scope != "artist":
                raise ValueError("Artist deletion lacks an artist ID; no album data was removed.")
            ident = int(ident)
            old_rows = [
                r
                for r in inventory["rows"]
                if str(r.get("artist_id" if scope == "artist" else "id")) == str(ident)
            ]
            ids = {str(r.get("id")) for r in old_rows}
            if scope == "album":
                ids.add(str(ident))
            groups = {str(r.get("album_id")) for r in old_rows}
            delete = kind in ("artistdelete", "albumdelete")
            artist = None
            rows = []
            tracks = []
            preview = []
            preview_failed = False
            if not delete:
                if scope == "artist":
                    artist = client.call("GET", f"artist/{ident}", quiet=True)
                    albums = client.call("GET", "album", params={"artistId": ident}, quiet=True)
                    if not isinstance(albums, list) or any(
                        not isinstance(a, dict)
                        or not a.get("id")
                        or str(a.get("artistId")) != str(ident)
                        for a in albums
                    ):
                        raise ValueError(
                            "Incomplete artist album response; previous data retained."
                        )
                    if not isinstance(artist, dict) or artist.get("id") != ident:
                        raise ValueError("Invalid artist identity; previous data retained.")
                    rows = [_album_row(a, artist) for a in albums]
                    tracks = scan_tracks(client, [artist], albums)
                else:
                    album = client.call("GET", f"album/{ident}", quiet=True)
                    if (
                        not isinstance(album, dict)
                        or album.get("id") != ident
                        or not album.get("artistId")
                    ):
                        raise ValueError("Invalid album identity; previous data retained.")
                    artist = client.call("GET", f"artist/{album['artistId']}", quiet=True)
                    if not isinstance(artist, dict) or artist.get("id") != album["artistId"]:
                        raise ValueError("Invalid artist identity; previous data retained.")
                    rows = [_album_row(album, artist)]
                    tracks = _tracks(client, album, artist)
                try:
                    preview = client.call(
                        "GET",
                        "rename",
                        params={
                            "artistId": artist["id"],
                            **({"albumId": ident} if scope == "album" else {}),
                        },
                        quiet=True,
                    )
                    if not isinstance(preview, list) or any(
                        not isinstance(p, dict)
                        or not p.get("trackFileId")
                        or not p.get("existingPath")
                        or not p.get("newPath")
                        for p in preview
                    ):
                        raise ValueError("Invalid rename preview")
                except Exception:
                    preview = []
                    preview_failed = True
            artist_row = _artist_row(artist) if artist else None
            # All reads complete before replacing this slice. Failures retain it.
            inventory["rows"] = [r for r in inventory["rows"] if r not in old_rows] + rows
            inventory["tracks"] = [
                t
                for t in inventory["tracks"]
                if str(t.get("lidarr_album_id")) not in ids and str(t.get("album_id")) not in groups
            ] + tracks
            if scope == "artist":
                inventory["artists"] = [
                    a for a in inventory["artists"] if str(a.get("id")) != str(ident)
                ]
            if artist:
                inventory["artists"] = [
                    a for a in inventory["artists"] if str(a.get("id")) != str(artist["id"])
                ] + [artist_row]
            snapshot = inventory.get("rename_preview")
            if isinstance(snapshot, dict):
                # Do not certify unknown legacy previews for the rest of the library.
                affected = ids | {str(r.get("id")) for r in rows}
                remaining = [
                    p
                    for p in snapshot.get("rows", [])
                    if str(p.get("albumId")) not in affected
                    and not (scope == "artist" and str(p.get("artistId")) == str(ident))
                ]
                errors = list(snapshot.get("failed_artists", []))
                aid = artist["id"] if artist else ident if scope == "artist" else None
                if aid is not None and scope == "artist":
                    errors = [a for a in errors if str(a) != str(aid)]
                if preview_failed and aid not in errors:
                    errors.append(aid)
                inventory["rename_preview"] = {
                    **snapshot,
                    "rows": remaining + [{**p, "checked_at": time.time()} for p in preview],
                    "failed_artists": errors,
                    "partial_checked_at": time.time(),
                }
            inventory["partial_checked_at"] = time.time()
            inventory["count"] = len(inventory["rows"])
            checks = dict(inventory.get("album_checked_at", {}))
            checks.update({r.get("album_id"): time.time() for r in rows if r.get("album_id")})
            inventory["album_checked_at"] = checks
            done.append(target)
            if preview_failed:
                failed.append(
                    {"target": target, "error": "Rename preview unavailable; album data updated."}
                )
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))
            failed.append({"target": target, "error": message})
            jobs.output(f"Affected Lidarr {scope} {ident}: {message}")
    if done:
        state_put(repo, "inventory", "lidarr", inventory)
        from .library import invalidate

        if not defer_invalidate:
            invalidate()
    summary = f"Updated {len(done)} affected targets; no full library scan or global playlist reconciliation."
    if failed:
        summary += f" {len(failed)} checks failed; see job details and retry the event or run a manual scan."
    return {
        "summary": summary,
        "updated": len(done),
        "failures": failed,
        "partial_success": bool(failed),
    }
