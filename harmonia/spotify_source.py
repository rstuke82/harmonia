"""Spotify playlist metadata, track extraction and artwork lookup."""

from typing import List
from typing import Tuple
import json
import re
from . import network
from .track_duration import source_duration
from html import unescape
from urllib.parse import urlparse
from .config import Config
from .runtime_output import emit
from .text_utils import clean_playlist_description, repair_text


class SpotifyAPI:
    """Spotify API wrapper - scrapes public playlists without auth."""

    @staticmethod
    def _clean_artwork_url(value: str) -> str:
        """
        Decode and validate a Spotify artwork URL.

        Spotify also serves CSS/JS/fonts from spotifycdn.com, so simply
        checking the hostname is not enough.
        """
        if not isinstance(value, str):
            return ""

        value = unescape(value)
        value = value.replace("\\/", "/")
        value = value.replace("\\u0026", "&")
        value = value.strip()

        if not value.startswith("https://"):
            return ""

        try:
            parsed = urlparse(value)
        except ValueError:
            return ""

        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()

        if not host:
            return ""

        blocked_suffixes = (
            ".css",
            ".js",
            ".mjs",
            ".map",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".json",
            ".html",
            ".svg",
        )

        if path.endswith(blocked_suffixes):
            return ""

        if "/_next/" in path or "/static/css/" in path or "/static/js/" in path:
            return ""

        # Current Spotify custom/playlist artwork CDN.
        if host.endswith(".spotifycdn.com") and (
            host.startswith("image-cdn-") or ".image-cdn-" in host
        ):
            return value

        # Standard Spotify artwork CDN.
        if host == "i.scdn.co" and "/image/" in path:
            return value

        # Spotify-generated mosaic playlist covers.
        if host == "mosaic.scdn.co":
            return value

        # Conservative fallback for explicit image files.
        if (host.endswith("spotifycdn.com") or host.endswith("scdn.co")) and re.search(
            r"\.(?:jpe?g|png|webp|gif|avif)$", path
        ):
            return value

        return ""

    @classmethod
    def _extract_artwork_url(cls, html_text: str, entity: dict = None) -> str:
        """Extract the main Spotify playlist cover URL."""
        entity = entity or {}
        candidates = []

        def add(value):
            cleaned = cls._clean_artwork_url(value)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

        # First prefer image fields from Spotify's playlist entity.
        def walk_images(obj, depth=0):
            if depth > 5:
                return
            if isinstance(obj, str):
                add(obj)
            elif isinstance(obj, list):
                for item in obj:
                    walk_images(item, depth + 1)
            elif isinstance(obj, dict):
                for key, value in obj.items():
                    key_lower = str(key).lower()
                    if any(token in key_lower for token in ("image", "cover", "art", "src", "url")):
                        walk_images(value, depth + 1)

        walk_images(entity)

        # OpenGraph is a strong signal for the playlist's main cover.
        og_patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]
        for pattern in og_patterns:
            for match in re.findall(pattern, html_text, re.IGNORECASE):
                add(match)

        # Spotify's playlist-header image is commonly eager-loaded.
        for tag in re.findall(r"<img\b[^>]*>", html_text, re.IGNORECASE):
            if re.search(r'loading=["\']eager["\']', tag, re.IGNORECASE):
                src_match = re.search(r'src=["\']([^"\']+)["\']', tag, re.IGNORECASE)
                if src_match:
                    add(src_match.group(1))

        # Generic Spotify CDN fallback. This handles hosts such as
        # image-cdn-fa.spotifycdn.com without hard-coding the region.
        direct_patterns = [
            r'https://(?:[A-Za-z0-9-]+\.)?image-cdn-[A-Za-z0-9-]+\.spotifycdn\.com/[^"\'\s<>]+',
            r"https://i\.scdn\.co/image/[A-Za-z0-9]+",
            r'https://mosaic\.scdn\.co/[^"\'\s<>]+',
        ]
        for pattern in direct_patterns:
            for match in re.findall(pattern, html_text, re.IGNORECASE):
                add(match)

        # Prefer playlist-cover/CDN style URLs over arbitrary nested images.
        for candidate in candidates:
            if "/image/" in candidate and ("image-cdn-" in candidate or "i.scdn.co" in candidate):
                return candidate

        return candidates[0] if candidates else ""

    @classmethod
    def _fetch_oembed_artwork(cls, playlist_id: str) -> str:
        """
        Fetch playlist artwork from Spotify's public oEmbed endpoint.

        Spotify oEmbed returns thumbnail_url for public playlists and is
        considerably more reliable for cover art than scraping embed HTML.
        """
        normalized_id = Config._extract_id(playlist_id, "spotify")
        if not normalized_id:
            return ""

        public_url = f"https://open.spotify.com/playlist/{normalized_id}"

        try:
            resp = network.get(
                "https://open.spotify.com/oembed",
                params={"url": public_url},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
                timeout=10,
            )

            if resp.status_code != 200:
                return ""

            try:
                data = resp.json()
            except ValueError:
                return ""

            return cls._clean_artwork_url(data.get("thumbnail_url", ""))

        except network.RequestException:
            return ""

    def get_playlist_tracks(
        self,
        playlist_id: str,
        fetch_artwork: bool = True,
    ) -> Tuple[List[dict], dict]:
        """
        Fetch a public Spotify playlist from a raw ID or full URL.

        Artwork discovery is optional so non-sync workflows such as
        missing-track triage and match editing do not perform artwork work.
        """

        normalized_id = Config._extract_id(playlist_id, "spotify")
        if not normalized_id:
            raise Exception("Could not extract Spotify playlist ID from the supplied URL")

        embed_url = f"https://open.spotify.com/embed/playlist/{normalized_id}"
        public_url = f"https://open.spotify.com/playlist/{normalized_id}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        }

        try:
            resp = network.get(embed_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                raise Exception(f"Failed to fetch Spotify playlist: {resp.status_code}")

            html_text = resp.text

            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">'
                r"({.*?})</script>",
                html_text,
                re.DOTALL,
            )
            if not match:
                raise Exception("Could not find playlist data in page")

            data = json.loads(match.group(1))
            entity = (
                data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
            )
            if not entity:
                raise Exception("Could not parse playlist entity data")

            name = repair_text(entity.get("name", "Unknown Playlist"))
            description = clean_playlist_description(entity.get("description", ""))

            image_url = ""

            if fetch_artwork:
                # Prefer Spotify's official oEmbed thumbnail. The embed/public
                # HTML structure changes frequently, while oEmbed exposes a
                # dedicated thumbnail_url for public playlist artwork.
                image_url = self._fetch_oembed_artwork(normalized_id)

                if image_url:
                    emit(f"  ✓ Artwork via Spotify oEmbed: {image_url}")
                else:
                    # Fall back to the embed page if oEmbed did not return art.
                    image_url = self._extract_artwork_url(html_text, entity)

                    # The normal public page may expose og:image/header artwork
                    # even when the embed widget does not.
                    if not image_url:
                        try:
                            public_resp = network.get(
                                public_url,
                                headers=headers,
                                timeout=10,
                            )
                            if public_resp.status_code == 200:
                                image_url = self._extract_artwork_url(public_resp.text)
                        except network.RequestException:
                            pass

                    if image_url:
                        emit(f"  ✓ Artwork extracted from Spotify page: {image_url}")
                    else:
                        emit(
                            "  ⚠ No Spotify playlist artwork found "
                            "(oEmbed and page fallbacks failed)"
                        )

            tracks = entity.get("trackList", [])
            if not tracks:
                items = entity.get("tracks", {}).get("items", [])
                tracks = [item.get("track", item) for item in items]

            track_list = []
            for track in tracks:
                if not track:
                    continue

                title = track.get("title") or track.get("name") or "Unknown Title"

                if "subtitle" in track:
                    artist_names = track.get("subtitle")
                else:
                    artists = track.get("artists", [])
                    artist_names = ", ".join(a.get("name", "Unknown Artist") for a in artists)

                if not artist_names:
                    artist_names = "Unknown Artist"

                # Spotify's embed payload is not consistent about album
                # metadata, but preserve it whenever it is available so the
                # matcher can prefer the original studio-album copy.
                album_name = track.get("albumName") or track.get("album_name") or ""

                album_obj = track.get("album")
                if not album_name and isinstance(album_obj, dict):
                    album_name = album_obj.get("name") or album_obj.get("title") or ""
                elif not album_name and isinstance(album_obj, str):
                    album_name = album_obj

                track_uri = str(track.get("uri", "") or "")
                source_track_id = str(track.get("id", "") or "")

                if not source_track_id and track_uri.startswith("spotify:track:"):
                    source_track_id = track_uri.rsplit(":", 1)[-1]

                track_list.append(
                    {
                        "title": repair_text(title),
                        "artist": repair_text(artist_names),
                        "artists": [
                            {"name": repair_text(a.get("name", ""))}
                            for a in track.get("artists", [])
                            if isinstance(a, dict) and a.get("name")
                        ],
                        "album": repair_text(album_name),
                        "source_id": source_track_id,
                        "uri": track_uri,
                        "duration": source_duration(track),
                    }
                )

            expected = (
                entity.get("trackListTotal")
                or entity.get("totalTrackCount")
                or entity.get("trackCount")
            )
            if not isinstance(expected, int):
                container = entity.get("tracks", {})
                expected = container.get("total") if isinstance(container, dict) else None
            return track_list, {
                "name": name,
                "description": description,
                "image_url": image_url,
                "source_url": public_url,
                "expected_track_count": expected,
                "complete": isinstance(expected, int) and len(track_list) >= expected,
            }

        except network.exceptions.Timeout:
            raise Exception("Request timed out. Spotify is tarpitting the connection.")
        except network.exceptions.RequestException as e:
            raise Exception(f"Connection error: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse playlist JSON: {e}")
        except Exception as e:
            raise Exception(f"Failed to extract playlist data: {e}")
