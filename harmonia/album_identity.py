"""Album identity shared by inventory findings, file evidence and navigation.

Album artist owns name identity; track performers do not override a known album.
Ambiguous names never join two release groups automatically.
"""

import hashlib
from collections import defaultdict
from .album_catalog import normalize


def names(row):
    return normalize(row.get("album_artist") or row.get("artist")), normalize(
        row.get("album") if "album" in row else row.get("title")
    )


class Albums:
    def __init__(self, lidarr, plex=None, mappings=None, album_links=None, track_links=None):
        self.rows = {str(a["album_id"]): a for a in lidarr.get("rows", []) if a.get("album_id")}
        self.ids = {
            str(a.get("id")): gid for gid, a in self.rows.items() if a.get("id") is not None
        }
        self.aliases = {gid: gid for gid in self.rows}
        self.by_name = defaultdict(set)
        self.by_path = defaultdict(set)
        self.by_plex = defaultdict(set)
        self.plex_rows = {}
        for gid, a in self.rows.items():
            name = names(a)
            if all(name):
                self.by_name[name].add(gid)
            for release in a.get("releases", []):
                if release.get("foreignReleaseId"):
                    self.aliases[str(release["foreignReleaseId"])] = gid
        for t in lidarr.get("tracks", []):
            gid = self.aliases.get(str(t.get("album_id") or "")) or self.ids.get(
                str(t.get("lidarr_album_id"))
            )
            if not gid:
                continue
            self.aliases[str(t.get("album_id") or gid)] = gid
            file = t.get("lidarr_file") or t.get("file")
            if file:
                self.by_path[self.path(file)].add(gid)
                from .file_tools import mapped

                local = mapped(file, config={"mappings": mappings or []})
                if local:
                    self.by_path[self.path(local)].add(gid)
                plex_path = mapped(file, "plex_path", config={"mappings": mappings or []})
                if plex_path:
                    self.by_path[self.path(plex_path)].add(gid)
        for gid, link in (album_links or {}).items():
            pid = str(link.get("plex_album_id") or "")
            if gid in self.rows and pid:
                self.by_plex[pid].add(gid)
        track_albums = {
            str(t.get("id")): str(t.get("album_id") or "") for t in lidarr.get("tracks", [])
        }
        for t in (plex or {}).get("rows", []):
            pid = str(t.get("plex_album_id") or "")
            if pid:
                self.plex_rows.setdefault(pid, t)
            # Confirmed track associations join the two service identities.
        plex_tracks = {str(t.get("plex_id")): t for t in (plex or {}).get("rows", [])}
        for tid, link in (track_links or {}).items():
            gid = track_albums.get(str(tid))
            p = plex_tracks.get(str(link.get("plex_id")), {})
            pid = str(p.get("plex_album_id") or "")
            if gid in self.rows and pid:
                self.by_plex[pid].add(gid)

    @staticmethod
    def path(value):
        from .track_bridge import path

        return path(value)

    def resolve(self, row, paths=(), hint=None):
        candidates = set()
        gid = self.aliases.get(
            str(row.get("album_id") or row.get("musicbrainz_id") or "")
        ) or self.ids.get(str(row.get("lidarr_album_id")))
        if gid:
            candidates.add(gid)
        pid = str(row.get("plex_album_id") or "")
        candidates.update(self.by_plex.get(pid, ()))
        for file in paths:
            candidates.update(self.by_path.get(self.path(file), ()))
        if len(candidates) == 1:
            return self.group(next(iter(candidates)), pid, "association")
        if len(candidates) > 1:
            return self.unresolved(row, pid, "Conflicting album associations")
        name = names(row)
        matches = self.by_name.get(name, set()) if all(name) else set()
        if len(matches) == 1:
            return self.group(next(iter(matches)), pid, "album artist and title")
        if len(matches) > 1:
            return self.unresolved(
                row, pid, "More than one release group has this album artist and title"
            )
        # Old verified findings can retain their album identity when performer
        # tags lack album_artist. Current conflicting associations take priority.
        hinted = self.aliases.get(str(hint or ""))
        if hinted:
            return self.group(hinted, pid, "saved album evidence")
        if pid:
            return {
                "album_key": "plex:" + pid,
                "review_album_id": "plex:" + pid,
                "plex_album_id": pid,
                "artist": row.get("album_artist") or row.get("artist", ""),
                "album": row.get("album") or row.get("title", ""),
                "identity_status": "plex",
            }
        if all(name):
            return {
                "album_key": "name:" + hashlib.sha256(repr(name).encode()).hexdigest(),
                "artist": row.get("album_artist") or row.get("artist", ""),
                "album": row.get("album") or row.get("title", ""),
                "identity_status": "name",
            }
        return self.unresolved(row, pid, "Album artist or title is missing")

    def group(self, gid, pid, method):
        a = self.rows[gid]
        return {
            "album_key": "album:" + gid,
            "album_id": gid,
            "lidarr_album_id": a.get("id"),
            "review_album_id": gid,
            "plex_album_id": pid or None,
            "artist": a.get("artist", ""),
            "album": a.get("title", ""),
            "artist_id": a.get("artist_id"),
            "identity_status": "resolved",
            "identity_method": method,
        }

    def unresolved(self, row, pid, reason):
        key = (
            "plex:" + pid
            if pid
            else "unresolved:"
            + hashlib.sha256(
                repr([names(row), row.get("album_id"), row.get("files")]).encode()
            ).hexdigest()
        )
        return {
            "album_key": key,
            "review_album_id": "plex:" + pid if pid else None,
            "plex_album_id": pid or None,
            "artist": row.get("album_artist") or row.get("artist", ""),
            "album": row.get("album") or row.get("title", ""),
            "identity_status": "review",
            "identity_reason": reason,
        }

    def finding(self, row):
        identity = self.resolve(row, row.get("files", []))
        from .file_audit import fingerprint

        legacy = row.get("_legacy_issue_id") or fingerprint(
            {k: row.get(k) for k in ("issue", "files", "evidence", "album_id", "id", "plex_id")}
        )
        result = {**row, **identity, "_legacy_issue_id": legacy}
        if identity["identity_status"] == "review":
            result.update(album_id=None, lidarr_album_id=None, artist_id=None)
        if row.get("identity_review"):
            result["identity_status"] = "review"
        return result
