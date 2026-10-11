"""Targeted post-import refresh for one Lidarr album and its mapped media."""
import os
import stat
import time
from pathlib import Path

from . import jobs
from .album_catalog import secondary_types
from .edition_preferences import release_summaries


def _album_row(album, artist):
    return {
        'id':album.get('id'),'album_id':album.get('foreignAlbumId'),'title':album.get('title',''),
        'artist':artist.get('artistName',album.get('artist',{}).get('artistName','')),
        'artist_id':album.get('artistId'),'artist_mbid':artist.get('foreignArtistId',''),
        'monitored':album.get('monitored',False),'statistics':album.get('statistics',{}),
        'releases':release_summaries(album.get('releases',[])),
        'anyReleaseOk':album.get('anyReleaseOk'),'year':str(album.get('releaseDate',''))[:4],
        'type':album.get('albumType','Other'),'secondary_types':secondary_types(album),
        'artwork':next((i.get('remoteUrl') for i in album.get('images',[]) if i.get('coverType')=='cover' and str(i.get('remoteUrl','')).startswith('https://')),''),
    }


def _tracks(client, album, artist):
    tracks=client.call('GET','track',params={'albumId':album['id']},quiet=True)
    files=client.call('GET','trackfile',params={'albumId':album['id']},quiet=True)
    if not isinstance(tracks,list) or not isinstance(files,list):raise ValueError('Incomplete Lidarr track response; previous data retained.')
    by_file={f['id']:f for f in files if f.get('id') is not None}
    result=[]
    for track in tracks:
        if track.get('albumId')!=album['id']:
            continue
        file=by_file.get(track.get('trackFileId'),{})
        result.append({'id':str(track['id']),'plex_id':str(track['id']),'title':track.get('title',''),
            'artist':artist.get('artistName',''),'album_artist':artist.get('artistName',''),'file_id':file.get('id'),'relative_path':file.get('relativePath',''),'album':album.get('title',''),'album_id':album.get('foreignAlbumId'),
            'lidarr_album_id':track.get('albumId'),'recording_id':track.get('foreignRecordingId',''),
            'disc_number':track.get('mediumNumber',1),'track_number':track.get('trackNumber'),
            'duration':track.get('duration'),'file':file.get('path','')})
    return result


def _merge_lidarr(repo, server, album, artist, tracks):
    from .lidarr import state_put
    inventory=repo.load('inventory').get('lidarr',{})
    if inventory.get('identity')!=server:
        inventory={'identity':server,'rows':[],'tracks':[],'artists':[],'count':0,'checked_at':0}
    rows=[r for r in inventory.get('rows',[]) if r.get('album_id')!=album.get('foreignAlbumId')]
    rows.append(_album_row(album,artist))
    saved_tracks=[t for t in inventory.get('tracks',[]) if t.get('album_id')!=album.get('foreignAlbumId')]
    saved_artists=[a for a in inventory.get('artists',[]) if str(a.get('id'))!=str(artist.get('id'))]
    def artwork(kinds):
        images=[i for i in artist.get('images',[]) if str(i.get('remoteUrl','')).startswith('https://')]
        return next((i.get('remoteUrl') for kind in kinds for i in images if str(i.get('coverType','')).casefold()==kind),'')
    saved_artists.append({'id':artist.get('id'),'name':artist.get('artistName'),'mbid':artist.get('foreignArtistId'),
        'monitored':artist.get('monitored'),'artwork':artwork(('poster','cover','logo','fanart')),
        'portrait_artwork':artwork(('poster','cover','logo')),'hero_artwork':artwork(('fanart','banner')),
        'images':[{k:i.get(k) for k in ('coverType','remoteUrl','url')} for i in artist.get('images',[]) if str(i.get('remoteUrl') or i.get('url') or '').startswith('https://')]})
    value={**inventory,'rows':rows,'tracks':saved_tracks+tracks,'artists':saved_artists,
        'count':len(rows),'partial_checked_at':time.time(),
        'album_checked_at':{**inventory.get('album_checked_at',{}),album.get('foreignAlbumId'):time.time()}}
    state_put(repo,'inventory','lidarr',value)
    return value


def _scan_paths(repo, lidarr, tracks):
    from .file_audit import AUDIO,scope,signature
    from .file_tools import mapped,settings
    from .lidarr import state_put
    cfg=settings()
    if not cfg.get('audit_enabled'):
        jobs.output('Targeted local-file scan skipped because Library Management scanning is disabled.')
        return {'checked':0,'missing':0,'unmapped':0,'disabled':True}
    key=scope(cfg,lidarr);snapshot=repo.load('file_audit').get(key,{})
    files=dict(snapshot.get('files',{}));paths=[];missing=[];unmapped=0
    for track in tracks:
        path=mapped(track.get('file',''),config=cfg)
        if not path:
            unmapped+=1
            jobs.output('No local path mapping for imported file: '+str(track.get('file') or track.get('title') or 'Unknown track'))
            continue
        if Path(path).suffix.lower() not in AUDIO:
            continue
        jobs.progress('Checking imported file: '+Path(path).name)
        paths.append(path)
        try:
            st=os.stat(path,follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):raise OSError('Not a regular audio file')
            entry={'signature':signature(st),'size':st.st_size}
            try:
                from .local_library import read_tags
                tags=read_tags(Path(path));entry['tags']={k:tags.get(k,'') for k in ('title','artist','album','recording_id','album_id','format','bitrate')}
            except Exception as exc:entry['tag_error']=type(exc).__name__
            files[path]=entry
        except OSError:
            files.pop(path,None);missing.append(path)
    issues=[i for i in snapshot.get('issues',[]) if not set(i.get('files',[])) & set(paths)]
    for path in missing:
        track=next((t for t in tracks if mapped(t.get('file',''),config=cfg)==path),{})
        issues.append({'issue':'Missing imported file','reason':'Lidarr reports this newly imported file, but its mapped path is absent.',
            'files':[path],'title':track.get('title',Path(path).name),'artist':track.get('artist',''),'album':track.get('album',''),
            'album_id':track.get('album_id'),'evidence':{'lidarr_path':track.get('file')}})
    state_put(repo,'file_audit',key,{**snapshot,'files':files,'issues':issues,'partial_checked_at':time.time()})
    return {'checked':len(paths),'missing':len(missing),'unmapped':unmapped,'disabled':False}


def _plex_folders(tracks, cfg):
    """Return mapped Plex parent folders as strings for the Plex API."""
    from .file_tools import mapped
    folders=set()
    for track in tracks:
        path=mapped(track.get('file',''),'plex_path',cfg)
        if path:
            folders.add(str(Path(path).parent))
    return sorted(folders)


def _wait_for_plex_album(plex, album, artist, refresh_requested=True, delays=(0,2,4), expected_files=None):
    """Recheck only the imported album while Plex processes an async path refresh."""
    from .matching import Matcher
    normalize=Matcher._normalize_match_text
    attempts=delays if refresh_requested else (0,)
    for index,delay in enumerate(attempts,1):
        if delay:
            jobs.progress(f'Waiting for Plex to index the refreshed folder · check {index} of {len(attempts)}')
            time.sleep(delay)
            if jobs.current():jobs.current().checkpoint()
        found=plex.search_library_subset(album.get('title',''))
        found=[r for r in found if normalize(r.get('album',''))==normalize(album.get('title','')) and normalize(r.get('album_artist') or r.get('artist',''))==normalize(artist.get('artistName',''))]
        if found:
            # A folder scan can expose only part of an album. Never declare it
            # complete until every imported file is represented in Plex.
            actual={path for row in found for path in row.get('files',[])}
            if not expected_files or set(expected_files)<=actual:return found
    return []


def _execute(payload, defer_reconcile=False):
    from .accounts import root_repository
    from .api import _config,_health_plex
    from .file_tools import mapped,settings as file_settings
    from .inventory import identity,reconcile
    from .lidarr import Client,config as lidarr_config,state_put
    from .lidarr_requests import save,server_id
    repo=root_repository();cfg=lidarr_config(repo);album_id=str(payload.get('album_id',''))
    if payload.get('server') and payload['server']!=server_id(cfg):raise ValueError('The Lidarr server changed before this album refresh started.')
    original_record=repo.load_one('lidarr_requests',album_id,{})
    if payload.get('plex_recheck') and (original_record.get('webhook_checked_at')!=payload.get('expected_webhook_at') or original_record.get('finalize_status')!='awaiting_plex' or original_record.get('webhook_failure')):
        return {'summary':'Skipped an outdated Plex import check.','pending':False,'album_id':album_id}
    from .lidarr_downloads import plex_checks_exhausted,PLEX_CHECK_STOPPED
    if payload.get('plex_recheck') and plex_checks_exhausted(original_record):
        save(repo,album_id,_expected_webhook_at=payload.get('expected_webhook_at'),
             finalize_status='failed',plex_check_after=0,finalize_error=PLEX_CHECK_STOPPED)
        return {'summary':PLEX_CHECK_STOPPED,'pending':False,'partial_success':True,'album_id':album_id}
    jobs.progress('Refreshing imported album from Lidarr')
    client=Client(cfg);album=client.call('GET',f"album/{int(payload['lidarr_id'])}")
    if album.get('foreignAlbumId')!=album_id:raise ValueError('The Lidarr album identity changed. Run a full Lidarr scan.')
    artist=client.call('GET',f"artist/{album['artistId']}");tracks=_tracks(client,album,artist)
    recheck=bool(payload.get('plex_recheck'))
    lidarr={} if recheck else _merge_lidarr(repo,server_id(cfg),album,artist,tracks)
    jobs.progress('Checking imported album files')
    local={'checked':0,'missing':0,'unmapped':0,'disabled':True} if recheck else _scan_paths(repo,lidarr,tracks)
    if not local.get('disabled'):
        jobs.output(f"Targeted local-file scan checked {local['checked']} files; {local['missing']} missing; {local['unmapped']} without a path mapping.")
    jobs.progress('Requesting a targeted Plex folder refresh')
    plex=_health_plex(_config(read_only=True,namespaces=[]));folders=_plex_folders(tracks,file_settings())
    if not folders:
        jobs.output('Targeted Plex refresh skipped: no Plex folder could be mapped from the imported Lidarr files. Add the Lidarr and Plex paths under Library Management.')
    for folder in ([] if recheck else folders):
        jobs.output('Refreshing Plex folder: '+folder)
        plex.refresh_library_path(folder)
    jobs.progress('Checking Plex for the imported album')
    expected=[mapped(t.get('file',''),'plex_path',file_settings()) for t in tracks if t.get('file')]
    expected=[path for path in expected if path]
    found=_wait_for_plex_album(plex,album,artist,bool(folders) and not recheck,expected_files=expected)
    if recheck and repo.load_one('lidarr_requests',album_id,{}).get('webhook_checked_at')!=payload.get('expected_webhook_at'):
        return {'summary':'Skipped an outdated Plex import check.','pending':False,'album_id':album_id}
    saved=repo.load('inventory').get('plex',{})
    if found and saved.get('identity')!=identity(plex):
        saved={'identity':identity(plex),'rows':[],'count':0,'checked_at':0}
    if found:
        ids={r['plex_id'] for r in found};rows=[r for r in saved.get('rows',[]) if r.get('plex_id') not in ids]+found
        state_put(repo,'inventory','plex',{**saved,'rows':rows,'count':len(rows),'partial_checked_at':time.time()})
    from .library_cache import invalidate
    from .artwork_sync import queue_pending
    if found or not recheck:
        if recheck:_merge_lidarr(repo,server_id(cfg),album,artist,tracks)
        queue_pending(repo)
        invalidate()
    reconciliation={} if defer_reconcile or (recheck and not found) else reconcile()
    status='completed' if found else 'awaiting_plex'
    record=repo.load_one('lidarr_requests',album_id,{})
    attempts=int(record.get('plex_check_attempts') or 0)+1 if recheck else 0
    if not found and recheck and plex_checks_exhausted({**record,'plex_check_attempts':attempts}):status='failed'
    changes={'finalize_status':status,'finalized_at':time.time(),
             'finalize_job_id':jobs.current().id if jobs.current() else '', 'finalize_error':'',
             'plex_check_started_at':record.get('plex_check_started_at') or record.get('webhook_imported_at') if recheck else time.time(),
             'plex_check_attempts':attempts,'plex_check_after':0 if found or status=='failed' else time.time()+min(900,30*2**min(attempts,5))}
    expected_count=max(len(tracks),int(album.get('statistics',{}).get('trackCount') or 0))
    changes['plex_confirmation']={'server':server_id(cfg),'checked_at':time.time(),
        'webhook_at':original_record.get('webhook_checked_at'),
        'found':len(found),'expected':expected_count,
        'album_ids':sorted({str(t.get('plex_album_id')) for t in found if t.get('plex_album_id')})}
    if status=='failed':changes['finalize_error']=PLEX_CHECK_STOPPED
    if not recheck:
        changes.update(download_status='Imported into Lidarr',download_error='',webhook_failure=None,
                       webhook_download_id='',webhook_imported_at=time.time())
    save(repo,album_id,_expected_webhook_at=payload.get('expected_webhook_at',original_record.get('webhook_checked_at')),**changes)
    message=f"Refreshed {len(tracks)} Lidarr tracks and checked {local.get('checked',0)} mapped files."
    message+=f" Plex returned {len(found)} matching tracks." if found else (' '+PLEX_CHECK_STOPPED if status=='failed' else ' Plex is refreshing; Harmonia will automatically check this album again.')
    local_issues=bool(local.get('missing') or local.get('unmapped'))
    return {'summary':message,'pending':not bool(found) and status!='failed','partial_success':local_issues or status=='failed','album_id':album_id,'tracks':len(tracks),'plex_tracks':len(found),'local':local,'reconciliation':reconciliation}


def execute(payload):
    if payload.get('batch'):
        return execute_batch(payload)
    try:
        return _execute(payload)
    except Exception as exc:
        from .accounts import root_repository
        from .lidarr_requests import save
        repo=root_repository();album_id=str(payload.get('album_id',''))
        record=repo.load_one('lidarr_requests',album_id,{})
        from .lidarr_downloads import plex_checks_exhausted
        attempts=int(record.get('plex_check_attempts') or 0)+1
        retry=bool(payload.get('plex_recheck')) and not plex_checks_exhausted({**record,'plex_check_attempts':attempts})
        save(repo,album_id,_expected_webhook_at=payload.get('expected_webhook_at', ...),
             finalize_status='awaiting_plex' if retry else 'failed',plex_check_attempts=attempts,
             plex_check_after=time.time()+300 if retry else 0,finalize_error=str(getattr(exc,'detail',exc)))
        raise


def execute_batch(payload):
    """Drain a bounded set of imported albums from one durable quiet-period batch."""
    import json
    import uuid
    from .accounts import root_repository
    from .lidarr_downloads import FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY,_finalize_batch
    repo=root_repository();batch=_finalize_batch(repo.load_one(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY,{}))
    processed=int(batch.get('processed_generation') or 0);generation=int(batch.get('generation') or 0)
    pending=sorted((item for item in batch.get('albums',{}).values() if processed<int(item.get('generation') or 0)<=generation),key=lambda item:item['generation'])[:10]
    if not pending:return {'summary':'No newer imported albums need finalization.','albums':[]}
    results=[]
    for index,item in enumerate(pending,1):
        jobs.progress(f"Finalizing imported album {index} of {len(pending)}",completed=index-1,total=len(pending))
        try:
            result=_execute(item,defer_reconcile=True);results.append({'album_id':item['album_id'],'ok':True,'result':result})
        except Exception as exc:
            from .lidarr_requests import save
            message=str(getattr(exc,'detail',exc));save(repo,str(item.get('album_id','')),finalize_status='failed',finalize_error=message)
            jobs.output(f"Album {item.get('album_id')}: {message}");results.append({'album_id':item.get('album_id'),'ok':False,'error':message})
        if jobs.current():jobs.current().checkpoint()
    if any(row['ok'] for row in results):
        jobs.progress('Reconciling imported albums')
        from .inventory import reconcile
        reconcile()
    completed=max(int(item.get('generation') or 0) for item in pending)
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT value FROM state WHERE namespace=? AND key=?',(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY)).fetchone()
        latest=_finalize_batch(json.loads(row[0]) if row else None);latest['processed_generation']=max(int(latest.get('processed_generation') or 0),completed)
        latest['albums']={key:value for key,value in latest.get('albums',{}).items() if int(value.get('generation') or 0)>latest['processed_generation']}
        db.execute('INSERT OR REPLACE INTO state(namespace,key,value) VALUES(?,?,?)',(FINALIZE_BATCH_NAMESPACE,FINALIZE_BATCH_KEY,json.dumps(latest)))
        if int(latest.get('generation') or 0)>latest['processed_generation']:
            follow_id=jobs.Store(repo).enqueue('album_finalize',{'batch':True,'generation':latest['generation'],'batch_token':str(uuid.uuid4()),
                'trigger_label':'Lidarr imported albums follow-up'},db=db)
        else:follow_id=None
    if follow_id:jobs.Store(repo).event(follow_id,'Queued the next bounded imported-album batch')
    failed=sum(not row['ok'] for row in results);succeeded=len(results)-failed
    return {'summary':f'Finalized {succeeded} imported album'+('' if succeeded==1 else 's')+(f'; {failed} failed and can be retried manually.' if failed else '.'),
        'albums':results,'partial_success':bool(failed),'follow_up_job_id':follow_id}
