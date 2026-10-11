"""Apple Music playlist metadata, track extraction and artwork lookup."""
from bs4 import BeautifulSoup
from typing import List
from typing import Optional
from typing import Tuple
import json
import re
from . import network as requests
from .track_duration import source_duration
from html import unescape
from urllib.parse import urlparse
from .config import Config
from .runtime_output import print
from .runtime_paths import APPLE_ARTWORK_SIZE
from .text_utils import clean_playlist_description, repair_text

class AppleMusicAPI:
    """
    Apple Music public-playlist scraper.

    Current music.apple.com playlist pages embed their server-rendered data
    in:
        <script id="serialized-server-data" type="application/json">...</script>

    This does not require an Apple Music developer token for public playlists.
    """

    @staticmethod
    def _normalize_artwork_url(value) -> str:
        """
        Normalize an Apple Music artwork URL.

        Only expand Apple's {w}/{h} artwork templates here. Do NOT blindly
        rewrite a fixed 1200x630 social-preview URL to 3000x3000; Apple's CDN
        preserves the source aspect ratio, so a URL containing "3000x3000bb"
        can still return a 3000x750 image.
        """
        if isinstance(value, list):
            for item in value:
                result = AppleMusicAPI._normalize_artwork_url(item)
                if result:
                    return result
            return ""

        if isinstance(value, dict):
            for key in ("url", "contentUrl", "src"):
                result = AppleMusicAPI._normalize_artwork_url(
                    value.get(key)
                )
                if result:
                    return result
            return ""

        if not isinstance(value, str):
            return ""

        value = unescape(value).replace("\\/", "/").strip()

        if not value.startswith("https://"):
            return ""

        # Apple sometimes exposes a generic rendition template with no asset
        # identity. It looks like artwork but every generated size is a 404.
        try:
            parsed = urlparse(value)
            if "mzstatic" in (parsed.hostname or "").lower() and re.fullmatch(
                r"/image/thumb/gen/(?:\{w\}|\d+)x(?:\{h\}|\d+)[^/]*",
                parsed.path,
                re.IGNORECASE,
            ):
                return ""
        except ValueError:
            return ""

        # Expand true Apple artwork templates only.
        if "{w}" in value or "{h}" in value:
            replacements = {
                "{w}": str(APPLE_ARTWORK_SIZE),
                "{h}": str(APPLE_ARTWORK_SIZE),
                "{f}": "jpg",
                "{c}": "bb",
            }
            for old, new in replacements.items():
                value = value.replace(old, new)

        return value

    @staticmethod
    def _artwork_dimensions_from_value(value) -> Tuple[Optional[int], Optional[int]]:
        """Read declared width/height from an Apple artwork object or URL."""
        if isinstance(value, dict):
            width = (
                value.get("width")
                or value.get("maximumWidth")
                or value.get("maxWidth")
            )
            height = (
                value.get("height")
                or value.get("maximumHeight")
                or value.get("maxHeight")
            )

            try:
                width = int(width) if width is not None else None
            except (TypeError, ValueError):
                width = None

            try:
                height = int(height) if height is not None else None
            except (TypeError, ValueError):
                height = None

            if width and height:
                return width, height

            for key in ("url", "contentUrl", "src"):
                if value.get(key):
                    return AppleMusicAPI._artwork_dimensions_from_value(
                        value.get(key)
                    )

        if isinstance(value, str):
            # Useful for fixed derivatives such as 1200x630wp-60.jpg.
            match = re.search(
                r"/(\d+)x(\d+)[A-Za-z0-9._-]*\.(?:jpe?g|png|webp)$",
                value,
                re.IGNORECASE,
            )
            if match:
                return int(match.group(1)), int(match.group(2))

        return None, None


    @staticmethod
    def _walk_json(obj):
        """Yield every dict inside an arbitrarily nested JSON structure."""
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from AppleMusicAPI._walk_json(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from AppleMusicAPI._walk_json(value)

    @classmethod
    def _extract_metadata(cls, data, soup) -> dict:
        """
        Extract playlist metadata and prefer true square playlist artwork.

        Apple pages can contain several images:
          - the playlist cover
          - album artwork for individual tracks
          - wide OpenGraph/social preview cards

        Rank playlist-level square artwork above social preview images.
        """
        name = ""
        description = ""

        # First obtain the playlist identity from SEO data.
        schema_images = []

        for obj in cls._walk_json(data):
            schema = obj.get("schemaContent")
            if not isinstance(schema, dict):
                continue

            schema_type = str(
                schema.get("@type")
                or schema.get("type")
                or ""
            ).casefold()

            if "playlist" not in schema_type and name:
                continue

            if not name:
                name = schema.get("name", "") or ""

            if not description:
                description = (
                    schema.get("description", "")
                    or schema.get("abstract", "")
                    or ""
                )

            raw_image = (
                schema.get("image")
                or schema.get("thumbnailUrl")
            )
            if raw_image:
                schema_images.append(raw_image)

        if not name:
            meta = soup.find("meta", attrs={"name": "apple:title"})
            if meta:
                name = meta.get("content", "") or ""

        if not name:
            meta = soup.find("meta", attrs={"property": "og:title"})
            if meta:
                name = meta.get("content", "") or ""

        if not description:
            meta = soup.find(
                "meta",
                attrs={"property": "og:description"},
            )
            if meta:
                description = meta.get("content", "") or ""

        from .source_names import clean
        name = clean(name)

        target_name = name.casefold().strip()
        candidates = []

        def add_candidate(raw, context_score=0, label="unknown"):
            url = cls._normalize_artwork_url(raw)
            if not url:
                return

            width, height = cls._artwork_dimensions_from_value(raw)

            # If the URL was a template, dimensions may no longer be visible
            # in the normalized URL. Treat requested dimensions as a hint,
            # but Plex will validate the actual downloaded pixels later.
            if not width or not height:
                width, height = cls._artwork_dimensions_from_value(url)

            score = float(context_score)

            if width and height:
                ratio = width / height if height else 0

                if 0.95 <= ratio <= 1.05:
                    score += 80
                elif 0.80 <= ratio <= 1.20:
                    score += 25
                else:
                    # Strongly demote wide social/banner art.
                    score -= 80

                score += min(min(width, height) / 100.0, 25.0)

            if "{w}" in str(raw) or "{h}" in str(raw):
                score += 20

            # Social-preview markers are weak candidates unless they are
            # actually square.
            if re.search(
                r"\d+x\d+(?:wp|sr|mv)",
                url,
                re.IGNORECASE,
            ):
                score -= 20

            candidates.append(
                {
                    "url": url,
                    "score": score,
                    "width": width,
                    "height": height,
                    "label": label,
                }
            )

        # Strongest candidates: objects that look like the playlist itself.
        for obj in cls._walk_json(data):
            obj_name = str(
                obj.get("name")
                or obj.get("title")
                or ""
            ).casefold().strip()

            obj_type = str(
                obj.get("kind")
                or obj.get("type")
                or obj.get("contentType")
                or obj.get("entityType")
                or ""
            ).casefold()

            play_params = obj.get("playParams")
            if isinstance(play_params, dict):
                obj_type += " " + str(
                    play_params.get("kind")
                    or play_params.get("type")
                    or ""
                ).casefold()

            exact_name = bool(
                target_name
                and obj_name
                and obj_name == target_name
            )
            playlist_type = "playlist" in obj_type

            if not (exact_name or playlist_type):
                continue

            context_score = 0
            if exact_name:
                context_score += 100
            if playlist_type:
                context_score += 60

            for key in (
                "artwork",
                "image",
                "images",
                "coverArt",
                "cover",
                "artworkUrl",
                "artworkURL",
            ):
                if key in obj:
                    add_candidate(
                        obj.get(key),
                        context_score=context_score,
                        label=f"playlist:{key}",
                    )

        # SEO images are fallbacks; these are often 1200x630 social cards.
        for raw in schema_images:
            add_candidate(
                raw,
                context_score=10,
                label="schema",
            )

        # OpenGraph image is the final fallback.
        og = soup.find("meta", attrs={"property": "og:image"})
        if og:
            add_candidate(
                og.get("content", ""),
                context_score=0,
                label="og:image",
            )

        candidates.sort(
            key=lambda item: item["score"],
            reverse=True,
        )

        image_url = candidates[0]["url"] if candidates else ""

        if candidates:
            best = candidates[0]
            dim_text = ""
            if best["width"] and best["height"]:
                dim_text = (
                    f" ({best['width']}x{best['height']} declared)"
                )

            print(
                f"  ✓ Apple artwork candidate [{best['label']}]"
                f"{dim_text}: {best['url']}"
            )

        return {
            "name": repair_text(
                name or "Apple Music Playlist"
            ),
            "description": clean_playlist_description(
                description
            ),
            "image_url": image_url,
        }


    @staticmethod
    def _track_from_item(item: dict) -> Optional[dict]:
        """Convert one Apple server-data item into our common track format."""
        if not isinstance(item, dict):
            return None

        artist = item.get("artistName")
        title = item.get("title") or item.get("name")

        if not isinstance(artist, str) or not artist.strip():
            return None
        if not isinstance(title, str) or not title.strip():
            return None

        # The server data can contain artist/album cards too. A real song
        # item normally has one or more of these track-specific fields.
        track_markers = (
            "duration",
            "playParams",
            "tertiaryLinks",
            "audioTraits",
            "contentDescriptor",
            "releaseDate",
        )
        if not any(key in item for key in track_markers):
            return None

        album = item.get("albumName", "") or ""

        if not album:
            tertiary = item.get("tertiaryLinks")
            if isinstance(tertiary, list) and tertiary:
                first = tertiary[0]
                if isinstance(first, dict):
                    album = first.get("title", "") or ""

        source_id = (
            item.get("id")
            or item.get("adamId")
            or ""
        )

        play_params = item.get("playParams")
        if isinstance(play_params, dict):
            source_id = (
                source_id
                or play_params.get("catalogId")
                or play_params.get("id")
                or ""
            )

        credit_names=[v.strip() for v in re.split(r'\s*(?:,|&|\bfeat(?:uring)?\.?\b|\bft\.?\b|\bx\b)\s*',artist,flags=re.I) if v.strip()]
        return {
            "title": repair_text(title).strip(),
            "artist": repair_text(artist).strip(),
            "artists": [{"name":repair_text(v)} for v in credit_names] if len(credit_names)>1 else [{"name":repair_text(artist).strip()}],
            "album": (
                repair_text(album).strip()
                if isinstance(album, str)
                else ""
            ),
            "source_id": str(source_id) if source_id else "",
            "duration": source_duration(item),
        }

    @classmethod
    def _extract_tracks(cls, data) -> List[dict]:
        """Extract ordered playlist tracks from serialized-server-data."""
        tracks = []
        seen = set()

        def add_track(item,dedupe=True):
            track = cls._track_from_item(item)
            if not track:
                return

            # Prefer catalog ID for dedupe, otherwise title + artist + album.
            key = (
                ("id", track["source_id"])
                if track["source_id"]
                else (
                    "text",
                    track["title"].casefold(),
                    track["artist"].casefold(),
                    track["album"].casefold(),
                )
            )

            if dedupe and key in seen:
                return

            seen.add(key)
            tracks.append(track)

        # First pass: Apple playlist pages currently organize songs under
        # section["items"]. Walking those lists preserves playlist order.
        for obj in cls._walk_json(data):
            sections = obj.get("sections")
            if not isinstance(sections, list):
                continue

            for section in sections:
                if not isinstance(section, dict):
                    continue
                items = section.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    # A playlist may intentionally contain the same catalog
                    # track more than once. Section items are the authoritative
                    # ordered list, so retain duplicate occurrences here.
                    add_track(item,dedupe=False)

        if tracks:
            return tracks

        # Fallback for future layout changes: scan all dictionaries while
        # retaining traversal order.
            for obj in cls._walk_json(data):
                add_track(obj)

        return tracks

    @classmethod
    def _extract_metadata_without_artwork(cls, data, soup) -> dict:
        """Extract playlist name/description without inspecting artwork."""
        name = ""
        description = ""

        for obj in cls._walk_json(data):
            schema = obj.get("schemaContent")
            if not isinstance(schema, dict):
                continue

            schema_type = str(
                schema.get("@type")
                or schema.get("type")
                or ""
            ).casefold()

            if "playlist" not in schema_type:
                continue

            if not name:
                name = schema.get("name", "") or ""

            if not description:
                description = (
                    schema.get("description", "")
                    or schema.get("abstract", "")
                    or ""
                )

            if name and description:
                break

        if not name:
            meta = soup.find("meta", attrs={"name": "apple:title"})
            if meta:
                name = meta.get("content", "") or ""

        if not name:
            meta = soup.find("meta", attrs={"property": "og:title"})
            if meta:
                name = meta.get("content", "") or ""

        if not description:
            meta = soup.find(
                "meta",
                attrs={"property": "og:description"},
            )
            if meta:
                description = meta.get("content", "") or ""

        from .source_names import clean
        name = clean(name)

        return {
            "name": repair_text(
                name or "Apple Music Playlist"
            ),
            "description": clean_playlist_description(
                description
            ),
            "image_url": "",
        }

    def get_playlist_tracks(
        self,
        playlist_url: str,
        fetch_artwork: bool = True,
    ) -> Tuple[List[dict], dict]:
        """
        Fetch tracks and metadata from a public Apple Music playlist.

        Artwork discovery is optional so non-sync workflows can fetch only
        the metadata needed for matching.
        """

        playlist_id = Config._extract_id(
            playlist_url,
            "applemusic",
        )

        if not playlist_id:
            raise Exception(
                "Could not extract Apple Music playlist ID from the supplied URL"
            )

        from .playlist_urls import canonical_playlist, apple_playlist_get
        url = canonical_playlist(Config._normalize_url_input(playlist_url), 'applemusic')[1]

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }

        try:
            resp = apple_playlist_get(
                requests, url,
                headers=headers,
                timeout=15,
            )

            if resp.status_code != 200:
                raise Exception(
                    f"Apple Music returned HTTP {resp.status_code} "
                    f"for {url}"
                )

            soup = BeautifulSoup(
                resp.content,
                "html.parser",
            )

            script = soup.find(
                "script",
                id="serialized-server-data",
            )

            if not script:
                raise Exception(
                    "Could not find Apple Music serialized-server-data "
                    "in the playlist page"
                )

            raw_json = script.string or script.get_text()

            if not raw_json or not raw_json.strip():
                raise Exception(
                    "Apple Music serialized-server-data was empty"
                )

            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError as e:
                raise Exception(
                    f"Could not parse Apple Music server data: {e}"
                )

            if fetch_artwork:
                metadata = self._extract_metadata(
                    data,
                    soup,
                )
            else:
                metadata = self._extract_metadata_without_artwork(
                    data,
                    soup,
                )

            tracks = self._extract_tracks(data)
            counts=[]
            for obj in self._walk_json(data):
                schema=obj.get('schemaContent') if isinstance(obj.get('schemaContent'),dict) else obj
                kind=str(schema.get('@type') or schema.get('type') or '').casefold()
                if 'playlist' in kind:
                    for key in ('trackCount','track-count','totalTrackCount','numTracks'):
                        value=schema.get(key)
                        if isinstance(value,int) and 0<=value<=10000:counts.append(value)
            expected=max(counts) if counts else None

            if not tracks and expected!=0:
                raise Exception(
                    "Apple Music page loaded, but no playlist tracks "
                    "could be extracted from serialized-server-data"
                )

            if fetch_artwork and not metadata.get("image_url"):
                print(
                    "  ⚠ No Apple Music playlist artwork found"
                )

            metadata["source_url"] = url
            metadata["source_id"] = playlist_id
            metadata['expected_track_count']=expected
            metadata['complete']=isinstance(expected,int) and len(tracks)>=expected

            return tracks, metadata

        except requests.exceptions.Timeout:
            raise Exception(
                "Request timed out. Apple Music is not responding."
            )
        except requests.exceptions.RequestException as e:
            raise Exception(
                f"Apple Music connection error: {e}"
            )
        except Exception as e:
            raise Exception(
                f"Failed to fetch Apple Music playlist: {e}"
            )


