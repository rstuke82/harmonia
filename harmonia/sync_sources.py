"""Source identity repair and playlist change observations."""

from collections import Counter
from typing import List
from .matching import Matcher
from .runtime_output import emit
from .terminal import section_header
from .text_utils import repair_text, source_album_display


class SourceObservations:
    @staticmethod
    def _source_track_snapshot_key(track: dict) -> str:
        """Return a stable source-track identity for sync-to-sync changes."""
        source_id = str(track.get("source_id", "") or "").strip()

        if source_id:
            return f"id:{source_id}"

        title = Matcher._normalize_match_text(track.get("title", ""))
        artist = Matcher._normalize_match_text(track.get("artist", ""))
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
                    "key": cls._source_track_snapshot_key(track),
                    "source_id": str(track.get("source_id", "") or ""),
                    "title": repair_text(track.get("title", "")),
                    "artist": repair_text(track.get("artist", "")),
                    "album": repair_text(track.get("album", "")),
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
        current_entries = self._snapshot_entries(tracks)
        previous_entries = self.config.source_snapshots.get(mapping_key)

        if not isinstance(previous_entries, list):
            return {
                "baseline": True,
                "current_entries": current_entries,
                "added": [],
                "removed": [],
                "added_indices": set(),
            }

        previous_counts = Counter(str(entry.get("key", "")) for entry in previous_entries)
        current_counts = Counter(str(entry.get("key", "")) for entry in current_entries)

        added = []
        added_indices = set()
        seen_current = Counter()

        for index, entry in enumerate(current_entries):
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
        self.config.source_snapshots[mapping_key] = self._snapshot_entries(tracks)

    @staticmethod
    def _print_source_changes(change_report: dict):
        """Display source playlist additions/removals before matching."""
        if change_report.get("baseline"):
            emit("  Source change tracking: baseline will be created after this sync")
            return

        added = change_report.get("added", [])
        removed = change_report.get(
            "removed",
            [],
        )

        if not added and not removed:
            emit("  Source changes since last sync: none")
            return

        emit(section_header("SOURCE PLAYLIST CHANGES"))

        for track in added:
            emit(
                f"  ADDED   {track.get('title', '')} - "
                f"{track.get('artist', '')} "
                f"{source_album_display(track)}"
            )

        for track in removed:
            emit(
                f"  REMOVED {track.get('title', '')} - "
                f"{track.get('artist', '')} "
                f"{source_album_display(track)}"
            )

    def _repair_source_entry(
        self, playlist_entry, source_type, playlist_id, canonical_url, dry_run
    ):
        # Normal syncs continue to repair older stored config entries.
        # Dry runs intentionally keep all local state untouched.
        if not dry_run:
            changed = False
            old_source_id = playlist_entry.get("source_id")

            if old_source_id != playlist_id:
                old_mapping_key = f"{source_type}:{old_source_id}" if old_source_id else None
                new_mapping_key = f"{source_type}:{playlist_id}"

                if old_mapping_key and old_mapping_key != new_mapping_key:
                    if old_mapping_key in self.config.mapping:
                        existing = self.config.mapping.pop(old_mapping_key)
                        self.config.mapping.setdefault(
                            new_mapping_key,
                            {},
                        ).update(existing)

                    if old_mapping_key in self.config.missing:
                        existing_missing = self.config.missing.pop(old_mapping_key)

                        if new_mapping_key not in self.config.missing:
                            self.config.missing[new_mapping_key] = existing_missing

                    if old_mapping_key in self.config.match_metadata:
                        existing_metadata = self.config.match_metadata.pop(old_mapping_key)
                        self.config.match_metadata.setdefault(
                            new_mapping_key,
                            {},
                        ).update(existing_metadata)

                    if old_mapping_key in self.config.source_snapshots:
                        existing_snapshot = self.config.source_snapshots.pop(old_mapping_key)
                        if new_mapping_key not in self.config.source_snapshots:
                            self.config.source_snapshots[new_mapping_key] = existing_snapshot

                    if old_mapping_key in self.config.ignored_tracks:
                        existing_ignored = self.config.ignored_tracks.pop(old_mapping_key)
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
