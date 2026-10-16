"""Playlist synchronization and saved matching state."""

from .sync_match_state import MatchState
from .sync_sources import SourceObservations
from .sync_matching import TrackMatching
from .sync_workflow import PlaylistWorkflow
from typing import List
from typing import Optional
from .config import Config
from .plex_client import PlexAPI
from .runtime_output import emit
from .terminal import section_header


class Syncer(MatchState, SourceObservations, TrackMatching, PlaylistWorkflow):
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
        return (
            playlist.get(
                "auto_sync",
                True,
            )
            is not False
        )

    @staticmethod
    def _playlist_favorite(
        playlist: dict,
    ) -> bool:
        """Missing favorite means OFF for existing playlists."""
        return (
            playlist.get(
                "favorite",
                False,
            )
            is True
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
            emit("✗ Cannot create Plex playlist: no tracks matched to the Plex library.")
            return None

        plex = self._get_plex()

        emit("\n→ Creating Plex playlist with first matched track...")

        playlist_id = plex.create_playlist(
            playlist_name,
            matched_tracks[0],
            description,
        )

        if not playlist_id:
            emit("✗ Failed to create Plex playlist.")
            return None

        emit(f"✓ Created Plex playlist '{playlist_name}' (ID: {playlist_id})")

        # Rebuild through the same occurrence-aware path used by sync.
        added = (
            len(matched_tracks) if plex.replace_playlist_tracks(playlist_id, matched_tracks) else 0
        )

        emit(f"✓ Added {added}/{len(matched_tracks)} matched tracks")

        if added != len(matched_tracks):
            emit("⚠ Some matched tracks could not be added to the Plex playlist.")

        # Update artwork if available
        if artwork_url:
            plex.update_playlist_metadata(
                playlist_id,
                playlist_name,
                description,
                artwork_url,
            )

        return playlist_id

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
            if self._playlist_favorite(playlist)
        ]

        if not favorites:
            emit(
                "✗ No favorite playlists are configured. "
                "Use Settings → Manage favorite playlists first."
            )
            return

        emit(section_header("SYNCING FAVORITE PLAYLISTS"))
        emit(f"Found {len(favorites)} favorite playlist{'s' if len(favorites) != 1 else ''}.\n")

        totals = self._run_stats()

        for playlist in favorites:
            try:
                result = self.sync_playlist(playlist)

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
                emit(f"✗ Error processing '{playlist.get('plex_playlist_name', 'Unknown')}': {e}")

        emit(section_header("FAVORITES SYNC SUMMARY"))
        emit(f"Favorite playlists synced: {totals['synced']}")
        emit(f"Source additions:          {totals['source_added']}")
        emit(f"Source removals:           {totals['source_removed']}")
        emit(f"New automatic matches:     {totals['new_matches']}")
        emit(f"Recovered LOST:            {totals['recovered_lost']}")
        emit(f"Newly LOST:                {totals['newly_lost']}")
        emit(f"Unresolved:                {totals['unresolved']}")
        emit(f"Ignored:                   {totals['ignored']}")
        emit(f"Errors:                    {totals['errors']}")

        return totals

    def sync_all(
        self,
        dry_run: bool = False,
        respect_auto_sync: bool = False,
    ):
        """Sync/audit all registered playlists and print one run summary."""
        playlists = self.config.config["playlists"]

        if not playlists:
            emit("✗ No playlists registered")
            return self._run_stats(
                errors=1,
            )

        if dry_run:
            emit(section_header("HARMONIA DRY RUN"))
            emit("Matching will be tested against Plex, but nothing will be changed or saved.\n")

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

        if respect_auto_sync and self.config.repository.load("tasks").get("sync_modes_v3"):
            from .sync_policy import eligible

            playlists = eligible(self.config.repository, playlists)
            respect_auto_sync = False
        for playlist in playlists:
            try:
                if respect_auto_sync and not self._auto_sync_enabled(playlist):
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
                emit(f"✗ Error processing '{playlist.get('plex_playlist_name', 'Unknown')}': {e}")

        emit(section_header("SYNC-ALL RUN SUMMARY"))

        if dry_run:
            emit(f"Playlists tested:        {totals['tested']}")
        else:
            emit(f"Playlists synced:        {totals['synced']}")

        emit(f"Auto-sync OFF audited:   {totals['audited']}")
        emit(f"Source additions:        {totals['source_added']}")
        emit(f"Source removals:         {totals['source_removed']}")
        emit(f"New automatic matches:   {totals['new_matches']}")
        emit(f"Recovered LOST:          {totals['recovered_lost']}")
        emit(f"Newly LOST:              {totals['newly_lost']}")
        emit(f"Unresolved:              {totals['unresolved']}")
        emit(f"Ignored:                 {totals['ignored']}")
        emit(f"Errors:                  {totals['errors']}")

        if respect_auto_sync and totals["synced"] == 0 and totals["audited"] and not dry_run:
            emit(
                "\n✓ Automatic sync is OFF for all registered playlists; "
                "diagnostic audits completed."
            )

        return totals
