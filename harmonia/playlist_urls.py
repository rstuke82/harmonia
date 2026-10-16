"""Trusted playlist destinations; never fetch a caller-supplied origin."""

import re
from urllib.parse import urlsplit, urljoin


APPLE_HOSTS = {"music.apple.com", "itunes.apple.com"}
SPOTIFY_HOSTS = {"open.spotify.com", "spotify.com", "www.spotify.com"}


def trusted_https_url(value, hosts):
    if any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Invalid playlist URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Use an HTTPS playlist URL on the official service host")
    return parsed


def canonical_playlist(value, source=None):
    value = str(value).strip()
    if source in (None, "spotify"):
        match = re.fullmatch(r"spotify:playlist:([A-Za-z0-9]{22})", value)
        if not match and source == "spotify":
            match = re.fullmatch(r"([A-Za-z0-9]{22})", value)
        if match:
            return "spotify", f"https://open.spotify.com/playlist/{match[1]}"
    if source == "applemusic" and re.fullmatch(r"pl\.[A-Za-z0-9._-]+", value):
        return "applemusic", f"https://music.apple.com/us/playlist/playlist/{value}"
    parsed = trusted_https_url(value, APPLE_HOSTS | SPOTIFY_HOSTS)
    if parsed.hostname in SPOTIFY_HOSTS and source in (None, "spotify"):
        match = re.fullmatch(r"/playlist/([A-Za-z0-9]{22})/?", parsed.path)
        if match:
            return "spotify", f"https://open.spotify.com/playlist/{match[1]}"
    if parsed.hostname in APPLE_HOSTS and source in (None, "applemusic"):
        match = re.fullmatch(
            r"/([A-Za-z]{2})/playlist/(?:[^/]+/)?(pl\.[A-Za-z0-9._-]+)/?", parsed.path
        )
        if match:
            return (
                "applemusic",
                f"https://music.apple.com/{match[1].lower()}/playlist/playlist/{match[2]}",
            )
    raise ValueError("Enter a valid Spotify or Apple Music playlist URL")


def apple_playlist_get(network, url, **kwargs):
    """Validate every redirect before issuing it, with a bounded chain."""
    for attempt in range(6):
        trusted_https_url(url, APPLE_HOSTS)
        response = network.get(url, allow_redirects=False, **kwargs)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location or attempt == 5:
            raise ValueError("Invalid or excessive Apple Music redirects")
        url = urljoin(url, location)
    raise ValueError("Excessive Apple Music redirects")
