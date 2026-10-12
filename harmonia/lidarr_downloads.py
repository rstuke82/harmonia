"""Read-only download snapshots and explicit, identity-checked queue actions."""
import threading
import time
import json
import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .lidarr_requests import server_id, save

_lock = threading.Lock()
_last = {}
_catalog = {}
FINALIZE_BATCH_NAMESPACE='album_finalize_batches'
FINALIZE_BATCH_KEY='imports'
FINALIZE_QUIET_SECONDS=20
PLEX_CHECK_MAX_ATTEMPTS=6
PLEX_CHECK_WINDOW=20*60
PLEX_CHECK_STOPPED='Plex did not confirm all imported files within the automatic check window. Automatic checks stopped. Review Plex/path mappings, then use Refresh This Album to retry.'


def plex_checks_exhausted(record):
    started=float(record.get('plex_check_started_at') or record.get('webhook_imported_at') or 0)
    return (not started or time.time()-started>=PLEX_CHECK_WINDOW or
            int(record.get('plex_check_attempts') or 0)>=PLEX_CHECK_MAX_ATTEMPTS)


def _message_summary(messages):
    detail='; '.join(messages)
    lower=detail.casefold()
    if 'has fewer tracks than existing release' in lower:
        rejected=sum(1 for message in messages if 'has fewer tracks than existing release' in message.casefold())
        count=f' ({rejected} track files rejected)' if rejected else ''
        return (f'Lidarr rejected this import because the downloaded release has fewer tracks than the existing release{count}. '
                'Review the selected release or import decision in Lidarr.', detail)
    if 'one or more tracks expected in this release were not imported' in lower:
        return ('Lidarr imported only part of this release. Review the missing tracks and import decision in Lidarr.', detail)
    return detail, ''

def _finalize_batch(value=None):
    batch=dict(value or {});batch.setdefault('generation',0);batch.setdefault('processed_generation',0)
    batch.setdefault('albums',{});batch.setdefault('count',0)
    return batch

def _active_finalize_batch(db):
    row=db.execute("""SELECT id FROM jobs WHERE action='album_finalize' AND status IN ('queued','running','cancelling')
                      AND COALESCE(json_extract(payload,'$.batch'),0)=1 ORDER BY created_at LIMIT 1""").fetchone()
    return row[0] if row else None

def _add_finalize(batch,album_id,record,received_at=None):
    batch=_finalize_batch(batch);batch['generation']+=1;batch['count']+=1
    batch['albums'][str(album_id)]={'album_id':str(album_id),'lidarr_id':record['lidarr_id'],'server':record['server'],
        'generation':batch['generation']}
    batch['last_received']=received_at or time.time()
    return batch

def finalize_batch_wait_seconds(repo,payload):
    if not payload.get('batch'):return 0
    batch=repo.load_one(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY,{})
    return max(0,float(batch.get('last_received') or 0)+FINALIZE_QUIET_SECONDS-time.time())

def coalesce_queued_album_finalizers(repo):
    """Replace a pre-upgrade per-album backlog with one resumable batch."""
    from .jobs import Store,now,worker_for
    collapsed=[];records=[];job_id=None
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT value FROM state WHERE namespace=? AND key=?',(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY)).fetchone()
        batch=_finalize_batch(json.loads(row[0]) if row else None)
        # At startup, running/cancelling rows belong to the previous process and
        # are safe to fold back into the durable batch before they are marked
        # interrupted by the job manager.
        for old_id,encoded in db.execute("SELECT id,payload FROM jobs WHERE action='album_finalize' AND status IN ('queued','running','cancelling')").fetchall():
            try:value=json.loads(encoded)
            except (TypeError,ValueError):continue
            if value.get('batch') or value.get('plex_recheck') or not value.get('album_id') or not value.get('lidarr_id'):continue
            batch=_add_finalize(batch,value['album_id'],value);collapsed.append(old_id);records.append(str(value['album_id']))
        if not collapsed:return {'collapsed':0,'job_id':_active_finalize_batch(db)}
        stamp=now();message='Coalesced into one imported-album finalization batch after upgrade'
        db.executemany("UPDATE jobs SET status='cancelled',progress=?,finished_at=?,cancel_requested=1 WHERE id=?",[(message,stamp,jid) for jid in collapsed])
        db.executemany('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',[(jid,stamp,'lifecycle',message) for jid in collapsed])
        db.executemany('''INSERT INTO diagnostic_events(created_at,level,operation,message,worker,job_id)
                          VALUES(?,?,?,?,?,?)''',[(stamp,'INFO','album_finalize',message,worker_for('album_finalize'),jid) for jid in collapsed])
        db.execute('INSERT OR REPLACE INTO state(namespace,key,value) VALUES(?,?,?)',(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY,json.dumps(batch)))
        job_id=_active_finalize_batch(db)
        if not job_id:
            job_id=Store(repo).enqueue('album_finalize',{'batch':True,'generation':batch['generation'],'batch_token':str(uuid.uuid4()),
                'trigger_label':'Lidarr imported albums'},db=db)
    for album_id in records:save(repo,album_id,finalize_job_id=job_id,finalize_status='queued')
    Store(repo).event(job_id,f'Combined {len(collapsed)} queued album finalizers into one batch')
    return {'collapsed':len(collapsed),'job_id':job_id}

def queue_finalize(repo, album_id, record, force=False):
    """Queue one idempotent album-scoped inventory refresh after import."""
    from .jobs import Store
    signature=f"{record.get('lidarr_id')}:{record.get('track_file_count',0)}"
    if not force and record.get('finalize_signature')==signature and record.get('finalize_status') in ('queued','running','completed','awaiting_plex'):
        return record.get('finalize_job_id')
    payload={'album_id':album_id,'lidarr_id':record['lidarr_id'],'server':record['server']}
    if force:
        jid=Store(repo).enqueue('album_finalize',{**payload,'trigger_label':'Manual album refresh'})
    else:
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM state WHERE namespace=? AND key=?',(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY)).fetchone()
            batch=_add_finalize(json.loads(row[0]) if row else None,album_id,record)
            db.execute('INSERT OR REPLACE INTO state(namespace,key,value) VALUES(?,?,?)',(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY,json.dumps(batch)))
            jid=_active_finalize_batch(db)
            if not jid:
                jid=Store(repo).enqueue('album_finalize',{'batch':True,'generation':batch['generation'],'batch_token':str(uuid.uuid4()),
                    'trigger_label':'Lidarr imported albums'},db=db)
    save(repo,album_id,finalize_job_id=jid,finalize_status='queued',finalize_signature=signature,finalize_error='')
    return jid

def queue(client):
    rows = []
    for page in range(1, 101):
        result = client.call('GET', 'queue', params={'page':page, 'pageSize':250, 'includeAlbum':'true'}, quiet=True)
        batch = result.get('records', [])
        rows.extend(batch)
        if len(rows) >= result.get('totalRecords',len(rows)) or not batch:
            return rows
    raise HTTPException(502, 'Lidarr queue exceeded the supported size; no queue action was taken.')

def describe(item):
    status = str(item.get('status','')).lower()
    state = str(item.get('trackedDownloadState','')).lower()
    severity = str(item.get('trackedDownloadStatus','')).lower()
    messages = [str(m) for group in item.get('statusMessages',[]) for m in ([group.get('title','')]+group.get('messages',[])) if m]
    if state in ('importfailed','failedpending') or severity in ('warning','error'):
        label = 'Import blocked' if status=='completed' or 'import' in state else 'Blocked'
    elif status in ('failed','warning'):
        label = 'Failed'
    elif state=='imported':
        label = 'Imported into Lidarr'
    elif status=='completed' or state in ('importpending','importing'):
        label = 'Importing'
    elif status=='downloading':
        label = 'Downloading'
    else:
        label = status.capitalize() or 'Waiting for download'
    size, left = item.get('size'), item.get('sizeleft')
    percent = round(max(0,min(100,(1-left/size)*100)),1) if isinstance(size,(int,float)) and size>0 and isinstance(left,(int,float)) else None
    error,error_detail=_message_summary(messages)
    return {'queue_id':item['id'],'download_id':item.get('downloadId',''),'title':item.get('title',''), 'status':label,'percent':percent,'remaining':left,'error':error,'error_detail':error_detail}

def snapshot(repo, cfg, force=False):
    from .lidarr import Client
    from .api import _record_log
    server = server_id(cfg)
    records = {k:v for k,v in repo.load('lidarr_requests').items() if v.get('server')==server and (v.get('lidarr_id') or v.get('status')=='not_in_lidarr')}
    if not cfg.get('enabled') or not records or (not force and time.monotonic()-_last.get(server,0)<15) or not _lock.acquire(False):
        return
    try:
        _last[server]=time.monotonic()
        client=Client(cfg)
        items=queue(client)
        # One catalog read supplies imported-file counts for all requested albums.
        cached=_catalog.get(server)
        if force or not cached or time.monotonic()-cached[0]>60:
            cached=(time.monotonic(),{a['id']:a for a in client.call('GET','album',quiet=True)})
            _catalog.clear()
            _catalog[server]=cached
        albums=cached[1]
        from .lidarr_requests import find_album
        for key,record in records.items():
            album=find_album(albums.values(),key,record)
            if album:
                actual_mbid=str(album.get('foreignAlbumId') or key)
                if record.get('lidarr_id')!=album.get('id') or record.get('removed_from_lidarr') or record.get('status')=='not_in_lidarr':
                    record={**record,'lidarr_id':album['id'],'status':'added','search_command_id':None,'removed_from_lidarr':False}
                    save(repo,key,_expected_webhook_at=record.get('webhook_checked_at'),lidarr_id=record['lidarr_id'],lidarr_album_mbid=actual_mbid,lidarr_url=cfg['url'].rstrip('/')+'/album/'+actual_mbid,status='added',search_command_id=None,removed_from_lidarr=False,error='')
            matches=[describe(q) for q in items if q.get('albumId')==(album.get('id') if album else record.get('lidarr_id'))]
            if not album:
                # The all-albums endpoint can lag immediately after an add.
                # Confirm the saved numeric ID directly before declaring the
                # durable request link broken; an active queue item is also
                # authoritative evidence that Lidarr still owns the album.
                direct=None
                if record.get('lidarr_id'):
                    try:direct=client.call('GET',f"album/{record['lidarr_id']}",quiet=True)
                    except HTTPException as exc:
                        if 'HTTP 404:' not in str(exc.detail):raise
                if direct and find_album([direct],key,record):
                    album=direct;albums[direct['id']]=direct
                elif matches or time.time()-float(record.get('updated_at') or 0)<180:
                    status=matches[0]['status'] if matches else 'Waiting for Lidarr inventory'
                    from .lidarr_request_events import preserve_failure, retry_identity_changes
                    status,errors,failure=preserve_failure(record,matches,False,status,'')
                    save(repo,key,_expected_webhook_at=record.get('webhook_checked_at'),downloads=matches,download_status=status,download_error=errors,webhook_failure=failure,**retry_identity_changes(record,matches),download_checked_at=time.time(),download_poll_error='',removed_from_lidarr=False)
                    continue
                else:
                    if record.get('webhook_failure'):
                        save(repo,key,_expected_webhook_at=record.get('webhook_checked_at'),download_checked_at=time.time(),download_poll_error='Album absent from Lidarr; retaining reported failure.')
                        continue
                    save(repo,key,_expected_webhook_at=record.get('webhook_checked_at'),lidarr_id=None,lidarr_url='',status='not_in_lidarr',removed_from_lidarr=True,search_command_id=None,downloads=[],download_status='Not found in Lidarr',download_error='',error='',download_checked_at=time.time(),download_poll_error='')
                    continue
            if record.get('status')=='search_pending' and record.get('search_command_id'):
                cmd=client.call('GET',f"command/{record['search_command_id']}",quiet=True)
                command_status=str(cmd.get('status','')).lower()
                if command_status in ('completed','failed','aborted','cancelled','orphaned'):
                    record['status']='search_completed' if command_status=='completed' else 'search_failed'
                    failure='' if command_status=='completed' else str(cmd.get('exception') or cmd.get('message') or command_status)
                    from .diagnostics import redact
                    failure=redact(failure.replace(client.key,'[REDACTED]'))
                    save(repo,key,_expected_webhook_at=record.get('webhook_checked_at'),status=record['status'],error=failure)
                    _record_log('INFO' if not failure else 'ERROR','Lidarr search',f"{record.get('title','')}: command {record['search_command_id']} {command_status} {failure}")
            album=album or albums.get(record['lidarr_id'])
            stats=(album or {}).get('statistics') or {}
            imported=stats.get('trackCount',0)>0 and stats.get('trackFileCount',0)>=stats['trackCount']
            status=matches[0]['status'] if matches else 'Imported into Lidarr' if imported else 'Not found in Lidarr' if album is None else 'Searching' if record.get('status')=='search_pending' else 'Search failed' if record.get('status')=='search_failed' else 'Cancelled' if record.get('download_status')=='Cancelled' else 'Waiting for download'
            if status!=record.get('download_status'):
                _record_log('INFO','Lidarr download',f"{record.get('artist','')} — {record.get('title','')}: {status}")
            from .diagnostics import redact
            for q in matches:
                q['error']=redact(q['error'].replace(client.key,'[REDACTED]'))
                q['error_detail']=redact(q.get('error_detail','').replace(client.key,'[REDACTED]'))
            errors='; '.join(q['error'] for q in matches if q['error'])
            from .lidarr_request_events import preserve_failure, retry_identity_changes
            status,errors,webhook_failure=preserve_failure(record,matches,imported,status,errors)
            if errors and errors!=record.get('download_error'):
                _record_log('ERROR','Lidarr download',f"{record.get('title','')}: {errors}")
            track_files=stats.get('trackFileCount',0)
            actual_mbid=str((album or {}).get('foreignAlbumId') or key)
            changes={'webhook_failure':webhook_failure,'download_status':status,'downloads':matches,'download_error':errors,'download_checked_at':time.time(),'download_poll_error':'','lidarr_album_mbid':actual_mbid,'lidarr_url':cfg['url'].rstrip('/')+'/album/'+actual_mbid,'track_file_count':track_files,'removed_from_lidarr':False}
            changes.update(retry_identity_changes(record,matches))
            if status=='Imported into Lidarr' and not record.get('fulfilled_at'):changes['fulfilled_at']=time.time()
            if not save(repo,key,_expected_webhook_at=record.get('webhook_checked_at'),**changes):
                continue  # A newer webhook arrived while the service reads ran.
            if status=='Imported into Lidarr' and (record.get('download_status')!='Imported into Lidarr' or record.get('finalize_status') is None):
                queue_finalize(repo,key,{**record,'download_status':status,'track_file_count':track_files})
    except (HTTPException,ValueError,KeyError,TypeError) as exc:
        for key in records:
            save(repo,key,_expected_webhook_at=records[key].get('webhook_checked_at'),download_poll_error=str(getattr(exc,'detail',exc)))
    finally:
        _lock.release()

class Action(BaseModel):
    action: Literal['cancel','replace','search']
    queue_id: int | None = Field(default=None,ge=1)
    download_id: str = Field(default='',max_length=300)
    confirmed: bool = False

def perform(repo,cfg,album,request):
    from .lidarr import Client
    from .lidarr_requests import active
    from .api import _record_log
    if not _lock.acquire(False):
        raise HTTPException(409,'Lidarr status or another action is updating. Try again shortly.')
    try:
        record=repo.load('lidarr_requests').get(album)
        if not record or record.get('server')!=server_id(cfg) or not record.get('lidarr_id'):
            raise HTTPException(404,'Confirmed album request not found on this Lidarr server.')
        with repo.connect() as db:
            if active(db,record.get('job_id')):
                raise HTTPException(409,'This album already has an active job.')
        client=Client(cfg)
        try:
            actual=client.call('GET',f"album/{record['lidarr_id']}")
        except HTTPException as exc:
            if 'HTTP 404:' not in str(exc.detail):raise
            candidates=[a for a in client.call('GET','album',quiet=True) if a.get('foreignAlbumId')==album]
            if len(candidates)!=1:
                save(repo,album,lidarr_id=None,lidarr_url='',status='not_in_lidarr',search_command_id=None,downloads=[],download_status='Not found in Lidarr',error='',download_error='')
                raise HTTPException(409,'This album is no longer in Lidarr. Refresh and choose Add Album Again.') from None
            actual=candidates[0];record={**record,'lidarr_id':actual['id'],'search_command_id':None,'status':'added'}
            save(repo,album,lidarr_id=actual['id'],search_command_id=None,status='added',error='')

        if actual.get('foreignAlbumId')!=album:
            raise HTTPException(409,'Album identity changed. Refresh before acting.')
        items=queue(client)
        matching=[q for q in items if q.get('albumId')==record['lidarr_id']]
        if request.action=='search':
            if matching:
                raise HTTPException(409,'A download is already queued. Review it before searching again.')
            if record.get('status')=='search_unknown':
                raise HTTPException(409,'Previous search submission is unconfirmed. Inspect Lidarr first.')
            if record.get('search_command_id'):
                command=client.call('GET',f"command/{record['search_command_id']}")
                if str(command.get('status','')).lower() in ('queued','started','running'):
                    raise HTTPException(409,'A search is already running in Lidarr.')
            # Persist uncertainty before the write; never blindly repeat a timed-out submission.
            save(repo,album,status='search_unknown')
            command=client.call('POST','command',json={'name':'AlbumSearch','albumIds':[record['lidarr_id']]})
            save(repo,album,status='search_pending',search_command_id=command['id'],error='',download_status='Searching')
            message='Album search submitted'
        else:
            if not request.confirmed:
                raise HTTPException(422,'Confirm removal of this album download first.')
            target=next((q for q in matching if q['id']==request.queue_id and q.get('downloadId')==request.download_id and request.download_id),None)
            if not target:
                raise HTTPException(409,'The selected download changed or finished. Refresh before acting.')
            if any(q.get('downloadId')==request.download_id and q.get('albumId')!=record['lidarr_id'] for q in items):
                raise HTTPException(409,'This download contains other albums. Manage it directly in Lidarr.')
            if describe(target)['status']=='Imported into Lidarr':
                raise HTTPException(409,'This download is already imported. Manage it directly in Lidarr.')
            client.call('DELETE',f"queue/{request.queue_id}",params={'removeFromClient':'true','blocklist':str(request.action=='replace').lower(),'skipRedownload':str(request.action=='cancel').lower(),'changeCategory':'false'})
            message='Download removed and blocklisted; replacement requested from Lidarr' if request.action=='replace' else 'Download cancelled; no immediate replacement requested'
            save(repo,album,downloads=[],download_status='Waiting for download' if request.action=='replace' else 'Cancelled',download_error='')
        _record_log('INFO','Lidarr download',f"{record.get('title','')}: {message}")
        _last.pop(server_id(cfg),None)
        return {'message':message}
    finally:
        _lock.release()

def register(app):
    from .lidarr import repository, enabled
    @app.post('/api/lidarr/requests/{album}/download-action')
    def action(album:str, request:Action):
        return perform(repository(),enabled(repository()),album,request)
    @app.post('/api/lidarr/requests/{album}/finalize', status_code=202)
    def finalize(album:str):
        repo=repository();cfg=enabled(repo);record=repo.load('lidarr_requests').get(album)
        if not record or record.get('server')!=server_id(cfg) or not record.get('lidarr_id'):
            raise HTTPException(404,'Confirmed album request not found on this Lidarr server.')
        jid=queue_finalize(repo,album,record,force=True)
        from .jobs import Store
        return Store(repo).get(jid)


def queue_plex_rechecks(store):
    """Resume durable pending imports, including those saved by older builds."""
    from .lidarr import config
    repo=store.repository;cfg=config(repo)
    if not cfg.get('enabled') or not cfg.get('url'):return
    server=server_id(cfg);queued=0
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for album,encoded in db.execute("SELECT key,value FROM state WHERE namespace='lidarr_requests'").fetchall():
            record=json.loads(encoded)
            if (record.get('server')!=server or not record.get('lidarr_id') or
                record.get('finalize_status')!='awaiting_plex' or record.get('webhook_failure') or
                record.get('download_status')!='Imported into Lidarr'):continue
            if plex_checks_exhausted(record):
                record.update(finalize_status='failed',plex_check_after=0,finalize_error=PLEX_CHECK_STOPPED)
                db.execute("UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?",(json.dumps(record),album))
                continue
            if float(record.get('plex_check_after') or 0)>time.time():continue
            previous=db.execute("SELECT status,payload FROM jobs WHERE id=?",(record.get('finalize_job_id') or '',)).fetchone()
            if previous and previous[0]=='cancelled' and json.loads(previous[1]).get('plex_recheck'):continue
            busy=db.execute("""SELECT 1 FROM jobs WHERE action='album_finalize'
                AND status IN ('queued','running','cancelling')""").fetchone()
            if busy:continue
            jid=store.enqueue('album_finalize',{'album_id':album,'lidarr_id':record['lidarr_id'],
                'server':server,'plex_recheck':True,'expected_webhook_at':record.get('webhook_checked_at'),
                'check_token':str(uuid.uuid4()),'trigger_label':f"Plex import check: {record.get('artist','')} — {record.get('title',album)}"},db=db)
            record['finalize_job_id']=jid
            db.execute("UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?",(json.dumps(record),album))
            queued+=1
            if queued>=1:break
