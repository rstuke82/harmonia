"""Optional Lidarr integration: read-only discovery, reviewed writes and cached metadata."""
from .history_links import HistorySource
import copy
import hashlib
import json
import re
import threading
import time
import uuid
from urllib.parse import urlsplit
from typing import Literal

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field
from . import jobs

_mb_lock = threading.Lock()
_mb_last = 0.0
_settings_lock = threading.Lock()


class Defaults(BaseModel):
    root_folder: str = ''
    quality_profile_id: int = Field(default=0, ge=0)
    metadata_profile_id: int = Field(default=0, ge=0)
    monitor: Literal['selected', 'all', 'future', 'missing', 'existing', 'none'] = 'selected'
    monitor_new: Literal['none', 'new', 'all'] = 'none'
    monitor_album: bool = True
    search_now: bool = False
    tags: list[int] = Field(default_factory=list, max_length=100)
    tag_existing: bool = False


class Settings(Defaults):
    enabled: bool = False
    url: str = ''
    api_key: str = Field(default='', max_length=1024)
    musicbrainz_enabled: bool = True
    cache_days: Literal[7, 30] = 7


class NewTag(Settings):
    label: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9-]+$')


class Lookup(BaseModel):
    title: str = Field(default='', max_length=500)
    artist: str = Field(default='', max_length=500)
    album: str = Field(default='', max_length=500)
    query: str = Field(default='', max_length=500)
    provider: Literal['lidarr', 'musicbrainz'] = 'lidarr'
    fields_search: bool = False
    force_refresh: bool = False
    lookup_session: str = Field(default='',max_length=64)
    recording_offset: int = Field(default=0, ge=0, le=10000)
    attempt: int = Field(default=1, ge=1, le=4)
    attempts: int = Field(default=1, ge=1, le=4)
    retry_delay: int = Field(default=0, ge=0, le=120)


class Preview(Defaults):
    album_id: uuid.UUID


class ExistingSearch(BaseModel):
    history_source: HistorySource | None = None
    album_id: uuid.UUID
    source_title: str = Field(default='', max_length=500)
    source_artist: str = Field(default='', max_length=500)


class Confirm(BaseModel):
    history_source: HistorySource | None = None
    preview_id: uuid.UUID
    source_title: str = Field(default='', max_length=500)
    source_artist: str = Field(default='', max_length=500)


def repository():
    from .accounts import root_repository
    return root_repository()


def config(repo):
    return {**Settings().model_dump(), **repo.load('lidarr').get('settings', {})}


def public_settings(cfg):
    return {k: v for k, v in cfg.items() if k != 'api_key'} | {'api_key_saved': bool(cfg.get('api_key'))}


def normalized_url(url):
    url = url.strip().rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, 'Enter a complete http:// or https:// Lidarr URL, without credentials or query parameters.')
    return url


def merged(request, repo):
    cfg = Settings(**request.model_dump()).model_dump()
    old = config(repo)
    cfg['url'] = normalized_url(cfg['url']) if cfg['url'].strip() else ''
    cfg['api_key'] = cfg['api_key'].strip()
    if not cfg['api_key'] and cfg['url'] == old['url']:
        cfg['api_key'] = old['api_key']
    return cfg


class Client:
    def __init__(self, cfg):
        if not cfg.get('url') or not cfg.get('api_key'):
            raise HTTPException(422, 'Enter the Lidarr URL and API key in Settings → Lidarr.')
        self.url = normalized_url(cfg['url'])
        self.key = cfg['api_key']

    def call(self, method, path, **kwargs):
        quiet = kwargs.pop('quiet', False)
        level = 'DEBUG' if quiet else 'INFO'
        started = time.monotonic()
        from .api import _record_log
        from .diagnostics import redact
        target = f'{method} /api/v1/{path}'
        identity = (kwargs.get('params') or {}).get('term') or (kwargs.get('params') or {}).get('foreignAlbumId')
        if identity:
            target += f' [{identity}]'
        _record_log(level, 'Lidarr', f'Starting {target}')
        try:
            response = requests.request(method, self.url + '/api/v1/' + path,
                headers={'X-Api-Key': self.key, 'Accept': 'application/json'},
                timeout=(5, 25), allow_redirects=False, **kwargs)
            if response.status_code in (401, 403):
                raise HTTPException(502, 'Lidarr rejected the API key. Check Settings → Lidarr.')
            if 300 <= response.status_code < 400:
                raise HTTPException(502, 'Lidarr redirected the request. Use its final server URL, including its URL base.')
            if not response.ok:
                detail = redact(response.text[:12000].replace(self.key, '[REDACTED]'))
                from .diagnostics import concise_error
                _record_log('DEBUG','Lidarr',f'{target}: {detail}')
                short=concise_error(detail)
                raise HTTPException(502, f'Lidarr returned HTTP {response.status_code}: {short}')
            result = response.json() if response.content else None
            from .api import _record_log
            _record_log(level, 'Lidarr', f'Completed {target}: HTTP {response.status_code} in {time.monotonic()-started:.2f}s')
            return result
        except HTTPException as exc:
            _record_log('ERROR', 'Lidarr', f'{target} failed after {time.monotonic()-started:.2f}s: {exc.detail}')
            raise
        except (requests.RequestException, ValueError) as exc:
            exact = redact(str(exc).replace(self.key, '[REDACTED]'))
            message = f'{target} failed after {time.monotonic()-started:.2f}s: {type(exc).__name__}: {exact}'
            _record_log('ERROR', 'Lidarr', message)
            suffix = 'This was a read-only request; no album was added or changed.' if method == 'GET' else 'Inspect Lidarr before retrying; it may have accepted the write.'
            raise HTTPException(504 if isinstance(exc, requests.Timeout) else 502, message + ' ' + suffix) from None

    def options(self):
        status = self.call('GET', 'system/status')
        return {'version': status.get('version', ''),
                'roots': self.call('GET', 'rootfolder'),
                'qualities': self.call('GET', 'qualityprofile'),
                'metadata': self.call('GET', 'metadataprofile'),
                'tags': self.call('GET', 'tag')}


def validate_defaults(cfg, options):
    if cfg['root_folder'] not in [r.get('path') for r in options['roots']]:
        raise HTTPException(422, 'Select a root folder from this Lidarr server.')
    for field, collection in [('quality_profile_id', 'qualities'), ('metadata_profile_id', 'metadata')]:
        if cfg[field] not in [r.get('id') for r in options[collection]]:
            raise HTTPException(422, 'Select valid quality and metadata profiles from this Lidarr server.')
    if any(tag not in [r.get('id') for r in options.get('tags', [])] for tag in cfg.get('tags', [])):
        raise HTTPException(422, 'Select valid tags from this Lidarr server.')
    if cfg['search_now'] and not cfg['monitor_album']:
        raise HTTPException(422, 'Enable monitoring for the requested album before searching immediately.')
    if cfg['monitor'] == 'selected' and not cfg['monitor_album']:
        raise HTTPException(422, 'Selected-album monitoring requires monitoring the requested album.')
    if cfg['monitor'] == 'none' and cfg['monitor_album']:
        raise HTTPException(422, 'Choose selected-album monitoring, or disable monitoring for the requested album.')


def enabled(repo):
    cfg = config(repo)
    if not cfg['enabled']:
        raise HTTPException(409, 'Enable and configure Lidarr in Settings → Lidarr first.')
    return cfg


def fingerprint(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def state_get(repo, namespace, key):
    with repo.connect() as db:
        row = db.execute('SELECT value FROM state WHERE namespace=? AND key=?', (namespace, key)).fetchone()
    return json.loads(row[0]) if row else None


def state_put(repo, namespace, key, value):
    with repo.connect() as db:
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?,?)', (namespace, key, json.dumps(value)))


def album_summary(album):
    artist = album.get('artist') or {}
    return {'album_id': album.get('foreignAlbumId'), 'title': album.get('title', ''),
            'artist': artist.get('artistName', ''), 'artist_id': artist.get('foreignArtistId'),
            'artwork': next((i.get('remoteUrl') or i.get('url') for i in (album.get('images') or []) if i.get('coverType') == 'cover' and str(i.get('remoteUrl') or i.get('url') or '').startswith('https://')), ''),
            'year': str(album.get('releaseDate', ''))[:4], 'type': album.get('albumType', ''),
            'secondary_types': [x.get('name', '') if isinstance(x, dict) else str(x) for x in (album.get('secondaryTypes') or [])], 'exists': bool(album.get('id')), 'monitored': bool(album.get('monitored')) if album.get('id') else None, 'lidarr_id': album.get('id')}


def mb_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _discovery_title(value):
    """Remove edition/version noise without changing the user's title casing."""
    value = str(value or '').strip()
    qualifiers = (
        r'remaster(?:ed)?(?:\s+\d{4})?|deluxe(?:\s+edition)?|expanded(?:\s+edition)?|'
        r'anniversary(?:\s+edition)?|special\s+edition|collector\'?s\s+edition|bonus(?:\s+track)?|'
        r'advance(?:\s+(?:copy|promo|version))?(?=\s*(?:[\])]|$))|radio\s+(?:edit|mix)|single\s+edit|ep|single'
    )
    value = re.sub(rf'\s*[\[(]\s*(?:{qualifiers})[^\])]*[\])]', ' ', value, flags=re.I)
    value = re.sub(rf'\s+(?:-|–|—|:)\s*(?:{qualifiers})(?:\s+version)?\s*$', ' ', value, flags=re.I)
    # Providers also append bare release types without punctuation. Keep this
    # anchored so words such as "Epilogue" and "Single Ladies" survive.
    value = re.sub(r'\s+(?:ep|single)(?:\s+version)?\s*$', ' ', value, flags=re.I)
    return re.sub(r'\s+', ' ', value).strip(' -–—:')


def album_search_rows(records, known):
    rows={}
    for record in records:
        groups = [(record, record.get('first-release-date', ''))] if known else [(r.get('release-group', {}), r.get('date', '')) for r in record.get('releases', [])]
        credit = ''.join(c.get('name', c.get('artist', {}).get('name', '')) + c.get('joinphrase', '') for c in record.get('artist-credit', []) if isinstance(c, dict))
        for group, release_date in groups:
            if not group.get('id'):
                continue
            year = (group.get('first-release-date') or release_date or '')[:4]
            previous = rows.get(group['id'], {}).get('year', '')
            if previous and (not year or previous < year):year = previous
            rows[group['id']] = {'album_id': group['id'], 'title': group.get('title', ''),
                'artist': credit if known else ''.join(c.get('name', c.get('artist', {}).get('name', '')) + c.get('joinphrase', '') for c in group.get('artist-credit', []) if isinstance(c, dict)), 'year': year,
                'type': group.get('primary-type', ''), 'secondary_types': group.get('secondary-types', []),
                'exists': None, **({'other_release':bool(group.get('other-release'))} if not known else {}), 'artwork':'https://coverartarchive.org/release-group/'+group['id']+'/front-500'}
    return list(rows.values())


def musicbrainz_search(repo, cfg, request, test=False, publish=None):
    global _mb_last
    from .musicbrainz_settings import settings, ordered, search_scores
    preferences = settings(repo)
    from .api import _record_log
    _record_log('INFO','MusicBrainz',f'Lookup {request.artist} — {request.title}; album={request.album or "unknown"}; attempt {request.attempt}/{request.attempts}; retry delay elapsed={request.retry_delay}s')
    if not preferences['enabled'] and not test:
        raise HTTPException(409, 'MusicBrainz lookup is disabled in Settings → MusicBrainz.')
    album = request.album.strip()
    known = bool(album and album.casefold() not in ('n/a', 'unknown'))
    # Search broad canonical titles; edition labels belong in result review,
    # not in the indexed discovery query.
    search_album = _discovery_title(album) if known else album
    from .song_albums import search_identity
    search_artist,search_song=search_identity(request.artist,request.title) if not known else (request.artist.strip(),request.title)
    if request.fields_search:
        recording = _discovery_title(search_song)
        artist = search_artist
        if not (recording or artist or known):
            raise HTTPException(422, 'Enter a recording, artist, or album for MusicBrainz lookup.')
        # A known album is the strongest discovery anchor. Song becomes the
        # discovery query only when no usable album title is available.
        entity = 'release-group' if known or not recording else 'recording'
        terms = []
        if entity == 'recording' and recording:
            terms.append(f'recording:{mb_quote(recording)}')
        if known:
            terms.append(f'releasegroup:{mb_quote(search_album)}')
        if artist:
            terms.append(f'artist:{mb_quote(artist)}')
        query = ' AND '.join(terms)
        known = entity == 'release-group'
    else:
        if not request.artist.strip() or (not request.title.strip() and not known):
            raise HTTPException(422, 'Enter a track title and artist for MusicBrainz lookup.')
        entity = 'release-group' if known else 'recording'
        term = f'releasegroup:{mb_quote(search_album)}' if known else f'recording:{mb_quote(_discovery_title(search_song))}'
        query = term + f' AND artist:{mb_quote(search_artist)}'
    key = hashlib.sha256((('song-albums-v6:'+str(request.recording_offset)+':' if entity == 'recording' else '') + entity + query).encode()).hexdigest()
    # Background jobs coalesce identical work; public-service rate limiting is per request.
    saved = state_get(repo, 'musicbrainz_cache', key)
    if not request.force_refresh and saved and time.time() - saved['at'] < preferences['cache_days'] * 86400:
        from .api import _record_log
        _record_log('INFO', 'MusicBrainz', f'Cache hit for {entity}: {query}; {len(saved["rows"])} candidates; priority={preferences["release_priority"]}; prefer studio={preferences["prefer_studio"]}')
        return {'rows': search_scores(ordered(saved['rows'], preferences),request), 'cached': True, 'provider': 'MusicBrainz',
            'lookup_source':saved.get('lookup_source','unknown'),'lookup_host':saved.get('lookup_host',''),'lookup_reason':saved.get('lookup_reason',''),'next_recording_offset':saved.get('next_recording_offset')}
    _record_log('INFO','MusicBrainz',f'{entity} lookup: {query}; cache={"bypass" if request.force_refresh else "miss"}')
    started = time.monotonic()
    try:
        from .musicbrainz_client import get as mb_get
        data = mb_get(repo, entity, {'query': query, 'limit': 25,**({'offset':request.recording_offset} if entity=='recording' and request.recording_offset else {})})
    except (requests.RequestException, ValueError) as exc:
        from .diagnostics import service_failure
        from .diagnostics import redact
        failure=service_failure('MusicBrainz', exc, started)
        response=getattr(exc,'response',None)
        detail=redact(response.text[:2000]) if response is not None else redact(str(exc))
        _record_log('ERROR','MusicBrainz',f'{failure.detail} — {detail}')
        if request.attempt < request.attempts:
            _record_log('INFO','MusicBrainz',f'Client may retry after {15*request.attempt}s if lookup remains open and error is retryable')
        raise failure from None
    route=data.get('_playlist_bridge_route') or {};lookup_source=route.get('source','unknown');lookup_host=route.get('host','');lookup_reason=route.get('reason','')
    rows = {}
    records = data.get('release-groups', []) if known else data.get('recordings', [])
    next_offset=request.recording_offset+len(records) if not known and records and request.recording_offset+len(records)<int(data.get('recording-count',request.recording_offset+len(records)+(1 if len(records)==25 else 0))) else None
    if not known:
        from .song_albums import containing_albums
        try:records = containing_albums(repo, records, request, preferences, mb_get,
            publish=(lambda partial:publish({'rows':search_scores(ordered(album_search_rows(partial,False),preferences),request),'provider':'MusicBrainz','cached':False})) if publish else None)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        except requests.RequestException as exc:
            from .diagnostics import service_failure
            raise service_failure('MusicBrainz',exc,started) from None
    result = album_search_rows(records,known)
    source_label='configured mirror' if lookup_source=='mirror' else 'public fallback' if lookup_source=='public_fallback' else 'public' if lookup_source=='public' else 'unknown route'
    _record_log('INFO','MusicBrainz',f'{source_label.title()} search completed in {time.monotonic()-started:.2f}s; {len(records)} records, {len(result)} album candidates; priority={preferences["release_priority"]}; prefer studio={preferences["prefer_studio"]}')
    state_put(repo, 'musicbrainz_cache', key, {'at': time.time(), 'rows': result,'lookup_source':lookup_source,'lookup_host':lookup_host,'lookup_reason':lookup_reason,'next_recording_offset':next_offset})
    with repo.connect() as db:
        db.execute("DELETE FROM state WHERE namespace='musicbrainz_cache' AND key NOT IN (SELECT key FROM state WHERE namespace='musicbrainz_cache' ORDER BY json_extract(value,'$.at') DESC LIMIT 500)")
    return {'rows': search_scores(ordered(result, preferences), request), 'cached': False, 'provider': 'MusicBrainz','lookup_source':lookup_source,'lookup_host':lookup_host,'lookup_reason':lookup_reason,'next_recording_offset':next_offset}



def resolve(client, album_id):
    rows = client.call('GET', 'album/lookup', params={'term': 'lidarr:' + str(album_id)})
    album = next((a for a in rows if a.get('foreignAlbumId') == str(album_id)), None)
    if not album or not (album.get('artist') or {}).get('foreignArtistId'):
        raise HTTPException(404, 'Lidarr could not resolve that album. Try another edition or check its metadata service.')
    return album


def prepare(repo, request):
    cfg = enabled(repo)
    client = Client(cfg)
    from .api import _record_log
    _record_log('INFO', 'Album review', f'Started read-only review of release group {request.album_id}; loading Lidarr profiles')
    options = client.options()
    _record_log('INFO', 'Album review', 'Lidarr profiles loaded; validating album options')
    defaults = request.model_dump(exclude={'album_id'})
    validate_defaults(defaults, options)
    _record_log('INFO', 'Album review', f'Resolving release group {request.album_id} through Lidarr metadata lookup')
    album = resolve(client, request.album_id)
    artist = album['artist']
    _record_log('INFO', 'Album review', f"Resolved {album.get('title')} — {artist.get('artistName')}; checking whether album exists in Lidarr")
    current = client.call('GET', 'album', params={'foreignAlbumId': str(request.album_id)})
    existing = next((a for a in current if a.get('foreignAlbumId') == str(request.album_id)), None)
    if artist.get('id') and not artist.get('monitored') and (defaults['monitor_album'] or defaults['search_now']):
        raise HTTPException(409, 'This artist is unmonitored in Lidarr. Enable its monitoring there first, or add without monitoring/search. Other albums will not be changed here.')
    _record_log('INFO', 'Album review', f"Review ready: {album.get('title')} — {artist.get('artistName')}; artist exists={bool(artist.get('id'))}, album exists={bool(existing)}. No Lidarr changes made.")
    token = str(uuid.uuid4())
    record = {'at': time.time(), 'config_hash': fingerprint(cfg), 'album_id': str(request.album_id),
              'artist_id': artist['foreignArtistId'], 'defaults': defaults,
              'title': album.get('title', ''), 'artist': artist.get('artistName', ''),
              'artwork': album_summary(album)['artwork'] or 'https://coverartarchive.org/release-group/'+str(request.album_id)+'/front-500'}
    state_put(repo, 'lidarr_previews', token, record)
    with repo.connect() as db:
        db.execute("DELETE FROM state WHERE namespace='lidarr_previews' AND json_extract(value,'$.at') < ? AND json_extract(value,'$.job_id') IS NULL", (time.time() - 3600,))
    return {'preview_id': token, 'album': album_summary(album), 'artist_exists': bool(artist.get('id')),
            'album_exists': bool(existing), 'album_monitored': bool(existing and existing.get('monitored')),
            'defaults': defaults, 'tags': [r['label'] for r in options.get('tags', []) if r['id'] in defaults.get('tags', [])], 'quality': next(r['name'] for r in options['qualities'] if r['id'] == defaults['quality_profile_id']),
            'metadata': next(r['name'] for r in options['metadata'] if r['id'] == defaults['metadata_profile_id'])}


def run_album_command(client, body, label, timeout=120, existing_id=None, accepted=None):
    from .api import _record_log
    jobs.progress(label)
    command = client.call('GET', f'command/{existing_id}') if existing_id else client.call('POST', 'command', json=body)
    if not command or not command.get('id'):
        raise ValueError(f'{label}: Lidarr did not return a command ID. Inspect Lidarr before retrying.')
    command_id = command['id']
    if accepted:
        accepted(command_id)
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        status = str(command.get('status', '')).lower()
        if status != previous:
            _record_log('INFO', 'Lidarr command', f'{label}: command {command_id} {status or "pending"}')
            previous = status
        if status == 'completed':
            return command_id
        if status in ('failed', 'aborted', 'cancelled', 'orphaned'):
            detail = str(command.get('exception') or command.get('message') or status).replace(client.key if hasattr(client, 'key') else '\0', '[REDACTED]')
            raise ValueError(f'{label}: command {command_id} {status}: {detail}')
        if time.monotonic() >= deadline:
            raise ValueError(f'{label}: command {command_id} still {status or "pending"} after {timeout}s. The album remains in Lidarr; inspect the command before retrying.')
        jobs.progress(f'{label} · command {command_id} {status or "pending"}')
        time.sleep(2)
        jobs.progress(f'Checking {label.lower()}')
        command = client.call('GET', f'command/{command_id}')


def execute_add(payload):
    repo = repository()
    cfg = enabled(repo)
    if fingerprint(cfg) != payload['config_hash']:
        raise ValueError('Lidarr settings changed after review. Review this album again before adding it.')
    client = Client(cfg)
    defaults = payload['defaults']
    jobs.progress('Checking the reviewed album in Lidarr')
    album = resolve(client, payload['album_id'])
    if album['artist']['foreignArtistId'] != payload['artist_id']:
        raise ValueError('Album identity changed in Lidarr. Review it again.')
    artist = album['artist']
    current = client.call('GET', 'album', params={'foreignAlbumId': payload['album_id']})
    existing = next((a for a in current if a.get('foreignAlbumId') == payload['album_id']), None)
    if artist.get('id') and not artist.get('monitored') and (defaults['monitor_album'] or defaults['search_now']):
        raise ValueError('Artist monitoring changed in Lidarr. Review its settings before retrying.')
    # Add selected tags without replacing the artist's existing tags.
    if artist.get('id') and defaults.get('tag_existing') and defaults.get('tags'):
        latest_artist = client.call('GET', 'artist/' + str(artist['id']))
        merged_tags = sorted(set(latest_artist.get('tags') or []) | set(defaults['tags']))
        if merged_tags != sorted(latest_artist.get('tags') or []):
            jobs.progress('Adding selected tags to the existing artist')
            client.call('PUT', 'artist/' + str(artist['id']), json={**latest_artist, 'tags': merged_tags})
            jobs.output('Selected tags added; existing artist tags retained')
    # Never change the profiles, path or catalog monitoring of an existing artist.
    if not existing:
        validate_defaults(defaults, client.options())
        body = copy.deepcopy(album)
        body.pop('id', None)
        body['monitored'] = defaults['monitor_album']
        body['addOptions'] = {'addType': 'manual', 'searchForNewAlbum': False}
        if not artist.get('id'):
            body['artist'] = {**artist, 'rootFolderPath': defaults['root_folder'],
                'qualityProfileId': defaults['quality_profile_id'], 'metadataProfileId': defaults['metadata_profile_id'],
                'monitored': defaults['monitor'] != 'none', 'monitorNewItems': defaults['monitor_new'],
                'tags': defaults.get('tags', []),
                'addOptions': {'monitor': 'unknown' if defaults['monitor'] == 'selected' else defaults['monitor'],
                    'albumsToMonitor': [payload['album_id']] if defaults['monitor'] == 'selected' else [],
                    'searchForMissingAlbums': False}}
            body['artist'].pop('id', None)
            body['artist'].pop('path', None)
        jobs.progress('Adding the selected album to Lidarr')
        result = client.call('POST', 'album', json=body)
        jobs.output(f"Added {payload['title']} — {payload['artist']} to Lidarr")
        from .lidarr_requests import save
        save(repo, payload['album_id'], lidarr_id=result.get('id'), status='added', error='')
        return finish_search(repo, client, payload, result.get('id'), added=True)
    if defaults['monitor_album'] and not existing.get('monitored'):
        jobs.progress('Monitoring the selected album in Lidarr')
        client.call('PUT', 'album/monitor', json={'albumIds': [existing['id']], 'monitored': True})
    from .lidarr_requests import save
    save(repo, payload['album_id'], lidarr_id=existing['id'], status='added', error='')
    return finish_search(repo, client, payload, existing['id'], added=False)


def wait_album_ready(client, album_id, timeout=120):
    """Observe Lidarr's initial refresh; never start another one."""
    deadline = time.monotonic() + timeout
    stable = 0
    while True:
        jobs.progress('Waiting for Lidarr initial refresh and track metadata')
        album = client.call('GET', f'album/{album_id}')
        commands = client.call('GET', 'command')
        pending = any(str(c.get('status','')).lower() in ('queued','started','running') and
            ((c.get('body',{}).get('name',c.get('name')) == 'RefreshAlbum' and c.get('body',{}).get('albumId') in (None,album_id)) or
             (c.get('body',{}).get('name',c.get('name')) == 'RefreshArtist' and c.get('body',{}).get('artistId') in (None,album.get('artistId')))) for c in commands)
        tracks = client.call('GET','track',params={'albumId':album_id})
        stable = stable + 1 if tracks and not pending else 0
        if stable >= 2:
            jobs.output(f'Lidarr album {album_id} ready: {len(tracks)} tracks, initial refresh no longer active')
            return
        if time.monotonic() >= deadline:
            raise TimeoutError('Initial Lidarr refresh or track metadata is still pending. No additional refresh or search was started.')
        time.sleep(2)


def select_preferred_edition(repo, client, album_id, group_id):
    """Rank Lidarr's own editions and preserve its import acceptance setting."""
    from .musicbrainz_settings import settings
    album=client.call('GET',f'album/{album_id}')
    if str(album.get('foreignAlbumId'))!=str(group_id):
        raise ValueError('Lidarr album identity changed; no search started.')
    releases=[r for r in (album.get('releases') or []) if isinstance(r,dict) and r.get('foreignReleaseId')]
    if not releases:raise ValueError('Lidarr has no release editions yet; no search started. Retry after its metadata refresh.')
    files=client.call('GET','trackfile',params={'albumId':album_id})
    if files:
        jobs.output('Existing album files found; preserving the current release and file identities.')
        return
    if album.get('anyReleaseOk') is False and len([r for r in releases if r.get('monitored')])==1:
        jobs.output('Preserving the explicitly selected Lidarr release.')
        return
    preferences=settings(repo)
    from .edition_preferences import ranker
    rank=ranker(preferences)
    jobs.progress('Ranking Lidarr editions by preferred countries, formats and official status')
    selected=min(releases,key=rank);release_id=str(selected['foreignReleaseId'])
    original_any=album.get('anyReleaseOk')
    for row in album.get('releases') or []:
        row['monitored']=str(row.get('foreignReleaseId'))==release_id
    jobs.progress('Applying the preferred edition in Lidarr')
    client.call('PUT',f'album/{album_id}',json=album)
    confirmed=client.call('GET',f'album/{album_id}')
    monitored=[str(r.get('foreignReleaseId')) for r in (confirmed.get('releases') or []) if r.get('monitored')]
    if confirmed.get('anyReleaseOk')!=original_any or monitored!=[release_id]:
        raise ValueError('Lidarr did not retain the preferred edition and original anyReleaseOk setting; no search started.')
    jobs.output(f"Preferred edition verified: {selected.get('country') or 'Unknown country'} · {selected.get('format') or 'Unknown format'} · {selected.get('trackCount')} tracks · {selected.get('disambiguation') or 'standard'} · {release_id}; anyReleaseOk unchanged ({original_any})")


def finish_search(repo, client, payload, album_id, added=False):
    from .lidarr_requests import save
    from .api import _record_log
    base = 'Album added' if added else 'Album is in Lidarr'
    result = {'album_id':album_id,'title':payload['title'],'added':added,'search_requested':False}
    if not payload['defaults']['search_now'] and not added:
        result['summary'] = base + '; no search requested'
        return result
    save(repo,payload['album_id'],status='search_pending')
    command_id = None
    submitting = False
    try:
        if not album_id:
            raise ValueError('Lidarr returned no confirmed album ID. Check Lidarr.')
        previous = state_get(repo,'lidarr_requests',payload['album_id']) or {}
        previous_id = previous.get('search_command_id')
        if previous_id:
            prior = client.call('GET', f'command/{previous_id}')
            if str(prior.get('status','')).lower() in ('failed','aborted','cancelled','orphaned'):
                previous_id = None
        if not previous_id:
            wait_album_ready(client,album_id)
            select_preferred_edition(repo,client,album_id,payload['album_id'])
            if not payload['defaults']['search_now']:
                save(repo,payload['album_id'],status='added',error='')
                result['summary']=base+'; preferred edition checked; no search requested'
                return result
        def accepted(cid):
            nonlocal command_id
            command_id = cid
            save(repo,payload['album_id'],search_command_id=cid,status='search_pending')
        submitting = True
        cid = run_album_command(client,{'name':'AlbumSearch','albumIds':[album_id]},f"Searching for {payload['title']}",existing_id=previous_id,accepted=accepted)
        save(repo,payload['album_id'],status='search_completed',error='')
        result.update(search_requested=True,search_command_id=cid,summary=base+'; search completed (downloads are managed by Lidarr)')
    except Exception as exc:
        from .diagnostics import redact
        detail = redact(str(getattr(exc,'detail',str(exc))).replace(getattr(client,'key','') or '\0','[REDACTED]'))
        pending = isinstance(exc,TimeoutError) or (command_id and 'still ' in detail)
        unknown = submitting and not command_id and isinstance(exc,HTTPException)
        status = 'search_unknown' if unknown else 'search_pending' if pending else 'search_failed'
        summary = base + ('; search submission unconfirmed' if unknown else '; search pending' if pending else '; search failed')
        save(repo,payload['album_id'],status=status,error='' if pending else detail,search_wait_detail=detail if pending else '')
        _record_log('WARNING' if pending else 'ERROR','Lidarr search',summary+': '+detail)
        result.update(partial_success=True,summary=summary,error=detail,search_command_id=command_id)
    return result


def execute(payload):
    from .lidarr_requests import save
    repo = repository()
    try:
        return execute_add(payload)
    except Exception as exc:
        from .diagnostics import redact
        save(repo,payload['album_id'],status='add_failed',error=redact(getattr(exc,'detail',str(exc))))
        raise


def retry_search(payload):
    from .lidarr_requests import server_id
    repo = repository()
    cfg = enabled(repo)
    if payload['server'] != server_id(cfg):
        raise ValueError('Lidarr server changed; review the album on the new server.')
    record = state_get(repo,'lidarr_requests',payload['album_id'])
    client = Client(cfg)
    album = client.call('GET',f"album/{record['lidarr_id']}")
    if album.get('foreignAlbumId') != payload['album_id']:
        raise ValueError('Lidarr album identity changed; search was not submitted.')
    from .lidarr_downloads import queue
    if any(q.get('albumId')==record['lidarr_id'] for q in queue(client)):
        raise ValueError('This album already has a queued download. Review it in Requests.')
    return finish_search(repo,client,{'album_id':payload['album_id'],'title':record['title'],'defaults':{'search_now':True}},record['lidarr_id'])


def register(app):
    @app.get('/api/settings/lidarr')
    def settings():
        repo = repository()
        return {**public_settings(config(repo)), 'cache_entries': len(repo.load('musicbrainz_cache'))}

    @app.get('/api/settings/lidarr/tags')
    def saved_tags():
        return Client(config(repository())).call('GET', 'tag')

    @app.post('/api/settings/lidarr/test')
    def test(request: Settings):
        return Client(merged(request, repository())).options()

    @app.post('/api/settings/lidarr/tag')
    def create_tag(request: NewTag):
        client = Client(merged(request, repository()))
        current = client.call('GET', 'tag')
        existing = next((t for t in current if t.get('label') == request.label), None)
        return existing or client.call('POST', 'tag', json={'label': request.label})

    @app.put('/api/settings/lidarr')
    def save(request: Settings):
        with _settings_lock:
            repo = repository()
            cfg = merged(request, repo)
            if cfg['enabled']:
                validate_defaults(cfg, Client(cfg).options())
            repo.save({'lidarr': {'settings': cfg}})
            return public_settings(cfg)

    @app.delete('/api/settings/lidarr/cache')
    def clear_cache():
        with _mb_lock:
            repository().save({'musicbrainz_cache': {}})
        return {'cleared': True}

    @app.post('/api/lidarr/search-jobs', status_code=202)
    def start_lookup(request: Lookup):
        repo=repository();enabled(repo)
        request.provider='musicbrainz'
        payload=request.model_dump(exclude={'attempt','attempts','retry_delay'})
        store=jobs.Store(repo)
        return {'id':store.enqueue('musicbrainz_lookup',payload)}

    @app.get('/api/lidarr/search-jobs/{job_id}')
    def lookup_status(job_id: str):
        job=jobs.Store(repository()).get(job_id)
        if not job or job['action']!='musicbrainz_lookup':raise HTTPException(404,'Search not found.')
        return {k:job.get(k) for k in ('id','status','progress','result','error')}

    @app.delete('/api/lidarr/search-jobs/{job_id}')
    def cancel_lookup(job_id: str):
        store=jobs.Store(repository());job=store.get(job_id)
        if not job or job['action']!='musicbrainz_lookup':raise HTTPException(404,'Search not found.')
        store.cancel(job_id)
        return {'cancelled':True}

    @app.post('/api/lidarr/search')
    def search(request: Lookup):
        repo = repository()
        cfg = enabled(repo)
        if request.provider == 'musicbrainz':
            return musicbrainz_search(repo, cfg, request)
        term = (' - '.join(v.strip() for v in (request.artist, request.album or request.title) if v.strip()) if request.fields_search else request.query.strip() or request.album.strip())
        if not term or term.casefold() == 'n/a':
            raise HTTPException(422, 'Enter an album name, or use Find albums for this track with MusicBrainz.')
        rows = Client(cfg).call('GET', 'album/lookup', params={'term': term})
        from .musicbrainz_settings import settings, ordered, search_scores
        preferences = settings(repo)
        ranked = ordered([album_summary(a) for a in rows if a.get('foreignAlbumId')], preferences)
        from .api import _record_log
        _record_log('INFO', 'Album search', f"Lidarr returned {len(ranked)} albums; applied release priority {', '.join(preferences['release_priority'])}; prefer studio={preferences['prefer_studio']}")
        return {'rows': search_scores(ranked,request)[:50], 'cached': False, 'provider': 'Lidarr'}

    @app.post('/api/lidarr/search-existing', status_code=202)
    def search_existing(request: ExistingSearch):
        from .api import job_store
        from .lidarr_requests import server_id, active
        repo = repository()
        cfg = enabled(repo)
        album_key = str(request.album_id)
        rows = Client(cfg).call('GET', 'album', params={'foreignAlbumId': album_key})
        album = next((a for a in rows if a.get('foreignAlbumId') == album_key and a.get('id')), None)
        if not album:
            raise HTTPException(409, 'This album is no longer in Lidarr. Search again to refresh the results.')
        server = server_id(cfg)
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM state WHERE namespace='lidarr_requests' AND key=?", (album_key,)).fetchone()
            record = json.loads(row[0]) if row else {}
            if record.get('server') != server:
                record = {}
            if record.get('status') == 'search_unknown':
                raise HTTPException(409, 'A previous search submission is unconfirmed. Inspect Lidarr before retrying.')
            sources = record.get('sources', [])
            source = {'title': request.source_title, 'artist': request.source_artist}
            if source['title'] and source not in sources:
                sources.append(source)
            if active(db, record.get('job_id')):
                jid = record['job_id']
            else:
                # Completed searches may be explicitly run again; pending ones resume their command.
                if record.get('status') == 'search_completed':
                    record.pop('search_command_id', None)
                jid = job_store().enqueue('lidarr_search', {'album_id': album_key, 'server': server}, db=db)
                record.update(job_id=jid, status='search_pending', error='')
            from .accounts import actor
            user=actor() or {}
            if user.get('id'):
                people=record.get('requesters') or []
                uid=str(user['id'])
                if not any(str(person.get('user_id'))==uid for person in people):
                    people.append({'user_id':uid,'name':user.get('name') or 'User '+uid,'requested_at':time.time()})
                record['requesters']=people
            now=time.time()
            record.update(server=server, lidarr_id=album['id'], title=album.get('title',''),
                          artist=(album.get('artist') or {}).get('artistName',''), artwork=album_summary(album)['artwork'] or record.get('artwork') or 'https://coverartarchive.org/release-group/'+album_key+'/front-500', sources=sources,
                          created_at=record.get('created_at') or now, updated_at=now)
            db.execute("INSERT INTO state(namespace,key,value) VALUES('lidarr_requests',?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value", (album_key, json.dumps(record)))
        from .history_links import requested
        requested(request.history_source,album_key,jid)
        return job_store().get(jid)

    @app.post('/api/lidarr/preview')
    def preview(request: Preview):
        return prepare(repository(), request)

    @app.post('/api/lidarr/add', status_code=202)
    def add(request: Confirm):
        from .api import job_store
        repo = repository()
        cfg = enabled(repo)
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM state WHERE namespace='lidarr_previews' AND key=?", (str(request.preview_id),)).fetchone()
            if not row:
                raise HTTPException(409, 'Review the album before adding it.')
            payload = json.loads(row[0])
            if payload.get('job_id'):
                job_id = payload['job_id']
            else:
                if time.time() - payload['at'] > 900 or payload['config_hash'] != fingerprint(cfg):
                    raise HTTPException(409, 'The preview expired or settings changed. Review the album again.')
                from .lidarr_requests import server_id, active
                server = server_id(cfg)
                old = db.execute("SELECT value FROM state WHERE namespace='lidarr_requests' AND key=?", (payload['album_id'],)).fetchone()
                record = json.loads(old[0]) if old else {}
                if record.get('server') != server:
                    record = {}
                source = {'title':request.source_title,'artist':request.source_artist}
                sources = record.get('sources',[])
                if source['title'] and source not in sources:
                    sources.append(source)
                if record and (active(db,record.get('job_id')) or record.get('lidarr_id')):
                    job_id = record['job_id']
                    record['sources'] = sources
                else:
                    from .accounts import actor
                    user=actor() or {}
                    payload['requester_id']=str(user.get('id') or '')
                    payload['requester_name']=user.get('name') or ''
                    job_id = job_store().enqueue('lidarr_add', payload, db=db)
                    now=time.time()
                    record = {'server':server,'sources':sources,'title':payload['title'],'artist':payload['artist'],'job_id':job_id,'status':'queued','created_at':now,'updated_at':now}
                from .accounts import actor
                user=actor() or {}
                if user.get('id'):
                    requesters=record.get('requesters') or []
                    uid=str(user['id'])
                    if not any(str(r.get('user_id'))==uid for r in requesters):
                        requesters.append({'user_id':uid,'name':user.get('name') or 'User '+uid,'requested_at':time.time()})
                    record['requesters']=requesters
                record['artwork']=record.get('artwork') or payload.get('artwork') or 'https://coverartarchive.org/release-group/'+payload['album_id']+'/front-500'
                db.execute("INSERT INTO state(namespace,key,value) VALUES('lidarr_requests',?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value", (payload['album_id'],json.dumps(record)))
                payload['job_id'] = job_id
                db.execute("UPDATE state SET value=? WHERE namespace='lidarr_previews' AND key=?", (json.dumps(payload), str(request.preview_id)))
        from .history_links import requested
        requested(request.history_source,payload['album_id'],job_id)
        return job_store().get(job_id)


def execute_lookup(payload):
    if payload.get('member_request'):
        from .member_album_search import execute
        return execute(payload)
    ctx=jobs.current()
    repo=ctx.store.repository if ctx else repository()
    def publish(result):
        if ctx:
            ctx.checkpoint()
            ctx.store.update(ctx.id,result=result)
    jobs.progress('Searching MusicBrainz')
    return musicbrainz_search(repo,enabled(repo),Lookup(**payload),publish=publish)
