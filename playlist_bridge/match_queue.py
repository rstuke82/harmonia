"""Durable reviewed match drafts; apply many edits with one sync per playlist."""
import copy
import hashlib
import json
import time
import uuid
from fastapi import HTTPException
from . import jobs


def draft_id(change):
    return hashlib.sha256((change['playlist_key'] + '\0' + change['search_key']).encode()).hexdigest()


def stage_changes(repo, changes):
    if not changes:
        raise HTTPException(409, 'No applicable matches to queue. Refresh the track and try again.')
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for change in changes:
            entry = {**change, 'revision': str(uuid.uuid4()), 'queued_at': time.time()}
            db.execute("INSERT OR REPLACE INTO state VALUES('match_drafts',?,?)", (draft_id(change), json.dumps(entry)))
    return {'queued': len({draft_id(c) for c in changes})}


def available(repo, db=None):
    def read(conn):
        active = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running','cancelling')")}
        return [{'id': key, **json.loads(value)} for key, value in conn.execute("SELECT key,value FROM state WHERE namespace='match_drafts'") if json.loads(value).get('job_id') not in active]
    if db is not None:
        return read(db)
    with repo.connect() as conn:
        return read(conn)


def stage_match(request):
    from .api import _config, job_store
    from .track_routes import inspect
    config = _config(read_only=True,namespaces=['health','source_snapshots','mapping','match_metadata','missing','ignored_tracks','artist_aliases'])
    track = request.model_dump(include={'title', 'artist', 'album'})
    changes = []
    wanted=None if request.playlist_keys is None else set(request.playlist_keys) | ({request.replace_playlist_key} if request.replace_playlist_key else set())
    for member in inspect(config, track, wanted)['memberships']:
        replacement = member['key'] == request.replace_playlist_key
        if not replacement and request.playlist_keys is not None and member['key'] not in request.playlist_keys:
            continue
        for entry in member['tracks']:
            if entry['status'] == 'ignored' or (not replacement and entry['status'] not in ('unresolved', 'lost')):
                continue
            changes.append({'playlist_key': member['key'], 'playlist_name': member['name'],
                'search_key': entry['search_key'], 'source': entry['source'], 'before': entry['status'],
                'previous_plex_id': entry['plex_id'], 'plex_id': request.plex_id, 'provenance': request.provenance,
                'candidate': {k: str(v)[:500] for k, v in getattr(request, 'candidate', {}).items() if k in ('title', 'artist', 'album')}})
    return stage_changes(job_store().repository, changes)


def execute(payload):
    from .api import _config, _health_plex, _playlist_key, _capture, job_store
    from .process_lock import ProcessLock
    from .sync_engine import Syncer
    from .matching import Matcher
    from .diagnostics import redact
    with ProcessLock():
        config = _config()
        syncer = Syncer(config)
        changes = payload['changes']
        playlists = {_playlist_key(p): p for p in config.config['playlists']}
        jobs.progress('Validating queued match choices')
        library = _health_plex(config).search_library('')
        lookup = {str(t.get('plex_id')): t for t in library}
        for c in changes:
            key = c['playlist_key']; search = c['search_key']
            old = config.mapping.get(key, {}).get(search)
            old = str(old) if old is not None else None
            if key not in playlists or syncer._find_ignored_track_key(key, c['source']):
                raise ValueError('A queued track was ignored or its playlist removed. Review the match queue.')
            if old not in (c['previous_plex_id'], c['plex_id']):
                raise ValueError('A saved match changed since review. Review the match queue before applying.')
            if c['plex_id'] not in lookup:
                raise ValueError('A queued Plex candidate is no longer available. Review the match queue.')
            if c['provenance'] == 'automatic' and str(Matcher.match_track(c['source'], library, {})) != c['plex_id']:
                raise ValueError('An automatic candidate changed. Retry matching before applying the queue.')
        jobs.progress(f"Saving {len(changes)} queued match edits")
        for c in changes:
            key = c['playlist_key']; search = c['search_key']
            config.mapping.setdefault(key, {})[search] = c['plex_id']
            syncer._set_match_provenance(key, search, c['provenance'], matched_track=lookup[c['plex_id']], plex_id=c['plex_id'])
            config.missing[key] = [t for t in config.missing.get(key, []) if not syncer._same_missing_identity(t, c['source'])]
        config.save()
        # Remove only applied revisions; newer selections stay in the queue.
        with job_store().repository.connect() as db:
            for c in changes:
                db.execute("DELETE FROM state WHERE namespace='match_drafts' AND key=? AND json_extract(value,'$.revision')=?", (c['id'], c['revision']))
        if payload.get('save_only'):
            jobs.output(f'Applied {len(changes)} reviewed match edits before sync')
            return {'matches_saved':len(changes)}
        selected = list(dict.fromkeys(c['playlist_key'] for c in changes))
        results = []
        for index, key in enumerate(selected, 1):
            playlist = playlists[key]
            jobs.target(key, playlist.get('plex_playlist_name', key), index, len(selected))
            jobs.progress('Syncing playlist after all queued edits')
            try:
                summary, _ = _capture(syncer.sync_playlist, playlist)
                ok = not summary.get('errors')
                results.append({'key': key, 'name': playlist.get('plex_playlist_name', key), 'ok': ok, 'error': '' if ok else 'Match edits saved; Plex sync or verification failed. See the track differences in the log.', 'result': {'summary': summary}})
            except Exception as exc:
                results.append({'key': key, 'name': playlist.get('plex_playlist_name', key), 'ok': False, 'error': redact(getattr(exc, 'detail', str(exc)), config)})
            jobs.activity(mode='completed' if results[-1]['ok'] else 'failed',
                          stage='Playlist completed' if results[-1]['ok'] else results[-1]['error'])
            ctx = jobs.current()
            if ctx:
                ctx.store.update(ctx.id, result={'playlists': results, 'total': len(selected), 'matches_saved': len(changes)})
        if any(not r['ok'] for r in results):
            return {'playlists': results, 'total': len(selected), 'matches_saved': len(changes), 'partial_success': True, 'summary': f'{len(changes)} match edits saved; {sum(not r["ok"] for r in results)} playlist sync(s) failed. Review the track differences and retry the affected sync. Your matches do not need to be entered again.'}
        return {'playlists': results, 'total': len(selected), 'matches_saved': len(changes)}


def register(app):
    from .api import job_store, MissingMatchRequest
    from .track_routes import ApplyRequest
    from pydantic import Field
    class StageRequest(MissingMatchRequest):
        candidate: dict = Field(default_factory=dict)

    @app.get('/api/matches/queue')
    def listing():
        rows = available(job_store().repository)
        return {'rows': rows, 'playlist_count': len({r['playlist_key'] for r in rows})}

    @app.post('/api/matches/queue')
    def stage(request: StageRequest):
        return stage_match(request)

    @app.post('/api/matches/queue/preview')
    def stage_preview(request: ApplyRequest):
        from . import track_routes
        from .api import _config
        with track_routes._lock:
            cached = copy.deepcopy(track_routes._cache.get(request.preview_id))
        from .accounts import owner_key
        if not cached or cached.get('owner')!=owner_key() or cached['expires'] < time.monotonic():
            raise HTTPException(409, 'The preview expired. Make a new preview.')
        config = _config(read_only=True,namespaces=['health','source_snapshots','mapping','match_metadata','missing','ignored_tracks','artist_aliases'])
        rows = [m for m in track_routes.inspect(config, cached['track'])['memberships'] if m['key'] in cached['keys']]
        if track_routes.fingerprint(config, rows) != cached['fingerprint']:
            raise HTTPException(409, 'The track changed after preview. Make a new preview.')
        return stage_changes(job_store().repository, cached['public']['changes'])

    @app.delete('/api/matches/queue/{key}')
    def discard(key: str):
        repo = job_store().repository
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if key not in {r['id'] for r in available(repo, db)}:
                raise HTTPException(409, 'This edit is already being applied. Check Activity.')
            db.execute("DELETE FROM state WHERE namespace='match_drafts' AND key=?", (key,))
        return {'discarded': True}

    @app.post('/api/matches/queue/apply', status_code=202)
    def apply():
        store = job_store()
        with store.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = available(store.repository, db)
            if not rows:
                raise HTTPException(409, 'No pending match edits to apply.')
            job_id = store.enqueue('match_batch', {'changes': rows}, db=db)
            for row in rows:
                db.execute("UPDATE state SET value=? WHERE namespace='match_drafts' AND key=?", (json.dumps({**row, 'job_id': job_id}), row['id']))
        return store.get(job_id)
