"""Saved match decisions and per-account exclusions."""

from typing import List
from typing import Optional
from datetime import datetime
from .matching import Matcher
from .text_utils import repair_text


class MatchState:
    @staticmethod
    def _ignored_track_keys(
        track: dict,
    ) -> List[str]:
        """
        Return stable permanent-ignore identities.

        Prefer source track ID, with normalized title+artist as a fallback.
        """
        keys = []

        source_id = str(track.get("source_id", "") or "").strip()

        if source_id:
            keys.append(f"id:{source_id}")

        title = Matcher._normalize_match_text(track.get("title", ""))
        artist = Matcher._normalize_match_text(track.get("artist", ""))

        if title or artist:
            keys.append(f"meta:{title}|{artist}")

        return keys

    def _get_ignored_bucket(
        self,
        mapping_key: str,
        create: bool = True,
    ) -> dict:
        """Return permanent-ignore records for one source playlist."""
        if create:
            return self.config.ignored_tracks.setdefault(
                mapping_key,
                {},
            )

        value = self.config.ignored_tracks.get(
            mapping_key,
            {},
        )
        return value if isinstance(value, dict) else {}

    def _find_ignored_track_key(
        self,
        mapping_key: str,
        track: dict,
    ) -> Optional[str]:
        """Return the stored ignore key matching a source track."""
        bucket = {
            **self._get_ignored_bucket("__global__", create=False),
            **self._get_ignored_bucket(mapping_key, create=False),
        }

        candidate_keys = self._ignored_track_keys(track)

        for key in candidate_keys:
            if key in bucket:
                return key

        meta_key = next(
            (key for key in candidate_keys if key.startswith("meta:")),
            None,
        )

        if meta_key:
            for stored_key, record in bucket.items():
                if not isinstance(record, dict):
                    continue

                if meta_key in self._ignored_track_keys(record):
                    return stored_key

        return None

    def _is_track_ignored(
        self,
        mapping_key: str,
        track: dict,
    ) -> bool:
        """Return whether a source track is permanently ignored."""
        return (
            self._find_ignored_track_key(
                mapping_key,
                track,
            )
            is not None
        )

    def _ignore_track(
        self,
        mapping_key: str,
        track: dict,
    ):
        """
        Permanently ignore a source track for one playlist.

        Remove any mapping/provenance so the next real sync removes the track
        from the destination Plex playlist.
        """
        keys = self._ignored_track_keys(track)

        if not keys:
            return

        bucket = self._get_ignored_bucket(
            mapping_key,
            create=True,
        )

        bucket[keys[0]] = {
            "source_id": str(track.get("source_id", "") or ""),
            "title": repair_text(track.get("title", "")),
            "artist": repair_text(track.get("artist", "")),
            "album": repair_text(track.get("album", "")),
            "ignored_at": datetime.now().isoformat(),
        }

        search_key = f"{track.get('title', '')}|{track.get('artist', '')}"

        self.config.mapping.get(
            mapping_key,
            {},
        ).pop(
            search_key,
            None,
        )

        self._remove_match_provenance(
            mapping_key,
            search_key,
        )

    def _restore_ignored_track(
        self,
        mapping_key: str,
        ignored_key: str,
    ) -> bool:
        """Restore one permanently ignored source track."""
        bucket = self._get_ignored_bucket(
            mapping_key,
            create=False,
        )

        if ignored_key not in bucket:
            return False

        del bucket[ignored_key]

        if not bucket:
            self.config.ignored_tracks.pop(
                mapping_key,
                None,
            )

        return True

    def _get_match_metadata_bucket(
        self,
        mapping_key: str,
        create: bool = True,
    ) -> dict:
        """Return per-track provenance metadata for a playlist."""
        if create:
            return self.config.match_metadata.setdefault(
                mapping_key,
                {},
            )

        value = self.config.match_metadata.get(
            mapping_key,
            {},
        )
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _plex_match_snapshot(
        plex_track: dict,
        plex_id: str = None,
    ) -> dict:
        """Return the small Plex metadata snapshot used for LOST history."""
        if not isinstance(plex_track, dict):
            return {
                "plex_id": str(plex_id or ""),
                "title": "",
                "artist": "",
                "album": "",
            }

        return {
            "plex_id": str(plex_track.get("plex_id") or plex_id or ""),
            "title": repair_text(plex_track.get("title", "")),
            "artist": repair_text(plex_track.get("artist", "")),
            "album": repair_text(plex_track.get("album", "")),
        }

    def _remember_match_snapshot(
        self,
        mapping_key: str,
        search_key: str,
        plex_track: dict,
        plex_id: str = None,
    ):
        """
        Remember the last known Plex title/artist/album for a saved mapping.

        If the mapping predates provenance tracking, this enriches the legacy
        record without guessing whether it was automatic or manual.
        """
        bucket = self._get_match_metadata_bucket(
            mapping_key,
            create=True,
        )
        current = bucket.get(
            search_key,
            {},
        )

        if not isinstance(current, dict):
            current = {}

        current = dict(current)
        current["matched_track"] = self._plex_match_snapshot(
            plex_track,
            plex_id,
        )
        current["updated_at"] = datetime.now().isoformat()
        bucket[search_key] = current

    def _previous_match_snapshot(
        self,
        mapping_key: str,
        search_key: str,
        plex_id: str,
    ) -> dict:
        """Return the last known Plex metadata for a mapping that went stale."""
        record = self._get_match_metadata_bucket(
            mapping_key,
            create=False,
        ).get(search_key)

        if isinstance(record, dict):
            previous = record.get("matched_track")

            if isinstance(previous, dict):
                snapshot = {
                    "plex_id": str(previous.get("plex_id") or plex_id or ""),
                    "title": repair_text(previous.get("title", "")),
                    "artist": repair_text(previous.get("artist", "")),
                    "album": repair_text(previous.get("album", "")),
                }

                return snapshot

        # Existing 1.2 mappings do not yet have the last-known metadata
        # snapshot. Keep the old Plex ID so the LOST record still has a
        # concrete reference and can explain why details are unavailable.
        return {
            "plex_id": str(plex_id or ""),
            "title": "",
            "artist": "",
            "album": "",
        }

    def _set_match_provenance(
        self,
        mapping_key: str,
        search_key: str,
        provenance: str,
        matched_track: dict = None,
        plex_id: str = None,
    ):
        """Record mapping provenance and, when known, the Plex match snapshot."""
        bucket = self._get_match_metadata_bucket(
            mapping_key,
            create=True,
        )
        current = bucket.get(
            search_key,
            {},
        )

        if not isinstance(current, dict):
            current = {}

        current = dict(current)
        current["provenance"] = provenance
        current["updated_at"] = datetime.now().isoformat()

        if matched_track is not None:
            current["matched_track"] = self._plex_match_snapshot(
                matched_track,
                plex_id,
            )

        bucket[search_key] = current
        if provenance == "manual" and not getattr(self.config, "read_only", False):
            for playlist in self.config.config.get("playlists", []):
                if f"{playlist.get('source', '')}:{playlist.get('source_id', '')}" == mapping_key:
                    playlist["ready_to_sync"] = True

    def _remove_match_provenance(
        self,
        mapping_key: str,
        search_key: str,
    ):
        """Remove provenance associated with one saved mapping."""
        bucket = self._get_match_metadata_bucket(
            mapping_key,
            create=False,
        )
        bucket.pop(search_key, None)

        if not bucket:
            self.config.match_metadata.pop(
                mapping_key,
                None,
            )

    def _get_match_provenance(
        self,
        mapping_key: str,
        search_key: str,
    ) -> str:
        """Return automatic/manual/legacy for a saved mapping."""
        record = self._get_match_metadata_bucket(
            mapping_key,
            create=False,
        ).get(search_key)

        if isinstance(record, dict):
            value = str(record.get("provenance", "")).strip().lower()
            if value in ("automatic", "manual"):
                return value

        return "legacy"

    def _match_provenance_counts(
        self,
        mapping_key: str,
    ) -> dict:
        """Count automatic/manual/legacy mappings for one playlist."""
        mapping = self.config.mapping.get(
            mapping_key,
            {},
        )
        counts = {
            "automatic": 0,
            "manual": 0,
            "legacy": 0,
        }

        for search_key in mapping:
            provenance = self._get_match_provenance(
                mapping_key,
                search_key,
            )
            counts[provenance] += 1

        return counts

    def collect_all_missing_tracks_deduped(
        self,
        playlists: List[dict] = None,
    ):
        """
        Return a deduplicated list of unresolved tracks.

        When playlists is None, all registered playlists are included.
        Otherwise only the supplied playlist subset contributes occurrences,
        playlist counts, and playlist names.

        Tracks are deduplicated by normalized title + artist. Album is shown
        when available, preferring a non-empty/non-N/A album from any
        occurrence. Results are sorted by unresolved occurrence count
        descending, then playlist count descending, then artist/title.
        """

        if playlists is None:
            playlists = self.config.config.get(
                "playlists",
                [],
            )

        deduped = {}

        for playlist in playlists:
            mapping_key = f"{playlist['source']}:{playlist['source_id']}"
            unresolved = self.config.missing.get(
                mapping_key,
                [],
            )

            for track in unresolved:
                title = repair_text(track.get("title", "")).strip()
                artist = repair_text(track.get("artist", "")).strip()
                album = repair_text(track.get("album", "")).strip()

                key = (
                    title.casefold(),
                    artist.casefold(),
                )

                if key not in deduped:
                    deduped[key] = {
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "duration": track.get("duration"),
                        "artists": track.get("artists", []),
                        "playlists": [],
                        "playlist_count": 0,
                        "occurrence_count": 0,
                        "lost_occurrence_count": 0,
                    }

                entry = deduped[key]
                entry["occurrence_count"] += 1

                if track.get("status") == "lost":
                    entry["lost_occurrence_count"] += 1

                if album and album.casefold() != "n/a":
                    current_album = entry.get("album", "").strip()

                    if not current_album or current_album.casefold() == "n/a":
                        entry["album"] = album

                playlist_name = playlist["plex_playlist_name"]

                if playlist_name not in entry["playlists"]:
                    entry["playlists"].append(playlist_name)
                    entry["playlist_count"] += 1

        results = list(deduped.values())
        results.sort(
            key=lambda item: (
                -item["occurrence_count"],
                -item["playlist_count"],
                item["artist"].casefold(),
                item["title"].casefold(),
                item["album"].casefold(),
            )
        )
        return results

    @staticmethod
    def _same_missing_identity(
        left: dict,
        right: dict,
    ) -> bool:
        """Compare missing tracks using normalized title + artist."""
        return (
            repair_text(left.get("title", "")).casefold().strip()
            == repair_text(right.get("title", "")).casefold().strip()
            and repair_text(left.get("artist", "")).casefold().strip()
            == repair_text(right.get("artist", "")).casefold().strip()
        )

    def _playlists_containing_missing_track(
        self,
        track: dict,
        playlists: List[dict],
    ) -> List[dict]:
        """Return included playlists that currently contain this missing track."""
        result = []

        for playlist in playlists:
            mapping_key = f"{playlist['source']}:{playlist['source_id']}"
            unresolved = self.config.missing.get(
                mapping_key,
                [],
            )

            if any(
                self._same_missing_identity(
                    candidate,
                    track,
                )
                for candidate in unresolved
            ):
                result.append(playlist)

        return result

    def _apply_global_missing_match(
        self,
        track: dict,
        selected_plex_track: dict,
        playlists: List[dict],
    ) -> int:
        """Apply one explicit manual match across selected playlist occurrences."""
        affected = 0
        now = datetime.now().isoformat()

        for playlist in playlists:
            mapping_key = f"{playlist['source']}:{playlist['source_id']}"
            unresolved = list(
                self.config.missing.get(
                    mapping_key,
                    [],
                )
            )
            remaining = []
            matches = []

            for candidate in unresolved:
                if self._same_missing_identity(
                    candidate,
                    track,
                ):
                    matches.append(candidate)
                else:
                    remaining.append(candidate)

            if not matches:
                continue

            playlist_mapping = self.config.mapping.setdefault(
                mapping_key,
                {},
            )

            plex_id = str(
                selected_plex_track.get(
                    "plex_id",
                    "",
                )
            )

            for occurrence in matches:
                search_key = f"{occurrence.get('title', '')}|{occurrence.get('artist', '')}"
                playlist_mapping[search_key] = plex_id

                self._set_match_provenance(
                    mapping_key,
                    search_key,
                    "manual",
                    matched_track=selected_plex_track,
                    plex_id=plex_id,
                )
                affected += 1

            if remaining:
                self.config.missing[mapping_key] = remaining
            else:
                self.config.missing.pop(
                    mapping_key,
                    None,
                )

            playlist["last_match_attempt"] = now

        if affected:
            self.config.save()

        return affected
