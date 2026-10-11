"""Queued description-only updates; never advance the successful sync time."""
import requests
from . import jobs


def queue(keys=None, inherited_only=False):
    from .api import _config, _playlist_key, job_store
    from .playlist_schedules import read
    store = job_store()
    playlists = _config(read_only=True, namespaces=[]).config.get('playlists', [])
    selected = [_playlist_key(p) for p in playlists
                if p.get('last_synced') and p.get('plex_playlist_id')
                and (keys is None or _playlist_key(p) in keys)
                and (not inherited_only or read(store.repository, _playlist_key(p))['mode'] == 'inherit')]
    return store.enqueue('playlist_description', {'playlist_keys': selected}) if selected else None


def queue_server_schedule():
    from .accounts import member_stores, as_user
    with as_user(None):
        queue(inherited_only=True)
    for user, _store in member_stores():
        with as_user(user):
            queue(inherited_only=True)


def execute(payload):
    from .api import _config, _playlist_key, _health_plex
    from .process_lock import ProcessLock
    from .playlist_description import render, MARKER, LEGACY_MARKER
    changed = 0
    with ProcessLock():
        config = _config(read_only=True, namespaces=[])
        plex = _health_plex(config)
        for playlist in config.config.get('playlists', []):
            if _playlist_key(playlist) not in payload.get('playlist_keys', []) or not playlist.get('last_synced'):
                continue
            jobs.progress('Updating next scheduled sync in Plex: ' + playlist.get('plex_playlist_name', 'Playlist'))
            response = requests.get(f"{plex.base_url}/playlists/{playlist['plex_playlist_id']}", headers=plex.headers, timeout=20)
            response.raise_for_status()
            rows = response.json().get('MediaContainer', {}).get('Metadata', [])
            if not rows:
                raise ValueError('Plex did not return the playlist metadata.')
            description = rows[0].get('summary', '')
            marker=MARKER if MARKER in description else LEGACY_MARKER
            if marker not in description:
                jobs.output('No Harmonia sync summary yet; it will be added on the next successful sync.')
                continue
            # Preserve the last successful sync's source/count summary exactly.
            old = description.split(marker, 1)[1]
            next_line = next(line for line in render('', playlist, 0, 0, 0).splitlines() if line.startswith('Next scheduled sync:'))
            lines = [line for line in old.splitlines() if not line.startswith('Next scheduled sync:')]
            at = next((i+1 for i, line in enumerate(lines) if line.startswith('Last successful sync:')), len(lines))
            lines.insert(at, next_line)
            updated = description.split(marker, 1)[0] + MARKER + '\n'.join(lines)
            write = requests.put(f"{plex.base_url}/playlists/{playlist['plex_playlist_id']}", headers=plex.headers, params={'summary': updated}, timeout=20)
            write.raise_for_status()
            changed += 1
    return {'summary': f'Updated scheduled-sync descriptions for {changed} playlists. No tracks or sync timestamps changed.'}
