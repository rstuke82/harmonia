"""Exact bounded candidate ranking; recording decisions remain in Matcher."""

from .matching import Matcher
from .match_candidates import consolidate


def _row(track, details):
    return {
        "plex_id": str(track.get("plex_id", "")),
        "title": track.get("title", ""),
        "artist": track.get("artist", ""),
        "album": track.get("album", ""),
        "duration": track.get("duration"),
        "source_duration": details.get("source_duration"),
        "track_artist": track.get("track_artist"),
        "album_artist": track.get("album_artist"),
        "track_number": track.get("track_number"),
        "disc_number": track.get("disc_number"),
        "edition": track.get("edition"),
        "_files": track.get("files", []),
        "_guids": track.get("guids", []),
        "_album_id": track.get("plex_album_id"),
        "duration_delta": details.get("duration_delta"),
        "score": round(details["adjusted_score"], 1),
        "identity_score": round(details["identity_score"], 1),
        "title_score": details["title_score"],
        "artist_score": details["artist_score"],
        "artist_credit_reason": details.get("artist_credit_reason", ""),
        "album_score": details["album_score"],
        "album_penalty": details["album_penalty"],
        "title_variant_penalty": details["title_variant_penalty"],
        "release_intent_penalty": details["release_intent_penalty"],
    }


def upper_bound(source, track):
    """An optimistic score, before album/version/credit/duration penalties."""
    title, _ = Matcher._title_score(str(source.get("title", "")), str(track.get("title", "")))
    if title >= 95:
        # Structured/omitted credits can raise artist confidence. Bound them
        # generously and let the real scorer apply every identity guard.
        artist, bonus = 100, 12
    else:
        artist = Matcher._artist_score(
            str(source.get("artist", "")),
            str(track.get("artist", "")),
            str(source.get("title", "")),
            str(track.get("title", "")),
        )
        bonus = 4 if title >= 85 else 0
    weighted = artist if artist >= 70 else artist * 0.3
    return round(min(100, title * 0.65 + weighted * 0.35 + bonus), 1)


def rank(source, tracks, limit):
    candidates = sorted(
        ((upper_bound(source, track), index, track) for index, track in enumerate(tracks)),
        key=lambda item: (-item[0], item[1]),
    )
    rows, result = [], []
    cutoff = None
    scored, next_check = 0, 32
    for bound, index, track in candidates:
        # Equality must still be scored: identity/title/artist/album break ties,
        # and duplicate entries belonging to a displayed group must be kept.
        if cutoff is not None and bound < cutoff:
            break
        row = _row(track, Matcher.score_candidate(source, track))
        row["_rank_index"] = index
        rows.append(row)
        scored += 1
        if scored >= next_check or scored == len(candidates):
            next_check *= 2
            rows.sort(
                key=lambda r: (
                    r["score"],
                    r["identity_score"],
                    r["title_score"],
                    r["artist_score"],
                    r["album_score"] or 0,
                    -r["_rank_index"],
                ),
                reverse=True,
            )
            result = consolidate(rows)
            if len(result) >= limit:
                cutoff = result[limit - 1]["score"]
    rows.sort(
        key=lambda r: (
            r["score"],
            r["identity_score"],
            r["title_score"],
            r["artist_score"],
            r["album_score"] or 0,
            -r["_rank_index"],
        ),
        reverse=True,
    )
    return consolidate(rows)[:limit], scored
