"""Playlist sync phases: fetch, match, write, verify and publish."""

from datetime import datetime
from . import network
import time
from .apple_music_source import AppleMusicAPI
from .config import Config
from .runtime_output import emit
from .spotify_source import SpotifyAPI
from .terminal import auto_sync_display, section_header
from .text_utils import repair_text, source_album_display, source_display_label


class PlaylistWorkflow:
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
        source_url = Config._normalize_url_input(playlist_entry["source_url"])
        playlist_id = Config._extract_id(
            source_url,
            source_type,
        )

        if not playlist_id:
            emit(f"✗ Could not extract playlist ID from stored URL: {source_url}")
            return self._run_stats(
                errors=1,
            )

        canonical_url = Config._canonical_source_url(
            source_url,
            source_type,
        )

        self._repair_source_entry(playlist_entry, source_type, playlist_id, canonical_url, dry_run)

        source_url = canonical_url

        plex_playlist_id = playlist_entry["plex_playlist_id"]
        playlist_name = playlist_entry["plex_playlist_name"]

        from .text_playlists import TextSource

        api = (
            TextSource()
            if source_type == "text"
            else SpotifyAPI()
            if source_type == "spotify"
            else AppleMusicAPI()
        )

        plex = self._get_plex()

        # A saved text source may await its first playable track.
        if not dry_run and plex_playlist_id:
            self._confirm_destination(plex, plex_playlist_id)

        if dry_run:
            emit(f"\n=== DRY RUN: '{playlist_name}' ({source_display_label(source_type)}) ===")
        else:
            emit(f"\n→ Syncing '{playlist_name}' from {source_display_label(source_type)}...")

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
            emit(f"✗ Failed to fetch: {e}")
            return self._run_stats(
                errors=1,
            )

        emit(f"  Found {len(source_tracks)} tracks")

        jobs.output(f"Source fetch completed in {time.monotonic() - source_started:.2f}s")
        match_started = time.monotonic()
        mapping_key = f"{source_type}:{playlist_id}"

        observed_source_tracks = source_tracks
        from .source_observations import validate as validate_source

        complete = validate_source(
            self.config.repository,
            mapping_key,
            observed_source_tracks,
            metadata,
            self.config.source_snapshots.get(mapping_key),
        )

        if not dry_run:
            from .source_history import record

            record(self.config.repository, mapping_key, observed_source_tracks, complete)
        source_changes = self._source_change_report(
            mapping_key,
            observed_source_tracks,
        )
        self._print_source_changes(source_changes)
        from .playlist_overlays import apply as apply_playlist_overlay

        source_tracks = apply_playlist_overlay(
            self.config.repository, mapping_key, observed_source_tracks
        )
        if len(source_tracks) != len(observed_source_tracks):
            jobs.output(
                f"Applied playlist edits: {len(observed_source_tracks)} source tracks → {len(source_tracks)} effective tracks"
            )

        (
            matched_tracks,
            unmatched,
            playlist_mapping,
            plex_library,
            match_stats,
        ) = self._match_source_tracks(
            source_tracks,
            mapping_key,
            source_added_indices=source_changes["added_indices"],
            record_provenance=not dry_run,
            mark_new_matches=True,
        )

        if dry_run:
            return self._dry_run_summary(
                source_tracks, matched_tracks, match_stats, source_changes, unmatched
            )

        jobs.output(
            f"Matching and library preparation completed in {time.monotonic() - match_started:.2f}s"
        )
        self._store_unmatched(
            mapping_key,
            unmatched,
        )

        self.config.mapping[mapping_key] = playlist_mapping

        emit(section_header("SYNCING TO PLEX"))

        operation_error = False

        if not plex_playlist_id:
            if not matched_tracks:
                self._save_source_snapshot(mapping_key, observed_source_tracks)
                self.config.save()
                if source_type == "text":
                    try:
                        from .sync_projection import update as publish_sync_page

                        publish_sync_page(
                            self.config, playlist_entry, source_tracks, plex_library, metadata, True
                        )
                    except Exception as exc:
                        jobs.output(
                            "Playlist page update deferred; reopen the page to retry: " + str(exc)
                        )
                    jobs.output(
                        "Text playlist saved. No playable tracks yet; request missing albums and sync after they arrive."
                    )
                    return self._run_stats(unresolved=len(unmatched))
                raise ValueError(
                    "No source tracks currently match playable items in Plex. Resolve or download at least one track, then retry recreation."
                )
            jobs.progress("Creating a replacement Plex playlist")
            plex_playlist_id = self._build_new_plex_playlist(
                playlist_name, metadata.get("description", ""), matched_tracks, ""
            )
            if not plex_playlist_id:
                raise ValueError(
                    "Plex did not confirm the replacement playlist. Inspect Plex before retrying; a playlist may have been created."
                )
            playlist_entry["plex_playlist_id"] = plex_playlist_id
            self.config.save()
        if matched_tracks:
            operation_error = not plex.replace_playlist_tracks(plex_playlist_id, matched_tracks)
        elif source_type == "text":
            operation_error = not plex.replace_playlist_tracks(plex_playlist_id, [])
        else:
            emit("⚠ No matched tracks; Plex playlist left unchanged.")

        jobs.progress("Updating playlist metadata and artwork", check=False)
        playlist_entry["source_name"] = repair_text(
            metadata.get("name", "") or playlist_entry.get("source_name") or playlist_name
        )
        playlist_entry["source_owner"] = (
            metadata.get("owner")
            or metadata.get("curator")
            or playlist_entry.get("source_owner", "")
        )
        playlist_entry["plex_playlist_name"] = (
            playlist_entry.get("custom_name") or playlist_entry["source_name"]
        )

        image_url = metadata.get(
            "image_url",
            "",
        )

        if image_url:
            emit("  → Artwork URL found in source")
        else:
            emit("  ⚠ No artwork URL found in source")

        operation_error = self._verify_and_describe(
            plex,
            plex_playlist_id,
            playlist_entry,
            source_tracks,
            matched_tracks,
            unmatched,
            match_stats,
            plex_library,
            metadata,
            mapping_key,
            operation_error,
        )
        self._save_source_snapshot(
            mapping_key,
            observed_source_tracks,
        )

        self.config.save()
        try:
            from .sync_projection import update as publish_sync_page

            publish_sync_page(
                self.config,
                playlist_entry,
                source_tracks,
                plex_library,
                metadata,
                not operation_error,
            )
        except Exception as exc:
            jobs.output("Playlist page update deferred; reopen the page to retry: " + str(exc))

        return self._sync_summary(
            source_tracks, matched_tracks, unmatched, match_stats, source_changes, operation_error
        )

    def _confirm_destination(self, plex, plex_playlist_id):
        from . import jobs

        jobs.progress("Checking destination Plex playlist")
        try:
            response = network.get(
                f"{plex.base_url}/playlists/{plex_playlist_id}",
                headers=plex.headers,
                timeout=(5, 10),
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
                raise ValueError(
                    "Plex did not confirm the destination playlist. Sync stopped safely."
                )
        except network.RequestException as exc:
            raise ValueError(
                f"Could not verify Plex playlist {plex_playlist_id} ({type(exc).__name__}). "
                "Check Plex connectivity and permissions; no playlist changes were attempted."
            ) from None

    def _dry_run_summary(
        self, source_tracks, matched_tracks, match_stats, source_changes, unmatched
    ):
        emit(section_header("DRY RUN SUMMARY"))
        emit(f"Source tracks: {len(source_tracks)}")
        emit(f"Would match:    {len(matched_tracks)}")
        emit(f"New matches:    {len(match_stats['new_matches'])}")
        emit(f"Lost matches:   {len(match_stats['lost_matches'])}")
        emit(f"Ignored:        {len(match_stats.get('ignored_tracks', []))}")

        if source_changes["baseline"]:
            emit("Source ADDED:   baseline")
            emit("Source REMOVED: baseline")
        else:
            emit(f"Source ADDED:   {len(source_changes['added'])}")
            emit(f"Source REMOVED: {len(source_changes['removed'])}")

        emit(f"Unmatched:      {len(unmatched)}")

        if unmatched:
            emit("\nUnmatched tracks:")
            for track in unmatched:
                emit(f"  ✗ {track['title']} - {track['artist']} {source_album_display(track)}")

        emit("\n✓ DRY RUN complete. No changes were made to Plex.")
        emit(
            "  config.json, mapping.json, missing_tracks.json, "
            "match_metadata.json, source_snapshots.json, "
            "ignored_tracks.json, schema state, and last sync times "
            "were also left unchanged."
        )
        return self._run_stats(
            tested=1,
            source_added=(0 if source_changes["baseline"] else len(source_changes["added"])),
            source_removed=(0 if source_changes["baseline"] else len(source_changes["removed"])),
            new_matches=len(match_stats["new_matches"]),
            recovered_lost=len(
                match_stats.get(
                    "recovered_lost",
                    [],
                )
            ),
            newly_lost=len(match_stats["lost_matches"]),
            unresolved=len(unmatched),
            ignored=len(
                match_stats.get(
                    "ignored_tracks",
                    [],
                )
            ),
        )

    def _verify_and_describe(
        self,
        plex,
        plex_playlist_id,
        playlist_entry,
        source_tracks,
        matched_tracks,
        unmatched,
        match_stats,
        plex_library,
        metadata,
        mapping_key,
        operation_error,
    ):
        from . import jobs

        # Reuse source/matching work; verify only the destination after writes.
        from .sync_health import persist_sync_health

        if not operation_error:
            jobs.progress("Verifying Plex playlist and updating health", check=False)
            try:
                actual = plex.get_playlist_items(str(plex_playlist_id))
                persist_sync_health(
                    self.config,
                    playlist_entry,
                    source_tracks,
                    matched_tracks,
                    unmatched,
                    match_stats,
                    actual,
                )
                from .verification import compare, describe, track_details

                verification = compare(matched_tracks, actual)
                operation_error = not verification["ok"] if matched_tracks else False
                if verification["duplicates_collapsed"]:
                    emit(
                        f"⚠ The playlist contained {verification['duplicates_collapsed']} duplicate entries that did not carry over to Plex."
                    )
                elif operation_error:
                    emit("✗ " + describe(verification))
                    for detail in track_details(matched_tracks, actual, plex_library):
                        emit("  " + detail)
            except Exception as exc:
                operation_error = True
                self.config.repository.record_health_attempt(
                    mapping_key, "Sync verification failed: " + str(exc)
                )
                emit("Could not verify synced playlist:", exc)
        if not operation_error:
            playlist_entry["last_synced"] = datetime.now().astimezone().isoformat()
            playlist_entry["ready_to_sync"] = False
            from .playlist_description import render

            plex.update_playlist_metadata(
                plex_playlist_id,
                playlist_entry["plex_playlist_name"],
                render(
                    metadata.get("description", ""),
                    playlist_entry,
                    len(source_tracks),
                    len(matched_tracks),
                    len(unmatched),
                    len(match_stats.get("ignored_tracks", [])),
                ),
                metadata.get("image_url", ""),
            )
        return operation_error

    def _sync_summary(
        self, source_tracks, matched_tracks, unmatched, match_stats, source_changes, operation_error
    ):
        if operation_error:
            emit("✗ Sync update or verification failed. Review the differences above.")
        elif matched_tracks:
            emit("✓ Sync completed with missing tracks." if unmatched else "✓ Sync complete!")
        else:
            emit("✓ Sync complete (no changes - no matched tracks)")

        if unmatched:
            emit(f"⚠ {len(unmatched)} tracks were not added because they are unmatched.")

        emit(section_header("SYNC SUMMARY"))
        emit(f"Source tracks:   {len(source_tracks)}")
        emit(f"Matched:         {len(matched_tracks)}")
        emit(f"New matches:     {len(match_stats['new_matches'])}")
        emit(f"Lost matches:    {len(match_stats['lost_matches'])}")
        emit(f"Ignored:         {len(match_stats.get('ignored_tracks', []))}")

        if source_changes["baseline"]:
            emit("Source ADDED:    baseline")
            emit("Source REMOVED:  baseline")
        else:
            emit(f"Source ADDED:    {len(source_changes['added'])}")
            emit(f"Source REMOVED:  {len(source_changes['removed'])}")

        emit(f"Unresolved:      {len(unmatched)}")

        return self._run_stats(
            tested=1,
            synced=1,
            source_added=(0 if source_changes["baseline"] else len(source_changes["added"])),
            source_removed=(0 if source_changes["baseline"] else len(source_changes["removed"])),
            new_matches=len(match_stats["new_matches"]),
            recovered_lost=len(
                match_stats.get(
                    "recovered_lost",
                    [],
                )
            ),
            newly_lost=len(match_stats["lost_matches"]),
            unresolved=len(unmatched),
            ignored=len(
                match_stats.get(
                    "ignored_tracks",
                    [],
                )
            ),
            errors=(1 if operation_error else 0),
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
        source_url = Config._normalize_url_input(playlist_entry.get("source_url", ""))
        playlist_id = (
            Config._extract_id(
                source_url,
                source_type,
            )
            or str(playlist_entry.get("source_id", "")).strip()
        )
        playlist_name = playlist_entry["plex_playlist_name"]
        plex_playlist_id = playlist_entry["plex_playlist_id"]

        emit(
            f"\n→ Auditing '{playlist_name}' "
            f"({source_display_label(source_type)}) - "
            f"Auto sync: {auto_sync_display(False)}"
        )

        if not playlist_id:
            emit("✗ Could not determine source playlist ID")
            return self._run_stats(
                errors=1,
            )

        from .text_playlists import TextSource

        api = (
            TextSource()
            if source_type == "text"
            else SpotifyAPI()
            if source_type == "spotify"
            else AppleMusicAPI()
        )
        mapping_key = f"{source_type}:{playlist_id}"

        try:
            observed_source_tracks, metadata = api.get_playlist_tracks(
                source_url,
                fetch_artwork=False,
            )
            from .source_observations import validate as validate_source

            validate_source(
                self.config.repository,
                mapping_key,
                observed_source_tracks,
                metadata,
                self.config.source_snapshots.get(mapping_key),
            )
            from .playlist_overlays import apply as apply_playlist_overlay

            source_tracks = apply_playlist_overlay(
                self.config.repository, mapping_key, observed_source_tracks
            )
        except Exception as e:
            emit(f"✗ Failed to fetch source playlist: {e}")
            return self._run_stats(
                errors=1,
            )

        plex = self._get_plex()
        plex_count = plex.get_playlist_item_count(plex_playlist_id)
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

        emit(section_header("AUTO SYNC OFF - AUDIT SUMMARY"))
        emit(f"Source tracks:      {len(source_tracks)}")
        emit("Plex playlist:      " + (str(plex_count) if plex_count is not None else "unknown"))
        emit(f"Matched in library: {len(matched_tracks)}")
        emit(f"Ignored:            {len(match_stats.get('ignored_tracks', []))}")
        emit(f"Unresolved:         {len(unmatched)}")

        if plex_count is None:
            emit("⚠ Could not compare source and Plex playlist counts.")
        elif plex_count != len(source_tracks):
            difference = len(source_tracks) - plex_count
            direction = f"{difference} fewer" if difference > 0 else f"{abs(difference)} more"
            emit(
                "⚠ Track count mismatch: Plex has "
                f"{direction} item"
                f"{'s' if abs(difference) != 1 else ''} than the source."
            )
        else:
            emit(f"✓ Source and Plex counts match ({plex_count}).")

        if write_missing:
            emit("✓ missing_tracks.json refreshed; Plex playlist left unchanged.")
        else:
            emit("✓ Dry-run audit complete; no local state or Plex data changed.")

        return self._run_stats(
            tested=1,
            audited=1,
            source_added=(0 if source_changes["baseline"] else len(source_changes["added"])),
            source_removed=(0 if source_changes["baseline"] else len(source_changes["removed"])),
            recovered_lost=len(
                match_stats.get(
                    "recovered_lost",
                    [],
                )
            ),
            newly_lost=len(match_stats["lost_matches"]),
            unresolved=len(unmatched),
            ignored=len(
                match_stats.get(
                    "ignored_tracks",
                    [],
                )
            ),
        )
