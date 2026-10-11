"""Read-only local file inventory and an independent MusicBrainz catalog.

Scans never write tags, rename files, or call Lidarr. A failed walk retains the
previous snapshot. MusicBrainz lookups happen only in explicit catalog jobs.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from . import jobs

EXTENSIONS={'.mp3','.flac','.m4a','.mp4','.aac','.ogg','.opus','.wav','.aiff','.aif','.ape','.wv'}

def mbid(value):
    try:return str(UUID(str(value)))
    except (ValueError,TypeError,AttributeError):return ''

def digest(value):return hashlib.sha256(str(value).encode()).hexdigest()

def repo():
    from .accounts import root_repository
    return root_repository()

def put(namespace,key,value):
    from .lidarr import state_put
    state_put(repo(),namespace,key,value)

class Root(BaseModel):
    path:str=Field(min_length=1,max_length=2000)
    plex_path:str=Field(default='',max_length=2000)
    @model_validator(mode='after')
    def absolute(self):
        if not Path(self.path).is_absolute() or Path(self.path)==Path('/'):
            raise ValueError('Choose a specific absolute music folder, not the filesystem root.')
        self.path=str(Path(self.path).absolute())
        return self

class Settings(BaseModel):
    enabled:bool=False
    itunes_enrichment:bool=True
    roots:list[Root]=Field(default_factory=list,max_length=20)
    @model_validator(mode='after')
    def distinct(self):
        paths=[Path(r.path).resolve() for r in self.roots]
        if len(set(paths))!=len(paths) or any(a!=b and a in b.parents for a in paths for b in paths):
            raise ValueError('Music folders must not overlap.')
        if self.enabled and not paths:raise ValueError('Add a music folder before enabling local scanning.')
        return self

def settings():return Settings(**repo().load('local_library_settings').get('current',{})).model_dump()
def scope(cfg):return digest(json.dumps(cfg['roots'],sort_keys=True))
def snapshot():
    cfg=settings()
    return repo().load('local_inventory').get(scope(cfg),{}) if cfg['enabled'] else {}

def valid_year(value):
    match=re.match(r'^(\d{4})(?:-|$)',str(value or ''))
    return match.group(1) if match and 1000<=int(match.group(1))<=int(time.strftime('%Y'))+2 else ''

def read_tags(file):
    import mutagen
    audio=mutagen.File(file,easy=True)
    if audio is None:raise ValueError('Unsupported or unreadable audio file')
    tags=audio.tags or {}
    def value(*keys):
        for key in keys:
            v=tags.get(key)
            if v:
                if isinstance(v,(list,tuple)):v=v[0]
                if isinstance(v,bytes):v=v.decode('utf-8','replace')
                return str(v)
        return ''
    # EasyID3 and EasyMP4 names; raw Vorbis names already match these.
    title=value('title');artist=value('artist');album_artist=value('albumartist') or artist
    info=audio.info
    description=str(getattr(info,'codec_description','') or getattr(info,'codec','') or '').upper()
    suffix=Path(file).suffix.lower()
    codec='ALAC' if 'ALAC' in description else 'AAC' if ('AAC' in description or 'MP4A' in description or suffix=='.aac') else suffix.lstrip('.').upper()
    return {'title':title or Path(file).stem,'artist':artist or album_artist,'album_artist':album_artist,
            'album':value('album'),'year':valid_year(value('originaldate')) or valid_year(value('date')),
            'artist_id':mbid(value('musicbrainz_albumartistid','musicbrainz_artistid')),
            'album_id':mbid(value('musicbrainz_releasegroupid')),'release_id':mbid(value('musicbrainz_albumid')),
            'recording_id':mbid(value('musicbrainz_trackid')),'release_track_id':mbid(value('musicbrainz_releasetrackid')),
            'track_number':value('tracknumber').split('/')[0],'disc_number':value('discnumber').split('/')[0] or '1',
            'duration':getattr(info,'length',0),'bitrate':getattr(info,'bitrate',0),
            'sample_rate':getattr(info,'sample_rate',0),'bits_per_sample':getattr(info,'bits_per_sample',0),
            'format':Path(file).suffix.lstrip('.').upper(),'codec':codec}

def scan():
    import mutagen  # Fail before replacing inventory if the reader is unavailable.
    cfg=settings()
    if not cfg['enabled']:return {'summary':'Local music scanning is disabled.'}
    old=snapshot();previous={t['file']:t for t in old.get('tracks',[])}
    result=[];errors=[];seen=set();started=time.time()
    for root in cfg['roots']:
        base=Path(root['path'])
        if not base.is_dir() or base.is_symlink():raise ValueError(f'Music folder unavailable: {base}. Previous scan retained.')
        def walk_error(exc):raise OSError(f'Cannot read music folder; previous scan retained: {exc}')
        for folder,dirs,files in os.walk(base,followlinks=False,onerror=walk_error):
            dirs[:]=[d for d in dirs if not Path(folder,d).is_symlink()]
            for name in files:
                file=Path(folder,name)
                if file.suffix.lower() not in EXTENSIONS or file.is_symlink():continue
                path=str(file)
                if path in seen:continue
                seen.add(path);st=file.stat();signature=[st.st_size,st.st_mtime_ns]
                if len(seen)%50==1:jobs.progress(f'Scanning local music · {len(seen)} files')
                try:
                    cached=previous.get(path,{})
                    row=dict(cached) if cached.get('signature')==signature else read_tags(path)
                    relative=file.relative_to(base).as_posix()
                    plex_file=root['plex_path'].rstrip('/')+'/'+relative if root['plex_path'] else path
                    row.update(id=digest(path),file=path,plex_file=plex_file,signature=signature,size=st.st_size)
                    result.append(row)
                except Exception as exc:
                    errors.append({'id':digest(path),'file':path,'title':name,'reason':f'Cannot read audio metadata: {type(exc).__name__}: {str(exc)[:200]}'})
    if scope(settings())!=scope(cfg):raise ValueError('Music folders changed during scan. Run the scan again.')
    put('local_inventory',scope(cfg),{'checked_at':time.time(),'tracks':result,'errors':errors})
    jobs.output(f'Local scan completed: {len(result)} audio files, {len(errors)} files need attention. No files were modified.')
    return {'summary':f'Scanned {len(result)} local audio files; {len(errors)} need attention.','seconds':round(time.time()-started,2)}

def mb_get(entity,params=None):
    """Cache metadata while central routing chooses public search or a mirror."""
    from .musicbrainz_settings import settings as mb_settings
    cfg=mb_settings(repo())
    if not cfg['enabled']:raise ValueError('MusicBrainz lookups are disabled.')
    search=bool((params or {}).get('query'))
    route='public-search' if search else cfg['server_url']
    key=digest(route+entity+json.dumps(params or {},sort_keys=True))
    cached=repo().load('local_metadata_cache').get(key,{})
    if cached and time.time()-cached['at']<cfg['cache_days']*86400:return cached['data']
    jobs.progress(f'MusicBrainz · {entity.split("/")[0]}')
    from .musicbrainz_client import get
    data=get(repo(),entity,params)
    put('local_metadata_cache',key,{'at':time.time(),'data':data});return data

def browse(entity,params):
    rows=[];offset=0
    while True:
        data=mb_get(entity,{**params,'limit':100,'offset':offset})
        batch=data.get(entity+'s',[]);rows.extend(batch);offset+=len(batch)
        if not batch or offset>=data.get(entity+'-count',offset):return rows

def credit(value):return ''.join(c.get('name',c.get('artist',{}).get('name',''))+c.get('joinphrase','') for c in value or [] if isinstance(c,dict))

def refresh_catalog(payload):
    aid=mbid(payload.get('artist_id'));rid=mbid(payload.get('release_id'));gid=mbid(payload.get('group_id'))
    if gid:
        rows=browse('release',{'release-group':gid,'inc':'media'})
        put('local_editions',gid,{'rows':rows,'checked_at':time.time()})
        return {'summary':f'Loaded {len(rows)} editions. Reopen Review Editions to see them.'}
    if rid:
        release=mb_get('release/'+rid,{'inc':'recordings+artist-credits+release-groups'})
        if not release.get('release-group',{}).get('id'):raise ValueError('Release has no MusicBrainz release group.')
        put('local_releases',rid,release)
        return {'summary':f'Metadata loaded for {release.get("title","release")}'}
    if not aid:raise ValueError('Select a MusicBrainz artist first.')
    artist=mb_get('artist/'+aid)
    groups=browse('release-group',{'artist':aid})
    put('local_artists',aid,{'id':aid,'name':artist.get('name',''),'groups':groups,'checked_at':time.time()})
    return {'summary':f'Loaded {len(groups)} release groups for {artist.get("name","")}.'}

def resolved_tracks():
    folders=repo().load('local_album_links');overrides=repo().load('local_track_links');releases=repo().load('local_releases')
    result=[]
    for original in snapshot().get('tracks',[]):
        t=dict(original);saved=overrides.get(t['id'],{})
        if saved.get('signature')==t.get('signature'):
            t.update({k:saved[k] for k in ('release_id','release_track_id','recording_id','track_number','disc_number')})
        else:
            folder=folders.get(digest(str(Path(t['file']).parent)),{})
            if folder.get('release_id'):t['release_id']=folder['release_id']
        release=releases.get(t.get('release_id'),{})
        if release:
            t['album_id']=release.get('release-group',{}).get('id') or t.get('album_id')
            t['album']=release.get('title') or t['album']
            track=next((x for m in release.get('media',[]) for x in m.get('tracks',[]) if t.get('release_track_id') and x.get('id')==t['release_track_id']),None)
            if track:
                t['recording_id']=track.get('recording',{}).get('id') or t.get('recording_id')
                t['title']=track.get('title') or track.get('recording',{}).get('title') or t['title']
                t['artist']=credit(track.get('artist-credit') or track.get('recording',{}).get('artist-credit') or release.get('artist-credit')) or t['artist']
        result.append(t)
    return result

def native_view():
    from collections import defaultdict
    from .api import _config
    from .inventory import current
    from .track_bridge import links
    cfg=settings();snap=snapshot();tracks=resolved_tracks()
    manual=repo().load('local_album_links');releases=repo().load('local_releases')
    track_links=repo().load('local_track_links')
    groups={};owned=defaultdict(list)
    for artist in repo().load('local_artists').values():
        for group in artist.get('groups',[]):
            groups[group['id']]={'album_id':group['id'],'title':group.get('title',''),'artist':artist['name'],'artist_id':artist['id'],'year':valid_year(group.get('first-release-date')),'type':group.get('primary-type','Other'),'secondary_types':group.get('secondary-types',[])}
    for original in tracks:
        t=dict(original)
        override=track_links.get(t['id'],{})
        if override.get('signature')==t.get('signature'):
            t.update({k:override[k] for k in ('release_id','release_track_id','recording_id','track_number','disc_number')})
        folder=str(Path(t['file']).parent);folder_id=digest(folder)
        saved=manual.get(folder_id,{})
        r=releases.get(t.get('release_id') or saved.get('release_id'),{})
        gid=r.get('release-group',{}).get('id') or t.get('album_id') or 'unidentified:'+folder_id
        owned[gid].append({**t,'folder_id':folder_id,'selected_release':r,'selected_release_id':r.get('id') or t.get('release_id')})
        groups.setdefault(gid,{'album_id':gid,'title':r.get('title') or t['album'] or Path(folder).name,'artist':credit(r.get('artist-credit')) or t['album_artist'] or 'Unknown artist','artist_id':t.get('artist_id',''),'year':valid_year(r.get('date')) or valid_year(t.get('year')),'type':r.get('release-group',{}).get('primary-type','Other'),'secondary_types':r.get('release-group',{}).get('secondary-types',[])})
    config=_config(read_only=True,namespaces=[]);plex,_=current(config.repository,config)
    linked=links([{**t,'file':t.get('plex_file',t['file'])} for t in tracks],plex.get('rows',[]))
    rows=[];errors=list(snap.get('errors',[]))
    for gid,g in groups.items():
        ts=owned[gid];editions={t['selected_release_id'] for t in ts if t['selected_release_id']}
        complete=False;expected=None
        # Compare release-track identities or disc/position+recording, never just counts.
        for eid in editions:
            release=releases.get(eid,{})
            expected_tracks=[(str(m.get('position',1)),str(x.get('position')),x) for m in release.get('media',[]) for x in m.get('tracks',[])]
            if not expected_tracks:continue
            expected=len(expected_tracks)
            et=[t for t in ts if t['selected_release_id']==eid]
            def present(d,n,x):return any(t.get('release_track_id')==x.get('id') or (t.get('disc_number')==d and t.get('track_number')==n and t.get('recording_id') and t['recording_id']==x.get('recording',{}).get('id')) for t in et)
            if all(present(d,n,x) for d,n,x in expected_tracks):complete=True;break
        found=sum(t['id'] in linked for t in ts)
        if not ts:status='Missing'
        elif gid.startswith('unidentified:'):status='Needs Attention'
        elif expected is None:status='Needs Attention'
        elif not complete:status='Partial'
        elif time.time()-plex.get('checked_at',0)>86400:status='Plex scan pending'
        elif found<len(ts):status='Awaiting Plex'
        else:status='Available'
        reason='MusicBrainz identity or edition metadata needs review.' if status=='Needs Attention' else 'Some edition tracks are missing.' if status=='Partial' else 'Local files are not yet confirmed in Plex.' if status in ('Awaiting Plex','Plex scan pending') else ''
        enriched=[{**t,'link':linked.get(t['id'])} for t in ts]
        rows.append({**g,'status':status,'local_count':len(ts),'plex_count':found,'expected_tracks':expected,'tracks':enriched,'release_ids':sorted(editions),'reason':reason})
        if reason:errors.append({'id':gid,'album_id':gid,'title':g['title'],'artist':g['artist'],'reason':reason})
    linked_ids={str(v['plex_id']) for v in linked.values()}
    if snap.get('checked_at') and plex.get('checked_at'):
        for t in plex.get('rows',[]):
            if str(t['plex_id']) not in linked_ids:errors.append({'id':'plex:'+str(t['plex_id']),'title':t.get('title'),'artist':t.get('artist'),'reason':'Plex item has no link to a scanned local file. Check music folders and path mappings.'})
    from .artist_library import directory,normalize,artist_key
    artist_links=repo().load('local_artist_links');extras=repo().load('local_artist_enrichment')
    for row in rows:
        row['year']=valid_year(row.get('year'))
        source=artist_key(row['artist'],row.get('artist_id',''))
        aid=artist_links.get(source,{}).get('artist_id') or row.get('artist_id')
        extra=extras.get(aid,{})
        matches=[a for a in extra.get('albums',[]) if normalize(a['title'])==normalize(row['title'])]
        if len(matches)==1:
            row['itunes_artwork']=matches[0]['artwork'];row['itunes_url']=matches[0]['url']
            if not row['year']:row['year']=matches[0]['year'];row['year_source']='iTunes'
    return {'artists':directory(rows),'enabled':cfg['enabled'],'rows':rows,'errors':errors,'checked_at':snap.get('checked_at'),'plex_checked_at':plex.get('checked_at')}

def availability(rows,apply_preferences=True):
    """One local-file verdict reused by Discover and Requests."""
    from .matching import Matcher
    from .user_preferences import blocked,preferences
    data=native_view();prefs=preferences();out=[]
    norm=Matcher._normalize_match_text
    def key(a,b):return norm(a),norm(b)
    catalog={r['album_id']:r for r in data['rows']}
    associations=repo().load('album_links')
    for row in rows:
        if apply_preferences and blocked(row):continue
        k=key(row['artist'],row['album'])
        saved=associations.get(digest(repr(k)),{}).get('album_id')
        exact=catalog.get(saved or row.get('mbid'))
        candidates=[r for r in data['rows'] if key(r['artist'],r['title'])==k]
        album=exact or (candidates[0] if len(candidates)==1 and not saved else None)
        status=album['status'] if album else 'Needs Attention' if len(candidates)>1 or saved else 'Not Available'
        if not data['checked_at'] or time.time()-data['checked_at']>86400:status='Unknown'
        detail={'Available':'Confirmed local edition and linked Plex tracks.','Partial':'Some tracks on the selected edition are missing.','Awaiting Plex':'Local edition is complete; Plex links need review.','Missing':'Not found in the scanned music folders.','Not Available':'Not found in the scanned music folders.','Unknown':'Run a current Local Music Library Scan.','Plex scan pending':'Local edition is complete; run a current Plex scan.','Needs Attention':'MusicBrainz identity or edition metadata needs review.'}.get(status,'Review the local library association.')
        if apply_preferences and prefs.get('hide_available') and status=='Available':continue
        out.append({**row,'availability':status,'availability_detail':detail,'availability_source':'local','checked_at':data['checked_at'],'local_album_id':album['album_id'] if album else None})
    return out

def register(app):
    from .artist_library import register as register_artists
    register_artists(app)
    class Catalog(BaseModel):
        artist_id:str=''
        release_id:str=''
        group_id:str=''
    class Association(BaseModel):
        release_id:str
    def enqueue(action,payload):
        from .api import job_store
        store=job_store();return store.get(store.enqueue(action,payload))
    @app.get('/api/settings/local-library')
    def get_settings():return settings()
    @app.put('/api/settings/local-library')
    def save_settings(body:Settings):
        put('local_library_settings','current',body.model_dump());return settings()
    @app.post('/api/library/local/scan')
    def start_scan():return enqueue('local_scan',{})
    @app.get('/api/library/local')
    def view():return native_view()
    @app.post('/api/library/local/catalog')
    def catalog(body:Catalog):
        if not (mbid(body.artist_id) or mbid(body.release_id) or mbid(body.group_id)):raise HTTPException(422,'Enter a valid MusicBrainz artist or release ID.')
        return enqueue('local_catalog',body.model_dump())
    @app.get('/api/library/local/releases/{group_id}')
    def editions(group_id:str):
        from .musicbrainz_settings import ordered_releases,settings as preferences
        if not mbid(group_id):raise HTTPException(422,'Link this album to MusicBrainz first.')
        saved=repo().load('local_editions').get(group_id,{})
        return {'rows':ordered_releases(saved.get('rows',[]),preferences(repo())),'checked_at':saved.get('checked_at')}
    @app.put('/api/library/local/folders/{folder_id}/link')
    def associate(folder_id:str,body:Association):
        rid=mbid(body.release_id)
        release=repo().load('local_releases').get(rid)
        if not release:raise HTTPException(409,'Load that release metadata first, then save the association.')
        if not any(digest(str(Path(t['file']).parent))==folder_id for t in snapshot().get('tracks',[])):raise HTTPException(404,'Folder not in the current local scan.')
        put('local_album_links',folder_id,{'release_id':rid,'checked_at':time.time()})
        return {'saved':True,'message':'Catalog association saved. File tags were not changed.'}

    class TrackAssociation(BaseModel):
        release_id:str
        release_track_id:str
    @app.put('/api/library/local/tracks/{track_id}/link')
    def track_associate(track_id:str,body:TrackAssociation):
        t=next((t for t in snapshot().get('tracks',[]) if t['id']==track_id),None)
        release=repo().load('local_releases').get(mbid(body.release_id),{})
        selected=next(((m,x) for m in release.get('media',[]) for x in m.get('tracks',[]) if x.get('id')==mbid(body.release_track_id)),None)
        if not t or not selected:raise HTTPException(409,'File or release track is not in the current inventory.')
        medium,track=selected
        put('local_track_links',track_id,{'signature':t['signature'],'release_id':release['id'],'release_track_id':track['id'],'recording_id':track.get('recording',{}).get('id',''),'track_number':str(track['position']),'disc_number':str(medium.get('position',1))})
        return {'saved':True}
