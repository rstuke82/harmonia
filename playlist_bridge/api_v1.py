"""Stable API-key endpoints for automations, Shortcuts, and Lidarr webhooks."""
import hashlib
import json
import re
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field


STATUS_EVENTS = {'grab','downloadfailed','downloadfailure','importfailed','importfailure'}
CATALOG_EVENTS = {'rename','retag','trackretag','artistadd','artistdelete','albumdelete'}
CATALOG_BATCH_NAMESPACE = 'lidarr_webhook_batches'
CATALOG_BATCH_KEY = 'catalog'
CATALOG_QUIET_SECONDS = 30


def _admin():
    from .accounts import actor
    user=actor()
    if not user or not user.get('admin'):raise HTTPException(403,'Administrator API key required.')
    return user


def _event_type(payload):
    value=payload.get('eventType') or payload.get('event_type') or ''
    return re.sub(r'[^a-z]','',str(value).casefold())


def _summary(payload):
    artist=payload.get('artist') if isinstance(payload.get('artist'),dict) else {}
    album=payload.get('album') if isinstance(payload.get('album'),dict) else {}
    return {
        'event_type':str(payload.get('eventType') or payload.get('event_type') or 'Unknown')[:80],
        'instance_name':str(payload.get('instanceName') or '')[:200],
        'artist_id':artist.get('id'),'artist':str(artist.get('name') or '')[:300],
        'album_id':album.get('id'),'album_mbid':str(album.get('mbId') or album.get('mbid') or '')[:100],
        'album':str(album.get('title') or '')[:300],
        'download_id':str(payload.get('downloadId') or '')[:300],
        'is_upgrade':bool(payload.get('isUpgrade',False)),
    }


def _record_event(repo, kind, detail, job_id=''):
    event={'id':str(uuid.uuid4()),'received_at':time.time(),'kind':kind,'job_id':job_id,**detail}
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO state(namespace,key,value) VALUES('api_events',?,?)",(event['id'],json.dumps(event)))
        rows=db.execute("SELECT key FROM state WHERE namespace='api_events' ORDER BY rowid DESC LIMIT -1 OFFSET 100").fetchall()
        if rows:db.executemany("DELETE FROM state WHERE namespace='api_events' AND key=?",rows)
    repo.add_log('INFO','API event',f"{kind}: {detail.get('event_type') or detail.get('url_host') or 'received'}",
                 job_id=job_id or None,service='Lidarr' if kind=='lidarr' else None,
                 entity_type='webhook' if kind=='lidarr' else 'api',details={'kind':kind,**detail})
    return event


def _catalog_batch(value=None):
    batch=dict(value or {})
    batch.setdefault('generation',0);batch.setdefault('processed_generation',0)
    batch.setdefault('count',0);batch.setdefault('event_counts',{})
    batch.setdefault('processed_event_counts',{})
    batch.setdefault('artist_ids',[]);batch.setdefault('album_ids',[])
    if 'targets' not in batch:
        batch['targets']={}
        if batch['generation']>batch['processed_generation']:
            for scope,ids in [('artist',batch['artist_ids'])] if batch['artist_ids'] else [('album',batch['album_ids'])]:
                for ident in ids:batch['targets'][f'{scope}:{ident}']={'scope':scope,'id':ident,'kind':'refresh','generation':batch['generation']}
            if not batch['targets']:batch['targets']['unknown']={'scope':'unknown','id':None,'kind':'refresh','generation':batch['generation']}
    return batch


def _add_to_catalog_batch(batch, kind, detail, received_at=None):
    received_at=received_at or time.time()
    batch=_catalog_batch(batch);batch['generation']+=1;batch['count']+=1
    batch['event_counts'][kind]=batch['event_counts'].get(kind,0)+1
    for field,target in (('artist_id','artist_ids'),('album_id','album_ids')):
        value=detail.get(field)
        if value is not None and value not in batch[target]:batch[target]=(batch[target]+[value])[-500:]
    scope='album' if kind=='albumdelete' or (detail.get('album_id') is not None and kind not in ('artistadd','artistdelete')) else 'artist'
    ident=detail.get(scope+'_id')
    key=f'{scope}:{ident}' if ident is not None else 'unknown'
    batch['targets'][key]={'scope':scope if ident is not None else 'unknown','id':ident,'kind':kind,'generation':batch['generation']}
    batch['first_received']=batch.get('first_received') or received_at
    batch['last_received']=received_at
    return batch


def _active_catalog_job(db):
    row=db.execute("""SELECT id FROM jobs WHERE action='lidarr_webhook'
                      AND status='queued'
                      AND json_extract(payload,'$.event')='catalog_batch'
                      ORDER BY created_at LIMIT 1""").fetchone()
    return row[0] if row else None


def _enqueue_catalog_batch(repo, db, batch, user_id='', follow_up=False):
    from .jobs import Store
    existing=_active_catalog_job(db)
    if existing:return existing,False
    payload={'event':'catalog_batch','generation':batch['generation'],'follow_up':bool(follow_up),
             'trigger_label':'Lidarr webhook batch','requested_by':str(user_id or '')}
    return Store(repo).enqueue('lidarr_webhook',payload,db=db),True


def catalog_batch_wait_seconds(repo, payload):
    """Keep a catalog batch queued until Lidarr has been quiet briefly."""
    if payload.get('event')!='catalog_batch':return 0
    batch=repo.load_one(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY,{})
    return max(0,float(batch.get('last_received') or 0)+CATALOG_QUIET_SECONDS-time.time())


def coalesce_queued_lidarr_webhooks(repo):
    """Collapse a pre-upgrade webhook backlog without losing download finalizers."""
    from .jobs import Store,now,worker_for
    store=Store(repo);created=False;job_id=None;collapsed=[]
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT value FROM state WHERE namespace=? AND key=?",(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY)).fetchone()
        batch=_catalog_batch(json.loads(row[0]) if row else None)
        jobs=db.execute("SELECT id,payload FROM jobs WHERE action='lidarr_webhook' AND status IN ('queued','running','cancelling')").fetchall()
        for old_id,encoded in jobs:
            try:old_payload=json.loads(encoded);kind=old_payload.get('event','')
            except (TypeError,ValueError):continue
            if kind not in CATALOG_EVENTS:continue
            batch=_add_to_catalog_batch(batch,kind,old_payload.get('detail') or {})
            collapsed.append(old_id)
        if collapsed:
            stamp=now();message='Coalesced into one Lidarr catalog refresh after upgrade'
            db.executemany("UPDATE jobs SET status='cancelled',progress=?,finished_at=?,cancel_requested=1 WHERE id=?",[(message,stamp,jid) for jid in collapsed])
            db.executemany('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',[(jid,stamp,'lifecycle',message) for jid in collapsed])
            db.executemany('''INSERT INTO diagnostic_events(created_at,level,operation,message,worker,job_id)
                              VALUES(?,?,?,?,?,?)''',[(stamp,'INFO','lidarr_webhook',message,worker_for('lidarr_webhook'),jid) for jid in collapsed])
            db.execute("INSERT OR REPLACE INTO state(namespace,key,value) VALUES(?,?,?)",(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY,json.dumps(batch)))
            job_id,created=_enqueue_catalog_batch(repo,db,batch,follow_up=False)
        cutoff=time.time()-86400
        for key,value in db.execute("SELECT key,value FROM state WHERE namespace='lidarr_webhook_receipts'").fetchall():
            try:expired=json.loads(value).get('received_at',0)<cutoff
            except (TypeError,ValueError):expired=True
            if expired:db.execute("DELETE FROM state WHERE namespace='lidarr_webhook_receipts' AND key=?",(key,))
    if created:store.event(job_id,f'Combined {len(collapsed)} queued Lidarr catalog events into one refresh')
    return {'collapsed':len(collapsed),'job_id':job_id}


def _queue_lidarr_event(repo, payload, user_id):
    from .jobs import Store
    detail=_summary(payload);kind=_event_type(payload)
    if kind in STATUS_EVENTS:
        from .lidarr_request_events import update
        result=update(repo,payload,kind)
        event=_record_event(repo,'lidarr',detail)
        return {'accepted':True,'event':detail['event_type'],'job_id':None,'event_id':event['id'],**result}
    if kind=='test':
        event=_record_event(repo,'lidarr',detail)
        return {'accepted':True,'event':'Test','job_id':None,'message':'Harmonia received the Lidarr test webhook.','event_id':event['id']}
    actionable={'download','albumdownload',*CATALOG_EVENTS}
    if kind not in actionable:
        event=_record_event(repo,'lidarr',detail)
        return {'accepted':True,'event':detail['event_type'],'job_id':None,'message':'Event recorded; no library scan is required.','event_id':event['id']}
    fingerprint=hashlib.sha256(json.dumps([kind,detail['album_id'],detail['album_mbid'],detail['artist_id'],detail['download_id'],detail['is_upgrade']],separators=(',',':')).encode()).hexdigest()
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        saved=db.execute("SELECT value FROM state WHERE namespace='lidarr_webhook_receipts' AND key=?",(fingerprint,)).fetchone()
        if saved:
            receipt=json.loads(saved[0])
            active=db.execute("SELECT status FROM jobs WHERE id=? AND status IN ('queued','running','cancelling')",(receipt.get('job_id'),)).fetchone()
            if active and (kind not in CATALOG_EVENTS or active[0]=='queued') and receipt.get('received_at',0)>time.time()-600:
                return {'accepted':True,'duplicate':True,'event':detail['event_type'],'job_id':receipt.get('job_id'),'message':'Duplicate Lidarr delivery already accepted.'}
        if kind in CATALOG_EVENTS:
            row=db.execute("SELECT value FROM state WHERE namespace=? AND key=?",(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY)).fetchone()
            batch=_add_to_catalog_batch(json.loads(row[0]) if row else None,kind,detail)
            db.execute("INSERT OR REPLACE INTO state(namespace,key,value) VALUES(?,?,?)",(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY,json.dumps(batch)))
            job_id,created=_enqueue_catalog_batch(repo,db,batch,user_id)
        else:
            job_payload={'event':kind,'detail':detail,'trigger_label':'Lidarr webhook','requested_by':str(user_id)}
            job_id=Store(repo).enqueue('lidarr_webhook',job_payload,db=db);created=True
        db.execute("INSERT OR REPLACE INTO state VALUES('lidarr_webhook_receipts',?,?)",(fingerprint,json.dumps({'received_at':time.time(),'job_id':job_id})))
    if created:Store(repo).event(job_id,f"Queued by Lidarr webhook · {detail['event_type']}")
    event=_record_event(repo,'lidarr',detail,job_id)
    return {'accepted':True,'duplicate':False,'event':detail['event_type'],'job_id':job_id,'message':'Lidarr event queued for processing.','event_id':event['id']}


def execute_lidarr_webhook(payload):
    """Process a trusted, reduced webhook envelope on the library worker."""
    from .accounts import root_repository
    from .lidarr_catalog import refresh
    from .lidarr import Client,config as lidarr_config
    from .lidarr_requests import server_id
    from . import jobs
    detail=payload.get('detail',{});event=payload.get('event','')
    if event=='catalog_batch':
        repo=root_repository();batch=_catalog_batch(repo.load_one(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY,{}))
        generation=int(batch.get('generation') or 0);processed=int(batch.get('processed_generation') or 0)
        if generation<=processed:
            return {'summary':'No newer Lidarr catalog events to process.'}
        count=generation-processed
        processed_counts=batch.get('processed_event_counts') or {}
        current_counts=batch.get('event_counts') or {}
        pending_counts={name:value-int(processed_counts.get(name) or 0) for name,value in current_counts.items() if value-int(processed_counts.get(name) or 0)>0}
        kinds=', '.join(f'{name}: {value}' for name,value in sorted(pending_counts.items()))
        jobs.progress(f'Processing {count:,} coalesced Lidarr catalog events')
        jobs.output('Lidarr event types: '+(kinds or 'catalog changes'))
        targets=sorted(batch['targets'].items(),key=lambda item:item[1]['generation'])[:10]
        result=refresh(repo,[value for key,value in targets])
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM state WHERE namespace=? AND key=?",(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY)).fetchone()
            latest=_catalog_batch(json.loads(row[0]) if row else None)
            for key,value in targets:
                if latest['targets'].get(key,{}).get('generation')==value['generation']:latest['targets'].pop(key,None)
            latest['processed_generation']=min((t['generation'] for t in latest['targets'].values()),default=latest['generation']+1)-1
            if not latest['targets']:latest['processed_event_counts']=dict(latest['event_counts'])
            latest['artist_ids']=[t['id'] for t in latest['targets'].values() if t['scope']=='artist']
            latest['album_ids']=[t['id'] for t in latest['targets'].values() if t['scope']=='album']
            db.execute("INSERT OR REPLACE INTO state(namespace,key,value) VALUES(?,?,?)",(CATALOG_BATCH_NAMESPACE,CATALOG_BATCH_KEY,json.dumps(latest)))
            follow_id,created=_enqueue_catalog_batch(repo,db,latest,payload.get('requested_by',''),True) if latest['targets'] else (None,False)
        if created:jobs.Store(repo).event(follow_id,'Queued remaining affected Lidarr targets')
        return {'summary':f"Processed {len(targets)} affected Lidarr targets from coalesced catalog events. "+result['summary'],'refresh':result,'partial_success':result.get('partial_success',False),'follow_up_job_id':follow_id}
    jobs.progress(f"Processing Lidarr webhook: {detail.get('event_type') or event}")
    if event in ('download','albumdownload'):
        cfg=lidarr_config(root_repository())
        if not cfg.get('enabled'):raise ValueError('Lidarr integration is disabled in Harmonia.')
        lidarr_id=detail.get('album_id')
        if not lidarr_id:raise ValueError('Lidarr import webhook did not include an album ID.')
        album=Client(cfg).call('GET',f'album/{int(lidarr_id)}')
        mbid=str(album.get('foreignAlbumId') or detail.get('album_mbid') or '')
        if not mbid:raise ValueError('Imported Lidarr album has no MusicBrainz release-group ID.')
        from .lidarr_requests import save
        for key,record in root_repository().load('lidarr_requests').items():
            if record.get('server')==server_id(cfg) and (str(record.get('lidarr_id'))==str(lidarr_id) or key==mbid):
                save(root_repository(),key,download_status='Imported into Lidarr',download_error='',webhook_failure=None,webhook_imported_at=time.time())
        from .album_finalize import execute
        return execute({'album_id':mbid,'lidarr_id':int(lidarr_id),'server':server_id(cfg),'trigger_label':'Lidarr webhook'})
    if event in CATALOG_EVENTS:
        scope='album' if event=='albumdelete' or (detail.get('album_id') is not None and event not in ('artistadd','artistdelete')) else 'artist'
        return refresh(root_repository(),[{'scope':scope,'id':detail.get(scope+'_id'),'kind':event}])
    return {'summary':'Event recorded; no catalog refresh required.'}


class ShareRequest(BaseModel):
    url:str=Field(min_length=1,max_length=2000)
    name:str=Field(default='',max_length=200)
    sync_mode:Literal['inherit','disabled']='inherit'


class EmptyRequest(BaseModel):
    pass


def register(app):
    from .accounts import actor,api_key_status

    @app.get('/api/v1/status')
    def status():
        from . import __version__,__build__
        user=actor()
        return {'status':'ok','version':__version__,'build':__build__,'account':{'id':user['id'],'name':user.get('name'),'admin':bool(user.get('admin'))},'api_key':api_key_status(user)}

    @app.post('/api/v1/share',status_code=202)
    @app.post('/api/v1/playlists',status_code=202)
    def share(body:ShareRequest):
        from .api import validated_payload,job_store
        user=actor()
        if user.get('can_playlists',True) is False:raise HTTPException(403,'Playlist creation and sync are disabled for this account.')
        try:data=validated_payload('add',{**body.model_dump(),'trigger_label':'iOS Shortcut / API'})
        except HTTPException:raise
        except (ValueError,TypeError) as exc:raise HTTPException(422,str(exc)) from exc
        store=job_store();job_id=store.enqueue('add',data)
        store.event(job_id,'Queued from a shared Apple Music or Spotify URL using the API')
        _record_event(store.repository,'share',{'user_id':str(user['id']),'url_host':urlsplit(body.url).hostname or ''},job_id)
        return {'accepted':True,'job_id':job_id,'status':'queued','message':'Playlist URL accepted. Follow the job in Activity.','job_url':f'/api/v1/jobs/{job_id}'}

    @app.get('/api/v1/jobs/{job_id}')
    def api_job(job_id:str):
        from .api import job_store
        row=job_store().get(job_id)
        if not row:raise HTTPException(404,'Job not found for this account.')
        return row

    @app.post('/api/v1/library/{service}/scan',status_code=202)
    def scan_library(service:Literal['lidarr','plex']):
        _admin()
        from .api import job_store
        store=job_store();job_id=store.enqueue(service+'_scan',{'trigger_label':'API request'})
        store.event(job_id,f'Queued by API · Scan {service.title()} Library')
        return {'accepted':True,'job_id':job_id,'status':'queued'}

    @app.post('/api/v1/library/reconcile',status_code=202)
    def reconcile_library():
        _admin()
        from .api import job_store
        store=job_store();job_id=store.enqueue('reconcile',{'trigger_label':'API request'})
        store.event(job_id,'Queued by API · Reconcile Availability')
        return {'accepted':True,'job_id':job_id,'status':'queued'}

    @app.post('/api/v1/webhooks/lidarr',status_code=202)
    async def lidarr_webhook(request:Request):
        user=_admin()
        try:length=int(request.headers.get('content-length') or 0)
        except ValueError as exc:raise HTTPException(400,'Invalid Content-Length header.') from exc
        if length<0:raise HTTPException(400,'Invalid Content-Length header.')
        if length>1024*1024:raise HTTPException(413,'Webhook payload is too large.')
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>1024*1024:raise HTTPException(413,'Webhook payload is too large.')
            body.extend(chunk)
        try:payload=json.loads(body)
        except Exception as exc:raise HTTPException(400,'Lidarr webhook must contain JSON.') from exc
        if not isinstance(payload,dict):raise HTTPException(422,'Lidarr webhook must contain a JSON object.')
        for field in ('artist','album'):
            if payload.get(field) is not None and not isinstance(payload[field],dict):raise HTTPException(422,f'{field} must be an object.')
            value=payload.get(field) or {}
            identifier=value.get('id')
            if identifier is not None and (isinstance(identifier,bool) or not isinstance(identifier,int) or identifier<0 or identifier>2**63-1):raise HTTPException(422,f'{field}.id must be a nonnegative integer.')
        from .accounts import root_repository
        return _queue_lidarr_event(root_repository(),payload,user['id'])

    @app.get('/api/v1/events')
    def events():
        _admin()
        from .accounts import root_repository
        return sorted(root_repository().load('api_events').values(),key=lambda row:row.get('received_at',0),reverse=True)[:100]
