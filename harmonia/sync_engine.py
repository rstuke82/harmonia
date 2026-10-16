"""Playlist synchronization and saved matching state."""
from collections import Counter
from typing import List
from typing import Optional
from typing import Tuple
from datetime import datetime
from . import network
import time
from .apple_music_source import AppleMusicAPI
from .config import Config
from .matching import Matcher
from .plex_client import PlexAPI
from .runtime_output import print
from .spotify_source import SpotifyAPI
from .terminal import Colors, auto_sync_display, colored, provenance_display, section_header
from .text_utils import repair_text, source_album_display, source_display_label

class Syncer:
    """Playlist synchronization and saved matching state, without interactive menus."""

    def __init__(self, config: Config):
        self.config = config
        self.plex = None


    @staticmethod
    def _run_stats(
        *,
        tested: int = 0,
        synced: int = 0,
        audited: int = 0,
        source_added: int = 0,
        source_removed: int = 0,
        new_matches: int = 0,
        recovered_lost: int = 0,
        newly_lost: int = 0,
        unresolved: int = 0,
        ignored: int = 0,
        errors: int = 0,
    ) -> dict:
        """Return one normalized per-playlist run-summary payload."""
        return {
            "tested": tested,
            "synced": synced,
            "audited": audited,
            "source_added": source_added,
            "source_removed": source_removed,
            "new_matches": new_matches,
            "recovered_lost": recovered_lost,
            "newly_lost": newly_lost,
            "unresolved": unresolved,
            "ignored": ignored,
            "errors": errors,
        }


    @staticmethod
    def _auto_sync_enabled(
        playlist: dict,
    ) -> bool:
        """Missing auto_sync means ON for pre-1.3 playlists."""
        return playlist.get(
            "auto_sync",
            True,
        ) is not False


    @staticmethod
    def _playlist_favorite(
        playlist: dict,
    ) -> bool:
        """Missing favorite means OFF for existing playlists."""
        return playlist.get(
            "favorite",
            False,
        ) is True


    @staticmethod
    def _ignored_track_keys(
        track: dict,
    ) -> List[str]:
        """
        Return stable permanent-ignore identities.

        Prefer source track ID, with normalized title+artist as a fallback.
        """
        keys = []

        source_id = str(
            track.get("source_id", "")
            or ""
        ).strip()

        if source_id:
            keys.append(
                f"id:{source_id}"
            )

        title = Matcher._normalize_match_text(
            track.get("title", "")
        )
        artist = Matcher._normalize_match_text(
            track.get("artist", "")
        )

        if title or artist:
            keys.append(
                f"meta:{title}|{artist}"
            )

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
        bucket = {**self._get_ignored_bucket("__global__", create=False), **self._get_ignored_bucket(mapping_key, create=False)}

        candidate_keys = self._ignored_track_keys(
            track
        )

        for key in candidate_keys:
            if key in bucket:
                return key

        meta_key = next(
            (
                key
                for key in candidate_keys
                if key.startswith("meta:")
            ),
            None,
        )

        if meta_key:
            for stored_key, record in bucket.items():
                if not isinstance(record, dict):
                    continue

                if meta_key in self._ignored_track_keys(
                    record
                ):
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
        keys = self._ignored_track_keys(
            track
        )

        if not keys:
            return

        bucket = self._get_ignored_bucket(
            mapping_key,
            create=True,
        )

        bucket[keys[0]] = {
            "source_id": str(
                track.get("source_id", "")
                or ""
            ),
            "title": repair_text(
                track.get("title", "")
            ),
            "artist": repair_text(
                track.get("artist", "")
            ),
            "album": repair_text(
                track.get("album", "")
            ),
            "ignored_at": datetime.now().isoformat(),
        }

        search_key = (
            f"{track.get('title', '')}|"
            f"{track.get('artist', '')}"
        )

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
            "plex_id": str(
                plex_track.get("plex_id")
                or plex_id
                or ""
            ),
            "title": repair_text(
                plex_track.get("title", "")
            ),
            "artist": repair_text(
                plex_track.get("artist", "")
            ),
            "album": repair_text(
                plex_track.get("album", "")
            ),
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
        current["matched_track"] = (
            self._plex_match_snapshot(
                plex_track,
                plex_id,
            )
        )
        current["updated_at"] = (
            datetime.now().isoformat()
        )
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
            previous = record.get(
                "matched_track"
            )

            if isinstance(previous, dict):
                snapshot = {
                    "plex_id": str(
                        previous.get("plex_id")
                        or plex_id
                        or ""
                    ),
                    "title": repair_text(
                        previous.get("title", "")
                    ),
                    "artist": repair_text(
                        previous.get("artist", "")
                    ),
                    "album": repair_text(
                        previous.get("album", "")
                    ),
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
        current["updated_at"] = (
            datetime.now().isoformat()
        )

        if matched_track is not None:
            current["matched_track"] = (
                self._plex_match_snapshot(
                    matched_track,
                    plex_id,
                )
            )

        bucket[search_key] = current
        if provenance == 'manual' and not getattr(self.config,'read_only',False):
            for playlist in self.config.config.get('playlists',[]):
                if f"{playlist.get('source','')}:{playlist.get('source_id','')}" == mapping_key:
                    playlist['ready_to_sync'] = True


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
            value = str(
                record.get("provenance", "")
            ).strip().lower()
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


    @staticmethod
    def _source_track_snapshot_key(track: dict) -> str:
        """Return a stable source-track identity for sync-to-sync changes."""
        source_id = str(
            track.get("source_id", "") or ""
        ).strip()

        if source_id:
            return f"id:{source_id}"

        title = Matcher._normalize_match_text(
            track.get("title", "")
        )
        artist = Matcher._normalize_match_text(
            track.get("artist", "")
        )
        return f"meta:{title}|{artist}"


    @classmethod
    def _snapshot_entries(
        cls,
        tracks: List[dict],
    ) -> List[dict]:
        """Convert current source tracks to lightweight persistent entries."""
        entries = []

        for track in tracks:
            entries.append(
                {
                    "duration": track.get("duration"),
                    "artists": track.get("artists", []),
                    "key": cls._source_track_snapshot_key(
                        track
                    ),
                    "source_id": str(
                        track.get("source_id", "") or ""
                    ),
                    "title": repair_text(
                        track.get("title", "")
                    ),
                    "artist": repair_text(
                        track.get("artist", "")
                    ),
                    "album": repair_text(
                        track.get("album", "")
                    ),
                }
            )

        return entries


    def _source_change_report(
        self,
        mapping_key: str,
        tracks: List[dict],
    ) -> dict:
        """
        Compare current source contents to the last successful snapshot.

        Counts duplicate occurrences correctly. On the first tracked sync,
        establish a baseline rather than labeling every track as ADDED.
        """
        current_entries = self._snapshot_entries(
            tracks
        )
        previous_entries = self.config.source_snapshots.get(
            mapping_key
        )

        if not isinstance(previous_entries, list):
            return {
                "baseline": True,
                "current_entries": current_entries,
                "added": [],
                "removed": [],
                "added_indices": set(),
            }

        previous_counts = Counter(
            str(entry.get("key", ""))
            for entry in previous_entries
        )
        current_counts = Counter(
            str(entry.get("key", ""))
            for entry in current_entries
        )

        added = []
        added_indices = set()
        seen_current = Counter()

        for index, entry in enumerate(
            current_entries
        ):
            key = str(entry.get("key", ""))
            seen_current[key] += 1

            if seen_current[key] > previous_counts[key]:
                added.append(entry)
                added_indices.add(index)

        removed = []
        seen_previous = Counter()

        for entry in previous_entries:
            key = str(entry.get("key", ""))
            seen_previous[key] += 1

            if seen_previous[key] > current_counts[key]:
                removed.append(entry)

        return {
            "baseline": False,
            "current_entries": current_entries,
            "added": added,
            "removed": removed,
            "added_indices": added_indices,
        }


    def _save_source_snapshot(
        self,
        mapping_key: str,
        tracks: List[dict],
    ):
        """Persist the latest source contents after a real sync."""
        self.config.source_snapshots[
            mapping_key
        ] = self._snapshot_entries(tracks)


    @staticmethod
    def _print_source_changes(change_report: dict):
        """Display source playlist additions/removals before matching."""
        if change_report.get("baseline"):
            print(
                "  Source change tracking: "
                "baseline will be created after this sync"
            )
            return

        added = change_report.get("added", [])
        removed = change_report.get(
            "removed",
            [],
        )

        if not added and not removed:
            print(
                "  Source changes since last sync: none"
            )
            return

        print(
            section_header(
                "SOURCE PLAYLIST CHANGES"
            )
        )

        for track in added:
            print(
                f"  ADDED   {track.get('title', '')} - "
                f"{track.get('artist', '')} "
                f"{source_album_display(track)}"
            )

        for track in removed:
            print(
                f"  REMOVED {track.get('title', '')} - "
                f"{track.get('artist', '')} "
                f"{source_album_display(track)}"
            )


    def _get_plex(self):
        """Get Plex instance, initialize if needed."""

        if self.plex is None:
            plex_cfg = self.config.get_plex()
            self.plex = PlexAPI(
                plex_cfg["url"],
                plex_cfg["token"],
                plex_cfg["music_library_key"],
                plex_cfg.get(
                    "music_library_name",
                    "",
                ),
                strict_errors=True,
            )

        return self.plex


    from .track_bridge import scoped as _catalog_scope


    @_catalog_scope
    def _match_source_tracks(
        self,
        source_tracks: List[dict],
        mapping_key: str,
        plex_library: List[dict] = None,
        source_added_indices: set = None,
        record_provenance: bool = True,
        mark_new_matches: bool = True,
    ) -> Tuple[
        List[str],
        List[dict],
        dict,
        Optional[List[dict]],
        dict,
    ]:
        """
        Match source tracks against Plex.

        Returns:
            matched Plex IDs in source order,
            unmatched source tracks,
            updated playlist mapping,
            Plex library for reuse,
            match-change statistics.

        Explicit manual mappings are respected while their Plex ID exists.
        Automatic and legacy mappings are rechecked for recording/guest
        identity conflicts. Missing or incompatible saved tracks are LOST
        unless a fresh automatic match is permitted and succeeds.
        """
        from . import jobs
        jobs.progress("Loading Plex library")
        plex = self._get_plex()
        source_added_indices = (
            source_added_indices or set()
        )

        original_mapping = dict(
            self.config.mapping.get(
                mapping_key,
                {},
            )
        )
        playlist_mapping = dict(
            original_mapping
        )

        if plex_library is None:
            print("→ Scanning Plex library...")
            plex_library = plex.search_library("")

            if not plex_library:
                print("✗ No Plex music tracks were found.")
                return (
                    [],
                    source_tracks,
                    playlist_mapping,
                    plex_library,
                    {
                        "new_matches": [],
                        "lost_matches": [],
                        "recovered_lost": [],
                        "stale_mappings": [],
                        "ignored_tracks": [],
                    },
                )

            print(
                f"  Found {len(plex_library)} tracks in Plex library"
            )

        plex_by_id = {
            str(track.get("plex_id")): track
            for track in plex_library
            if track.get("plex_id") is not None
        }

        source_keys={f"{t.get('title','')}|{t.get('artist','')}" for t in source_tracks}
        unused=set(original_mapping)-source_keys
        if unused:
            jobs.output(f'{len(unused)} stored mappings do not belong to the current source track list; not counted as matched')
        print("→ Matching tracks...")

        matched_tracks = []
        unmatched = []
        new_matches = []
        lost_matches = []
        recovered_lost = []
        stale_mappings = []
        ignored_matches = []
        blocked_artists={Matcher._normalize_match_text(v.get('name','')) for v in self.config.repository.load('blocked_artists').values()} if mapping_key.startswith('text:') else set()

        for i, track in enumerate(
            source_tracks,
            1,
        ):
            if i == 1 or i % 10 == 0 or i == len(source_tracks):
                jobs.progress(f"Comparing track {i} of {len(source_tracks)} · resolving saved matches", completed=i, total=len(source_tracks))
            search_key = (
                f"{track['title']}|{track['artist']}"
            )
            was_mapped = search_key in original_mapping
            source_added = (
                i - 1
            ) in source_added_indices

            if self._is_track_ignored(
                mapping_key,
                track,
            ) or Matcher._normalize_match_text(track.get('artist','')) in blocked_artists:
                ignored_matches.append(
                    dict(track)
                )

                if record_provenance:
                    playlist_mapping.pop(
                        search_key,
                        None,
                    )
                    self._remove_match_provenance(
                        mapping_key,
                        search_key,
                    )

                status_bits = [
                    colored(
                        "IGNORED",
                        Colors.YELLOW,
                    )
                ]

                if source_added:
                    status_bits.append(
                        colored(
                            "NEW",
                            Colors.MAGENTA,
                        )
                    )

                print(
                    f"  [{i}/{len(source_tracks)}] "
                    f"{colored('○', Colors.YELLOW)} "
                    f"{' '.join(status_bits)} "
                    f"{colored(track['title'], Colors.CYAN)} - "
                    f"{colored(track['artist'], Colors.GREEN)} "
                    f"{source_album_display(track)}"
                )
                continue

            matched_track = None
            plex_id = None
            cached_valid = False
            stale_cached_mapping = False
            stale_provenance = "legacy"
            previous_match = {
                "plex_id": "",
                "title": "",
                "artist": "",
                "album": "",
            }
            allow_automatic_match = True

            if search_key in playlist_mapping:
                cached_id = str(
                    playlist_mapping[search_key]
                )
                matched_track = plex_by_id.get(
                    cached_id
                )

                if (matched_track is not None
                        and self._get_match_provenance(mapping_key,search_key)!='manual'
                        and Matcher._recording_version_mismatch(track,matched_track)):
                    jobs.output('Reconsidering automatic match: source and saved track have different recording or featured-artist identities')
                    matched_track=None

                if matched_track is not None:
                    plex_id = cached_id
                    cached_valid = True

                    if record_provenance:
                        cached_provenance = (
                            self._get_match_provenance(
                                mapping_key,
                                search_key,
                            )
                        )

                        # Legacy mappings predate provenance tracking. Only
                        # for those mappings, independently run the current
                        # automatic matcher without allowing the saved
                        # mapping to short-circuit it. If today's matcher
                        # independently chooses the exact same Plex track,
                        # it is safe to promote the mapping to automatic.
                        #
                        # Manual and already-automatic mappings never enter
                        # this validation path.
                        if cached_provenance == "legacy":
                            current_auto_id = (
                                Matcher.match_track(
                                    track,
                                    plex_library,
                                    {},
                                )
                            )

                            if (
                                current_auto_id is not None
                                and str(current_auto_id)
                                == cached_id
                            ):
                                self._set_match_provenance(
                                    mapping_key,
                                    search_key,
                                    "automatic",
                                    matched_track=matched_track,
                                    plex_id=cached_id,
                                )
                            else:
                                self._remember_match_snapshot(
                                    mapping_key,
                                    search_key,
                                    matched_track,
                                    cached_id,
                                )
                        else:
                            self._remember_match_snapshot(
                                mapping_key,
                                search_key,
                                matched_track,
                                cached_id,
                            )
                else:
                    stale_cached_mapping = True

                    stale_provenance = (
                        self._get_match_provenance(
                            mapping_key,
                            search_key,
                        )
                    )
                    previous_match = (
                        self._previous_match_snapshot(
                            mapping_key,
                            search_key,
                            cached_id,
                        )
                    )
                    stale_mappings.append(
                        {
                            "source": dict(track),
                            "previous_match": previous_match,
                            "previous_provenance": stale_provenance,
                        }
                    )

                    # A stale mapping that was explicitly manual -- or a
                    # legacy mapping whose origin cannot be known -- should
                    # never be silently replaced by a fresh automatic choice.
                    # Surface it as LOST for human review instead.
                    if stale_provenance in (
                        "manual",
                        "legacy",
                    ):
                        allow_automatic_match = False

                    playlist_mapping.pop(
                        search_key,
                        None,
                    )

                    if record_provenance:
                        self._remove_match_provenance(
                            mapping_key,
                            search_key,
                        )

            if (
                not cached_valid
                and allow_automatic_match
            ):
                plex_id = Matcher.match_track(
                    track,
                    plex_library,
                    playlist_mapping,
                )

                if plex_id:
                    plex_id = str(plex_id)
                    playlist_mapping[
                        search_key
                    ] = plex_id
                    matched_track = plex_by_id.get(
                        plex_id
                    )

                    if record_provenance:
                        self._set_match_provenance(
                            mapping_key,
                            search_key,
                            "automatic",
                            matched_track=matched_track,
                            plex_id=plex_id,
                        )

            if plex_id:
                matched_tracks.append(
                    str(plex_id)
                )

                if (
                    stale_cached_mapping
                    and stale_provenance == "automatic"
                ):
                    recovered_lost.append(
                        {
                            "source": dict(track),
                            "replacement_plex_id": str(plex_id),
                        }
                    )

                is_new_match = (
                    mark_new_matches
                    and not was_mapped
                )

                if is_new_match:
                    new_matches.append(track)

                status_bits = []

                if source_added:
                    status_bits.append(
                        colored("NEW", Colors.MAGENTA)
                    )

                matched_info = ""

                if matched_track:
                    album = matched_track.get(
                        "album",
                        "",
                    )
                    matched_title = colored(
                        matched_track["title"],
                        Colors.CYAN,
                    )
                    matched_artist = colored(
                        matched_track["artist"],
                        Colors.GREEN,
                    )

                    if album:
                        album_str = colored(
                            f"({album})",
                            Colors.YELLOW,
                        )
                        matched_info = (
                            f" → {matched_title} - "
                            f"{matched_artist} {album_str}"
                        )
                    else:
                        matched_info = (
                            f" → {matched_title} - "
                            f"{matched_artist}"
                        )

                source_title = colored(
                    track["title"],
                    Colors.CYAN,
                )
                source_artist = colored(
                    track["artist"],
                    Colors.GREEN,
                )
                status_text = (
                    " " + " ".join(status_bits)
                    if status_bits
                    else ""
                )

                print(
                    f"  [{i}/{len(source_tracks)}] "
                    f"{colored('✓', Colors.GREEN)}"
                    f"{status_text} "
                    f"{source_title} - {source_artist} "
                    f"{source_album_display(track)}"
                    f"{matched_info}"
                )
                continue

            unmatched_track = dict(track)

            is_lost = (
                was_mapped
                and stale_cached_mapping
            )

            if is_lost:
                previous_provenance = (
                    stale_provenance
                )

                unmatched_track["status"] = "lost"
                unmatched_track["previous_match"] = (
                    previous_match
                )
                unmatched_track[
                    "previous_provenance"
                ] = previous_provenance
                lost_matches.append(
                    unmatched_track
                )

            unmatched.append(
                unmatched_track
            )

            source_title = colored(
                track["title"],
                Colors.CYAN,
            )
            source_artist = colored(
                track["artist"],
                Colors.GREEN,
            )

            status_bits = []

            if is_lost:
                status_bits.append(
                    colored("LOST", Colors.RED)
                )

            if source_added:
                status_bits.append(
                    colored("NEW", Colors.MAGENTA)
                )

            status_text = (
                " " + " ".join(status_bits)
                if status_bits
                else ""
            )

            print(
                f"  [{i}/{len(source_tracks)}] "
                f"{colored('✗', Colors.RED)}"
                f"{status_text} "
                f"{source_title} - {source_artist} "
                f"{source_album_display(track)}"
            )

            if is_lost:
                previous_title = (
                    previous_match.get(
                        "title",
                        "",
                    )
                )
                previous_artist = (
                    previous_match.get(
                        "artist",
                        "",
                    )
                )
                previous_album = (
                    previous_match.get(
                        "album",
                        "",
                    )
                )
                previous_id = (
                    previous_match.get(
                        "plex_id",
                        "",
                    )
                )

                if previous_title or previous_artist:
                    previous_text = (
                        f"{previous_title} - "
                        f"{previous_artist}"
                    )

                    if previous_album:
                        previous_text += (
                            f" ({previous_album})"
                        )

                    print(
                        "      Previous Plex match: "
                        f"{previous_text} "
                        f"[{provenance_display(previous_provenance)}]"
                    )
                elif previous_id:
                    print(
                        "      Previous Plex match: "
                        f"metadata unavailable "
                        f"(Plex ID {previous_id}) "
                        f"[{provenance_display(previous_provenance)}]"
                    )

        return (
            matched_tracks,
            unmatched,
            playlist_mapping,
            plex_library,
            {
                "new_matches": new_matches,
                "lost_matches": lost_matches,
                "recovered_lost": recovered_lost,
                "stale_mappings": stale_mappings,
                "ignored_tracks": ignored_matches,
            },
        )


    def _store_unmatched(
        self,
        mapping_key: str,
        unmatched: List[dict],
    ):
        """Store unmatched tracks."""

        if unmatched:
            stored_tracks = []

            for t in unmatched:
                stored = {
                    "title": t["title"],
                    "artist": t["artist"],
                    "album": t.get("album", ""),
                    "source_id": t.get("source_id", ""),
                    "duration": t.get("duration"),
                    "artists": t.get("artists", []),
                }

                if t.get("status") == "lost":
                    stored["status"] = "lost"
                    stored["previous_match"] = dict(
                        t.get(
                            "previous_match",
                            {},
                        )
                    )
                    stored["previous_provenance"] = (
                        t.get(
                            "previous_provenance",
                            "legacy",
                        )
                    )

                stored_tracks.append(
                    stored
                )

            self.config.missing[
                mapping_key
            ] = stored_tracks

            print(
                f"\n⚠ {len(unmatched)} tracks unmatched"
            )
        else:
            # Clear stale missing-track records.
            self.config.missing.pop(mapping_key, None)


    def _build_new_plex_playlist(
        self,
        playlist_name: str,
        description: str,
        matched_tracks: List[str],
        artwork_url: str = None,
    ) -> Optional[str]:
        """
        Create a Plex playlist and populate it in source order.

        The first matched track is supplied during playlist creation
        because Plex requires a media URI when creating a normal
        audio playlist.
        """

        if not matched_tracks:
            print(
                "✗ Cannot create Plex playlist: "
                "no tracks matched to the Plex library."
            )
            return None

        plex = self._get_plex()

        print(
            "\n→ Creating Plex playlist with "
            "first matched track..."
        )

        playlist_id = plex.create_playlist(
            playlist_name,
            matched_tracks[0],
            description,
        )

        if not playlist_id:
            print("✗ Failed to create Plex playlist.")
            return None

        print(
            f"✓ Created Plex playlist "
            f"'{playlist_name}' (ID: {playlist_id})"
        )

        # Rebuild through the same occurrence-aware path used by sync.
        added = len(matched_tracks) if plex.replace_playlist_tracks(playlist_id, matched_tracks) else 0

        print(
            f"✓ Added {added}/{len(matched_tracks)} "
            "matched tracks"
        )

        if added != len(matched_tracks):
            print(
                "⚠ Some matched tracks could not be added "
                "to the Plex playlist."
            )

        # Update artwork if available
        if artwork_url:
            plex.update_playlist_metadata(
                playlist_id,
                playlist_name,
                description,
                artwork_url,
            )

        return playlist_id




    def sync_playlist(
        self,
        playlist_entry: dict,
        dry_run: bool = False,
    ):
        """
        Sync a specific playlist.

        When dry_run=True, Harmonia performs source fetching, Plex
        library scanning, and matching only. It does not:
          - modify the Plex playlist;
          - update playlist metadata/artwork;
          - write config.json, mapping.json, or missing_tracks.json;
          - update last_synced.
        """

        source_type = playlist_entry["source"]
        source_url = Config._normalize_url_input(
            playlist_entry["source_url"]
        )
        playlist_id = Config._extract_id(
            source_url,
            source_type,
        )

        if not playlist_id:
            print(
                f"✗ Could not extract playlist ID from stored URL: "
                f"{source_url}"
            )
            return self._run_stats(
                errors=1,
            )

        canonical_url = Config._canonical_source_url(
            source_url,
            source_type,
        )

        # Normal syncs continue to repair older stored config entries.
        # Dry runs intentionally keep all local state untouched.
        if not dry_run:
            changed = False
            old_source_id = playlist_entry.get("source_id")

            if old_source_id != playlist_id:
                old_mapping_key = (
                    f"{source_type}:{old_source_id}"
                    if old_source_id
                    else None
                )
                new_mapping_key = (
                    f"{source_type}:{playlist_id}"
                )

                if (
                    old_mapping_key
                    and old_mapping_key != new_mapping_key
                ):
                    if old_mapping_key in self.config.mapping:
                        existing = self.config.mapping.pop(
                            old_mapping_key
                        )
                        self.config.mapping.setdefault(
                            new_mapping_key,
                            {},
                        ).update(existing)

                    if old_mapping_key in self.config.missing:
                        existing_missing = (
                            self.config.missing.pop(
                                old_mapping_key
                            )
                        )

                        if new_mapping_key not in self.config.missing:
                            self.config.missing[
                                new_mapping_key
                            ] = existing_missing

                    if old_mapping_key in self.config.match_metadata:
                        existing_metadata = (
                            self.config.match_metadata.pop(
                                old_mapping_key
                            )
                        )
                        self.config.match_metadata.setdefault(
                            new_mapping_key,
                            {},
                        ).update(existing_metadata)

                    if old_mapping_key in self.config.source_snapshots:
                        existing_snapshot = (
                            self.config.source_snapshots.pop(
                                old_mapping_key
                            )
                        )
                        if new_mapping_key not in self.config.source_snapshots:
                            self.config.source_snapshots[
                                new_mapping_key
                            ] = existing_snapshot

                    if old_mapping_key in self.config.ignored_tracks:
                        existing_ignored = (
                            self.config.ignored_tracks.pop(
                                old_mapping_key
                            )
                        )
                        self.config.ignored_tracks.setdefault(
                            new_mapping_key,
                            {},
                        ).update(existing_ignored)

                playlist_entry["source_id"] = playlist_id
                changed = True

            if playlist_entry.get("source_url") != canonical_url:
                playlist_entry["source_url"] = canonical_url
                changed = True

            if changed:
                self.config.save()

        source_url = canonical_url

        plex_playlist_id = playlist_entry[
            "plex_playlist_id"
        ]
        playlist_name = playlist_entry[
            "plex_playlist_name"
        ]

        from .text_playlists import TextSource
        api = TextSource() if source_type == "text" else SpotifyAPI() if source_type == "spotify" else AppleMusicAPI()

        plex = self._get_plex()

        # A saved text source may await its first playable track.
        if not dry_run and plex_playlist_id:
            from . import jobs
            jobs.progress("Checking destination Plex playlist")
            try:
                response = network.get(
                    f"{plex.base_url}/playlists/{plex_playlist_id}",
                    headers=plex.headers, timeout=(5, 10),
                )
                if response.status_code == 404:
                    raise ValueError(
                        f"Plex playlist {plex_playlist_id} is missing or inaccessible (HTTP 404). "
                        "Sync stopped before matching or changing Plex. Check the configured "
                        "Plex account and destination playlist."
                    )
                response.raise_for_status()
                metadata = response.json().get("MediaContainer", {}).get("Metadata", [])
                if not metadata or str(metadata[0].get("ratingKey", "")) != str(plex_playlist_id):
                    raise ValueError("Plex did not confirm the destination playlist. Sync stopped safely.")
            except network.RequestException as exc:
                raise ValueError(
                    f"Could not verify Plex playlist {plex_playlist_id} ({type(exc).__name__}). "
                    "Check Plex connectivity and permissions; no playlist changes were attempted."
                ) from None

        if dry_run:
            print(
                f"\n=== DRY RUN: '{playlist_name}' "
                f"({source_display_label(source_type)}) ==="
            )
        else:
            print(
                f"\n→ Syncing '{playlist_name}' from "
                f"{source_display_label(source_type)}..."
            )

        from . import jobs
        jobs.progress("Loading source playlist: " + playlist_name)
        source_started = time.monotonic()
        try:
            source_tracks, metadata = api.get_playlist_tracks(
                source_url,
                # Artwork is irrelevant during a dry run and can involve
                # additional network work.
                fetch_artwork=not dry_run,
            )
        except Exception as e:
            print(f"✗ Failed to fetch: {e}")
            return self._run_stats(
                errors=1,
            )

        print(
            f"  Found {len(source_tracks)} tracks"
        )

        jobs.output(f'Source fetch completed in {time.monotonic()-source_started:.2f}s')
        match_started = time.monotonic()
        mapping_key = (
            f"{source_type}:{playlist_id}"
        )

        observed_source_tracks=source_tracks
        from .source_observations import validate as validate_source
        complete=validate_source(self.config.repository,mapping_key,observed_source_tracks,metadata,self.config.source_snapshots.get(mapping_key))

        if not dry_run:
            from .source_history import record
            record(self.config.repository,mapping_key,observed_source_tracks,complete)
        source_changes = self._source_change_report(
            mapping_key,
            observed_source_tracks,
        )
        self._print_source_changes(
            source_changes
        )
        from .playlist_overlays import apply as apply_playlist_overlay
        source_tracks=apply_playlist_overlay(self.config.repository,mapping_key,observed_source_tracks)
        if len(source_tracks)!=len(observed_source_tracks):
            jobs.output(f'Applied playlist edits: {len(observed_source_tracks)} source tracks → {len(source_tracks)} effective tracks')

        (
            matched_tracks,
            unmatched,
            playlist_mapping,
            plex_library,
            match_stats,
        ) = self._match_source_tracks(
            source_tracks,
            mapping_key,
            source_added_indices=source_changes[
                "added_indices"
            ],
            record_provenance=not dry_run,
            mark_new_matches=True,
        )

        if dry_run:
            print(section_header("DRY RUN SUMMARY"))
            print(
                f"Source tracks: {len(source_tracks)}"
            )
            print(
                f"Would match:    {len(matched_tracks)}"
            )
            print(
                f"New matches:    {len(match_stats['new_matches'])}"
            )
            print(
                f"Lost matches:   {len(match_stats['lost_matches'])}"
            )
            print(
                f"Ignored:        "
                f"{len(match_stats.get('ignored_tracks', []))}"
            )

            if source_changes["baseline"]:
                print("Source ADDED:   baseline")
                print("Source REMOVED: baseline")
            else:
                print(
                    f"Source ADDED:   {len(source_changes['added'])}"
                )
                print(
                    f"Source REMOVED: {len(source_changes['removed'])}"
                )

            print(
                f"Unmatched:      {len(unmatched)}"
            )

            if unmatched:
                print("\nUnmatched tracks:")
                for track in unmatched:
                    print(
                        f"  ✗ {track['title']} - "
                        f"{track['artist']} "
                        f"{source_album_display(track)}"
                    )

            print(
                "\n✓ DRY RUN complete. "
                "No changes were made to Plex."
            )
            print(
                "  config.json, mapping.json, missing_tracks.json, "
                "match_metadata.json, source_snapshots.json, "
                "ignored_tracks.json, schema state, and last sync times "
                "were also left unchanged."
            )
            return self._run_stats(
                tested=1,
                source_added=(
                    0
                    if source_changes["baseline"]
                    else len(source_changes["added"])
                ),
                source_removed=(
                    0
                    if source_changes["baseline"]
                    else len(source_changes["removed"])
                ),
                new_matches=len(
                    match_stats["new_matches"]
                ),
                recovered_lost=len(
                    match_stats.get(
                        "recovered_lost",
                        [],
                    )
                ),
                newly_lost=len(
                    match_stats["lost_matches"]
                ),
                unresolved=len(
                    unmatched
                ),
                ignored=len(
                    match_stats.get(
                        "ignored_tracks",
                        [],
                    )
                ),
            )

        jobs.output(f'Matching and library preparation completed in {time.monotonic()-match_started:.2f}s')
        self._store_unmatched(
            mapping_key,
            unmatched,
        )

        self.config.mapping[mapping_key] = (
            playlist_mapping
        )

        print(
            section_header(
                "SYNCING TO PLEX"
            )
        )

        operation_error = False

        if not plex_playlist_id:
            if not matched_tracks:
                self._save_source_snapshot(mapping_key,observed_source_tracks)
                self.config.save()
                if source_type=='text':
                    try:
                        from .sync_projection import update as publish_sync_page
                        publish_sync_page(self.config, playlist_entry, source_tracks, plex_library, metadata, True)
                    except Exception as exc:
                        jobs.output('Playlist page update deferred; reopen the page to retry: ' + str(exc))
                    jobs.output('Text playlist saved. No playable tracks yet; request missing albums and sync after they arrive.')
                    return self._run_stats(unresolved=len(unmatched))
                raise ValueError('No source tracks currently match playable items in Plex. Resolve or download at least one track, then retry recreation.')
            jobs.progress('Creating a replacement Plex playlist')
            plex_playlist_id=self._build_new_plex_playlist(playlist_name,metadata.get('description',''),matched_tracks,'')
            if not plex_playlist_id:raise ValueError('Plex did not confirm the replacement playlist. Inspect Plex before retrying; a playlist may have been created.')
            playlist_entry['plex_playlist_id']=plex_playlist_id
            self.config.save()
        if matched_tracks:
            operation_error = not plex.replace_playlist_tracks(plex_playlist_id, matched_tracks)
        elif source_type=='text':
            operation_error=not plex.replace_playlist_tracks(plex_playlist_id, [])
        else:
            print('⚠ No matched tracks; Plex playlist left unchanged.')

        jobs.progress("Updating playlist metadata and artwork", check=False)
        playlist_entry['source_name']=repair_text(metadata.get('name','') or playlist_entry.get('source_name') or playlist_name)
        playlist_entry['source_owner']=metadata.get('owner') or metadata.get('curator') or playlist_entry.get('source_owner','')
        playlist_entry['plex_playlist_name']=playlist_entry.get('custom_name') or playlist_entry['source_name']


        image_url = metadata.get(
            "image_url",
            "",
        )

        if image_url:
            print(
                "  → Artwork URL found in source"
            )
        else:
            print(
                "  ⚠ No artwork URL found in source"
            )

        # Reuse source/matching work; verify only the destination after writes.
        from .sync_health import persist_sync_health
        if not operation_error:
            jobs.progress("Verifying Plex playlist and updating health", check=False)
            try:
                actual = plex.get_playlist_items(str(plex_playlist_id))
                persist_sync_health(self.config, playlist_entry, source_tracks, matched_tracks,
                                    unmatched, match_stats, actual)
                from .verification import compare, describe, track_details
                verification = compare(matched_tracks, actual)
                operation_error = not verification['ok'] if matched_tracks else False
                if verification['duplicates_collapsed']:
                    print(f"⚠ The playlist contained {verification['duplicates_collapsed']} duplicate entries that did not carry over to Plex.")
                elif operation_error:
                    print('✗ ' + describe(verification))
                    for detail in track_details(matched_tracks, actual, plex_library):
                        print('  ' + detail)
            except Exception as exc:
                operation_error = True
                self.config.repository.record_health_attempt(mapping_key, "Sync verification failed: " + str(exc))
                print("Could not verify synced playlist:", exc)
        if not operation_error:
            playlist_entry["last_synced"] = datetime.now().astimezone().isoformat()
            playlist_entry['ready_to_sync'] = False
            from .playlist_description import render
            plex.update_playlist_metadata(plex_playlist_id,playlist_entry['plex_playlist_name'],
                render(metadata.get('description',''),playlist_entry,len(source_tracks),len(matched_tracks),len(unmatched),len(match_stats.get('ignored_tracks',[]))),metadata.get('image_url',''))
        self._save_source_snapshot(
            mapping_key,
            observed_source_tracks,
        )

        self.config.save()
        try:
            from .sync_projection import update as publish_sync_page
            publish_sync_page(self.config, playlist_entry, source_tracks, plex_library, metadata, not operation_error)
        except Exception as exc:
            jobs.output('Playlist page update deferred; reopen the page to retry: ' + str(exc))

        if operation_error:
            print('✗ Sync update or verification failed. Review the differences above.')
        elif matched_tracks:
            print('✓ Sync completed with missing tracks.' if unmatched else '✓ Sync complete!')
        else:
            print(
                "✓ Sync complete "
                "(no changes - no matched tracks)"
            )

        if unmatched:
            print(
                f"⚠ {len(unmatched)} tracks were not "
                "added because they are unmatched."
            )

        print(section_header("SYNC SUMMARY"))
        print(f"Source tracks:   {len(source_tracks)}")
        print(f"Matched:         {len(matched_tracks)}")
        print(
            f"New matches:     {len(match_stats['new_matches'])}"
        )
        print(
            f"Lost matches:    {len(match_stats['lost_matches'])}"
        )
        print(
            f"Ignored:         "
            f"{len(match_stats.get('ignored_tracks', []))}"
        )

        if source_changes["baseline"]:
            print("Source ADDED:    baseline")
            print("Source REMOVED:  baseline")
        else:
            print(
                f"Source ADDED:    {len(source_changes['added'])}"
            )
            print(
                f"Source REMOVED:  {len(source_changes['removed'])}"
            )

        print(f"Unresolved:      {len(unmatched)}")

        return self._run_stats(
            tested=1,
            synced=1,
            source_added=(
                0
                if source_changes["baseline"]
                else len(source_changes["added"])
            ),
            source_removed=(
                0
                if source_changes["baseline"]
                else len(source_changes["removed"])
            ),
            new_matches=len(
                match_stats["new_matches"]
            ),
            recovered_lost=len(
                match_stats.get(
                    "recovered_lost",
                    [],
                )
            ),
            newly_lost=len(
                match_stats["lost_matches"]
            ),
            unresolved=len(
                unmatched
            ),
            ignored=len(
                match_stats.get(
                    "ignored_tracks",
                    [],
                )
            ),
            errors=(
                1
                if operation_error
                else 0
            ),
        )


    def audit_playlist_without_sync(
        self,
        playlist_entry: dict,
        write_missing: bool = True,
    ):
        """
        Audit an Auto Sync OFF playlist without changing its Plex playlist.

        The source is fetched, source/Plex counts are compared, and matching
        is run against the selected Plex music library. Only missing_tracks.json
        may be refreshed. Mappings, provenance, source snapshots, last_synced,
        playlist metadata, artwork, and Plex playlist contents are untouched.
        """
        source_type = playlist_entry["source"]
        source_url = Config._normalize_url_input(
            playlist_entry.get("source_url", "")
        )
        playlist_id = Config._extract_id(
            source_url,
            source_type,
        ) or str(
            playlist_entry.get("source_id", "")
        ).strip()
        playlist_name = playlist_entry[
            "plex_playlist_name"
        ]
        plex_playlist_id = playlist_entry[
            "plex_playlist_id"
        ]

        print(
            f"\n→ Auditing '{playlist_name}' "
            f"({source_display_label(source_type)}) - "
            f"Auto sync: {auto_sync_display(False)}"
        )

        if not playlist_id:
            print("✗ Could not determine source playlist ID")
            return self._run_stats(
                errors=1,
            )

        from .text_playlists import TextSource
        api = TextSource() if source_type == "text" else SpotifyAPI() if source_type == "spotify" else AppleMusicAPI()
        mapping_key = f"{source_type}:{playlist_id}"

        try:
            observed_source_tracks, metadata = api.get_playlist_tracks(
                source_url,
                fetch_artwork=False,
            )
            from .source_observations import validate as validate_source
            validate_source(self.config.repository,mapping_key,observed_source_tracks,metadata,
                            self.config.source_snapshots.get(mapping_key))
            from .playlist_overlays import apply as apply_playlist_overlay
            source_tracks=apply_playlist_overlay(self.config.repository,mapping_key,observed_source_tracks)
        except Exception as e:
            print(f"✗ Failed to fetch source playlist: {e}")
            return self._run_stats(
                errors=1,
            )

        plex = self._get_plex()
        plex_count = plex.get_playlist_item_count(
            plex_playlist_id
        )
        source_changes = self._source_change_report(
            mapping_key,
            observed_source_tracks,
        )

        (
            matched_tracks,
            unmatched,
            _playlist_mapping,
            _plex_library,
            match_stats,
        ) = self._match_source_tracks(
            source_tracks,
            mapping_key,
            source_added_indices=set(),
            record_provenance=False,
            mark_new_matches=False,
        )

        self._store_unmatched(
            mapping_key,
            unmatched,
        )

        if write_missing:
            self.config.save_missing_only()

        print(
            section_header(
                "AUTO SYNC OFF - AUDIT SUMMARY"
            )
        )
        print(f"Source tracks:      {len(source_tracks)}")
        print(
            "Plex playlist:      "
            + (
                str(plex_count)
                if plex_count is not None
                else "unknown"
            )
        )
        print(f"Matched in library: {len(matched_tracks)}")
        print(
            f"Ignored:            "
            f"{len(match_stats.get('ignored_tracks', []))}"
        )
        print(f"Unresolved:         {len(unmatched)}")

        if plex_count is None:
            print(
                "⚠ Could not compare source and Plex playlist counts."
            )
        elif plex_count != len(source_tracks):
            difference = len(source_tracks) - plex_count
            direction = (
                f"{difference} fewer"
                if difference > 0
                else f"{abs(difference)} more"
            )
            print(
                "⚠ Track count mismatch: Plex has "
                f"{direction} item"
                f"{'s' if abs(difference) != 1 else ''} than the source."
            )
        else:
            print(
                f"✓ Source and Plex counts match ({plex_count})."
            )

        if write_missing:
            print(
                "✓ missing_tracks.json refreshed; Plex playlist left unchanged."
            )
        else:
            print(
                "✓ Dry-run audit complete; no local state or Plex data changed."
            )

        return self._run_stats(
            tested=1,
            audited=1,
            source_added=(
                0
                if source_changes["baseline"]
                else len(source_changes["added"])
            ),
            source_removed=(
                0
                if source_changes["baseline"]
                else len(source_changes["removed"])
            ),
            recovered_lost=len(
                match_stats.get(
                    "recovered_lost",
                    [],
                )
            ),
            newly_lost=len(
                match_stats["lost_matches"]
            ),
            unresolved=len(
                unmatched
            ),
            ignored=len(
                match_stats.get(
                    "ignored_tracks",
                    [],
                )
            ),
        )


    def sync_favorites(
        self,
    ):
        """
        Explicitly sync only playlists marked as favorites.

        This is a manual action, so favorite playlists are synced even if
        Auto Sync is OFF.
        """
        favorites = [
            playlist
            for playlist in self.config.config.get(
                "playlists",
                [],
            )
            if self._playlist_favorite(
                playlist
            )
        ]

        if not favorites:
            print(
                "✗ No favorite playlists are configured. "
                "Use Settings → Manage favorite playlists first."
            )
            return

        print(
            section_header(
                "SYNCING FAVORITE PLAYLISTS"
            )
        )
        print(
            f"Found {len(favorites)} favorite playlist"
            f"{'s' if len(favorites) != 1 else ''}.\n"
        )

        totals = self._run_stats()

        for playlist in favorites:
            try:
                result = self.sync_playlist(
                    playlist
                )

                if isinstance(
                    result,
                    dict,
                ):
                    for key in totals:
                        totals[key] += int(
                            result.get(
                                key,
                                0,
                            )
                            or 0
                        )
                else:
                    totals["errors"] += 1

            except Exception as e:
                totals["errors"] += 1
                print(
                    f"✗ Error processing "
                    f"'{playlist.get('plex_playlist_name', 'Unknown')}': "
                    f"{e}"
                )

        print(
            section_header(
                "FAVORITES SYNC SUMMARY"
            )
        )
        print(
            f"Favorite playlists synced: {totals['synced']}"
        )
        print(
            f"Source additions:          {totals['source_added']}"
        )
        print(
            f"Source removals:           {totals['source_removed']}"
        )
        print(
            f"New automatic matches:     {totals['new_matches']}"
        )
        print(
            f"Recovered LOST:            {totals['recovered_lost']}"
        )
        print(
            f"Newly LOST:                {totals['newly_lost']}"
        )
        print(
            f"Unresolved:                {totals['unresolved']}"
        )
        print(
            f"Ignored:                   {totals['ignored']}"
        )
        print(
            f"Errors:                    {totals['errors']}"
        )

        return totals


    def sync_all(
        self,
        dry_run: bool = False,
        respect_auto_sync: bool = False,
    ):
        """Sync/audit all registered playlists and print one run summary."""
        playlists = self.config.config[
            "playlists"
        ]

        if not playlists:
            print("✗ No playlists registered")
            return self._run_stats(
                errors=1,
            )

        if dry_run:
            print(
                section_header(
                    "HARMONIA DRY RUN"
                )
            )
            print(
                "Matching will be tested against Plex, "
                "but nothing will be changed or saved.\n"
            )

        totals = self._run_stats()

        def add_result(result):
            if not isinstance(result, dict):
                totals["errors"] += 1
                return

            for key in totals:
                totals[key] += int(
                    result.get(
                        key,
                        0,
                    )
                    or 0
                )

        if respect_auto_sync and self.config.repository.load('tasks').get('sync_modes_v3'):
            from .sync_policy import eligible
            playlists=eligible(self.config.repository,playlists)
            respect_auto_sync=False
        for playlist in playlists:
            try:
                if (
                    respect_auto_sync
                    and not self._auto_sync_enabled(
                        playlist
                    )
                ):
                    add_result(
                        self.audit_playlist_without_sync(
                            playlist,
                            write_missing=not dry_run,
                        )
                    )
                    continue

                add_result(
                    self.sync_playlist(
                        playlist,
                        dry_run=dry_run,
                    )
                )

            except Exception as e:
                totals["errors"] += 1
                print(
                    f"✗ Error processing "
                    f"'{playlist.get('plex_playlist_name', 'Unknown')}': "
                    f"{e}"
                )

        print(
            section_header(
                "SYNC-ALL RUN SUMMARY"
            )
        )

        if dry_run:
            print(
                f"Playlists tested:        {totals['tested']}"
            )
        else:
            print(
                f"Playlists synced:        {totals['synced']}"
            )

        print(
            f"Auto-sync OFF audited:   {totals['audited']}"
        )
        print(
            f"Source additions:        {totals['source_added']}"
        )
        print(
            f"Source removals:         {totals['source_removed']}"
        )
        print(
            f"New automatic matches:   {totals['new_matches']}"
        )
        print(
            f"Recovered LOST:          {totals['recovered_lost']}"
        )
        print(
            f"Newly LOST:              {totals['newly_lost']}"
        )
        print(
            f"Unresolved:              {totals['unresolved']}"
        )
        print(
            f"Ignored:                 {totals['ignored']}"
        )
        print(
            f"Errors:                  {totals['errors']}"
        )

        if (
            respect_auto_sync
            and totals["synced"] == 0
            and totals["audited"]
            and not dry_run
        ):
            print(
                "\n✓ Automatic sync is OFF for all registered playlists; "
                "diagnostic audits completed."
            )

        return totals










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
            mapping_key = (
                f"{playlist['source']}:{playlist['source_id']}"
            )
            unresolved = self.config.missing.get(
                mapping_key,
                [],
            )

            for track in unresolved:
                title = repair_text(
                    track.get("title", "")
                ).strip()
                artist = repair_text(
                    track.get("artist", "")
                ).strip()
                album = repair_text(
                    track.get("album", "")
                ).strip()

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
                    entry[
                        "lost_occurrence_count"
                    ] += 1

                if album and album.casefold() != "n/a":
                    current_album = (
                        entry.get("album", "").strip()
                    )

                    if (
                        not current_album
                        or current_album.casefold() == "n/a"
                    ):
                        entry["album"] = album

                playlist_name = playlist[
                    "plex_playlist_name"
                ]

                if playlist_name not in entry["playlists"]:
                    entry["playlists"].append(
                        playlist_name
                    )
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
            mapping_key = (
                f"{playlist['source']}:{playlist['source_id']}"
            )
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
                result.append(
                    playlist
                )

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
            mapping_key = (
                f"{playlist['source']}:{playlist['source_id']}"
            )
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
                    matches.append(
                        candidate
                    )
                else:
                    remaining.append(
                        candidate
                    )

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
                search_key = (
                    f"{occurrence.get('title', '')}|"
                    f"{occurrence.get('artist', '')}"
                )
                playlist_mapping[
                    search_key
                ] = plex_id

                self._set_match_provenance(
                    mapping_key,
                    search_key,
                    "manual",
                    matched_track=selected_plex_track,
                    plex_id=plex_id,
                )
                affected += 1

            if remaining:
                self.config.missing[
                    mapping_key
                ] = remaining
            else:
                self.config.missing.pop(
                    mapping_key,
                    None,
                )

            playlist[
                "last_match_attempt"
            ] = now

        if affected:
            self.config.save()

        return affected


