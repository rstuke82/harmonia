"""Match source occurrences against the saved Harmonia catalog."""

from .track_bridge import scoped as _catalog_scope
from typing import List
from typing import Optional
from typing import Tuple
from .matching import Matcher
from .runtime_output import emit
from .terminal import Colors, colored, provenance_display
from .text_utils import source_album_display


class TrackMatching:
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
        source_added_indices = source_added_indices or set()

        original_mapping = dict(
            self.config.mapping.get(
                mapping_key,
                {},
            )
        )
        playlist_mapping = dict(original_mapping)

        if plex_library is None:
            emit("→ Scanning Plex library...")
            plex_library = plex.search_library("")

            if not plex_library:
                emit("✗ No Plex music tracks were found.")
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

            emit(f"  Found {len(plex_library)} tracks in Plex library")

        plex_by_id = {
            str(track.get("plex_id")): track
            for track in plex_library
            if track.get("plex_id") is not None
        }

        source_keys = {f"{t.get('title', '')}|{t.get('artist', '')}" for t in source_tracks}
        unused = set(original_mapping) - source_keys
        if unused:
            jobs.output(
                f"{len(unused)} stored mappings do not belong to the current source track list; not counted as matched"
            )
        emit("→ Matching tracks...")

        matched_tracks = []
        unmatched = []
        new_matches = []
        lost_matches = []
        recovered_lost = []
        stale_mappings = []
        ignored_matches = []
        blocked_artists = (
            {
                Matcher._normalize_match_text(v.get("name", ""))
                for v in self.config.repository.load("blocked_artists").values()
            }
            if mapping_key.startswith("text:")
            else set()
        )

        for i, track in enumerate(
            source_tracks,
            1,
        ):
            if i == 1 or i % 10 == 0 or i == len(source_tracks):
                jobs.progress(
                    f"Comparing track {i} of {len(source_tracks)} · resolving saved matches",
                    completed=i,
                    total=len(source_tracks),
                )
            search_key = f"{track['title']}|{track['artist']}"
            was_mapped = search_key in original_mapping
            source_added = (i - 1) in source_added_indices

            if (
                self._is_track_ignored(
                    mapping_key,
                    track,
                )
                or Matcher._normalize_match_text(track.get("artist", "")) in blocked_artists
            ):
                ignored_matches.append(dict(track))

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

                emit(
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
                cached_id = str(playlist_mapping[search_key])
                matched_track = plex_by_id.get(cached_id)

                if (
                    matched_track is not None
                    and self._get_match_provenance(mapping_key, search_key) != "manual"
                    and Matcher._recording_version_mismatch(track, matched_track)
                ):
                    jobs.output(
                        "Reconsidering automatic match: source and saved track have different recording or featured-artist identities"
                    )
                    matched_track = None

                if matched_track is not None:
                    plex_id = cached_id
                    cached_valid = True

                    if record_provenance:
                        cached_provenance = self._get_match_provenance(
                            mapping_key,
                            search_key,
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
                            current_auto_id = Matcher.match_track(
                                track,
                                plex_library,
                                {},
                            )

                            if current_auto_id is not None and str(current_auto_id) == cached_id:
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

                    stale_provenance = self._get_match_provenance(
                        mapping_key,
                        search_key,
                    )
                    previous_match = self._previous_match_snapshot(
                        mapping_key,
                        search_key,
                        cached_id,
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

            if not cached_valid and allow_automatic_match:
                plex_id = Matcher.match_track(
                    track,
                    plex_library,
                    playlist_mapping,
                )

                if plex_id:
                    plex_id = str(plex_id)
                    playlist_mapping[search_key] = plex_id
                    matched_track = plex_by_id.get(plex_id)

                    if record_provenance:
                        self._set_match_provenance(
                            mapping_key,
                            search_key,
                            "automatic",
                            matched_track=matched_track,
                            plex_id=plex_id,
                        )

            if plex_id:
                matched_tracks.append(str(plex_id))

                if stale_cached_mapping and stale_provenance == "automatic":
                    recovered_lost.append(
                        {
                            "source": dict(track),
                            "replacement_plex_id": str(plex_id),
                        }
                    )

                is_new_match = mark_new_matches and not was_mapped

                if is_new_match:
                    new_matches.append(track)

                status_bits = []

                if source_added:
                    status_bits.append(colored("NEW", Colors.MAGENTA))

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
                        matched_info = f" → {matched_title} - {matched_artist} {album_str}"
                    else:
                        matched_info = f" → {matched_title} - {matched_artist}"

                source_title = colored(
                    track["title"],
                    Colors.CYAN,
                )
                source_artist = colored(
                    track["artist"],
                    Colors.GREEN,
                )
                status_text = " " + " ".join(status_bits) if status_bits else ""

                emit(
                    f"  [{i}/{len(source_tracks)}] "
                    f"{colored('✓', Colors.GREEN)}"
                    f"{status_text} "
                    f"{source_title} - {source_artist} "
                    f"{source_album_display(track)}"
                    f"{matched_info}"
                )
                continue

            unmatched_track = dict(track)

            is_lost = was_mapped and stale_cached_mapping

            if is_lost:
                previous_provenance = stale_provenance

                unmatched_track["status"] = "lost"
                unmatched_track["previous_match"] = previous_match
                unmatched_track["previous_provenance"] = previous_provenance
                lost_matches.append(unmatched_track)

            unmatched.append(unmatched_track)

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
                status_bits.append(colored("LOST", Colors.RED))

            if source_added:
                status_bits.append(colored("NEW", Colors.MAGENTA))

            status_text = " " + " ".join(status_bits) if status_bits else ""

            emit(
                f"  [{i}/{len(source_tracks)}] "
                f"{colored('✗', Colors.RED)}"
                f"{status_text} "
                f"{source_title} - {source_artist} "
                f"{source_album_display(track)}"
            )

            if is_lost:
                previous_title = previous_match.get(
                    "title",
                    "",
                )
                previous_artist = previous_match.get(
                    "artist",
                    "",
                )
                previous_album = previous_match.get(
                    "album",
                    "",
                )
                previous_id = previous_match.get(
                    "plex_id",
                    "",
                )

                if previous_title or previous_artist:
                    previous_text = f"{previous_title} - {previous_artist}"

                    if previous_album:
                        previous_text += f" ({previous_album})"

                    emit(
                        "      Previous Plex match: "
                        f"{previous_text} "
                        f"[{provenance_display(previous_provenance)}]"
                    )
                elif previous_id:
                    emit(
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
                    stored["previous_provenance"] = t.get(
                        "previous_provenance",
                        "legacy",
                    )

                stored_tracks.append(stored)

            self.config.missing[mapping_key] = stored_tracks

            emit(f"\n⚠ {len(unmatched)} tracks unmatched")
        else:
            # Clear stale missing-track records.
            self.config.missing.pop(mapping_key, None)
