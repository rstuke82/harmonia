"""Small, transactional request updates from Lidarr status webhooks; no service I/O."""
import json
import time
from .diagnostics import redact
from .lidarr_requests import server_id


def update(repo, payload, kind):
    from .lidarr import config
    cfg = config(repo)
    if not cfg.get('enabled'):
        return {'updated': 0, 'message': 'Lidarr integration is disabled.'}
    server = server_id(cfg)
    albums = payload.get('albums') or []
    if not isinstance(albums, list):
        albums = []
    if isinstance(payload.get('album'), dict):
        albums = albums + [payload['album']]
    ids = {str(a['id']) for a in albums if isinstance(a, dict) and a.get('id') is not None}
    mbids = {str(a.get('mbId') or a.get('mbid') or a.get('foreignAlbumId')) for a in albums if isinstance(a, dict) and (a.get('mbId') or a.get('mbid') or a.get('foreignAlbumId'))}
    download_id = str(payload.get('downloadId') or '')
    failure = kind != 'grab'
    status = 'Download failed' if kind.startswith('download') else 'Import failed' if failure else 'Download grabbed'
    message = str(payload.get('message') or payload.get('error') or status)[:4000]
    message = redact(message.replace(str(cfg.get('api_key') or '\x00'), '[REDACTED]'))
    changed = 0
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        records = db.execute("SELECT key,value FROM state WHERE namespace='lidarr_requests'").fetchall()
        # Never guess by artist/title or a shared artist ID.
        for key, encoded in records:
            row = json.loads(encoded)
            if row.get('server') != server or row.get('removed_from_lidarr'):
                continue
            identified = str(row.get('lidarr_id')) in ids or key in mbids or str(row.get('lidarr_album_mbid') or '') in mbids
            if not identified and not ids and not mbids and download_id:
                identified = download_id == row.get('webhook_download_id') or any(str(d.get('download_id') or d.get('downloadId') or '') == download_id for d in row.get('downloads', []))
            if not identified:
                continue
            # Old deliveries cannot downgrade a completed import or available album.
            if row.get('plex_available') or row.get('download_status') == 'Imported into Lidarr' or row.get('finalize_status') == 'completed':
                continue
            retired = row.get('webhook_retired_download_ids', [])
            if download_id and download_id in retired:
                continue
            if not failure and row.get('webhook_failure'):
                # A Grab has no reliable ordering information. Only current queue
                # evidence or a successful import can recover a failed attempt.
                continue
            if not failure and download_id and download_id == row.get('webhook_download_id'):
                continue
            previous_id = row.get('webhook_download_id')
            if not failure and previous_id and download_id and download_id != previous_id:
                row['webhook_retired_download_ids'] = list(dict.fromkeys([*retired, previous_id]))
            if failure and download_id and row.get('webhook_download_id') and download_id != row['webhook_download_id']:
                continue
            if failure and row.get('webhook_failure') == {'kind': kind, 'download_id': download_id, 'message': message}:
                continue
            row.update(download_status=status, download_error=message if failure else '',
                       webhook_download_id=download_id or row.get('webhook_download_id', ''),
                       webhook_failure={'kind': kind, 'download_id': download_id, 'message': message} if failure else None,
                       webhook_checked_at=time.time(), download_checked_at=time.time(), updated_at=time.time())
            if not failure:
                row.update(error='', downloads=[])
            db.execute("UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?", (json.dumps(row), key))
            changed += 1
    return {'updated': changed, 'message': f'Updated {changed} affected requests; no scan or activity job started.'}


def preserve_failure(record, matches, imported, status, errors):
    """A quiet queue is not proof that a failed import recovered."""
    failure = record.get('webhook_failure')
    if failure and not imported:
        old_id = failure.get('download_id')
        retry = any((d.get('download_id') and d['download_id'] != old_id) and d['download_id'] not in record.get('webhook_retired_download_ids', []) and not d.get('error') for d in matches)
        if not retry:
            return record.get('download_status') or status, errors or failure.get('message') or 'Lidarr reported a failure', failure
    if not matches and not imported and not failure and record.get('download_status') == 'Download grabbed':
        status = 'Download grabbed'
    return status, errors, None


def retry_identity_changes(record, matches):
    """Advance attempt identity only from a current, non-retired queue item."""
    retired = record.get('webhook_retired_download_ids', [])
    retry_id = next((d.get('download_id') for d in matches
                     if d.get('download_id') and not d.get('error')
                     and d['download_id'] not in retired), None)
    old_id = record.get('webhook_download_id')
    if not retry_id or retry_id == old_id:
        return {}
    return {'webhook_download_id': retry_id,
            'webhook_retired_download_ids': list(dict.fromkeys([*retired, *([old_id] if old_id else [])]))}
