"""Supplementary Lidarr path mappings; never an independent ownership catalog."""

from pathlib import PurePosixPath
from urllib.parse import quote
from pydantic import BaseModel, Field, model_validator
from fastapi import HTTPException


class Mapping(BaseModel):
    lidarr_path: str
    local_path: str = ""
    plex_path: str = ""

    @model_validator(mode="after")
    def validate_paths(self):
        for name in ("lidarr_path", "local_path", "plex_path"):
            value = getattr(self, name).strip().rstrip("/")
            if (name == "lidarr_path" or value) and (
                not value.startswith("/") or ".." in PurePosixPath(value).parts
            ):
                raise ValueError("Use absolute music-folder paths without parent traversal.")
            setattr(self, name, value)
        return self


class Settings(BaseModel):
    mappings: list[Mapping] = Field(default_factory=list, max_length=20)
    itunes_enrichment: bool = True
    audit_enabled: bool = False

    @model_validator(mode="after")
    def unique(self):
        paths = [r.lidarr_path for r in self.mappings]
        if len(paths) != len(set(paths)):
            raise ValueError("Each Lidarr folder can have only one mapping.")
        local = [m.local_path for m in self.mappings if m.local_path]
        if self.audit_enabled and not local:
            raise ValueError("Add a mapped Harmonia folder before enabling local file scanning.")
        return self


def settings():
    from .accounts import root_repository

    return Settings(**root_repository().load("file_tools").get("settings", {})).model_dump()


def mapped(file, field="local_path", config=None):
    if not file or ".." in PurePosixPath(file).parts:
        return ""
    for m in sorted(
        (config or settings())["mappings"], key=lambda m: len(m["lidarr_path"]), reverse=True
    ):
        prefix = m["lidarr_path"]
        if file == prefix or file.startswith(prefix + "/"):
            return m[field] + file[len(prefix) :] if m[field] else ""
    return ""


def plex_url(inventory, item):
    machine = inventory.get("machine_identifier")
    return (
        "https://app.plex.tv/desktop/#!/server/"
        + quote(str(machine), safe="")
        + "/details?key="
        + quote("/library/metadata/" + str(item), safe="")
        if machine and item
        else None
    )


def refresh_metadata(payload):
    from .api import _config
    from .inventory import current
    from .local_library import mb_get, put
    from .artist_library import enrich
    from . import jobs

    provider_failures = []
    config = _config(read_only=True, namespaces=[])
    _, inv = current(config.repository, config)
    aid = str(payload.get("artist_id", ""))
    artist = next((a for a in inv.get("artists", []) if str(a["id"]) == aid), None)
    from .accounts import root_repository
    from .metadata_sources import effective_mbid

    artist_mbid = effective_mbid(root_repository(), artist or {})
    if not artist or not artist_mbid:
        raise ValueError("Scan Lidarr first, then review this artist’s MusicBrainz link.")
    jobs.progress("Refreshing MusicBrainz metadata for " + artist["name"])
    data = mb_get("artist/" + artist_mbid, {"inc": "annotation+tags"})
    put("library_artist_metadata", artist_mbid, data)
    jobs.progress("Refreshing MusicBrainz album catalog for " + artist["name"])
    offset = 0
    release_groups = []
    while offset < 500:
        # Release groups do not have a status of their own. The indexed status
        # field means at least one release in the group is Official.
        page = mb_get(
            "release-group",
            {"query": f"arid:{artist_mbid} AND status:official", "limit": 100, "offset": offset},
        )
        groups = page.get("release-groups", [])
        release_groups.extend(groups)
        offset += len(groups)
        if not groups or offset >= int(page.get("release-group-count") or len(release_groups)):
            break
    put(
        "library_artist_catalog",
        artist_mbid,
        {
            "checked_at": __import__("time").time(),
            "rows": [
                {
                    "id": r.get("id"),
                    "title": r.get("title", ""),
                    "first_release_date": r.get("first-release-date", ""),
                    "primary_type": r.get("primary-type") or "Other",
                    "secondary_types": r.get("secondary-types", []),
                    "official": True,
                }
                for r in release_groups
            ],
        },
    )
    if settings()["itunes_enrichment"]:
        jobs.progress("Refreshing iTunes metadata for " + artist["name"])
        reviewed_apple = (
            root_repository().load("library_artist_links").get(aid, {}).get("itunes_id")
        )
        enrich(
            data,
            [a["title"] for a in inv.get("rows", []) if str(a.get("artist_id")) == aid],
            reviewed_apple,
        )
    from .metadata_sources import settings as metadata_settings, order as metadata_order

    wanted = metadata_settings(root_repository())
    try:
        if any(
            "fanart" in metadata_order(wanted, field)
            for field in ("artist_portrait", "artist_hero", "album_artwork")
        ):
            from .fanart import configured as fanart_configured, lookup as fanart_lookup

            if fanart_configured(root_repository()):
                jobs.progress("Refreshing Fanart.tv artwork for " + artist["name"])
                fanart_lookup(root_repository(), "artist", artist_mbid, True)
    except Exception as exc:
        provider_failures.append("Fanart.tv")
        jobs.output(f"Fanart.tv artwork was not refreshed: {exc}")
    try:
        if any(
            "lastfm" in metadata_order(wanted, field)
            for field in ("artist_description", "genres", "popular_tracks")
        ):
            from .discover import lastfm

            jobs.progress("Refreshing Last.fm artist information for " + artist["name"])
            info = lastfm(
                "artist.getInfo", artist=artist["name"], mbid=artist_mbid, autocorrect=1
            ).get("artist", {})
            import re
            import html

            summary = html.unescape(re.sub(r"<[^>]+>", " ", info.get("bio", {}).get("summary", "")))
            summary = re.sub(
                r"\s+", " ", re.sub(r"Read more on Last\.fm.*$", "", summary, flags=re.I)
            ).strip()
            popular = []
            if "lastfm" in metadata_order(wanted, "popular_tracks"):
                top = lastfm(
                    "artist.getTopTracks",
                    artist=artist["name"],
                    mbid=artist_mbid,
                    autocorrect=1,
                    limit=10,
                )
                popular = [
                    {
                        "name": t.get("name", ""),
                        "playcount": int(t.get("playcount") or 0),
                        "listeners": int(t.get("listeners") or 0),
                        "url": t.get("url", ""),
                    }
                    for t in top.get("toptracks", {}).get("track", [])
                    if t.get("name")
                ]
            put(
                "library_lastfm_artist_metadata",
                artist_mbid,
                {
                    "description": summary,
                    "genres": [
                        t.get("name", "")
                        for t in info.get("tags", {}).get("tag", [])
                        if t.get("name")
                    ],
                    "url": info.get("url", ""),
                    "popular_tracks": popular,
                },
            )
    except Exception as exc:
        provider_failures.append("Last.fm")
        jobs.output(f"Last.fm supplemental metadata was not refreshed: {exc}")
    return {
        "failed_providers": provider_failures,
        "summary": f"Metadata refreshed with {len(release_groups)} MusicBrainz album identities. Lidarr remains the library authority.",
    }


def refresh_all_metadata(payload=None):
    from .api import _config
    from .inventory import current
    from . import jobs

    config = _config(read_only=True, namespaces=[])
    _, inv = current(config.repository, config)
    artists = inv.get("artists", [])
    if not artists:
        raise ValueError("Run a Lidarr Library Scan before refreshing album metadata.")
    from .accounts import root_repository, actor
    from .metadata_sources import settings as metadata_settings
    from . import metadata_refresh as batch
    import hashlib
    import json
    import time

    repo = root_repository()
    ctx = jobs.current()
    previous = repo.load_one(batch.NAMESPACE, "run", {})
    from .lidarr_requests import server_id
    from .lidarr import config as lidarr_config

    fingerprint = hashlib.sha256(
        json.dumps(
            [
                server_id(lidarr_config(repo)),
                metadata_settings(repo),
                settings().get("itunes_enrichment"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    resume = previous.get("fingerprint") == fingerprint and previous.get("status") != "completed"
    done = dict(previous.get("done", {})) if resume else {}
    links = repo.load("library_artist_links")
    identities = {
        str(a.get("id", "")): hashlib.sha256(
            json.dumps(
                [
                    links.get(str(a.get("id")), {}).get("musicbrainz_id") or a.get("mbid", ""),
                    links.get(str(a.get("id")), {}).get("itunes_id"),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        for a in artists
    }
    done = {aid: mbid for aid, mbid in done.items() if identities.get(aid) == mbid}
    state = {
        "fingerprint": fingerprint,
        "done": done,
        "failed": [],
        "status": "running",
        "job_id": ctx.id if ctx else None,
        "account_id": None
        if not ctx or ctx.store.repository.directory == repo.directory
        else int(actor()["id"]),
        "published": batch.generations(repo),
        "published_at": time.time(),
    }
    batch.save(repo, state)
    failed = []
    resumed = len(done)

    def result():
        return {
            "summary": f"Refreshed metadata for {len(done)} of {len(artists)} artists. {len(artists) - len(done)} remaining.",
            "artists": len(artists),
            "completed": len(done),
            "resumed": resumed,
            "remaining": len(artists) - len(done),
            "failed": failed,
        }

    try:
        for artist in artists:
            aid = str(artist.get("id", ""))
            if aid in done:
                continue
            jobs.progress(
                f"Refreshing artist metadata · {len(done)} of {len(artists)} complete · {artist.get('name', '')}",
                completed=len(done),
                total=len(artists),
            )
            try:
                value = refresh_metadata({"artist_id": aid})
                if value.get("failed_providers"):
                    failed.append(
                        {"artist": artist.get("name", ""), "providers": value["failed_providers"]}
                    )
                else:
                    done[aid] = identities[aid]
            except Exception as exc:
                # Detailed errors belong to the redacted Activity log, not durable state.
                failed.append(
                    {
                        "artist": artist.get("name", ""),
                        "error": "Metadata refresh failed; see Activity.",
                    }
                )
                jobs.output(f"Could not refresh {artist.get('name', 'Unknown artist')}: {exc}")
            state.update(done=done, failed=failed)
            batch.publish(repo, state)
            if ctx:
                ctx.store.update(ctx.id, result=result())
        state["status"] = "retry" if failed else "completed"
    except BaseException:
        state["status"] = "interrupted" if ctx and ctx.stop.is_set() else "cancelled"
        raise
    finally:
        batch.publish(repo, state, force=True)
        if ctx:
            ctx.store.update(ctx.id, result=result())
    return result()


def register(app):
    from .accounts import actor, root_repository
    from .lidarr import state_put
    from .api import job_store

    def admin():
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Administrator access required")

    @app.get("/api/settings/file-tools")
    def read():
        admin()
        return settings()

    @app.put("/api/settings/file-tools")
    def save(value: Settings):
        admin()
        previous = settings()
        state_put(root_repository(), "file_tools", "settings", value.model_dump())
        if value.audit_enabled and not previous.get("audit_enabled"):
            from . import tasks

            if tasks.local_scan_ready():
                tasks.restore_defaults(job_store(), "file_audit")
        return settings()

    @app.post("/api/library/artists/{artist_id}/metadata", status_code=202)
    def refresh(artist_id: str):
        admin()
        store = job_store()
        return store.get(store.enqueue("library_metadata", {"artist_id": artist_id}))

    class AuditRequest(BaseModel):
        verify_duplicates: bool = False

    @app.post("/api/library/file-audit", status_code=202)
    def audit(body: AuditRequest):
        admin()
        if not settings()["audit_enabled"]:
            raise HTTPException(409, "Enable local file scanning in Library Management first.")
        store = job_store()
        action = "file_audit_duplicates" if body.verify_duplicates else "file_audit"
        return store.get(store.enqueue(action, {}))

    class Review(BaseModel):
        intentional: bool

    @app.put("/api/library/issues/{issue_id}/review")
    def review(issue_id: str, body: Review):
        admin()
        import re
        import time

        if not re.fullmatch("[0-9a-f]{64}", issue_id):
            raise HTTPException(422, "Invalid issue identifier")
        state_put(
            root_repository(),
            "file_issue_reviews",
            issue_id,
            {"intentional": body.intentional, "reviewed_at": time.time()},
        )
        return {"saved": True}
