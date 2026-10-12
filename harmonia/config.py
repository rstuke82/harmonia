"""Account-scoped configuration and saved playlist state."""
from typing import List
from typing import Optional
import copy
import re
from . import network as requests
from urllib.parse import unquote
from urllib.parse import urlparse
from .matching import Matcher
from .runtime_paths import CONFIG_FILE, IGNORED_TRACKS_FILE, MAPPING_FILE, MATCH_METADATA_FILE, MISSING_FILE, SOURCE_SNAPSHOTS_FILE
from .text_utils import repair_text

class Config:
    """Handle configuration file management"""

    STATE_FILES = {
        "config": CONFIG_FILE,
        "mapping": MAPPING_FILE,
        "missing": MISSING_FILE,
        "match_metadata": MATCH_METADATA_FILE,
        "source_snapshots": SOURCE_SNAPSHOTS_FILE,
        "ignored_tracks": IGNORED_TRACKS_FILE,
    }

    def __init__(self, *, read_only=False, namespaces=None):
        from .storage import get_repository, read_json
        from .accounts import personal_repository, root_repository, actor, is_owner
        self.repository = personal_repository()
        self._account = actor()
        self.read_only = read_only
        self.config = read_json(CONFIG_FILE)
        runtime = self.repository.load("runtime")
        self.config.update(runtime)
        if self._account and not __import__("harmonia.accounts",fromlist=["is_owner"]).is_owner(self._account):
            self.config["plex"] = {**self.config.get("plex", {}), "token": self._account["plex_token"]}
        if not isinstance(self.config.get("plex"), dict):
            self.config["plex"] = {}
        self.config.setdefault("playlists", [])
        names = ("mapping", "missing", "match_metadata", "source_snapshots", "ignored_tracks", "artist_aliases")
        for name in (names if not read_only or namespaces is None else namespaces):
            setattr(self, name, (root_repository() if name=="artist_aliases" else self.repository).load(name))
        self._baseline = None if read_only else copy.deepcopy(self._buckets())
        if self._baseline is not None:
            self._baseline["runtime"] = copy.deepcopy(runtime)
        if "artist_aliases" in self.__dict__:
            Matcher.set_artist_aliases(self.artist_aliases)

    def __getattr__(self, name):
        if name in ("mapping", "missing", "match_metadata", "source_snapshots", "ignored_tracks", "artist_aliases", "health", "health_attempts"):
            value = self.repository.load(name)
            setattr(self, name, value)
            return value
        raise AttributeError(name)

    def _assert_writable(self):
        if self.read_only:
            raise RuntimeError("Cannot save a read-only configuration")

    def reload_artist_aliases(self):
        """Reload artist aliases and update the matcher immediately."""
        self.artist_aliases = self.repository.load("artist_aliases")
        Matcher.set_artist_aliases(
            self.artist_aliases
        )

    def save_artist_aliases(self):
        self._assert_writable()
        """Persist the standalone alias file atomically and reload aliases."""
        self.repository.save({"artist_aliases": self.artist_aliases}, self._baseline, replace=True)
        self._baseline["artist_aliases"] = copy.deepcopy(self.artist_aliases)
        self.reload_artist_aliases()

    @staticmethod
    def _ensure_artist_alias_shell():
        pass

    def _buckets(self):
        from .storage import STARTUP_KEYS
        buckets = {name: getattr(self, name) for name in (
            "mapping", "missing", "match_metadata", "source_snapshots", "ignored_tracks", "artist_aliases")}
        buckets["runtime"] = {k: v for k, v in self.config.items() if k not in STARTUP_KEYS}
        return buckets

    def _save_state_only(self, name, data):
        self._assert_writable()
        self.repository.save({name: data}, self._baseline, replace=True)
        self._baseline[name] = copy.deepcopy(data)

    def save_missing_only(self):
        self._save_state_only("missing", self.missing)

    def save(self):
        self._assert_writable()
        from .storage import STARTUP_KEYS, read_json
        buckets = self._buckets()
        self.repository.save(buckets, self._baseline, replace=True)
        self._baseline = copy.deepcopy(buckets)
        startup = {k: v for k, v in self.config.items() if k in STARTUP_KEYS}
        if not (self._account and not __import__("harmonia.accounts",fromlist=["is_owner"]).is_owner(self._account)) and read_json(CONFIG_FILE) != startup:
            self.repository.save_startup(startup)

    @staticmethod
    def _get_plex_music_libraries(
        plex_url: str,
        plex_token: str,
    ) -> List[dict]:
        """Return Plex music libraries available to this server/token."""
        resp = requests.get(
            f"{plex_url.rstrip('/')}/library/sections",
            headers={
                "X-Plex-Token": plex_token,
                "Accept": "application/json",
            },
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                "Failed to get Plex library sections: "
                f"HTTP {resp.status_code}"
            )

        sections = (
            resp.json()
            .get("MediaContainer", {})
            .get("Directory", [])
        )

        libraries = []

        for section in sections:
            if section.get("type") != "artist":
                continue

            key = section.get("key")

            if key is None:
                continue

            libraries.append(
                {
                    "key": str(key),
                    "name": repair_text(
                        section.get("title", "")
                    ).strip()
                    or f"Music Library {key}",
                }
            )

        return libraries

    def get_plex(self) -> dict:
        """Return validated saved Plex settings without discovery or prompts."""
        from .diagnostics import validate_plex_config
        return validate_plex_config(self)

    def add_playlist(
        self,
        source_url: str,
        source_type: str,
        plex_playlist_name: str,
        plex_playlist_id: str,
    ):
        """Add a new playlist to sync"""
        canonical_url = self._canonical_source_url(source_url, source_type)
        playlist_entry = {
            "source": source_type,
            "source_url": canonical_url,
            "source_id": self._extract_id(canonical_url, source_type),
            "plex_playlist_id": plex_playlist_id,
            "plex_playlist_name": plex_playlist_name,
            "last_synced": None,
            "last_match_attempt": None,
            "auto_sync": True,
            "favorite": False,
        }

        self.config["playlists"].append(playlist_entry)
        self.save()
        return playlist_entry

    @staticmethod
    def _normalize_url_input(value: str) -> str:
        """Normalize pasted playlist text into a usable URL/URI."""
        if not value:
            return ""

        value = unquote(str(value).strip())
        value = value.replace("\\&", "&").strip()

        # Accept Markdown links copied from chat/web pages:
        # [https://...](https://...)
        markdown = re.search(r"\[[^\]]*\]\((https?://[^)]+)\)", value)
        if markdown:
            value = markdown.group(1)

        # Accept text that contains a Spotify/Apple playlist URL plus
        # surrounding punctuation or commentary.
        url_match = re.search(
            r"https?://(?:open\.)?spotify\.com/playlist/[^\s<>)\]]+"
            r"|https?://(?:music|itunes)\.apple\.com/[^\s<>)\]]+",
            value,
            re.IGNORECASE,
        )
        if url_match:
            value = url_match.group(0)

        return value.strip().strip("<>[](){}.,;\"'")

    @staticmethod
    def _extract_id(url: str, source_type: str) -> Optional[str]:
        """Extract a canonical playlist ID while ignoring URL query data."""
        value = Config._normalize_url_input(url)
        if not value:
            return None

        if source_type == "text":
            import uuid
            try:return str(uuid.UUID(value.removeprefix("text:")))
            except ValueError:return None

        if source_type == "spotify":
            # spotify:playlist:7eahWLng9go8LDR5gcW6A3
            uri_match = re.fullmatch(
                r"spotify:playlist:([A-Za-z0-9]{22})",
                value,
                re.IGNORECASE,
            )
            if uri_match:
                return uri_match.group(1)

            # Bare Spotify playlist ID.
            if re.fullmatch(r"[A-Za-z0-9]{22}", value):
                return value

            # Parse only the path component, so ?si=..., &nd=1,
            # &dlsi=..., etc. can never become part of the ID.
            try:
                parsed = urlparse(value)
                host = parsed.netloc.lower().split(":", 1)[0]
                if host in {"open.spotify.com", "spotify.com", "www.spotify.com"}:
                    parts = [p for p in parsed.path.split("/") if p]
                    for index, part in enumerate(parts[:-1]):
                        if part.lower() == "playlist":
                            candidate = parts[index + 1]
                            if re.fullmatch(r"[A-Za-z0-9]{22}", candidate):
                                return candidate
            except ValueError:
                pass

            # Last-resort extraction from pasted Spotify text.
            match = re.search(
                r"(?:open\.)?spotify\.com/playlist/([A-Za-z0-9]{22})",
                value,
                re.IGNORECASE,
            )
            return match.group(1) if match else None

        if source_type == "applemusic":
            # Apple Music IDs are not always purely alphanumeric after "pl.".
            # Replay playlists, for example, use IDs such as:
            #   pl.rp-B7CXevA0GM
            # Parse only the path so query parameters never become part of ID.
            try:
                parsed = urlparse(value)
                path = parsed.path
            except ValueError:
                path = value

            # Normal Apple Music playlist URL:
            # /us/playlist/replay-all-time/pl.rp-B7CXevA0GM
            match = re.search(
                r"/playlist/[^/]+/(pl\.[A-Za-z0-9._-]+)",
                path,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)

            # Fallback for any path/text containing a playlist ID.
            match = re.search(
                r"(pl\.[A-Za-z0-9._-]+)",
                path,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)

            # Also accept a bare Apple Music playlist ID.
            if re.fullmatch(r"pl\.[A-Za-z0-9._-]+", value, re.IGNORECASE):
                return value

            return None

        return None

    @staticmethod
    def _canonical_source_url(url: str, source_type: str) -> str:
        """Return a stable URL for storage and future syncs."""
        if source_type == 'text':
            return Config._normalize_url_input(url)
        from .playlist_urls import canonical_playlist
        return canonical_playlist(Config._normalize_url_input(url), source_type)[1]

    def find_playlist(self, source_url: str) -> Optional[dict]:
        """Find a playlist by canonical source identity, not query string."""
        normalized = self._normalize_url_input(source_url)

        if "spotify.com" in normalized.lower() or normalized.lower().startswith("spotify:playlist:"):
            source_type = "spotify"
        elif "music.apple.com" in normalized.lower() or "itunes.apple.com" in normalized.lower():
            source_type = "applemusic"
        else:
            source_type = None

        requested_id = (
            self._extract_id(normalized, source_type)
            if source_type
            else None
        )

        for playlist in self.config["playlists"]:
            if source_type and playlist.get("source") == source_type:
                existing_id = playlist.get("source_id") or self._extract_id(
                    playlist.get("source_url", ""),
                    source_type,
                )
                if requested_id and existing_id == requested_id:
                    return playlist

            if playlist.get("source_url") == normalized:
                return playlist

        return None

    def remove_playlist(self, index: int) -> bool:
        """Remove playlist by index"""
        if 0 <= index < len(self.config["playlists"]):
            self.config["playlists"].pop(index)
            self.save()
            return True
        return False


