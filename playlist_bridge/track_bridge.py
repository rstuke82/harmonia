"""Lidarr recording identity linked to account-visible Plex media."""
import contextvars,functools,hashlib,posixpath,time
from collections import defaultdict
_UNSET=object()
_context=contextvars.ContextVar('catalog_matching',default=_UNSET)

def path(value):return posixpath.normpath(str(value or '').replace('\\','/')) if value else ''
def identity(row):
    from .matching import Matcher
    return tuple(Matcher._normalize_match_text(row.get(k,'')) for k in ('title','artist','album'))
def scan_tracks(client,artists,albums):
    from . import jobs
    result=[];by_album={a['id']:a for a in albums}
    for i,artist in enumerate(artists):
        jobs.progress(f"Scanning Lidarr tracks: {artist.get('artistName','')} · {i+1}/{len(artists)}",completed=i,total=len(artists))
        tracks=client.call('GET','track',params={'artistId':artist['id']},quiet=True)
        files=client.call('GET','trackfile',params={'artistId':artist['id']},quiet=True)
        if not isinstance(tracks,list) or not isinstance(files,list):raise ValueError('Incomplete Lidarr track response; previous snapshot retained')
        by_file={f['id']:f for f in files}
        for t in tracks:
            album=by_album.get(t.get('albumId'),{})
            file=by_file.get(t.get('trackFileId'),{})
            result.append({'id':str(t['id']),'plex_id':str(t['id']),'title':t.get('title',''),'artist':artist.get('artistName',''),'album_artist':album.get('artist',{}).get('artistName') or artist.get('artistName',''),'album':album.get('title',''),'album_id':album.get('foreignAlbumId'),'lidarr_album_id':t.get('albumId'),'recording_id':t.get('foreignRecordingId',''),'disc_number':t.get('mediumNumber',1),'track_number':t.get('trackNumber'),'duration':t.get('duration'),'file':file.get('path',''),'relative_path':file.get('relativePath',''),'file_id':file.get('id')})
    return result

def links(tracks,plex,manual=None):
    by_path=defaultdict(list);by_identity=defaultdict(list);by_id={str(p['plex_id']):p for p in plex}
    for p in plex:
        for f in p.get('files',[]):by_path[path(f)].append(p)
        by_identity[identity(p)].append(p)
        album_identity=identity({**p,'artist':p.get('album_artist') or p.get('artist','')})
        if album_identity!=identity(p):by_identity[album_identity].append(p)
    result={}
    for t in tracks:
        if not t.get('file'):continue
        lid=str(t['id']);saved=(manual or {}).get(lid,{})
        if saved and saved.get('identity')==list(identity(t)):
            p=by_id.get(str(saved.get('plex_id')))
            if p and list(identity(p))==saved.get('plex_identity'):
                result[lid]={'plex_id':p['plex_id'],'method':'manual'};continue
        candidates=by_path.get(path(t.get('file')),[]) if t.get('file') else []
        method='file path'
        if not candidates:
            candidates=by_identity.get(identity(t),[]) if all(identity(t)) else []
            method='exact metadata'
        candidates={str(p['plex_id']):p for p in candidates}
        if len(candidates)==1:result[lid]={'plex_id':next(iter(candidates)),'method':method}
    return result

def context(config,plex,album_id=None,inventory=None):
    from .accounts import root_repository
    from .lidarr import config as settings
    from .inventory import current,identity as plex_identity
    from .lidarr_requests import server_id
    cfg=settings(root_repository())
    if not cfg.get('enabled'):return None
    if inventory is None:_,inventory=current(config.repository,config)
    tracks=inventory.get('tracks',[])
    if album_id is not None:tracks=[t for t in tracks if str(t.get('album_id'))==str(album_id)]
    # Old scans are intentionally not interpreted as an empty library.
    if 'tracks' not in inventory:return {'pending':True,'tracks':[],'links':{}}
    from types import SimpleNamespace
    p=config.config['plex'];scope=server_id(cfg)+':'+hashlib.sha256((p['url'].rstrip('/')+'|'+str(p.get('music_library_key',''))).encode()).hexdigest()
    manual=root_repository().load('track_links').get(scope,{})
    from .file_tools import mapped,settings as file_settings
    file_config=file_settings()
    tracks=[{**t,'lidarr_file':t.get('file',''),'file':mapped(t.get('file',''),'plex_path',file_config) or t.get('file','')} for t in tracks]
    linked=links(tracks,plex,manual)
    return {'tracks':tracks,'links':linked,'scope':scope,'resolved':{},'source_links':config.repository.load('catalog_source_matches').get(scope,{}),'pending':time.time()-inventory.get('checked_at',0)>86400}

def scoped(fn):
    @functools.wraps(fn)
    def run(self,source_tracks,mapping_key,plex_library=None,*args,**kwargs):
        library=plex_library if plex_library is not None else self._get_plex().search_library('')
        ctx=context(self.config,library)
        from . import jobs
        if ctx is not None:jobs.output(ctx.get('provider','Lidarr')+' matching: '+('run a current library scan before new automatic matches' if ctx['pending'] else f"{len(ctx['tracks'])} catalog tracks; {len(ctx['links'])} linked to Plex"))
        token=_context.set(ctx)
        try:
            result=fn(self,source_tracks,mapping_key,library,*args,**kwargs)
            if ctx and ctx.get('scope') and not self.config.read_only and kwargs.get('record_provenance',True):
                from .lidarr import state_put
                state_put(self.config.repository,'catalog_source_matches',ctx['scope'],{**ctx['source_links'],**ctx['resolved']})
                state_put(self.config.repository,'catalog_links',ctx['scope'],{'links':ctx['links'],'checked_at':time.time()})
            return result
        finally:_context.reset(token)
    return run

def duration_recording(source,candidates):
    """Resolve distinct recordings only with a close, clearly separated duration."""
    import math
    def seconds(row):
        value=row.get('duration')
        if isinstance(value,bool):return None
        try:value=float(value)
        except (TypeError,ValueError):return None
        if not math.isfinite(value) or value<=0:return None
        return value/1000 if value>10000 else value
    duration=seconds(source)
    if duration is None:return None
    groups=defaultdict(list)
    for candidate in candidates:
        value=seconds(candidate)
        # An unknown competitor cannot safely be eliminated by duration.
        if value is None:return None
        groups[candidate.get('recording_id') or candidate['id']].append((abs(duration-value),candidate))
    ranked=sorted((min(rows,key=lambda row:row[0]) for rows in groups.values()),key=lambda row:row[0])
    if not ranked or ranked[0][0]>5:return None
    if len(ranked)>1 and ranked[1][0]-ranked[0][0]<10:return None
    return ranked[0][1]

def match(cls,source,plex,mapping=None):
    key=f"{source['title']}|{source['artist']}"
    if mapping and key in mapping:
        saved=next((p for p in plex if str(p.get('plex_id'))==str(mapping[key])),None)
        if saved and not cls._recording_version_mismatch(source,saved):return mapping[key]
    ctx=_context.get()
    if ctx is _UNSET:
        from .config import Config
        ctx=context(Config(read_only=True,namespaces=[]),plex)
    if ctx is None:return cls._match_catalog_track(source,plex,{})
    if ctx['pending']:return None
    # Harmonia-added tracks selected from the Lidarr inventory retain their
    # recording identity, so they do not need to be rediscovered by title.
    recording_id=str(source.get('recording_id') or '')
    direct=[t for t in ctx['tracks'] if recording_id and str(t.get('recording_id') or '')==recording_id]
    if len(direct)==1:
        lid=str(direct[0]['id'])
        pid=ctx['links'].get(lid,{}).get('plex_id')
        target=next((p for p in plex if str(p.get('plex_id'))==str(pid)),None)
        return pid if target and not cls._recording_version_mismatch(source,target) else cls._match_catalog_track(source,plex,{})
    source_key=hashlib.sha256(repr(identity(source)).encode()).hexdigest()
    cached=ctx.get('source_links',{}).get(source_key,{})
    candidate=next((t for t in ctx['tracks'] if str(t['id'])==cached.get('lidarr_id') and list(identity(t))==cached.get('identity')),None)
    lid=cls._match_catalog_track(source,[candidate],{}) if candidate else cls._match_catalog_track(source,ctx['tracks'],{})
    chosen=next((t for t in ctx['tracks'] if str(t['id'])==str(lid)),None)
    # A source can already be present in Plex before Lidarr's catalog naming
    # agrees (soundtracks and deluxe labels are common). Keep the normal
    # high-confidence matcher available instead of reporting no automatic
    # candidate solely because catalog hydration is incomplete.
    if not chosen:return cls._match_catalog_track(source,plex,{})
    if chosen:
        same=[t for t in ctx['tracks'] if identity(t)==identity(chosen)]
        if len({t.get('recording_id') or t['id'] for t in same})>1:
            chosen=duration_recording(source,same)
            if chosen is None:return None
            # Duration only resolves identity ambiguity; normal acceptance
            # and recording/version protections must still pass.
            lid=cls._match_catalog_track(source,[chosen],{})
            if lid is None:return None
        ctx.setdefault('resolved',{})[source_key]={'lidarr_id':str(lid),'identity':list(identity(chosen))}
    pid=ctx['links'].get(str(lid),{}).get('plex_id')
    target=next((p for p in plex if str(p.get('plex_id'))==str(pid)),None)
    return pid if target and not cls._recording_version_mismatch(source,target) else cls._match_catalog_track(source,plex,{})


def source_status(ctx,source):
    if ctx is None:return None
    if ctx.get('pending'):return 'Waiting for a current library scan'
    key=hashlib.sha256(repr(identity(source)).encode()).hexdigest()
    saved=ctx.get('source_links',{}).get(key,{})
    track=next((t for t in ctx['tracks'] if str(t['id'])==saved.get('lidarr_id') and list(identity(t))==saved.get('identity')),None)
    if not track:return 'Not yet identified in '+ctx.get('provider','Lidarr')
    if str(track['id']) in ctx['links']:return 'Linked to '+ctx.get('provider','Lidarr')+' and Plex'
    return 'In '+ctx.get('provider','Lidarr')+' · awaiting Plex or link review' if track.get('file') else 'Awaiting download'
