"""Plex library and playlist service client."""
from io import BytesIO
try:
    from PIL import Image
except ImportError:
    Image = None
from typing import List
from typing import Optional
from typing import Tuple
from fuzzywuzzy import fuzz
import re
from . import network
from urllib.parse import urlparse
from .matching import Matcher
from .runtime_output import print
from .runtime_paths import APPLE_ARTWORK_SIZE
from .text_utils import clean_playlist_description, repair_text

class PlexAPI:
    """Plex API wrapper"""

    def __init__(
        self,
        plex_url: str,
        plex_token: str,
        music_library_key: str,
        music_library_name: str = "",
        strict_errors: bool = False,
    ):
        self.strict_errors = strict_errors
        self.base_url = plex_url.rstrip("/")
        self.token = plex_token
        self.music_library_key = str(
            music_library_key
        ).strip()
        self.music_library_name = str(
            music_library_name or ""
        ).strip()
        self.headers = {
            "X-Plex-Token": plex_token,
            "Accept": "application/json",
        }

        if not self.music_library_key:
            raise ValueError(
                "Plex music library is not configured"
            )

        try:
            self.machine_identifier = self._get_machine_identifier()
        except Exception as exc:
            if self.strict_errors:
                from .diagnostics import PlexDiagnosticError, plex_error
                raise PlexDiagnosticError(plex_error(exc)) from exc
            raise

    def _get_machine_identifier(self) -> str:
        """Get Plex server machine identifier."""
        try:
            resp = network.get(
                f"{self.base_url}/identity",
                headers=self.headers,
                timeout=10,
             allow_redirects=False)

            if self.strict_errors:
                resp.raise_for_status()
            if resp.status_code != 200:
                raise Exception(
                    f"Plex /identity returned HTTP {resp.status_code}: "
                    f"{resp.text[:500]}"
                )

            data = resp.json()
            identifier = data.get("MediaContainer", {}).get(
                "machineIdentifier"
            )

            if not identifier and self.strict_errors:
                raise ValueError("Not a Plex identity response")
            if not identifier:
                raise Exception(
                    "Plex did not return a machineIdentifier"
                )

            return identifier

        except network.RequestException as e:
            if self.strict_errors:
                raise
            raise Exception(
                f"Could not connect to Plex /identity: {e}"
            )
        except ValueError as e:
            if self.strict_errors:
                raise
            raise Exception(
                f"Plex returned invalid JSON from /identity: {e}"
            )

    def _library_uri(self, plex_track_id: str) -> str:
        """
        Build the URI Plex expects for library media in playlists.
        """
        return (
            f"server://{self.machine_identifier}"
            f"/com.plexapp.plugins.library/library/metadata/"
            f"{plex_track_id}"
        )

    def search_library(
        self, title: str = "", artist: str = ""
    ) -> List[dict]:
        """Load tracks only from the configured Plex music library."""
        from .library_cache import load
        return load(self, self._fetch_library)

    def _fetch_library(self):

        try:
            resp = network.get(
                f"{self.base_url}/library/sections/"
                f"{self.music_library_key}/all",
                headers=self.headers,
                params={"type": 10,"includeGuids":1},
                timeout=30,
             allow_redirects=False)

            if self.strict_errors:
                resp.raise_for_status()
            if resp.status_code != 200:
                library_label = (
                    f" '{self.music_library_name}'"
                    if self.music_library_name
                    else ""
                )
                print(
                    f"Failed to fetch tracks from Plex music library"
                    f"{library_label}: {resp.status_code}"
                )
                return []

            payload = resp.json()
            if self.strict_errors:
                if not isinstance(payload, dict) or not isinstance(payload.get("MediaContainer"), dict):
                    raise ValueError("Not a Plex response")
                if not isinstance(payload["MediaContainer"].get("Metadata", []), list):
                    raise ValueError("Invalid Plex track list")

            rows = self._parse_library_tracks(payload)
            if not rows:
                return rows
            # Album dates come from Plex album metadata, not track imports.
            albums_response = network.get(
                f"{self.base_url}/library/sections/{self.music_library_key}/all",
                headers=self.headers, params={"type": 9}, timeout=30,
             allow_redirects=False)
            albums_response.raise_for_status()
            albums = {str(a.get("ratingKey")): a for a in albums_response.json().get("MediaContainer", {}).get("Metadata", [])}
            artists_response = network.get(
                f"{self.base_url}/library/sections/{self.music_library_key}/all",
                headers=self.headers, params={"type": 8}, timeout=30,
             allow_redirects=False)
            artists_response.raise_for_status()
            artists = {str(a.get("ratingKey")): a for a in artists_response.json().get("MediaContainer", {}).get("Metadata", [])}
            for row in rows:
                album = albums.get(row.get("plex_album_id"), {})
                artist = artists.get(row.get("plex_artist_id"), {})
                row["plex_album_added_at"] = album.get("addedAt")
                row["plex_album_thumb"] = album.get("thumb") or row.get("plex_album_thumb", "")
                row["plex_artist_thumb"] = artist.get("thumb") or row.get("plex_artist_thumb", "")
                row["plex_artist_art"] = artist.get("art") or row.get("plex_artist_art", "")
            return rows

        except Exception as e:
            if self.strict_errors:
                from .diagnostics import PlexDiagnosticError, plex_error
                raise PlexDiagnosticError(plex_error(e)) from e
            print(f"Error searching library: {e}")
            return []

    @staticmethod
    def _parse_library_tracks(payload):
        tracks = payload.get("MediaContainer", {}).get("Metadata", [])
        return [
                {
                    "title": repair_text(t.get("title", "")),
                    # Plex uses originalTitle for the track artist when it
                    # differs from the album artist (common on soundtracks
                    # and Various Artists compilations).
                    "artist": repair_text(
                        t.get("originalTitle")
                        or t.get("grandparentTitle", "")
                    ),
                    "track_artist": repair_text(
                        t.get("originalTitle", "")
                    ),
                    "album_artist": repair_text(
                        t.get("grandparentTitle", "")
                    ),
                    "album": repair_text(
                        t.get("parentTitle", "")
                    ),
                    "plex_id": str(t.get("ratingKey")),
                    "plex_album_id": str(t.get("parentRatingKey") or ""),
                    "plex_album_thumb": t.get("parentThumb") or "",
                    "plex_album_art": t.get("parentArt") or "",
                    "plex_artist_id": str(t.get("grandparentRatingKey") or ""),
                    "plex_artist_thumb": t.get("grandparentThumb") or "",
                    "plex_artist_art": t.get("grandparentArt") or "",
                    "key": t.get("key"),
                    "duration": t.get("duration"),
                    "track_number": t.get("index"),
                    "disc_number": t.get("parentIndex"),
                    "edition": repair_text(t.get("editionTitle") or ""),
                    "files": [p.get("file") for m in t.get("Media",[]) for p in m.get("Part",[]) if p.get("file")],
                    "guids": [g.get("id") for g in t.get("Guid",[]) if g.get("id")],
                }
                for t in tracks
                if t.get("ratingKey")
            ]

    def refresh_library_path(self, path: str = ""):
        """Ask Plex to refresh one mapped folder without rescanning Harmonia's inventory."""
        params = {"path": path} if path else {}
        response = network.get(
            f"{self.base_url}/library/sections/{self.music_library_key}/refresh",
            headers=self.headers,
            params=params,
            timeout=(5, 20),
         allow_redirects=False)
        response.raise_for_status()

    def search_library_subset(self, query: str) -> List[dict]:
        """Find albums by title, then fetch their complete track lists."""
        response = network.get(
            f"{self.base_url}/library/sections/{self.music_library_key}/search",
            headers=self.headers, params={"type": 9, "query": query}, timeout=(5, 20),
         allow_redirects=False)
        response.raise_for_status()
        rows = []
        candidates=response.json().get("MediaContainer", {}).get("Metadata", [])
        if len(candidates)>100:
            raise ValueError("Plex album search exceeded the supported result limit.")
        for album in candidates:
            if Matcher._normalize_match_text(album.get("title", ""))!=Matcher._normalize_match_text(query):
                continue
            key = str(album.get("ratingKey") or "")
            if album.get("type") != "album" or not key.isdigit():
                continue
            for page in range(100):
                child = network.get(
                    f"{self.base_url}/library/metadata/{key}/children",
                    headers=self.headers,
                    params={"includeGuids": 1, "X-Plex-Container-Start": page * 200,
                            "X-Plex-Container-Size": 200}, timeout=(5, 20),
                 allow_redirects=False)
                child.raise_for_status()
                payload = child.json()
                container = payload.get("MediaContainer", {})
                tracks = container.get("Metadata", [])
                parsed = self._parse_library_tracks(payload)
                for row in parsed:
                    row["plex_album_added_at"] = album.get("addedAt")
                    row["plex_album_thumb"] = album.get("thumb") or row.get("plex_album_thumb", "")
                rows.extend(parsed)
                if not tracks or page * 200 + len(tracks) >= int(container.get("totalSize", len(tracks))):
                    break
            else:
                raise ValueError("Plex album exceeded the supported track limit; previous data retained.")
        return rows

    def search_artists(
        self,
        query: str = "",
        limit: int = 10,
    ) -> List[dict]:
        """Search artists only inside the configured Plex music library."""
        try:
            resp = network.get(
                f"{self.base_url}/library/sections/"
                f"{self.music_library_key}/all",
                headers=self.headers,
                params={"type": 8},
                timeout=30,
             allow_redirects=False)

            if resp.status_code != 200:
                print(
                    "Failed to fetch Plex artists: "
                    f"{resp.status_code}"
                )
                return []

            artists = (
                resp.json()
                .get("MediaContainer", {})
                .get("Metadata", [])
            )

            unique = {}

            for artist in artists:
                title = repair_text(
                    artist.get("title", "")
                ).strip()
                rating_key = artist.get(
                    "ratingKey"
                )

                if not title:
                    continue

                key = title.casefold()
                if key not in unique:
                    unique[key] = {
                        "name": title,
                        "plex_id": (
                            str(rating_key)
                            if rating_key is not None
                            else ""
                        ),
                    }

            values = list(unique.values())
            search = repair_text(query).strip()

            if not search:
                return sorted(
                    values,
                    key=lambda item: item["name"].casefold(),
                )[:limit]

            search_norm = Matcher._normalize_match_text(
                search
            )
            ranked = []

            for artist in values:
                artist_norm = Matcher._normalize_match_text(
                    artist["name"]
                )
                score = max(
                    fuzz.ratio(
                        search_norm,
                        artist_norm,
                    ),
                    fuzz.token_set_ratio(
                        search_norm,
                        artist_norm,
                    ),
                )
                ranked.append(
                    (score, artist["name"].casefold(), artist)
                )

            ranked.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                ),
                reverse=True,
            )

            return [
                {
                    **item[2],
                    "score": item[0],
                }
                for item in ranked[:limit]
            ]

        except Exception as e:
            print(
                f"Error searching Plex artists: {e}"
            )
            return []

    def get_audio_playlists(self) -> List[dict]:
        """
        Return every Plex audio playlist visible to this server/token.

        This is used only by read-only developer diagnostics. Both normal and
        smart audio playlists are included when Plex exposes them.
        """

        try:
            resp = network.get(
                f"{self.base_url}/playlists",
                headers=self.headers,
                timeout=15,
             allow_redirects=False)

            if resp.status_code != 200:
                print(
                    f"Failed to get Plex playlists: "
                    f"{resp.status_code}"
                )
                return []

            playlists = (
                resp.json()
                .get("MediaContainer", {})
                .get("Metadata", [])
            )

            results = []

            for playlist in playlists:
                playlist_type = str(
                    playlist.get("playlistType")
                    or playlist.get("type")
                    or ""
                ).casefold()

                # Plex normally reports playlistType="audio". If the field is
                # absent on a server/version, retain the item unless it is
                # explicitly known to be video/photo.
                if playlist_type in (
                    "video",
                    "photo",
                ):
                    continue

                if (
                    playlist_type
                    and playlist_type != "audio"
                ):
                    continue

                rating_key = playlist.get(
                    "ratingKey"
                )

                if rating_key is None:
                    continue

                results.append(
                    {
                        "plex_id": str(rating_key),
                        "title": repair_text(
                            playlist.get("title", "")
                        ),
                        "smart": bool(
                            playlist.get("smart")
                        ),
                        "leaf_count": playlist.get(
                            "leafCount"
                        ),
                    }
                )

            return results

        except Exception as e:
            print(
                f"Error getting Plex playlists: {e}"
            )
            return []

    def get_playlist(self, playlist_id: str) -> dict:
        """Get playlist details."""

        try:
            resp = network.get(
                f"{self.base_url}/playlists/{playlist_id}",
                headers=self.headers,
                timeout=10,
             allow_redirects=False)

            if resp.status_code == 200:
                return (
                    resp.json()
                    .get("MediaContainer", {})
                    .get("Metadata", [{}])[0]
                )

            print(
                f"Failed to get playlist details: {resp.status_code}"
            )
            return {}

        except Exception as e:
            print(f"Error getting playlist: {e}")
            return {}

    def get_playlist_items(self, playlist_id: str) -> List[dict]:
        """
        Get playlist items.

        IMPORTANT:
        Plex exposes playlistItemID for the item inside the playlist.
        That is different from the track's ratingKey and is what must
        be used when deleting a playlist item.
        """

        try:
            resp = network.get(
                f"{self.base_url}/playlists/{playlist_id}/items",
                headers=self.headers,
                timeout=15,
             allow_redirects=False)

            if self.strict_errors:
                resp.raise_for_status()
            if resp.status_code != 200:
                print(
                    f"Failed to get playlist items: "
                    f"{resp.status_code}"
                )
                return []

            if self.strict_errors:
                payload = resp.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("MediaContainer"), dict):
                    raise ValueError("Not a Plex response")
                if not isinstance(payload["MediaContainer"].get("Metadata", []), list):
                    raise ValueError("Invalid Plex track list")

            items = (
                resp.json()
                .get("MediaContainer", {})
                .get("Metadata", [])
            )

            container = resp.json().get('MediaContainer', {})
            total = int(container.get('totalSize', container.get('size', len(items))))
            while len(items) < total:
                page = network.get(f'{self.base_url}/playlists/{playlist_id}/items',
                    headers=self.headers, params={'X-Plex-Container-Start':len(items), 'X-Plex-Container-Size':1000},
                    timeout=15, allow_redirects=False)
                page.raise_for_status()
                more = page.json().get('MediaContainer', {}).get('Metadata', [])
                if not isinstance(more, list) or not more or len(items) + len(more) > total:
                    raise ValueError('Plex did not return a complete playlist snapshot')
                items.extend(more)
            return [
                {
                    "playlist_item_id": i.get("playlistItemID"),
                    "plex_id": str(i.get("ratingKey"))
                    if i.get("ratingKey") is not None
                    else None,
                    "title": i.get("title", ""),
                }
                for i in items
            ]

        except Exception as e:
            if self.strict_errors:
                from .diagnostics import PlexDiagnosticError, plex_error
                raise PlexDiagnosticError(plex_error(e)) from e
            print(f"Error getting playlist items: {e}")
            return []

    def get_playlist_item_count(
        self,
        playlist_id: str,
    ) -> Optional[int]:
        """Return the current Plex playlist item count, or None on failure."""
        try:
            resp = network.get(
                f"{self.base_url}/playlists/{playlist_id}/items",
                headers=self.headers,
                timeout=15,
             allow_redirects=False)

            if resp.status_code != 200:
                print(
                    "Failed to get Plex playlist item count: "
                    f"{resp.status_code}"
                )
                return None

            container = resp.json().get(
                "MediaContainer",
                {},
            )

            for field in ("totalSize", "size"):
                value = container.get(field)
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.isdigit():
                    return int(value)

            items = container.get(
                "Metadata",
                [],
            )
            return len(items) if isinstance(items, list) else 0

        except Exception as e:
            print(
                f"Error getting Plex playlist item count: {e}"
            )
            return None

    def create_playlist(
        self,
        title: str,
        first_track_plex_id: str,
        description: str = "",
    ) -> Optional[str]:
        """
        Create a normal Plex audio playlist.

        Plex requires a media URI when creating a normal playlist, so
        the first matched track is used as the initial playlist item.
        """

        try:
            uri = self._library_uri(first_track_plex_id)

            resp = network.post(
                f"{self.base_url}/playlists",
                headers=self.headers,
                params={
                    "type": "audio",
                    "title": title,
                    "smart": 0,
                    "uri": uri,
                },
                timeout=15,
             allow_redirects=False)

            if resp.status_code not in [200, 201]:
                print(
                    f"Plex error ({resp.status_code}): "
                    f"{resp.text[:2000]}"
                )
                return None

            try:
                data = resp.json()
            except ValueError:
                print(
                    "Plex created the playlist but returned "
                    f"non-JSON data: {resp.text[:1000]}"
                )
                return None

            container = data.get("MediaContainer", {})
            metadata = container.get("Metadata", [])

            if not metadata:
                print(
                    f"No playlist metadata in Plex response: "
                    f"{resp.text[:2000]}"
                )
                return None

            playlist_id = metadata[0].get("ratingKey")

            if not playlist_id:
                print(
                    "Plex response did not contain a playlist ratingKey"
                )
                return None

            # Description is updated separately because Plex playlist
            # creation does not reliably accept it in all versions.
            if description:
                self.update_playlist_metadata(
                    str(playlist_id), title, description
                )

            return str(playlist_id)

        except network.RequestException as e:
            print(f"Error creating playlist: {e}")
            return None
        except Exception as e:
            print(f"Error creating playlist: {e}")
            return None

    def add_to_playlist(
        self, playlist_id: str, track_plex_id: str
    ) -> bool:
        """Add a Plex library track to a Plex playlist."""

        try:
            uri = self._library_uri(track_plex_id)

            resp = network.put(
                f"{self.base_url}/playlists/{playlist_id}/items",
                headers=self.headers,
                params={"uri": uri},
                timeout=15,
             allow_redirects=False)

            if resp.status_code not in [200, 201]:
                print(
                    f"Warning: Failed to add track "
                    f"{track_plex_id}: {resp.status_code} "
                    f"{resp.text[:500]}"
                )
                return False

            return True

        except Exception as e:
            print(f"Error adding to playlist: {e}")
            return False

    def replace_playlist_tracks(self, playlist_id, track_ids):
        """Recover failed writes before replacing an existing playlist.

        Persist the previous ordered occurrences before any destructive call.
        The snapshot survives a process restart and stays until verification.
        """
        import hashlib
        from .accounts import personal_repository
        from .playlist_recovery import write_lock
        from . import jobs
        ids = [str(value) for value in track_ids]
        key = hashlib.sha256(repr((getattr(self, 'base_url', ''), str(playlist_id))).encode()).hexdigest()
        with write_lock(key):
            strict = getattr(self, 'strict_errors', False)
            self.strict_errors = True
            repo = personal_repository()
            try:
                pending = repo.load_one('playlist_write_recovery', key)
                if pending:
                    jobs.output('Recovering an interrupted Plex playlist update')
                    if not self._restore_playlist_tracks(playlist_id, pending['previous']):
                        jobs.output('Playlist recovery failed. Previous contents are saved; retry sync to recover them.')
                        return False
                    repo.delete('playlist_write_recovery', key)
                current = self._playlist_track_ids(playlist_id)
                if current == ids:
                    jobs.output('Plex playlist contents and order unchanged; skipping track writes')
                    return True
                repo.put('playlist_write_recovery', key, {'playlist_id':str(playlist_id), 'previous':current})
                try:
                    success = self._write_playlist_tracks(playlist_id, ids)
                    if success:
                        if self._verify_playlist_tracks(playlist_id, ids, allow_duplicate_collapse=True):
                            repo.delete('playlist_write_recovery', key)
                            return True
                        # Some Plex servers accept a batch but omit or reorder
                        # its items. Retry individual ordered writes once, with
                        # the durable recovery snapshot still in place.
                        jobs.output('Plex batch verification failed; retrying individual ordered additions' if len(set(ids))==len(ids) else 'Plex did not retain the verified repeated occurrences; restoring previous contents')
                        if len(set(ids))==len(ids) and self._write_playlist_tracks(playlist_id, ids, individual=True) and self._verify_playlist_tracks(playlist_id, ids):
                            repo.delete('playlist_write_recovery', key)
                            return True
                except Exception:
                    # Never expose a service URL/token from an exception here.
                    jobs.output('Plex playlist update failed; restoring previous contents')
                restored = self._restore_playlist_tracks(playlist_id, current)
                if restored:
                    repo.delete('playlist_write_recovery', key)
                    jobs.output('Playlist update failed; previous contents restored.')
                else:
                    jobs.output('Playlist update and recovery failed. Previous contents are saved; retry sync to recover them.')
                return False
            finally:
                self.strict_errors = strict

    def _verify_playlist_tracks(self, playlist_id, expected, allow_duplicate_collapse=False):
        from . import jobs
        actual = self._playlist_track_ids(playlist_id)
        if actual == expected:
            return True
        if allow_duplicate_collapse:
            from .verification import compare
            verification = compare(expected, [{'plex_id': value} for value in actual])
            if verification['ok']:
                # Sync's final verification reports the omitted duplicates as a warning.
                # Recovery keeps exact verification so a saved snapshot is not lost.
                return True
        jobs.output(f'Plex verification mismatch: expected {len(expected)} tracks; returned {len(actual)}')
        differences = [(i, expected[i] if i < len(expected) else '<none>',
                        actual[i] if i < len(actual) else '<none>')
                       for i in range(max(len(expected), len(actual)))
                       if (expected[i] if i < len(expected) else None) !=
                          (actual[i] if i < len(actual) else None)]
        for i, wanted, returned in differences[:10]:
            jobs.output(f'Plex position {i+1}: expected library ID {wanted}; returned {returned}')
        if len(differences) > 10:
            jobs.output(f'{len(differences)-10} additional positions differ')
        return False

    def _playlist_track_ids(self, playlist_id):
        items = self.get_playlist_items(playlist_id)
        ids = [str(t.get('plex_id') or t.get('ratingKey') or '') for t in items]
        if any(not value for value in ids):
            raise ValueError('Plex returned a playlist item without a library ID')
        return ids

    def _restore_playlist_tracks(self, playlist_id, ids):
        try:
            if self._playlist_track_ids(playlist_id) == ids:
                return True
            if self._write_playlist_tracks(playlist_id, ids) and self._verify_playlist_tracks(playlist_id, ids):
                return True
            from . import jobs
            if len(set(ids))<len(ids):return False
            jobs.output('Retrying playlist recovery with individual ordered additions')
            return self._write_playlist_tracks(playlist_id, ids, individual=True) and self._verify_playlist_tracks(playlist_id, ids)
        except Exception:
            return False

    def _confirmed_play_queue(self, ids):
        """Prepare and verify every occurrence before touching the destination."""
        from urllib.parse import quote_plus
        from . import jobs
        headers={**self.headers,'X-Plex-Client-Identifier':'Harmonia-sync'}
        def create(values):
            uri='library:///directory/'+quote_plus('/library/metadata/'+','.join(values))
            response=network.post(f'{self.base_url}/playQueues',headers=headers,
                params={'type':'audio','uri':uri,'shuffle':0,'repeat':0,'continuous':0,
                        'includeRelated':0,'window':len(ids)},timeout=15,allow_redirects=False)
            response.raise_for_status()
            container=response.json().get('MediaContainer',{})
            queue_id=container.get('playQueueID')
            if not queue_id:raise ValueError('Plex did not return a play queue ID')
            return str(queue_id),container
        def read(queue_id,container):
            if str(container.get('playQueueID',queue_id))!=queue_id:
                raise ValueError('Plex returned a different play queue')
            rows=container.get('Metadata',[])
            total=container.get('playQueueTotalCount')
            # Creation/mutation responses may contain only a window. Fetch a
            # full window; never infer completeness from HTTP 200 alone.
            if len(rows)!=len(ids) or (total is not None and int(total)!=len(rows)):
                response=network.get(f'{self.base_url}/playQueues/{queue_id}',headers=headers,
                    params={'own':0,'window':len(ids),'includeBefore':1,'includeAfter':1},
                    timeout=15,allow_redirects=False)
                response.raise_for_status()
                container=response.json().get('MediaContainer',{})
                rows=container.get('Metadata',[]);total=container.get('playQueueTotalCount')
                if str(container.get('playQueueID',queue_id))!=queue_id:
                    raise ValueError('Plex returned a different play queue')
            queued=[str(row.get('ratingKey') or '') for row in rows]
            complete=total is None or int(total)==len(queued)
            return queued,complete,total
        queue_id,container=create(ids)
        queued,complete,total=read(queue_id,container)
        if complete and queued==ids:
            jobs.output(f'Plex play queue verified: {len(ids)} ordered occurrences')
            return queue_id
        jobs.output(f'Plex initial queue differs: expected {len(ids)} occurrences; returned {len(queued)}; reported total {total if total is not None else "unknown"}. Building occurrences individually.')
        queue_id,container=create(ids[:1])
        seed=[str(row.get('ratingKey') or '') for row in container.get('Metadata',[])]
        if seed!=ids[:1] or int(container.get('playQueueTotalCount',1))!=1:
            raise ValueError('Plex did not confirm the first queue occurrence')
        for index,value in enumerate(ids[1:],2):
            jobs.progress(f'Preparing Plex queue · occurrence {index} of {len(ids)}',check=False,completed=index,total=len(ids))
            response=network.put(f'{self.base_url}/playQueues/{queue_id}',headers=headers,
                params={'uri':self._library_uri(value)},timeout=15,allow_redirects=False)
            response.raise_for_status()
            container=response.json().get('MediaContainer',{})
        queued,complete,total=read(queue_id,container)
        if not complete or queued!=ids:
            from collections import Counter
            missing=Counter(ids)-Counter(queued)
            evidence=', '.join(f'{key} x{count}' for key,count in list(missing.items())[:5]) or 'none; order differs or response is incomplete'
            raise ValueError(f'Plex queue verification failed: expected {len(ids)} occurrences; returned {len(queued)}; reported total {total if total is not None else "unknown"}; missing library IDs: {evidence}')
        jobs.output(f'Plex play queue verified: {len(ids)} ordered occurrences after individual construction')
        return queue_id

    def _write_playlist_tracks(self, playlist_id, track_ids, individual=False):
        """Use an ordered play queue for repeats, retaining the playlist identity."""
        from . import jobs
        ids = [str(value) for value in track_ids]
        existing = self.get_playlist_items(playlist_id)
        current_ids = [str(t.get('plex_id') or t.get('ratingKey')) for t in existing]
        if current_ids == ids:
            jobs.output('Plex playlist contents and order unchanged; skipping track writes')
            return True
        queue_id = None
        if not individual and len(set(ids)) < len(ids):
            jobs.progress('Preparing source duplicates in an ordered Plex play queue', check=False)
            try:
                queue_id=self._confirmed_play_queue(ids)
            except Exception as exc:
                from .diagnostics import redact
                if isinstance(exc,network.exceptions.RequestException):
                    response=getattr(exc,'response',None)
                    detail=f'Plex queue request failed (HTTP {response.status_code})' if response is not None else 'Plex queue request failed or timed out'
                else:detail=str(exc)
                jobs.output('Plex repeated-track queue unavailable; destination left unchanged. '+redact(detail, secrets=self.headers.values()))
                return False
        prefix = 0
        if not individual and queue_id is None and len(set(ids)) == len(ids) and len(set(current_ids)) == len(current_ids):
            while prefix < min(len(ids), len(current_ids)) and ids[prefix] == current_ids[prefix]:
                prefix += 1
        if prefix:
            # Keep the unchanged prefix and delete only the changed suffix.
            # Reverse deletion retains the original playlist item identifiers.
            for item in reversed(existing[prefix:]):
                item_id = item.get('playlist_item_id')
                if not item_id or not self.remove_from_playlist(playlist_id, str(item_id)):
                    return False
            ids = ids[prefix:]
        else:
            jobs.progress('Clearing destination Plex playlist', check=False)
            if not self.clear_playlist(playlist_id):
                print('✗ Could not clear the destination playlist; stopping this update.')
                return False
        if queue_id:
            response = network.put(f'{self.base_url}/playlists/{playlist_id}/items', headers=self.headers,
                params={'playQueueID':queue_id}, timeout=15, allow_redirects=False)
            if response.status_code in (200, 201):
                print(f'✓ Submitted {len(ids)} ordered occurrences from the play queue')
                return True
            jobs.output(f'Plex rejected the verified play queue (HTTP {response.status_code}); restoring previous contents.')
            return False
        # Plex accepts comma-separated library IDs in one URI. Keep repeat
        # occurrences on the ordered play-queue path above.
        batches = [','.join(ids[i:i+100]) for i in range(0,len(ids),100)] if not individual and len(set(ids)) == len(ids) else ids
        for position, track_id in enumerate(batches, 1):
            jobs.progress(f'Updating Plex playlist · occurrence {position} of {len(ids)}', check=False, completed=position, total=len(ids))
            if not self.add_to_playlist(playlist_id, track_id):
                return False
        return True

    def remove_from_playlist(
        self, playlist_id: str, playlist_item_id: str
    ) -> bool:
        """
        Remove an item from a Plex playlist.

        playlist_item_id must be Plex's playlistItemID, not the
        track's ratingKey.
        """

        try:
            resp = network.delete(
                f"{self.base_url}/playlists/{playlist_id}/items/"
                f"{playlist_item_id}",
                headers=self.headers,
                timeout=15,
             allow_redirects=False)

            if resp.status_code not in [200, 204]:
                print(
                    f"Warning: Failed to remove playlist item "
                    f"{playlist_item_id}: {resp.status_code} "
                    f"{resp.text[:500]}"
                )
                return False

            return True

        except Exception as e:
            print(f"Error removing from playlist: {e}")
            return False

    def clear_playlist(self, playlist_id: str) -> bool:
        """
        Remove every item from a playlist.

        Items are removed one at a time because Plex's playlist item
        endpoint identifies each item by playlistItemID.
        """

        items = self.get_playlist_items(playlist_id)

        if not items:
            return True

        success = True

        for item in items:
            item_id = item.get("playlist_item_id")

            if not item_id:
                print(
                    f"Warning: Playlist item for "
                    f"'{item.get('title', 'Unknown')}' has no "
                    "playlistItemID; cannot remove it."
                )
                return False

            if not self.remove_from_playlist(
                playlist_id, str(item_id)
            ):
                return False

        return success

    def update_playlist_metadata(
        self,
        playlist_id: str,
        title: str,
        description: str,
        artwork_url: str = None,
    ):
        """Update playlist name, description, and optionally artwork."""

        try:
            clean_title = repair_text(
                title
            )
            clean_description = clean_playlist_description(
                description or ""
            )

            params = {
                "title": clean_title,
                "summary": clean_description,
            }

            resp = network.put(
                f"{self.base_url}/playlists/{playlist_id}",
                headers=self.headers,
                params=params,
                timeout=15,
             allow_redirects=False)

            if resp.status_code not in [200, 204]:
                print(
                    f"Warning: Failed to update playlist metadata: "
                    f"{resp.status_code} {resp.text[:500]}"
                )

            # Attempt to update artwork if URL provided
            if artwork_url:
                if self._update_playlist_artwork(
                    playlist_id,
                    artwork_url,
                ):
                    print("  ✓ Synced artwork from source")

        except Exception as e:
            print(f"Error updating playlist metadata: {e}")

    @staticmethod
    def _detect_image_dimensions(data: bytes) -> Tuple[Optional[int], Optional[int]]:
        """
        Detect PNG/JPEG dimensions using only the Python standard library.
        This avoids adding Pillow as a required dependency.
        """
        if not data:
            return None, None

        # PNG: signature + IHDR width/height.
        if (
            len(data) >= 24
            and data[:8] == b"\x89PNG\r\n\x1a\n"
        ):
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return width, height

        # JPEG: scan marker segments until a Start Of Frame marker.
        if len(data) >= 4 and data[:2] == b"\xff\xd8":
            i = 2
            sof_markers = {
                0xC0, 0xC1, 0xC2, 0xC3,
                0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            }

            while i + 3 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue

                while i < len(data) and data[i] == 0xFF:
                    i += 1

                if i >= len(data):
                    break

                marker = data[i]
                i += 1

                # Standalone markers.
                if marker in (0xD8, 0xD9):
                    continue

                if i + 1 >= len(data):
                    break

                segment_length = int.from_bytes(
                    data[i:i + 2],
                    "big",
                )

                if segment_length < 2:
                    break

                if marker in sof_markers and i + 7 < len(data):
                    height = int.from_bytes(
                        data[i + 3:i + 5],
                        "big",
                    )
                    width = int.from_bytes(
                        data[i + 5:i + 7],
                        "big",
                    )
                    return width, height

                i += segment_length

        return None, None

    @staticmethod
    def _apple_artwork_variants(
        url: str,
    ) -> List[str]:
        """
        Generate Apple CDN artwork alternatives.

        Query strings such as ?l=en-US are preserved. The rendition regex is
        applied only to parsed.path so those query parameters no longer block
        creation of Apple's square "cc" crop URLs.
        """
        variants = [url]

        try:
            parsed = urlparse(url)
            host = (
                parsed.hostname
                or ""
            ).lower()
        except ValueError:
            return variants

        if "mzstatic" not in host:
            return variants

        path = parsed.path
        size = APPLE_ARTWORK_SIZE

        match = re.search(
            r"/\d+x\d+[A-Za-z0-9._-]*\.(?:jpe?g|png|webp)$",
            path,
            re.IGNORECASE,
        )

        if match:
            base_path = path[:match.start()]

            # A generic /image/thumb/gen rendition has no underlying artwork
            # identifier, so manufacturing alternate sizes only creates 404s.
            if base_path.rstrip('/').endswith('/image/thumb/gen'):
                return variants

            for candidate_path in (
                f"{base_path}/{size}x{size}cc.jpg",
                f"{base_path}/{size}x{size}bb-999.jpg",
            ):
                candidate = parsed._replace(
                    path=candidate_path
                ).geturl()

                if candidate not in variants:
                    variants.append(
                        candidate
                    )

        cc_path = re.sub(
            r"/(\d+)x(\d+)bb(?:-\d+)?\.(jpe?g|png|webp)$",
            r"/\1x\2cc.\3",
            path,
            flags=re.IGNORECASE,
        )

        if cc_path != path:
            candidate = parsed._replace(
                path=cc_path
            ).geturl()

            if candidate not in variants:
                variants.append(candidate)

        return variants

    @staticmethod
    def _center_crop_square(
        image_data: bytes,
        content_type: str,
    ) -> Tuple[
        Optional[bytes],
        Optional[str],
        Optional[int],
    ]:
        """
        Center-crop an image to the largest possible square.

        A 1200x630 image becomes 630x630 by removing 285 pixels from
        each horizontal side. No stretching is performed.
        """
        if Image is None:
            return None, None, None

        try:
            with Image.open(
                BytesIO(image_data)
            ) as image:
                width, height = image.size
                side = min(
                    width,
                    height,
                )

                if side < 300:
                    return None, None, side

                left = max(
                    0,
                    (width - side) // 2,
                )
                top = max(
                    0,
                    (height - side) // 2,
                )

                cropped = image.crop(
                    (
                        left,
                        top,
                        left + side,
                        top + side,
                    )
                )

                output = BytesIO()
                normalized_type = (
                    str(content_type or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )

                if normalized_type == "image/png":
                    cropped.save(
                        output,
                        format="PNG",
                        optimize=True,
                    )
                    output_type = "image/png"
                else:
                    if cropped.mode not in (
                        "RGB",
                        "L",
                    ):
                        cropped = cropped.convert(
                            "RGB"
                        )

                    cropped.save(
                        output,
                        format="JPEG",
                        quality=95,
                        optimize=True,
                    )
                    output_type = "image/jpeg"

                return (
                    output.getvalue(),
                    output_type,
                    side,
                )

        except Exception:
            return None, None, None

    def _update_playlist_artwork(
        self,
        playlist_id: str,
        artwork_url: str,
    ) -> bool:
        """
        Upload square artwork to Plex.

        Preference order:
        1. already-square source artwork;
        2. Apple CDN square crop variants;
        3. local center crop of the first large non-square image.
        """

        if not artwork_url:
            return False

        variants = self._apple_artwork_variants(
            artwork_url
        )

        last_error = None
        crop_fallback = None

        for index, candidate_url in enumerate(
            variants,
            1,
        ):
            try:
                image_resp = network.get(
                    candidate_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": (
                            "image/avif,image/webp,image/apng,"
                            "image/*,*/*;q=0.8"
                        ),
                    },
                    timeout=15,
                    _quiet_statuses={404},
                 allow_redirects=False)

                if image_resp.status_code != 200:
                    last_error = (
                        f"HTTP {image_resp.status_code} downloading "
                        f"artwork variant {index}"
                    )
                    continue

                content_type = (
                    image_resp.headers.get(
                        "Content-Type",
                        "",
                    )
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )

                if not content_type.startswith(
                    "image/"
                ):
                    last_error = (
                        f"variant {index} returned "
                        f"{content_type or 'unknown content type'}"
                    )
                    continue

                if not image_resp.content:
                    last_error = (
                        f"variant {index} returned an empty image"
                    )
                    continue

                width, height = (
                    self._detect_image_dimensions(
                        image_resp.content
                    )
                )

                if width and height:
                    print(
                        f"  → Artwork variant {index}: "
                        f"{width}x{height} actual pixels"
                    )

                    ratio = (
                        width / height
                        if height
                        else 0
                    )

                    if not (
                        0.95
                        <= ratio
                        <= 1.05
                    ):
                        if (
                            min(
                                width,
                                height,
                            )
                            >= 300
                            and crop_fallback is None
                        ):
                            crop_fallback = {
                                "data": image_resp.content,
                                "content_type": content_type,
                                "width": width,
                                "height": height,
                            }

                            print(
                                "    ↳ not square; saved as "
                                "center-crop fallback"
                            )
                        else:
                            print(
                                "    ↳ rejected: not square enough "
                                "for direct Plex upload"
                            )

                        last_error = (
                            f"variant {index} was {width}x{height}"
                        )
                        continue

                    if min(
                        width,
                        height,
                    ) < 300:
                        print(
                            "    ↳ rejected: artwork is below "
                            "300x300"
                        )
                        last_error = (
                            f"variant {index} was only "
                            f"{width}x{height}"
                        )
                        continue

                else:
                    try:
                        host = (
                            urlparse(
                                candidate_url
                            ).hostname
                            or ""
                        ).lower()
                    except ValueError:
                        host = ""

                    if "mzstatic" in host:
                        last_error = (
                            "could not verify Apple artwork dimensions"
                        )
                        continue

                upload_resp = network.post(
                    f"{self.base_url}/library/metadata/"
                    f"{playlist_id}/posters",
                    headers={
                        "X-Plex-Token": self.token,
                        "Content-Type": content_type,
                    },
                    data=image_resp.content,
                    timeout=20,
                 allow_redirects=False)

                if upload_resp.status_code in [
                    200,
                    201,
                    204,
                ]:
                    if width and height:
                        print(
                            f"  ✓ Plex poster uploaded at "
                            f"{width}x{height}"
                        )
                    return True

                last_error = (
                    f"Plex poster upload HTTP "
                    f"{upload_resp.status_code}"
                )

            except network.exceptions.Timeout:
                last_error = (
                    f"artwork variant {index} timed out"
                )
            except network.exceptions.RequestException as e:
                last_error = (
                    f"artwork variant {index} request error: {e}"
                )
            except Exception as e:
                last_error = (
                    f"artwork variant {index} error: {e}"
                )

        if crop_fallback is not None:
            original_width = crop_fallback[
                "width"
            ]
            original_height = crop_fallback[
                "height"
            ]

            cropped_data, cropped_type, side = (
                self._center_crop_square(
                    crop_fallback["data"],
                    crop_fallback[
                        "content_type"
                    ],
                )
            )

            if (
                cropped_data
                and cropped_type
                and side
            ):
                print(
                    f"  → Local center crop: "
                    f"{original_width}x{original_height} "
                    f"→ {side}x{side}"
                )

                try:
                    upload_resp = network.post(
                        f"{self.base_url}/library/metadata/"
                        f"{playlist_id}/posters",
                        headers={
                            "X-Plex-Token": self.token,
                            "Content-Type": cropped_type,
                        },
                        data=cropped_data,
                        timeout=20,
                     allow_redirects=False)

                    if upload_resp.status_code in [
                        200,
                        201,
                        204,
                    ]:
                        print(
                            f"  ✓ Plex poster uploaded at "
                            f"{side}x{side} "
                            "(center crop)"
                        )
                        return True

                    last_error = (
                        "center-cropped Plex poster upload "
                        f"HTTP {upload_resp.status_code}"
                    )

                except network.exceptions.Timeout:
                    last_error = (
                        "center-cropped Plex poster upload "
                        "timed out"
                    )
                except network.exceptions.RequestException as e:
                    last_error = (
                        "center-cropped Plex poster upload "
                        f"request error: {e}"
                    )

            elif Image is None:
                last_error = (
                    "local center crop requires Pillow "
                    "(pip install pillow)"
                )
            else:
                last_error = (
                    "local center crop could not process "
                    f"{original_width}x{original_height} artwork"
                )

        print(
            "  ⚠ No suitable square artwork was uploaded"
        )

        if last_error:
            print(
                f"    Last artwork issue: {last_error}"
            )

        return False


