"""Last.fm discovery and user-safe album requests using server defaults."""

import json
import uuid
import hashlib
import time
import threading
from . import network
from difflib import SequenceMatcher
from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal
from .accounts import root_repository, actor, as_user, put
from .lidarr_downloads import Action as DownloadAction
from .saved_views import saved, DISCOVER, REQUESTS

_lock = threading.Lock()
HISTORY_DEFAULTS = {
    "history_days": 180,
    "history_min_plays": 2,
    "history_max_suggestions": 100,
    "history_sort": "recent",
}
HISTORY_PAGE_SIZE = 200
TRENDING_ARTIST_LIMIT = 8
TRENDING_ALBUMS_PER_ARTIST = 8


def lastfm_settings():
    return {**HISTORY_DEFAULTS, **root_repository().load("lastfm").get("settings", {})}


def _lastfm_cache_key(method, params):
    return hashlib.sha256(repr((method, sorted(params.items()))).encode()).hexdigest()


def cached_lastfm(method, **params):
    """Return saved Last.fm data without making an external request.

    Discover pages use this path so opening the UI cannot start a large batch
    of Last.fm lookups. The scheduled/manual refresh job owns that work.
    Expired data remains useful until the next successful refresh.
    """
    entry = root_repository().load_one("lastfm_cache", _lastfm_cache_key(method, params), {})
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
        return None
    return entry["data"]


def lastfm(method, refresh=False, **params):
    repo = root_repository()
    settings = lastfm_settings()
    key = settings.get("api_key", "")
    if not key:
        raise HTTPException(
            409, "Last.fm is not configured. Ask the administrator to add an API key."
        )
    cache_key = _lastfm_cache_key(method, params)
    cache = repo.load_one("lastfm_cache", cache_key, {})
    if not refresh and cache.get("expires", 0) > time.time():
        return cache["data"]
    with _lock:
        cache = repo.load_one("lastfm_cache", cache_key, {})
        if not refresh and cache.get("expires", 0) > time.time():
            return cache["data"]
        try:
            response = network.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={"method": method, "api_key": key, "format": "json", **params},
                timeout=(5, 15),
                _service="Last.fm",
            )
            response.raise_for_status()
            data = response.json()
        except (network.RequestException, ValueError) as exc:
            repo.add_log("ERROR", "Last.fm", f"{method}: lookup failed ({type(exc).__name__})")
            raise HTTPException(
                503, "Last.fm is temporarily unavailable. Try again later."
            ) from exc
        if data.get("error"):
            repo.add_log("ERROR", "Last.fm", f"{method}: service error {data['error']}")
            raise HTTPException(
                502, "Last.fm could not complete this lookup. Check the API key and username."
            )
        from .lidarr import state_put

        now = time.time()
        state_put(
            repo,
            "lastfm_cache",
            cache_key,
            {"cached_at": now, "expires": now + 21600, "data": data},
        )
        repo.add_log("INFO", "Last.fm", f"{method}: lookup completed; cached for six hours")
        return data


def _history_params(username):
    settings = lastfm_settings()
    days = int(settings.get("history_days", 180) or 0)
    params = {"user": username, "limit": HISTORY_PAGE_SIZE, "extended": 1}
    if days:
        params["from"] = int((time.time() - days * 86400) // 86400 * 86400)
    return params


def _history_key(username):
    return hashlib.sha256(
        repr((username.casefold(), int(lastfm_settings().get("history_days", 180) or 0))).encode()
    ).hexdigest()


def _saved_history(username, manifest):
    repo = root_repository()
    pages = repo.load("lastfm_history_pages")
    cutoff = _history_params(username).get("from", 0)
    tracks = []
    for batch in manifest.get("batches", []):
        for page in range(1, batch["pages"] + 1):
            for row in pages.get(f"{batch['id']}:{page}", []):
                if not isinstance(row, dict):
                    continue
                date = row.get("date", {})
                try:
                    stamp = int(date.get("uts", 0)) if isinstance(date, dict) else 0
                except (TypeError, ValueError):
                    stamp = 0
                if stamp >= cutoff:
                    tracks.append(row)
    return tracks


def history_tracks(username, refresh=False):
    """Fetch every page with durable checkpoints and incremental updates."""
    from .lidarr import state_put
    from .jobs import progress

    repo = root_repository()
    key = _history_key(username)
    manifest = repo.load("lastfm_history_sync").get(key, {})
    pending = manifest.get("pending")
    if not pending:
        params = _history_params(username)
        # Freeze the upper bound so new scrobbles cannot shift pagination.
        upper = int(time.time()) - 1
        lower = (
            max(params.get("from", 0), int(manifest.get("watermark", 0)) + 1)
            if manifest.get("batches")
            else params.get("from", 0)
        )
        if lower > upper:
            return _saved_history(username, manifest), False
        pending = {
            "id": uuid.uuid4().hex,
            "from": lower,
            "to": upper,
            "next_page": 1,
            "total_pages": 1,
            "plays": 0,
        }
        manifest = {**manifest, "pending": pending}
        state_put(repo, "lastfm_history_sync", key, manifest)
    while pending["next_page"] <= pending["total_pages"]:
        page = pending["next_page"]
        progress(
            f"Last.fm history for {username}: fetching page {page} · {pending['plays']} plays saved"
        )
        data = lastfm(
            "user.getRecentTracks",
            refresh=True,
            user=username,
            limit=HISTORY_PAGE_SIZE,
            extended=1,
            page=page,
            **{"from": pending["from"], "to": pending["to"]},
        )
        recent = data.get("recenttracks", {}) if isinstance(data, dict) else {}
        if not isinstance(recent, dict):
            raise HTTPException(
                502, "Last.fm returned invalid listening history. Refresh again to resume."
            )
        rows = recent.get("track", [])
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise HTTPException(
                502, "Last.fm returned invalid listening history. Refresh again to resume."
            )
        # Now-playing entries have no timestamp and are not completed scrobbles.
        rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("date"), dict)
            and row["date"].get("uts")
        ]
        attr = recent.get("@attr", {})
        try:
            total_pages = max(1, int(attr.get("totalPages", 1))) if isinstance(attr, dict) else 1
        except (TypeError, ValueError):
            raise HTTPException(
                502, "Last.fm returned invalid pagination. Refresh again to resume."
            )
        state_put(repo, "lastfm_history_pages", f"{pending['id']}:{page}", rows)
        pending = {
            **pending,
            "next_page": page + 1,
            "total_pages": total_pages,
            "plays": pending["plays"] + len(rows),
        }
        manifest = {**manifest, "pending": pending}
        state_put(repo, "lastfm_history_sync", key, manifest)
        progress(
            f"Last.fm history for {username}: page {page}/{total_pages} · {pending['plays']} plays saved",
            completed=page,
            total=total_pages,
        )
    manifest = {
        **manifest,
        "batches": [
            *manifest.get("batches", []),
            {"id": pending["id"], "pages": pending["next_page"] - 1},
        ],
        "watermark": pending["to"],
        "updated_at": time.time(),
    }
    manifest.pop("pending", None)
    state_put(repo, "lastfm_history_sync", key, manifest)
    return _saved_history(username, manifest), False


def cached_history_tracks(username):
    """Keep the last completed snapshot visible during a background refresh."""
    manifest = root_repository().load("lastfm_history_sync").get(_history_key(username), {})
    if manifest.get("batches"):
        return _saved_history(username, manifest), False, True
    # Preserve the previous release's saved data until the first full sync finishes.
    params = _history_params(username)
    tracks = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        data = cached_lastfm("user.getRecentTracks", page=page, **params)
        if not isinstance(data, dict):
            break
        recent = data.get("recenttracks", {})
        if not isinstance(recent, dict):
            break
        rows = recent.get("track", [])
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            break
        tracks.extend(row for row in rows if isinstance(row, dict))
        attr = recent.get("@attr", {})
        try:
            total_pages = max(1, int(attr.get("totalPages", 1))) if isinstance(attr, dict) else 1
        except (TypeError, ValueError):
            break
        page += 1
    return tracks, False, False


def refresh_lastfm():
    """Refresh the signed-in user's Discovery feeds without scanning libraries."""
    from .user_preferences import preferences
    from .jobs import progress

    username = preferences().get("lastfm_username", "").strip()
    if not username:
        return {"summary": "Last.fm refresh skipped — no username is linked to this account"}
    progress(f"Refreshing Last.fm listening history for {username}")
    recent_rows, truncated = history_tracks(username, refresh=True)
    progress(f"Preparing personalized album recommendations for {username}")
    top = lastfm("user.getTopArtists", refresh=True, user=username, period="3month", limit=5)
    seeds = top.get("topartists", {}).get("artist", [])
    if isinstance(seeds, dict):
        seeds = [seeds]
    suggestions = {}
    seed_names = {str(seed.get("name", "")).casefold() for seed in seeds if isinstance(seed, dict)}
    for seed in seeds:
        if not isinstance(seed, dict) or not seed.get("name"):
            continue
        similar = lastfm("artist.getSimilar", artist=seed["name"], limit=4)
        candidates = similar.get("similarartists", {}).get("artist", [])
        if isinstance(candidates, dict):
            candidates = [candidates]
        for candidate in candidates:
            name = candidate.get("name", "") if isinstance(candidate, dict) else ""
            if not name or name.casefold() in seed_names:
                continue
            try:
                score = float(candidate.get("match", 0))
            except (TypeError, ValueError):
                score = 0
            if name.casefold() not in suggestions or score > suggestions[name.casefold()]["score"]:
                suggestions[name.casefold()] = {"name": name, "score": score, "seed": seed["name"]}
    recommended = []
    seen = set()
    for candidate in sorted(suggestions.values(), key=lambda row: row["score"], reverse=True)[:10]:
        albums = (
            lastfm("artist.getTopAlbums", artist=candidate["name"], limit=3)
            .get("topalbums", {})
            .get("album", [])
        )
        if isinstance(albums, dict):
            albums = [albums]
        for album in albums:
            if not isinstance(album, dict):
                continue
            row = album_row(album)
            identity = (row["artist"].casefold(), row["album"].casefold())
            if not row["album"] or identity in seen:
                continue
            seen.add(identity)
            row["recommendation_reason"] = f"Because you listen to {candidate['seed']}"
            recommended.append(row)
    from .lidarr import state_put

    state_put(
        root_repository(),
        "lastfm_recommendations",
        username.casefold(),
        {"rows": recommended, "updated_at": time.time()},
    )
    progress("Refreshing Last.fm discovery recommendations")
    chart = lastfm("chart.getTopArtists", refresh=True, limit=TRENDING_ARTIST_LIMIT)
    artists = chart.get("artists", {}).get("artist", []) if isinstance(chart, dict) else []
    if isinstance(artists, dict):
        artists = [artists]
    for row in artists if isinstance(artists, list) else []:
        name = row.get("name", "") if isinstance(row, dict) else ""
        if name:
            lastfm(
                "artist.getTopAlbums", refresh=True, artist=name, limit=TRENDING_ALBUMS_PER_ARTIST
            )
    state_put(
        root_repository(), "lastfm_feed_refresh", username.casefold(), {"updated_at": time.time()}
    )
    state_put(root_repository(), "lastfm_feed_refresh", "trending", {"updated_at": time.time()})
    return {
        "summary": f"Last.fm refreshed for {username}: {len(recent_rows)} recent plays and {len(recommended)} personalized album recommendations cached"
    }


def album_row(a):
    artist = a.get("artist", "")
    images = a.get("image", [])
    image = next(
        (i.get("#text", "") for i in reversed(images) if i.get("#text", "").startswith("https://")),
        "",
    )
    tracks = a.get("tracks", {}).get("track", []) if isinstance(a.get("tracks"), dict) else []
    if isinstance(tracks, dict):
        tracks = [tracks]
    return {
        "album": a.get("name", ""),
        "artist": artist.get("name", "") if isinstance(artist, dict) else artist,
        "mbid": a.get("mbid", ""),
        "artwork": image,
        "artist_mbid": artist.get("mbid", "") if isinstance(artist, dict) else "",
        "tracks": [t.get("name", "") for t in tracks if isinstance(t, dict) and t.get("name")],
    }


def recent_album_rows(tracks):
    """Collapse Last.fm recent tracks into album rows while retaining play evidence."""
    grouped = {}
    for track in tracks:
        if not isinstance(track, dict):
            continue
        attributes = track.get("@attr")
        if (
            isinstance(attributes, dict)
            and str(attributes.get("nowplaying", "")).casefold() == "true"
        ):
            continue
        artist = track.get("artist") or {}
        album = track.get("album") or {}
        artist_name = (
            (artist.get("name") or artist.get("#text", ""))
            if isinstance(artist, dict)
            else str(artist)
        )
        album_name = album.get("#text", "") if isinstance(album, dict) else str(album)
        if not artist_name or not album_name:
            continue
        images = track.get("image", [])
        image = next(
            (
                i.get("#text", "")
                for i in reversed(images)
                if isinstance(i, dict) and i.get("#text", "").startswith("https://")
            ),
            "",
        )
        album_mbid = album.get("mbid", "") if isinstance(album, dict) else ""
        # Last.fm credits soundtrack/compilation tracks to each performer. A
        # stable album ID, or the shared album title and artwork, represents
        # one album instead of many false gaps.
        identity = (
            ("mbid", album_mbid.casefold())
            if album_mbid
            else ("album", album_name.casefold(), image.casefold())
        )
        key = identity
        row = grouped.setdefault(
            key,
            {
                "artist": artist_name,
                "album": album_name,
                "artist_mbid": artist.get("mbid", "") if isinstance(artist, dict) else "",
                "mbid": album_mbid,
                "artists": [],
                "artwork": "",
                "tracks": [],
                "plays": 0,
                "last_played": 0,
                "_artist_plays": {},
                "_artist_tracks": {},
            },
        )
        if artist_name not in row["artists"]:
            row["artists"].append(artist_name)
        title = track.get("name", "")
        if title and title not in row["tracks"]:
            row["tracks"].append(title)
        row["_artist_plays"][artist_name] = row["_artist_plays"].get(artist_name, 0) + 1
        if title:
            row["_artist_tracks"].setdefault(artist_name, set()).add(title.casefold())
        row["artwork"] = row["artwork"] or image
        row["plays"] += 1
        date = track.get("date")
        try:
            row["last_played"] = max(
                row["last_played"], int(date.get("uts", 0) if isinstance(date, dict) else 0)
            )
        except (TypeError, ValueError):
            pass
    for row in grouped.values():
        ranked = sorted(
            row["_artist_plays"],
            key=lambda name: (
                len(row["_artist_tracks"].get(name, set())),
                row["_artist_plays"][name],
            ),
            reverse=True,
        )
        # Some extended Last.fm responses put the album name in the artist
        # slot for part of the history. Do not let that synthetic credit beat
        # a real performer observed on the same album.
        from .matching import Matcher

        album_identity = Matcher._normalize_match_text(row.get("album", ""))
        real_ranked = [
            name for name in ranked if Matcher._normalize_match_text(name) != album_identity
        ]
        if real_ranked:
            ranked = real_ranked
            row["artists"] = [
                name
                for name in row["artists"]
                if Matcher._normalize_match_text(name) != album_identity
            ]
        if ranked:
            leader = ranked[0]
            leader_tracks = len(row["_artist_tracks"].get(leader, set()))
            total_tracks = max(1, len(row["tracks"]))
            # A featured guest should not turn an ordinary artist album into
            # Various Artists. Reserve that label for a true split where no
            # credited performer owns a majority of the observed tracks.
            row["artist"] = (
                leader
                if len(ranked) == 1 or leader_tracks > total_tracks / 2
                else "Various Artists"
            )
        row.pop("_artist_plays", None)
        row.pop("_artist_tracks", None)
        seed = row.get("mbid") or repr(
            (row.get("album", "").casefold(), row.get("artwork", "").casefold())
        )
        row["history_key"] = hashlib.sha256(seed.encode()).hexdigest()
    return list(grouped.values())


def _history_candidates(library, row):
    """Rank existing Harmonia Library albums; no separate service catalogs."""
    candidates = _album_link_candidates(
        library, "", row.get("artist", ""), row.get("album", ""), row.get("tracks") or []
    )
    for candidate in candidates:
        candidate.update(
            _history_match_evidence(
                row,
                candidate.get("title"),
                candidate.get("artist"),
                candidate.pop("_track_records", []),
            )
        )
    return sorted(candidates, key=lambda value: -(value.get("score") or 0))[:100]


def _similarity(left, right):
    from .matching import Matcher

    left, right = (
        Matcher._normalize_match_text(left or ""),
        Matcher._normalize_match_text(right or ""),
    )
    if not left or not right:
        return 0
    return round(SequenceMatcher(None, left, right).ratio() * 100)


def _canonical_album(value):
    """Normalize an album identity while ignoring provider release labels."""
    from .matching import Matcher
    from .lidarr import _discovery_title

    return Matcher._normalize_match_text(_discovery_title(value or ""))


def _album_lookup_title(value):
    canonical = _canonical_album(value)
    return canonical[4:] if canonical.startswith("the ") else canonical


def _artist_similarity(source_artists, candidate):
    """Compare album credits, allowing a named artist inside a group credit."""
    from .matching import Matcher

    right = Matcher._normalize_match_text(candidate or "")
    if not right:
        return 0
    best = 0
    for source in source_artists:
        left = Matcher._normalize_match_text(source or "")
        if not left:
            continue
        if left == right:
            score = 100
        elif min(len(left), len(right)) >= 4 and (left in right or right in left):
            score = 94
        else:
            score = round(SequenceMatcher(None, left, right).ratio() * 100)
        best = max(best, score)
    return best


def _history_match_evidence(row, title, artist, candidate_tracks):
    """Score a library album against Last.fm album, artist and track evidence."""
    left, right = _canonical_album(row.get("album")), _canonical_album(title)
    album_score = 100 if left and left == right else _similarity(left, right)
    credits = list(row.get("artists") or [])
    if row.get("artist") and row.get("artist") not in credits:
        credits.append(row["artist"])
    artist_score = _artist_similarity(credits, artist)
    comparison = _track_comparison(row.get("tracks") or [], candidate_tracks)
    matches = sum(1 for item in comparison if item["matched"])
    track_score = (
        round(sum(item["score"] for item in comparison) / len(comparison)) if comparison else 0
    )
    # A leading article is equivalent only with corroborating artist AND song
    # evidence. Never erase it globally or weaken ambiguous-album review.
    without_article = lambda value: value[4:] if value.startswith("the ") else value
    if (
        left
        and without_article(left) == without_article(right)
        and artist_score >= 82
        and matches > 0
    ):
        album_score = 100
    punctuation = lambda value: value.rstrip("!?.,").strip()
    if (
        left
        and punctuation(left)
        and punctuation(left) == punctuation(right)
        and artist_score == 100
        and matches > 0
    ):
        album_score = 100
    # Album title plus a compatible credited artist is enough. Track evidence
    # can resolve compilation and collaboration credits, but requires two
    # recordings so a common album title cannot create a false match.
    accepted = bool(album_score >= 94 and (artist_score >= 82 or matches >= 2))
    score = round(album_score * 0.45 + artist_score * 0.25 + track_score * 0.30)
    return {
        "accepted": accepted,
        "score": score,
        "album_score": album_score,
        "artist_score": artist_score,
        "track_score": track_score,
        "matched_tracks": matches,
        "source_track_count": len(comparison),
        "tracks": comparison,
    }


def _best_history_match(row, candidates, allow_ties=False):
    """Return one strong candidate; close ties stay unresolved for review."""
    ranked = []
    for candidate in candidates:
        evidence = _history_match_evidence(
            row, candidate.get("title"), candidate.get("artist"), candidate.get("tracks") or []
        )
        if evidence["accepted"]:
            ranked.append((evidence["score"], evidence, candidate))
    ranked.sort(key=lambda item: -item[0])
    if not ranked:
        return None
    if not allow_ties and len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 5:
        return None
    return ranked[0][2], ranked[0][1]


def _track_comparison(source_titles, candidate_tracks):
    """Pair source tracks to distinct Lidarr tracks and expose the evidence to the UI."""
    remaining = list(candidate_tracks)
    result = []
    for source in source_titles[:200]:
        choices = [
            (index, _similarity(source, row.get("title", "")))
            for index, row in enumerate(remaining)
        ]
        index, score = max(choices, key=lambda value: value[1], default=(-1, 0))
        candidate = remaining.pop(index) if index >= 0 else None
        result.append(
            {
                "source": source,
                "candidate": candidate.get("title") if candidate else None,
                "score": score,
                "matched": score >= 82,
                "disc_number": candidate.get("disc_number") if candidate else None,
                "track_number": candidate.get("track_number") if candidate else None,
            }
        )
    return result


def _album_link_candidates(library, query="", artist="", album="", source_tracks=None):
    """Use the same album identity, artwork and evidence as Harmonia Library."""
    terms = query.casefold().split()
    candidates = [
        row
        for row in library
        if not terms
        or all(
            term in (str(row.get("artist", "")) + " " + str(row.get("title", ""))).casefold()
            for term in terms
        )
    ]
    if not terms and album:
        canonical = _canonical_album(album)
        lambda value: value[4:] if value.startswith("the ") else value
        # Rank artist and album together before limiting the evidence work. An
        # exact title from a different artist must not hide the correct album.
        candidates = sorted(
            candidates,
            key=lambda row: -(
                _similarity(canonical, _canonical_album(row.get("title"))) * 2
                + _artist_similarity([artist], row.get("artist"))
            ),
        )[:100]
    result = []
    for row in candidates:
        tracks = row.get("tracks") or []
        evidence = _history_match_evidence(
            {"album": album, "artist": artist, "tracks": source_tracks or []},
            row.get("title"),
            row.get("artist"),
            tracks,
        )
        stats = row.get("statistics") or {}
        result.append(
            {
                **{
                    k: row.get(k)
                    for k in (
                        "album_id",
                        "library_album_id",
                        "target_type",
                        "target_id",
                        "plex_target_id",
                        "plex_present",
                        "plex_linked",
                        "title",
                        "artist",
                        "artwork",
                        "year",
                        "type",
                        "monitored",
                        "status",
                    )
                },
                "lidarr_id": row.get("id"),
                "state": row.get("library_state") or row.get("status") or "Unknown",
                "files": int(stats.get("trackFileCount") or 0),
                "track_count": int(stats.get("trackCount") or len(tracks)),
                "selected_release": next(
                    (release for release in row.get("releases", []) if release.get("monitored")),
                    None,
                ),
                **evidence,
                "_track_records": tracks,
                "candidate_tracks": len(tracks),
            }
        )
    return sorted(result, key=lambda row: -(row.get("score") or 0))[:100]


def _library_link_candidates(query="", artist="", album="", source_tracks=None):
    from .library import link_targets

    return [
        {k: v for k, v in row.items() if k != "_track_records"}
        for row in _album_link_candidates(link_targets(), query, artist, album, source_tracks)
    ]


def album_association_key(artist, album):
    from .matching import Matcher

    return hashlib.sha256(
        repr((_canonical_album(album), Matcher._normalize_match_text(artist or ""))).encode()
    ).hexdigest()


def availability(rows, apply_preferences=True, deduplicate=True):
    if not rows:
        return []
    from .matching import Matcher
    from .config import Config
    from .library import cached_link_library, link_targets

    Config(read_only=True, namespaces=[])
    model = cached_link_library()
    targets = link_targets(model)
    # All matching catalogs come from the unified Library. Underlying snapshot
    # metadata supplies scan freshness and queue status only.
    plex = {
        **model["_plex"],
        "rows": [track for target in targets for track in target.get("plex_tracks", [])],
    }
    lidarr = {
        **model["_lidarr"],
        "rows": [target for target in targets if target["target_type"] == "lidarr"],
        "tracks": [
            track
            for target in targets
            if target["target_type"] == "lidarr"
            for track in target.get("tracks", [])
        ],
    }
    normalize = Matcher._normalize_match_text

    def key(artist, album):
        return normalize(artist), normalize(album)

    p = {}
    plex_groups = {}
    for track in plex.get("rows", []):
        p.setdefault(
            key(track.get("album_artist") or track.get("artist", ""), track.get("album", "")), []
        ).append(track)
        group = str(track.get("plex_album_id") or "") or repr(
            key(track.get("album_artist") or track.get("artist", ""), track.get("album", ""))
        )
        plex_groups.setdefault(group, []).append(track)
    l = {}
    for t in lidarr.get("rows", []):
        l.setdefault(key(t.get("artist", ""), t.get("title", "")), []).append(t)
    lidarr_tracks = {}
    for track in lidarr.get("tracks", []):
        lidarr_tracks.setdefault(track.get("album_id"), []).append(track)
    plex_titles = {}
    lidarr_titles = {}
    for group_tracks in plex_groups.values():
        first = group_tracks[0]
        plex_titles.setdefault(_album_lookup_title(first.get("album")), []).append(
            {
                "title": first.get("album"),
                "artist": first.get("album_artist") or first.get("artist"),
                "tracks": group_tracks,
            }
        )
    for candidate in lidarr.get("rows", []):
        lidarr_titles.setdefault(_album_lookup_title(candidate.get("title")), []).append(
            {
                "title": candidate.get("title"),
                "artist": candidate.get("artist"),
                "tracks": lidarr_tracks.get(candidate.get("album_id"), []),
                "album": candidate,
            }
        )
    from .lidarr import config as lidarr_config

    base = lidarr_config(root_repository()).get("url", "").rstrip("/")
    from .user_preferences import blocked, preferences

    prefs = preferences()
    bridge = model["_ctx"] if "tracks" in lidarr else None
    plex_by_id = {str(t["plex_id"]): t for t in plex.get("rows", [])}
    album_links = (
        root_repository().load_one("plex_album_links", bridge.get("scope", ""), {})
        if bridge
        else {}
    )
    associations = root_repository().load("album_links")
    from .accounts import personal_repository

    saved_links = personal_repository().load("lastfm_album_links")
    by_mbid = {a.get("album_id"): a for a in lidarr.get("rows", []) if a.get("album_id")}
    # Resolve reusable account-owned evidence once, rather than per history album.
    preference_repo = personal_repository()
    artist_rules = (
        list(preference_repo.load("blocked_artists").values()) if apply_preferences else []
    )
    album_rules = list(preference_repo.load("blocked_albums").values()) if apply_preferences else []
    bridged_by_album = {}
    for track in (bridge or {}).get("tracks", []):
        pid = bridge["links"].get(str(track["id"]), {}).get("plex_id")
        if pid in plex_by_id:
            bridged_by_album.setdefault(track.get("album_id"), {})[pid] = plex_by_id[pid]
    target_by_plex_album = {}
    for target in targets:
        for track in target.get("plex_tracks", []):
            target_by_plex_album.setdefault(str(track.get("plex_album_id")), target)
    seen = set()
    result = []
    lidarr_enabled = bool(lidarr_config(root_repository()).get("enabled"))
    checked_at = (
        min(plex.get("checked_at", 0), lidarr.get("checked_at", 0))
        if lidarr_enabled
        else plex.get("checked_at", 0)
    )
    fresh = bool(checked_at and time.time() - checked_at < 86400)
    for row in rows:
        k = key(row["artist"], row["album"])
        if (
            (deduplicate and k in seen)
            or not all(k)
            or (
                apply_preferences
                and blocked(row, artist_rules=artist_rules, album_rules=album_rules)
            )
        ):
            continue
        seen.add(k)
        saved_link = saved_links.get(album_association_key(row["artist"], row["album"]), {})
        association = associations.get(hashlib.sha256(repr(k).encode()).hexdigest(), {})
        linked_id = association.get("album_id")
        if association.get("target_type") == "plex" and not saved_link:
            saved_link = association
            linked_id = None
        if saved_link.get("target_type") == "lidarr":
            linked_id = saved_link.get("target_id")
        candidates = l.get(k, [])
        exact = [by_mbid[i] for i in (linked_id or row.get("mbid"),) if i in by_mbid]
        album = exact[0] if len(exact) == 1 else candidates[0] if len(candidates) == 1 else None
        tracks = (
            p.get(key(album.get("artist", ""), album.get("title", "")), [])
            if album
            else p.get(k, [])
        )
        canonical = _album_lookup_title(row.get("album"))
        plex_match = _best_history_match(row, plex_titles.get(canonical, []), allow_ties=True)
        lidarr_match = (
            _best_history_match(row, lidarr_titles.get(canonical, [])) if not album else None
        )
        if plex_match:
            tracks = plex_match[0]["tracks"]
        if saved_link.get("target_type") == "plex" and saved_link.get("target_id") in plex_groups:
            tracks = plex_groups[saved_link["target_id"]]
        if lidarr_match:
            album = lidarr_match[0]["album"]
        if album and bridge is not None:
            bridged = list(bridged_by_album.get(album.get("album_id"), {}).values())
            if bridged:
                tracks = bridged
        if album:
            saved_plex_id = str(
                album_links.get(str(album.get("album_id")), {}).get("plex_album_id") or ""
            )
            if saved_plex_id in plex_groups:
                tracks = plex_groups[saved_plex_id]
        ambiguous = bool(
            (linked_id and linked_id not in by_mbid) or (len(candidates) > 1 and not exact)
        )
        status = "Unknown"
        explanation = "Availability needs a current library scan."
        queue = [
            q
            for q in lidarr.get("queue", [])
            if album
            and (
                q.get("albumId") == album.get("id")
                or q.get("album", {}).get("id") == album.get("id")
            )
        ]
        if fresh:
            if ambiguous:
                status = "Needs Attention"
                explanation = "Multiple Lidarr albums share this artist and title; their association needs review."
            elif tracks and album:
                status = "Available"
                explanation = "Confirmed in Plex and linked to Lidarr."
                total = album.get("statistics", {}).get("trackCount")
                if total and len(tracks) < total:
                    status = "Partially Available"
                    explanation = (
                        f"{len(tracks)}/{total} tracks found in Plex; edition counts may differ."
                    )
            elif tracks:
                status = "Needs Attention" if lidarr_enabled else "Available"
                explanation = (
                    "Available in Plex, but not linked to Lidarr."
                    if lidarr_enabled
                    else "Confirmed in Harmonia Library and playable in Plex."
                )
            elif queue:
                from .lidarr_downloads import describe

                states = [describe(q)["status"] for q in queue]
                status = "Importing" if "Importing" in states else "Downloading"
                explanation = "In the active Lidarr download queue."
                if any(x in ("Blocked", "Import blocked", "Failed") for x in states):
                    status = "Needs Attention"
                    explanation = "Lidarr reports a download or import problem."
            elif album:
                status = (
                    "Awaiting Plex"
                    if album.get("statistics", {}).get("trackFileCount", 0) > 0
                    else "Requested"
                )
                explanation = (
                    "Files found in Lidarr; waiting for Plex."
                    if status == "Awaiting Plex"
                    else "Added to Lidarr; no active download or available files."
                )
            else:
                status = "Not Available"
                explanation = "Not found in the latest Plex and Lidarr scans."
        if apply_preferences and prefs.get("hide_available") and fresh and tracks and not ambiguous:
            continue
        from urllib.parse import quote

        first = tracks[0] if tracks else {}
        item = first.get("plex_album_id") or first.get("plex_id")
        machine = plex.get("machine_identifier")
        plex_url = (
            "https://app.plex.tv/desktop/#!/server/"
            + quote(str(machine), safe="")
            + "/details?key="
            + quote("/library/metadata/" + str(item), safe="")
            if machine and item
            else None
        )
        lidarr_url = (
            base + "/album/" + quote(str(album["album_id"]), safe="")
            if actor() and actor().get("admin") and base and album and album.get("album_id")
            else None
        )
        plex_art = (
            (
                "/api/library/plex-albums/"
                + str(first["plex_album_id"])
                + "/image?v="
                + __import__("hashlib")
                .sha256(str(first.get("plex_album_thumb", "")).encode())
                .hexdigest()[:16]
            )
            if first.get("plex_album_id") and first.get("plex_album_thumb")
            else row.get("artwork", "")
        )
        selected_library = album or target_by_plex_album.get(str(item))
        if selected_library:
            plex_art = selected_library.get("artwork") or plex_art
        result.append(
            {
                **row,
                "artwork": plex_art,
                "history_key": row.get("history_key")
                or album_association_key(row["artist"], row["album"]),
                "plex_url": plex_url,
                "plex_present": bool(tracks),
                "lidarr_url": lidarr_url,
                "availability": status,
                "availability_detail": explanation,
                "checked_at": checked_at or None,
                "lidarr_album_id": album.get("album_id") if album else None,
                "monitored": album.get("monitored") if album else None,
                "statistics": album.get("statistics", {}) if album else {},
                "releases": album.get("releases", []) if album else [],
                "any_release_ok": album.get("anyReleaseOk") if album else None,
            }
        )
    return result


def history_present(row):
    """Presence is established by Plex tracks or imported Lidarr files."""
    return bool(
        row.get("plex_present")
        or row.get("availability") == "Available"
        or (
            row.get("lidarr_album_id")
            and int((row.get("statistics") or {}).get("trackFileCount") or 0) > 0
        )
    )


def register(app):
    class Settings(BaseModel):
        api_key: str = Field(default="", max_length=200)
        history_days: Literal[30, 90, 180, 365, 0] = 180
        history_min_plays: int = Field(default=2, ge=1, le=10)
        history_max_suggestions: int = Field(default=100, ge=25, le=200)
        history_sort: Literal["recent", "plays"] = "recent"

    @app.get("/api/settings/lastfm")
    def settings():
        saved = lastfm_settings()
        return {
            "configured": bool(saved.get("api_key")),
            **{key: saved[key] for key in HISTORY_DEFAULTS},
        }

    @app.put("/api/settings/lastfm")
    def save_settings(body: Settings):
        saved = lastfm_settings()
        values = body.model_dump(exclude={"api_key"})
        values["api_key"] = body.api_key.strip() or saved.get("api_key", "")
        put("lastfm", "settings", values)
        return settings()

    @app.get("/api/discover/library-albums")
    @app.get("/api/discover/lidarr-albums")
    def link_candidates(q: str = Query(default="", max_length=200)):
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        return _library_link_candidates(q)

    class LinkSearch(BaseModel):
        query: str = Field(default="", max_length=200)
        artist: str = Field(default="", max_length=300)
        album: str = Field(default="", max_length=300)
        tracks: list[str] = Field(default_factory=list, max_length=200)

    @app.post("/api/discover/library-albums/search")
    @app.post("/api/discover/lidarr-albums/search")
    def search_link_candidates(body: LinkSearch):
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        return _library_link_candidates(body.query, body.artist, body.album, body.tracks)

    class Link(BaseModel):
        artist: str = Field(min_length=1, max_length=300)
        album: str = Field(min_length=1, max_length=300)
        album_id: str | None = Field(default=None, max_length=100)

    @app.put("/api/discover/album-link")
    def link_album(body: Link):
        if not actor() or not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        from .matching import Matcher

        k = tuple(Matcher._normalize_match_text(v) for v in (body.artist, body.album))
        key = hashlib.sha256(repr(k).encode()).hexdigest()
        if body.album_id is None:
            from .accounts import delete

            delete("album_links", key)
        else:
            from .library import link_targets

            selected = next(
                (a for a in link_targets() if a["library_album_id"] == body.album_id), None
            )
            if not selected:
                raise HTTPException(
                    409, "Album not in Harmonia Library. Refresh the library first."
                )
            put(
                "album_links",
                key,
                {
                    "album_id": body.album_id,
                    "target_type": selected["target_type"],
                    "target_id": selected["target_id"],
                    "artist": body.artist,
                    "album": body.album,
                },
            )
        return {"saved": True}

    @app.get("/api/discover")
    @saved("discovery v18", DISCOVER, enabled=lambda p: not (p.get("q") or p.get("artist")))
    def discover(
        q: str = Query(default="", max_length=160),
        username: str = Query(default="", max_length=100),
        artist: str = Query(default="", max_length=200),
        album: str = Query(default="", max_length=200),
        trending: bool = False,
    ):
        from .user_preferences import preferences

        if not username and not trending:
            username = preferences().get("lastfm_username", "")
        refresh_required = False
        if not username and not trending and not q.strip() and not artist:
            return {
                "heading": "Recommended for you",
                "rows": [],
                "connect_required": True,
                "description": "Connect Last.fm in Account to get personalized album suggestions.",
            }
        if artist and album:
            data = lastfm("album.getInfo", artist=artist, album=album)
            return {
                "heading": album,
                "rows": availability([album_row(data.get("album", {}))]),
                "detail": True,
            }
        if artist:
            data = lastfm("artist.getTopAlbums", artist=artist, limit=40)
            return {
                "heading": f"Top albums by {artist}",
                "rows": availability(
                    [album_row(a) for a in data.get("topalbums", {}).get("album", [])]
                ),
            }
        if q.strip():
            data = lastfm("album.search", album=q.strip(), limit=30)
            rows = data.get("results", {}).get("albummatches", {}).get("album", [])
            heading = "Album search"
        elif username.strip():
            data = root_repository().load("lastfm_recommendations").get(username.strip().casefold())
            return {
                "heading": "Recommended for you",
                "rows": availability((data or {}).get("rows", [])),
                "refresh_required": data is None,
                "updated_at": (data or {}).get("updated_at"),
                "description": "Album suggestions based on your listening and similar artists.",
            }
        else:
            chart = cached_lastfm("chart.getTopArtists", limit=TRENDING_ARTIST_LIMIT)
            refresh_required = chart is None
            artists = (chart or {}).get("artists", {}).get("artist", [])
            if isinstance(artists, dict):
                artists = [artists]
            groups = []
            from .user_preferences import blocked

            for artist in artists:
                if blocked({"artist": artist["name"], "artist_mbid": artist.get("mbid", "")}):
                    continue
                albums = cached_lastfm(
                    "artist.getTopAlbums", artist=artist["name"], limit=TRENDING_ALBUMS_PER_ARTIST
                )
                if albums is None:
                    refresh_required = True
                    albums = cached_lastfm("artist.getTopAlbums", artist=artist["name"], limit=2)
                groups.append((albums or {}).get("topalbums", {}).get("album", []))
            rows = [
                group[i]
                for i in range(TRENDING_ALBUMS_PER_ARTIST)
                for group in groups
                if len(group) > i
            ]
            heading = "Albums from trending artists"
        return {
            "heading": heading,
            "rows": availability([album_row(a) for a in rows]),
            "attribution": "Powered by Last.fm",
            "cached_for_hours": 6,
            "refresh_required": refresh_required,
            "updated_at": root_repository()
            .load("lastfm_feed_refresh")
            .get("trending", {})
            .get("updated_at"),
        }

    @app.get("/api/discover/history-gaps")
    @saved("listening history v20261011_77", DISCOVER)
    def history_gaps():
        """Albums heard recently on Last.fm that are not confirmed in the library."""
        from .user_preferences import preferences

        username = preferences().get("lastfm_username", "").strip()
        if not username:
            return {
                "heading": "Missing from your listening history",
                "rows": [],
                "configured": False,
            }
        settings = lastfm_settings()
        tracks, truncated, complete = cached_history_tracks(username)
        albums = recent_album_rows(tracks)
        albums = [
            row
            for row in albums
            if int(row.get("plays") or 0) >= int(settings["history_min_plays"])
        ]
        from .accounts import personal_repository
        from .user_preferences import history_ignored

        ignored = list(personal_repository().load("lastfm_history_ignored").values())
        albums = [row for row in albums if not history_ignored(row, ignored)]
        links = personal_repository().load("lastfm_history_links")
        from .library import link_targets

        targets = link_targets() if links else []
        plex_ids = {str(a.get("target_id")) for a in targets if a.get("target_type") == "plex"}
        plex_ids.update(str(a.get("plex_target_id")) for a in targets if a.get("plex_target_id"))
        lidarr_albums = {
            str(a.get("target_id")): a for a in targets if a.get("target_type") == "lidarr"
        }
        lidarr_ids = set(lidarr_albums)
        linked = []
        unlinked = []
        for row in albums:
            saved = links.get(row["history_key"], {})
            valid = (saved.get("target_type") == "plex" and saved.get("target_id") in plex_ids) or (
                saved.get("target_type") == "lidarr" and saved.get("target_id") in lidarr_ids
            )
            if valid and saved.get("origin") == "request":
                target = lidarr_albums.get(saved.get("target_id"), {})
                valid = bool(int((target.get("statistics") or {}).get("trackFileCount") or 0) > 0)
            (linked if valid else unlinked).append(row)
        checked = availability(unlinked)
        missing = [row for row in checked if not history_present(row)]
        if settings["history_sort"] == "plays":
            missing.sort(key=lambda row: (-(row.get("plays") or 0), -(row.get("last_played") or 0)))
        else:
            missing.sort(key=lambda row: (-(row.get("last_played") or 0), -(row.get("plays") or 0)))
        missing = missing[: int(settings["history_max_suggestions"])]
        return {
            "heading": "Missing from your listening history",
            "rows": missing,
            "configured": True,
            "scrobbles_checked": len(tracks),
            "albums_checked": len(albums),
            "history_days": settings["history_days"],
            "minimum_plays": settings["history_min_plays"],
            "truncated": truncated,
            "refresh_required": not complete,
            "updated_at": root_repository()
            .load("lastfm_feed_refresh")
            .get(username.casefold(), {})
            .get("updated_at"),
        }

    class HistoryLinkSearch(BaseModel):
        history_key: str = Field(min_length=64, max_length=64)
        artist: str = Field(default="", max_length=300)
        album: str = Field(min_length=1, max_length=500)
        tracks: list[str] = Field(default_factory=list, max_length=200)
        artists: list[str] = Field(default_factory=list, max_length=200)

    @app.post("/api/discover/history-gaps/candidates")
    def history_candidates(body: HistoryLinkSearch):
        from .library import link_targets

        return {"rows": _history_candidates(link_targets(), body.model_dump())}

    class HistoryLink(HistoryLinkSearch):
        target_type: str = Field(pattern="^(plex|lidarr)$")
        target_id: str = Field(min_length=1, max_length=100)

    @app.put("/api/discover/history-gaps/link")
    def save_history_link(body: HistoryLink):
        from .accounts import personal_repository
        from .library import link_targets

        valid = any(
            row["target_type"] == body.target_type and row["target_id"] == body.target_id
            for row in link_targets()
        )
        if not valid:
            raise HTTPException(
                409, "That library album is no longer available. Refresh the library and try again."
            )
        from .lidarr import state_put

        state_put(
            personal_repository(),
            "lastfm_history_links",
            body.history_key,
            {
                "target_type": body.target_type,
                "target_id": body.target_id,
                "artist": body.artist,
                "album": body.album,
                "linked_at": time.time(),
            },
        )
        state_put(
            personal_repository(),
            "lastfm_album_links",
            album_association_key(body.artist, body.album),
            {
                "target_type": body.target_type,
                "target_id": body.target_id,
                "artist": body.artist,
                "album": body.album,
                "linked_at": time.time(),
            },
        )
        return {"saved": True}

    def endpoint(path):
        return next(r.endpoint for r in app.routes if getattr(r, "path", "") == path)

    def permitted():
        user = actor()
        if not user or not (user.get("can_request") or user.get("admin")):
            raise HTTPException(403, "Album requests are disabled for this account.")
        from .lidarr import config

        if not config(root_repository()).get("enabled"):
            raise HTTPException(409, "Lidarr is disabled. Album requests are unavailable.")
        return user

    from .member_album_search import install as install_member_search

    install_member_search(app)

    from .history_links import HistorySource

    class Album(BaseModel):
        history_source: HistorySource | None = None
        album_id: uuid.UUID
        title: str = Field(default="", max_length=500)
        artist: str = Field(default="", max_length=500)

    @app.post("/api/requests-user/add", status_code=202)
    def add(body: Album):
        user = permitted()
        if not user.get("admin") and user.get("auto_approve", False) is False:
            key = user["id"] + ":" + str(body.album_id)
            with root_repository().connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT 1 FROM state WHERE namespace='pending_requests' AND key=?", (key,)
                ).fetchone()
                count = db.execute(
                    "SELECT COUNT(*) FROM state WHERE namespace='pending_requests' AND json_extract(value,'$.user_id')=?",
                    (user["id"],),
                ).fetchone()[0]
                if not existing and count >= 25:
                    raise HTTPException(
                        429,
                        "You have 25 requests awaiting approval. Wait for administrator review before requesting more.",
                        headers={"Retry-After": "60"},
                    )
                db.execute(
                    "INSERT OR REPLACE INTO state(namespace,key,value) VALUES('pending_requests',?,?)",
                    (
                        key,
                        json.dumps(
                            {
                                **body.model_dump(mode="json"),
                                "user_id": user["id"],
                                "at": time.time(),
                            }
                        ),
                    ),
                )
            return {
                "queued": False,
                "request_id": str(body.album_id),
                "message": "Request submitted for administrator approval.",
            }
        from .lidarr import Preview, Confirm, config, Defaults

        # The selected release is resolved and reviewed against server defaults.
        # Users cannot override roots, profiles, tags, monitoring or search policy.
        with as_user(None):
            preview = endpoint("/api/lidarr/preview")(
                Preview(
                    album_id=body.album_id, **Defaults(**config(root_repository())).model_dump()
                )
            )
            job = endpoint("/api/lidarr/add")(
                Confirm(
                    preview_id=preview["preview_id"],
                    source_title=body.title,
                    source_artist=body.artist,
                )
            )
        with root_repository().connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM state WHERE namespace='lidarr_requests' AND key=?",
                (str(body.album_id),),
            ).fetchone()
            if row:
                record = json.loads(row[0])
                people = record.get("requesters", [])
                if not any(r.get("user_id") == user["id"] for r in people):
                    people.append(
                        {
                            "user_id": user["id"],
                            "name": user.get("name", ""),
                            "requested_at": time.time(),
                        }
                    )
                    record["requesters"] = people
                    db.execute(
                        "UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?",
                        (json.dumps(record), str(body.album_id)),
                    )
        from .history_links import requested

        requested(body.history_source, body.album_id, job.get("id"))
        put(
            "user_requests",
            user["id"] + ":" + str(body.album_id),
            {"user_id": user["id"], "album_id": str(body.album_id), "at": time.time()},
        )
        return {
            "queued": True,
            "job_id": job.get("id"),
            "request_id": str(body.album_id),
            "message": "Album request queued.",
        }

    def requested_albums(user):
        mine = {
            str(r["album_id"])
            for r in root_repository().load("user_requests").values()
            if r.get("user_id") == user["id"]
        }
        for key, row in root_repository().load("lidarr_requests").items():
            if any(
                r.get("user_id") == user["id"]
                for r in row.get("requesters", [])
                if isinstance(r, dict)
            ):
                mine.add(str(key))
        return mine

    def safe_requests(downloads=False):
        user = actor()
        from .lidarr import config

        if not config(root_repository()).get("enabled"):
            return []
        if downloads and not (
            user.get("admin") or user.get("can_downloads") or user.get("can_manage_downloads")
        ):
            raise HTTPException(403, "Download visibility is disabled for this account.")
        mine = requested_albums(user)
        view_all = bool(user.get("admin") or user.get("can_view_all_requests"))
        with as_user(None):
            records = endpoint("/api/lidarr/requests").__wrapped__(compute_availability=False)
        linked = availability(
            [
                {
                    "artist": r.get("artist", ""),
                    "album": r.get("title", ""),
                    "mbid": r.get("library_album_id") or r["key"],
                    "request_key": r["key"],
                }
                for r in records
                if view_all or r["key"] in mine
            ],
            apply_preferences=False,
            deduplicate=False,
        )
        by_id = {r.get("request_key") or r.get("mbid"): r for r in linked}
        from .diagnostics import concise_error
        from .storage import read_json

        secrets = [
            config(root_repository()).get("api_key"),
            user.get("plex_token"),
            read_json(root_repository().directory / "config.json").get("plex", {}).get("token"),
        ]

        def safe_error(value):
            from .diagnostics import redact

            return concise_error(redact(value or "", secrets=secrets))

        result = []
        for r in records:
            if not view_all and r["key"] not in mine:
                continue
            info = by_id.get(r["key"], {})
            if user.get("admin"):
                from .request_confirmation import apply
                from .lidarr_requests import server_id

                info = apply(r, info, server_id(config(root_repository())))
            status = info.get("availability", "Unknown")
            r = {
                **r,
                "availability": status,
                "availability_detail": info.get(
                    "availability_detail", "Waiting for your Plex library scan."
                ),
                "availability_checked_at": info.get("checked_at"),
                "plex_available": status in ("Available", "Partially Available"),
                "plex_fully_available": status == "Available",
                "plex_checked_at": info.get("checked_at"),
            }
            fields = (
                "plex_fully_available",
                "download_error",
                "download_poll_error",
                "error",
                "finalize_active",
                "finalize_status",
                "finalize_error",
                "key",
                "title",
                "artist",
                "status",
                "active",
                "sources",
                "availability",
                "availability_detail",
                "availability_checked_at",
                "plex_available",
                "plex_checked_at",
                "download_status",
                "download_checked_at",
                "created_at",
                "updated_at",
                "link_checked_at",
                "lidarr_url",
                "lidarr_home_url",
                "lidarr_available",
                "removed_from_lidarr",
                "fulfilled_at",
                "requesters",
                "artwork",
                "year",
                "library_album_id",
                "lidarr_album_mbid",
            )
            value = {k: r.get(k) for k in fields}
            if not user.get("admin"):
                if not view_all:
                    value["requesters"] = [
                        r for r in r.get("requesters", []) if r.get("user_id") == user["id"]
                    ]
                value["sources"] = []
            for field in ("download_error", "download_poll_error", "error", "finalize_error"):
                value[field] = safe_error(value.get(field))
            value["album_id"] = r["key"]
            value["display_status"] = r.get("download_status") or r.get("availability", "Unknown")
            value["detail"] = r.get("availability_detail", "")
            if user.get("admin") or user.get("can_downloads") or user.get("can_manage_downloads"):
                value["downloads"] = [
                    {
                        k: d.get(k)
                        for k in (
                            "queue_id",
                            "download_id",
                            "title",
                            "status",
                            "percent",
                            "remaining",
                            "error",
                            "error_detail",
                        )
                    }
                    for d in r.get("downloads", [])
                ]
                for download in value["downloads"]:
                    for field in ("error", "error_detail"):
                        download[field] = safe_error(download.get(field))
                value["lidarr_id"] = r.get("lidarr_id")
            result.append(value)
        for key, r in root_repository().load("pending_requests").items():
            if view_all or r["user_id"] == user["id"]:
                from .accounts import users

                requester = users().get(str(r["user_id"]), {})
                result.append(
                    {
                        "key": key,
                        "album_id": r["album_id"],
                        "title": r.get("title") or "Album request",
                        "artist": r.get("artist"),
                        "status": "approval",
                        "display_status": "Awaiting approval",
                        "created_at": r.get("at"),
                        "artwork": "https://coverartarchive.org/release-group/"
                        + str(r["album_id"])
                        + "/front-500",
                        "requesters": [
                            {
                                "user_id": str(r["user_id"]),
                                "name": requester.get("name") or "Requester unavailable",
                                "avatar": requester.get("avatar", ""),
                            }
                        ],
                        "sources": [{"title": r.get("title", ""), "artist": r.get("artist", "")}],
                        "downloads": [],
                    }
                )
        return result

    @app.get("/api/requests-user")
    @saved("personal requests", REQUESTS)
    def own_requests():
        return safe_requests()

    @app.get("/api/requests-user/downloads")
    @saved("personal downloads", REQUESTS)
    def downloads():
        return safe_requests(True)

    @app.post("/api/requests-user/{album}/download-action")
    def download_action(album: str, body: DownloadAction):
        if not (actor().get("admin") or actor().get("can_manage_downloads")):
            raise HTTPException(403, "Download management is disabled for this account.")
        if not actor().get("admin") and album not in requested_albums(actor()):
            raise HTTPException(403, "You can manage only your own requests.")
        from .lidarr_downloads import perform
        from .lidarr import config

        with as_user(None):
            return perform(root_repository(), config(root_repository()), album, body)

    @app.get("/api/requests-user/pending")
    def pending():
        if not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        return [{"key": k, **v} for k, v in root_repository().load("pending_requests").items()]

    @app.post("/api/requests-user/pending/{key}/approve")
    def approve(key: str):
        if not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        value = root_repository().load("pending_requests").get(key)
        if not value:
            raise HTTPException(404, "Pending request not found.")
        from .accounts import users, delete

        user = users().get(value["user_id"])
        if not user or user.get("disabled"):
            raise HTTPException(409, "Requestor is disabled or no longer exists.")
        with as_user({**user, "auto_approve": True}):
            result = add(Album(**value))
        delete("pending_requests", key)
        return result

    @app.delete("/api/requests-user/pending/{key}")
    def reject(key: str):
        if not actor().get("admin"):
            raise HTTPException(403, "Administrator access required.")
        from .accounts import delete

        delete("pending_requests", key)
        return {"removed": True}
