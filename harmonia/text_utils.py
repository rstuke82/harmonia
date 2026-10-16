"""Shared text repair and source labels."""

import re
from .terminal import Colors, colored, dimmed


def repair_text(value) -> str:
    """
    Repair common UTF-8 text that was accidentally decoded as Latin-1.

    Example:
        We Didnât -> We Didn’t

    Already-correct Unicode is left unchanged.
    """
    if value is None:
        return ""

    text = str(value)

    suspicious_markers = ("Ã", "Â", "â", "ð", "Å", "�")

    def suspicious_count(candidate: str) -> int:
        return sum(candidate.count(marker) for marker in suspicious_markers) + sum(
            1 for ch in candidate if 0x80 <= ord(ch) <= 0x9F
        )

    # Two passes also repairs common double-encoded strings.
    for _ in range(2):
        before = suspicious_count(text)

        if before == 0:
            break

        best = text
        best_count = before

        for encoding in ("latin-1", "cp1252"):
            try:
                candidate = text.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue

            candidate_count = suspicious_count(candidate)

            if candidate_count < best_count:
                best = candidate
                best_count = candidate_count

        if best == text:
            break

        text = best

    return text


def clean_playlist_description(value) -> str:
    """
    Return a meaningful playlist description, or an empty string.

    Spotify/Apple Music public metadata sometimes exposes a generated label
    such as:
        Playlist · 86 Songs
    rather than a user-authored description. Plex already knows the object is
    a playlist and shows its item count, so those generated descriptions add
    no useful information and are suppressed.

    Genuine source descriptions are preserved.
    """
    text = repair_text(value).strip()

    if not text:
        return ""

    # Common generated descriptions from public playlist metadata.
    # Allow an optional trailing duration/metadata segment, e.g.
    # "Playlist · 86 Songs · 5 hr 12 min".
    generic_patterns = (
        r"^playlist\s*[·•]\s*\d+\s+songs?"
        r"(?:\s*[·•]\s*.*)?$",
        r"^\d+\s+songs?$",
    )

    if any(
        re.fullmatch(
            pattern,
            text,
            flags=re.IGNORECASE,
        )
        for pattern in generic_patterns
    ):
        return ""

    return text


def source_display_name(source_type: str) -> str:
    """Return one consistent uncolored user-facing service name."""
    names = {
        "spotify": "Spotify",
        "applemusic": "Apple Music",
        "text": "Custom Playlist",
    }
    value = str(source_type or "").lower()
    return names.get(value, str(source_type or "").title())


def source_display_label(source_type: str) -> str:
    """Return a brand-colored service label for terminal display."""
    value = str(source_type or "").lower()
    name = source_display_name(source_type)

    if value == "spotify":
        return colored(name, Colors.SPOTIFY)

    if value == "applemusic":
        return colored(name, Colors.APPLE_MUSIC)

    return name


def source_album_display(track: dict) -> str:
    """Return source album as subdued secondary match metadata."""
    album = repair_text(track.get("album", "") or "").strip()

    if album:
        return colored(
            f"({album})",
            Colors.DIM + Colors.YELLOW,
        )

    return dimmed("(N/A)")
