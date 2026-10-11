"""Persistent, server-scoped inventories. Successful snapshots replace atomically."""
import hashlib
import json
import time
from . import jobs

def identity(client):
    return hashlib.sha256(repr((client.base_url,client.music_library_key,sorted(client.headers.items()))).encode()).hexdigest()

def snapshot(repo, service, key):
    # Inventories can contain tens of thousands of rows. Loading one service
    # must not decode the other large service snapshot as a side effect.
    value=repo.load_one('inventory',service,{})
    return value if value.get('identity')==key else {}

def publish(repo, service, key, rows, started, **extra):
    value={'identity':key,'rows':rows,'checked_at':time.time(),'seconds':round(time.monotonic()-started,2),'count':len(rows),**extra}
    from .lidarr import state_put
    if service=='plex':
        from .accounts import root_repository
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Artwork can change while a service fetch is in flight. Carry
            # those verified newer image paths into the published snapshot.
            fetched_at=time.time()-(time.monotonic()-started)
            newer={}
            for edit in root_repository().load('artwork_plex_sync').values():
                if edit.get('machine_identifier')!=value.get('machine_identifier') or edit.get('applied_at',0)<fetched_at:continue
                newer.setdefault((edit['id_key'],str(edit['plex_id'])),{})[edit['path_key']]=edit['path']
            if newer:
                for row in rows:
                    for id_key in ('plex_album_id','plex_artist_id'):
                        row.update(newer.get((id_key,str(row.get(id_key) or '')),{}))
            db.execute("INSERT OR REPLACE INTO state VALUES('inventory',?,?)",(service,json.dumps(value)))
    else:
        state_put(repo,'inventory',service,value)
    return value

def scan(service, force=False):
    from .api import job_store,_config,_health_plex
    from .lidarr import Client,config as lidarr_config
    from .lidarr_requests import server_id
    from .lidarr_downloads import queue,snapshot as refresh_requests
    repo=job_store().repository;started=time.monotonic()
    jobs.progress(f'Scanning {service} library inventory')
    if service=='plex':
        client=_health_plex(_config(read_only=True,namespaces=[]));key=identity(client)
        rows=client._fetch_library() # explicit scan: always fresh and strict
        jobs.progress(f'Saving {len(rows)} Plex tracks')
        result=publish(repo,service,key,rows,started,machine_identifier=client.machine_identifier)
        from .library_cache import invalidate
        invalidate()
    else:
        cfg=lidarr_config(repo)
        from .availability import preferences
        if not force and not preferences(repo).refresh_lidarr:return {'summary':'Lidarr inventory scans are disabled in Settings.'}
        if not cfg.get('enabled'):return {'summary':'Lidarr integration is disabled; no inventory changed.'}
        key=server_id(cfg);client=Client(cfg)
        artists=client.call('GET','artist');albums=client.call('GET','album');downloads=queue(client)
        try:naming=client.call('GET','config/naming',quiet=True)
        except Exception:naming={}
        if not isinstance(artists,list) or not isinstance(albums,list):raise ValueError('Invalid Lidarr catalog; previous inventory retained')
        from .edition_preferences import release_summaries
        artist_lookup={a.get('id'):a for a in artists}
        rows=[{'id':a.get('id'),'album_id':a.get('foreignAlbumId'),'title':a.get('title',''),'artist':artist_lookup.get(a.get('artistId'),{}).get('artistName',a.get('artist',{}).get('artistName','')),'artist_id':a.get('artistId'),'monitored':a.get('monitored',False),'statistics':a.get('statistics',{}),'releases':release_summaries(a.get('releases',[])),'anyReleaseOk':a.get('anyReleaseOk')} for a in albums]
        from .track_bridge import scan_tracks
        tracks=scan_tracks(client,artists,albums)
        album_lookup={a['id']:a for a in albums}
        for row in rows:
            original=album_lookup.get(row['id'],{})
            from .album_catalog import secondary_types
            row['secondary_types']=secondary_types(original)
            row.update(year=str(original.get('releaseDate',''))[:4],type=original.get('albumType','Other'),artist_mbid=artist_lookup.get(original.get('artistId'),{}).get('foreignArtistId',''))
            row['artwork']=next((i.get('remoteUrl') for i in original.get('images',[]) if i.get('coverType')=='cover' and i.get('remoteUrl','').startswith('https://')), '')
        from .file_audit import rename_previews
        rename_preview=rename_previews(client,{'rows':rows,'tracks':tracks})
        jobs.progress(f'Saving {len(rows)} Lidarr albums and {len(tracks)} tracks')
        def artist_artwork(artist,kinds):
            images=[i for i in artist.get('images',[]) if str(i.get('remoteUrl','')).startswith('https://')]
            return next((i.get('remoteUrl') for kind in kinds for i in images if str(i.get('coverType','')).casefold()==kind),'')
        result=publish(repo,service,key,rows,started,naming=naming if isinstance(naming,dict) else {},rename_preview=rename_preview,artists=[{
            'id':a.get('id'),'name':a.get('artistName'),'mbid':a.get('foreignArtistId'),
            'monitored':a.get('monitored'),
            'artwork':artist_artwork(a,('poster','cover','logo','fanart')),
            'portrait_artwork':artist_artwork(a,('poster','cover','logo')),
            'hero_artwork':artist_artwork(a,('fanart','banner')),
            'images':[{k:i.get(k) for k in ('coverType','remoteUrl','url')} for i in a.get('images',[]) if str(i.get('remoteUrl') or i.get('url') or '').startswith('https://')],
        } for a in artists],queue=downloads,tracks=tracks)
        refresh_requests(repo,cfg,force=True)
    from .library import invalidate as invalidate_library
    invalidate_library()
    from .artwork_sync import queue_pending
    from .accounts import root_repository
    queue_pending(root_repository())
    reconciliation=queue_reconciliation(repo)
    from .accounts import member_stores,as_user,actor,is_owner
    if not actor() or is_owner(actor()):
        for user,member_store in member_stores():
            with as_user(user):
                if service=='plex':
                    # Shared accounts can have narrower music visibility. Never
                    # reuse the owner's inventory as proof of user availability.
                    try:
                        member_client=_health_plex(_config(read_only=True,namespaces=[]))
                        member_rows=member_client._fetch_library()
                        publish(member_store.repository,'plex',identity(member_client),member_rows,time.monotonic(),machine_identifier=member_client.machine_identifier)
                    except Exception:
                        jobs.output('A user library scan failed; its previous snapshot was retained.')
                        continue
                queue_reconciliation(member_store.repository)
    failed=(result.get('rename_preview') or {}).get('failed_artists',[])
    summary=f"Scanned {result['count']} {service} items; inventories linked to Harmonia tracks."
    if failed:summary+=f" Rename previews unavailable for {len(failed)} artists; naming checks are incomplete."
    return {'summary':summary,'count':result['count'],'seconds':result['seconds'],'reconciliation':reconciliation,'partial_success':bool(failed)}

def current(repo,config):
    from .accounts import actor, as_user, root_repository
    from .api import _health_plex
    from .lidarr import config as lidarr_config
    from .lidarr_requests import server_id
    from types import SimpleNamespace
    settings=config.config.get('plex',{})
    client=SimpleNamespace(base_url=settings.get('url','').rstrip('/'),music_library_key=str(settings.get('music_library_key','')).strip(),headers={'X-Plex-Token':settings.get('token',''),'Accept':'application/json'})
    plex=snapshot(repo,'plex',identity(client))
    shared=root_repository()
    return plex,snapshot(shared,'lidarr',server_id(lidarr_config(shared)))

def queue_reconciliation(repo):
    """Coalesce scan completions into a library job after both fetches finish."""
    from .jobs import Store
    store=Store(repo)
    job_id=store.enqueue('reconcile',{})
    return {'queued':True,'job_id':job_id}


def reconcile():
    from .process_lock import ProcessLock
    # Also protect direct finalization callers and HTTP playlist mutations.
    with ProcessLock(wait=True):
        return _reconcile()


def _reconcile():
    """Link saved/manual selections or unique exact identities; never mutate matches."""
    from .api import job_store,_config,_playlist_key
    from .matching import Matcher
    from .sync_engine import Syncer
    from .lidarr_requests import save,server_id
    from .lidarr import config as lidarr_config
    repo=job_store().repository;config=_config(read_only=True);syncer=Syncer(config)
    plex,lidarr=current(repo,config)
    normalize=Matcher._normalize_match_text
    def trackkey(t):return (normalize(t.get('title','')),normalize(t.get('artist','')),normalize(t.get('album','')))
    by_id={str(t['plex_id']):t for t in plex.get('rows',[])};exact={}
    for t in plex.get('rows',[]):exact.setdefault(trackkey(t),[]).append(t['plex_id'])
    albums={}
    for a in lidarr.get('rows',[]):albums.setdefault((normalize(a['artist']),normalize(a['title'])),[]).append(a)
    links={};count=0
    for p in config.config.get('playlists',[]):
        key=_playlist_key(p);source=config.source_snapshots.get(key,[])
        if not isinstance(source,list):source=[]
        source=source+config.missing.get(key,[])
        for t in source:
            count+=1
            if count==1 or count%100==0:jobs.progress(f'Linking source tracks to library inventories · {count} checked')
            search=f"{t.get('title','')}|{t.get('artist','')}";saved=config.mapping.get(key,{}).get(search)
            ids=exact.get(trackkey(t),[])
            manual=syncer._get_match_provenance(key,search)=='manual'
            match=str(saved) if saved and str(saved) in by_id else (str(ids[0]) if len(ids)==1 and not manual else None)
            candidates=albums.get((normalize(t.get('artist','')),normalize(t.get('album',''))),[])
            album=candidates[0] if len(candidates)==1 else None
            link_id=hashlib.sha256(json.dumps([key,t],sort_keys=True).encode()).hexdigest()
            links[link_id]={'playlist_key':key,'source':t,'plex_id':match,'lidarr_album_id':album.get('album_id') if album else None,'available':bool(match),'ignored':bool(syncer._find_ignored_track_key(key,t))}
    # One replaced snapshot removes obsolete links after removals/rescans.
    repo.save({'inventory_links':{'current':{'rows':list(links.values()),'checked_at':time.time(),'plex_identity':plex.get('identity'),'lidarr_identity':lidarr.get('identity')}}})
    cfg=lidarr_config(repo)
    for key,r in repo.load('lidarr_requests').items():
        if r.get('server')!=server_id(cfg):continue
        statuses=[]
        for source in r.get('sources',[]):
            matches=[v for v in links.values() if normalize(v['source'].get('title',''))==normalize(source.get('title','')) and normalize(v['source'].get('artist',''))==normalize(source.get('artist',''))]
            statuses.append({**source,'available':bool(matches) and any(m['available'] for m in matches)})
        save(repo,key,plex_tracks=statuses,plex_available=bool(statuses) and all(t['available'] for t in statuses),plex_checked_at=plex.get('checked_at'))
    return {'linked_tracks':len(links),'available':sum(v['available'] for v in links.values())}

def register(app):
    @app.get('/api/inventory/status')
    def status():
        from .api import job_store,_config
        repo=job_store().repository
        try:plex,lidarr=current(repo,_config(read_only=True,namespaces=[]))
        except Exception:plex,lidarr={},{}
        return {s:{k:v for k,v in value.items() if k not in ('rows','artists','queue','identity')} for s,value in [('plex',plex),('lidarr',lidarr)]}


def read_plex(config):
    """Read the indexed saved scan without constructing a live Plex client."""
    from .library_inventory import indexed
    from fastapi import HTTPException
    value=indexed(config)['plex']
    if not value.get('checked_at'):
        raise HTTPException(409,'No saved Plex inventory is available for this account. Run Scan Plex Library first.')
    return value.get('rows',[])
