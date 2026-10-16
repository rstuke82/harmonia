"""Durable user edits layered over provider-owned playlist sources."""
import hashlib
import json
import uuid
from fastapi import HTTPException
from pydantic import BaseModel, Field

FIELDS=('title','artist','album','source_id','recording_id')
# Provider metadata is retained for matching and display, but is not editable.
PROVIDER_FIELDS=('duration','artists')

def identity(track):
    source=str(track.get('source_id') or track.get('recording_id') or '').strip()
    if source:return 'id:'+source
    value={k:str(track.get(k) or '').strip().casefold() for k in ('title','artist','album')}
    return 'meta:'+hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def entries(tracks,origin='source'):
    counts={};result=[]
    for track in tracks:
        key=identity(track);counts[key]=counts.get(key,0)+1
        result.append({**{k:track.get(k,'') for k in FIELDS},**{k:track[k] for k in PROVIDER_FIELDS if k in track},'editor_id':f'{key}#{counts[key]}','origin':origin})
    return result

def merged(repo,key,tracks):
    source=entries(tracks)
    saved=repo.load_one('playlist_overlays',key,{}) or {}
    if not saved:return source
    removed=set(saved.get('removed',[]));available={r['editor_id']:r for r in source if r['editor_id'] not in removed}
    for editor_id,fields in saved.get('edited',{}).items():
        if editor_id in available:available[editor_id].update(fields)
    for row in saved.get('added',[]):available[row['editor_id']]={**row,'origin':'bridge'}
    ordered=[]
    for item in saved.get('order',[]):
        if item in available:ordered.append(available.pop(item))
    # Newly added upstream tracks remain visible even though they were not in
    # the saved ordering. Provider order is retained among those new entries.
    ordered.extend(available.values())
    return ordered

def apply(repo,key,tracks):
    return [{k:r.get(k,'') for k in FIELDS} | {k:r[k] for k in PROVIDER_FIELDS if k in r} for r in merged(repo,key,tracks)]

class Row(BaseModel):
    editor_id:str=Field(max_length=300)
    origin:str='source'
    title:str=Field(min_length=1,max_length=500)
    artist:str=Field(min_length=1,max_length=500)
    album:str=Field(default='',max_length=500)
    source_id:str=Field(default='',max_length=500)
    recording_id:str=Field(default='',max_length=500)
class Edit(BaseModel):
    revision:int=0
    rows:list[Row]=Field(max_length=5000)

def register(app):
    from .accounts import personal_repository,actor
    from .api import job_store
    from .config import Config
    from .lidarr import config as lidarr_config
    from .lidarr_requests import server_id
    from .inventory import snapshot
    def playlist(key):
        config=Config(read_only=True,namespaces=[])
        row=next((p for p in config.config.get('playlists',[]) if f"{p.get('source')}:{p.get('source_id')}"==key),None)
        if not row:raise HTTPException(404,'Playlist not found')
        if actor() and not actor().get('admin') and actor().get('can_playlists',True) is False:raise HTTPException(403,'Playlist changes are disabled for this account.')
        return config,row
    @app.get('/api/playlists/{key}/editor')
    def read(key:str):
        config,row=playlist(key);repo=personal_repository()
        raw=config.source_snapshots.get(key)
        if not isinstance(raw,list):raise HTTPException(409,'Sync this playlist once before editing it.')
        source=entries(raw);saved=repo.load_one('playlist_overlays',key,{}) or {}
        return {'revision':saved.get('revision',0),'name':row.get('plex_playlist_name'),'source':row.get('source'),'rows':merged(repo,key,raw),'source_count':len(source)}
    @app.put('/api/playlists/{key}/editor',status_code=202)
    def save(key:str,body:Edit):
        config,_row=playlist(key);repo=personal_repository()
        raw=config.source_snapshots.get(key)
        if not isinstance(raw,list):raise HTTPException(409,'Sync this playlist once before editing it.')
        source_rows={r['editor_id']:r for r in entries(raw)};source_ids=set(source_rows);submitted=set()
        existing={r['editor_id']:r for r in merged(repo,key,raw)}
        added=[];order=[];edited={};seen=set()
        for row in body.rows:
            editor_id=row.editor_id
            if editor_id in seen:raise HTTPException(422,'Playlist rows must have unique editor IDs.')
            seen.add(editor_id)
            source=source_rows.get(editor_id)
            same_source=(row.origin=='source' and source is not None
                and row.source_id==source.get('source_id','')
                and row.recording_id==source.get('recording_id','')
                and identity(row.model_dump())==identity(source))
            if same_source:
                submitted.add(editor_id)
                fields={k:row.model_dump()[k] for k in FIELDS if row.model_dump()[k]!=source.get(k,'')}
                if fields:edited[editor_id]=fields
            else:
                editor_id=editor_id if editor_id.startswith('bridge:') else 'bridge:'+str(uuid.uuid4())
                previous=existing.get(row.editor_id,{})
                metadata={k:previous[k] for k in PROVIDER_FIELDS if k in previous and identity(previous)==identity(row.model_dump()) and all(previous.get(field,'')==row.model_dump().get(field,'') for field in ('source_id','recording_id'))}
                added.append({**row.model_dump(),**metadata,'editor_id':editor_id,'origin':'bridge'})
            order.append(editor_id)
        store=job_store()
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved=db.execute("SELECT value FROM state WHERE namespace='playlist_overlays' AND key=?",(key,)).fetchone()
            current=json.loads(saved[0]) if saved else {}
            if body.revision!=current.get('revision',0):
                raise HTTPException(409,'This playlist was edited elsewhere. Reopen the editor and try again.')
            value={'revision':body.revision+1,'removed':sorted(source_ids-submitted),'edited':edited,'added':added,'order':order}
            db.execute("INSERT OR REPLACE INTO state VALUES('playlist_overlays',?,?)",(key,json.dumps(value)))
            job=store.enqueue('sync',{'scope':'selected','playlist_keys':[key],'overlay_revision':body.revision+1},db=db)
        return store.get(job)
    @app.get('/api/playlists/{key}/editor/search')
    def search(key:str,q:str=''):
        playlist(key);root=__import__('harmonia.accounts',fromlist=['root_repository']).root_repository()
        inv=snapshot(root,'lidarr',server_id(lidarr_config(root)))
        words=[w.casefold() for w in q.split() if w.strip()]
        rows=[]
        for t in inv.get('tracks',[]):
            text=' '.join(str(t.get(k,'')) for k in ('title','artist','album')).casefold()
            if words and not all(w in text for w in words):continue
            rows.append({k:t.get(k,'') for k in ('title','artist','album','recording_id')})
            if len(rows)>=50:break
        return rows
