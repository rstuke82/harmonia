"""Persistent album request tracking, shared across source playlists."""
import hashlib
import json
import time
from fastapi import HTTPException
from .saved_views import saved, REQUESTS


def server_id(cfg):
    return hashlib.sha256(cfg['url'].rstrip('/').encode()).hexdigest()


def save(repo, album, **changes):
    expected_event = changes.pop('_expected_webhook_at', ...)
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM state WHERE namespace='lidarr_requests' AND key=?", (album,)).fetchone()
        if not row:
            return
        value = json.loads(row[0])
        if expected_event is not ... and value.get('webhook_checked_at') != expected_event:
            return False
        meaningful=any(value.get(k)!=v for k,v in changes.items() if k!='download_checked_at')
        value.update(changes)
        if meaningful:value['updated_at']=time.time()
        db.execute("UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?", (json.dumps(value), album))
        return True


def active(db, job_id):
    row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id or '',)).fetchone()
    return bool(row and row[0] in ('queued','running','cancelling'))


def _album_identity(album):
    if not isinstance(album,dict):return '',''
    artist=album.get('artist') or ''
    artist_name=artist.get('artistName') if isinstance(artist,dict) else artist
    return str(album.get('title') or ''),str(artist_name or album.get('artistName') or '')


def find_album(albums, album_key, record):
    """Find a request's live Lidarr album without trusting one stale identifier."""
    from .matching import Matcher
    rows=[row for row in (albums or []) if isinstance(row,dict)]
    exact=[row for row in rows if str(row.get('foreignAlbumId') or row.get('album_id') or '')==str(album_key)]
    if len(exact)==1:return exact[0]
    title=Matcher._normalize_match_text(record.get('title',''))
    artist=Matcher._normalize_match_text(record.get('artist',''))
    def same(row):
        row_title,row_artist=_album_identity(row)
        return (not title or Matcher._normalize_match_text(row_title)==title) and (not artist or Matcher._normalize_match_text(row_artist)==artist)
    saved=[row for row in rows if record.get('lidarr_id') is not None and str(row.get('id'))==str(record.get('lidarr_id')) and same(row)]
    if len(saved)==1:return saved[0]
    named=[row for row in rows if title and same(row)]
    return named[0] if len(named)==1 else None


def repair_links(repo,cfg,album_key=''):
    """Re-associate durable requests with Lidarr by release-group MBID."""
    from .lidarr import Client
    server=server_id(cfg);records={k:v for k,v in repo.load('lidarr_requests').items() if v.get('server')==server and (not album_key or k==album_key)}
    albums=Client(cfg).call('GET','album',quiet=True)
    repaired=0;missing=0
    for key,record in records.items():
        found=find_album(albums,key,record)
        if found:
            changed=record.get('lidarr_id')!=found['id'] or record.get('removed_from_lidarr')
            actual_mbid=str(found.get('foreignAlbumId') or key)
            save(repo,key,lidarr_id=found['id'],lidarr_album_mbid=actual_mbid,lidarr_url=cfg['url'].rstrip('/')+'/album/'+actual_mbid,removed_from_lidarr=False,status='added' if record.get('status')=='not_in_lidarr' else record.get('status','added'),link_checked_at=time.time(),error='',download_status='' if record.get('download_status')=='Not found in Lidarr' else record.get('download_status',''))
            repaired+=int(changed)
        else:
            save(repo,key,lidarr_id=None,lidarr_url='',removed_from_lidarr=True,status='not_in_lidarr',link_checked_at=time.time(),download_status='Not found in Lidarr')
            missing+=1
    return {'checked':len(records),'repaired':repaired,'missing':missing,'message':f'Checked {len(records)} request links; repaired {repaired}; {missing} not present in Lidarr.'}


def register(app):
    from .lidarr import repository, enabled
    from .lidarr_downloads import register as register_downloads
    register_downloads(app)
    @app.get('/api/lidarr/requests')
    @saved('requests',REQUESTS)
    def list_requests(compute_availability=True):
        repo = repository()
        from .lidarr import config
        cfg=config(repo)
        server = server_id(cfg)
        rows = []
        from .accounts import users
        account_rows=users()
        account_names={str(k):v.get('name') or ('User '+str(k)) for k,v in account_rows.items()}
        request_history={}
        for value in repo.load('user_requests').values():
            request_history.setdefault(str(value.get('album_id')),[]).append({'user_id':str(value.get('user_id','')),'name':account_names.get(str(value.get('user_id','')),'User '+str(value.get('user_id',''))),'requested_at':value.get('at'),'status':value.get('status','requested')})
        with repo.connect() as db:
            for key,value in repo.load('lidarr_requests').items():
                if value.get('server') != server:
                    continue
                stored=value.get('requesters') or []
                if not stored and value.get('job_id'):
                    job=db.execute('SELECT payload,created_at FROM jobs WHERE id=?',(value['job_id'],)).fetchone()
                    payload=json.loads(job[0]) if job else {}
                    uid=payload.get('requester_id')
                    if not uid:
                        evidence=db.execute("SELECT DISTINCT user_id FROM diagnostic_events WHERE job_id=? AND user_id IS NOT NULL AND user_id!=''",(value['job_id'],)).fetchall()
                        if len(evidence)==1:uid=evidence[0][0]
                    if uid:
                        stored=[{'user_id':str(uid),'name':account_names.get(str(uid)) or payload.get('requester_name') or 'User '+str(uid),'requested_at':value.get('created_at')}]
                        value={**value,'requesters':stored}
                        db.execute("UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?",(json.dumps(value),key))
                merged=list(request_history.get(str(key),[]))
                for person in stored:
                    uid=str(person.get('user_id',''))
                    if not any(str(v.get('user_id'))==uid for v in merged):
                        merged.append({**person,'name':account_names.get(uid) or person.get('name') or 'User '+uid})
                merged=[{**person,'avatar':account_rows.get(str(person.get('user_id','')),{}).get('avatar','')} for person in merged]
                if value.get('status')=='search_pending' and value.get('error'):
                    value={**value,'search_wait_detail':value['error'],'error':''}
                finalize_active=active(db,value.get('finalize_job_id'))
                value = dict(value, key=key, active=active(db,value.get('job_id')) or finalize_active, finalize_active=finalize_active,requesters=merged)
                if not value['active'] and value['status'] in ('queued','adding'):
                    value['status'] = 'add_failed'
                rows.append(value)
        inventory_rows=repo.load('inventory').get('lidarr',{}).get('rows',[])
        for r in rows:
            found=find_album(inventory_rows,r['key'],r)
            if found:
                r.update(
                    library_album_id=str(found.get('album_id') or found.get('foreignAlbumId') or r.get('lidarr_album_mbid') or r['key']),
                    lidarr_album_mbid=str(found.get('album_id') or found.get('foreignAlbumId') or r.get('lidarr_album_mbid') or r['key']),
                    artwork=found.get('artwork') or found.get('cover') or r.get('artwork') or '',
                    year=found.get('year') or found.get('release_year'),
                )
            else:
                r['library_album_id']=str(r.get('lidarr_album_mbid') or r['key'])
        for r in rows:
            r['artwork']=r.get('artwork') or 'https://coverartarchive.org/release-group/'+r['key']+'/front-500'
        if not compute_availability:return rows
        from .discover import availability
        linked=availability([{'artist':r.get('artist',''),'album':r.get('title',''),'mbid':r.get('library_album_id') or r['key'],'request_key':r['key']} for r in rows],apply_preferences=False,deduplicate=False)
        by_id={r.get('request_key') or r['mbid']:r for r in linked}
        for r in rows:
            info=by_id.get(r['key'],{})
            from .request_confirmation import apply as confirmed_availability
            info=confirmed_availability(r,info,server_id(cfg))
            if info.get('artwork'):r['artwork']=info['artwork']
            availability_status=info.get('availability','Unknown')
            r.update(availability_source=info.get('availability_source','lidarr'),availability_checked_at=info.get('checked_at'),availability=availability_status,availability_detail=info.get('availability_detail','Waiting for a library scan.'),plex_available=availability_status in ('Available','Partially Available'),plex_fully_available=availability_status=='Available',plex_checked_at=info.get('checked_at'),lidarr_home_url=cfg.get('url','').rstrip('/'),lidarr_available=bool(r.get('lidarr_id') and not r.get('removed_from_lidarr')))
            if r['plex_available'] and r['lidarr_available'] and not r.get('active') and not r.get('download_error') and not any(d.get('error') for d in r.get('downloads',[])):
                if r.get('error') or r.get('status') in ('search_failed','search_unknown','add_failed'):
                    save(repo,r['key'],error='',status='search_completed')
                    r.update(error='',status='search_completed')

        return rows

    @app.post('/api/lidarr/requests/repair')
    def repair_all():
        from .accounts import actor
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        return repair_links(repository(),enabled(repository()))

    @app.post('/api/lidarr/requests/{album}/repair')
    def repair_one(album:str):
        from .accounts import actor
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        result=repair_links(repository(),enabled(repository()),album)
        if not result['checked']:raise HTTPException(404,'Request not found.')
        return result

    @app.delete('/api/lidarr/requests/{album}')
    def remove_request(album:str):
        from .accounts import actor
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        repo=repository()
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM state WHERE namespace='lidarr_requests' AND key=?",(album,)).fetchone()
            if row and active(db,json.loads(row[0]).get('job_id')):raise HTTPException(409,'Wait for the current request job to finish before removing it.')
            db.execute("DELETE FROM state WHERE namespace='lidarr_requests' AND key=?",(album,))
            db.execute("DELETE FROM state WHERE namespace='user_requests' AND json_extract(value,'$.album_id')=?",(album,))
        return {'removed':True,'message':'Removed from Harmonia. Lidarr album and downloads are unchanged.'}

    @app.post('/api/lidarr/requests/{album}/retry-search', status_code=202)
    def retry(album: str):
        from .api import job_store
        repo = repository()
        cfg = enabled(repo)
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM state WHERE namespace='lidarr_requests' AND key=?", (album,)).fetchone()
            if not row:
                raise HTTPException(404, 'Album request not found.')
            value = json.loads(row[0])
            if value.get('server') != server_id(cfg) or not value.get('lidarr_id'):
                raise HTTPException(409, 'No confirmed album on this Lidarr server. Check Lidarr before retrying.')
            if value.get('status') == 'search_unknown':
                raise HTTPException(409, 'The search submission timed out without a command ID. Inspect Lidarr before submitting another search.')
            if active(db,value.get('job_id')):
                return job_store().get(value['job_id'])
            payload = {'album_id':album,'server':value['server']}
            jid = job_store().enqueue('lidarr_search',payload,db=db)
            value.update(job_id=jid,status='search_pending',error='',updated_at=time.time())
            db.execute("UPDATE state SET value=? WHERE namespace='lidarr_requests' AND key=?", (json.dumps(value),album))
        return job_store().get(jid)
