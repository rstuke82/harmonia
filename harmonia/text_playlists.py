"""Reviewed, account-owned text sources; pasted content is data, never instructions."""
import re,uuid
from datetime import datetime,timezone
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,Field

class Track(BaseModel):
    title:str=Field(min_length=1,max_length=500)
    artist:str=Field(min_length=1,max_length=500)
    album:str=Field(default='',max_length=500)
class Source(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    tracks:list[Track]=Field(min_length=1,max_length=2000)
    source_id:str | None=None
    revision:int=0
class Paste(BaseModel):
    text:str=Field(max_length=200000)
    format:Literal['artist-title','title-artist']='artist-title'

def parse(text,order):
    rows=[]
    for line in text.splitlines():
        raw=line.strip()
        if not raw:continue
        value=re.sub(r'^\s*(?:\d+[.)]|[-*•])\s+','',raw).strip().strip('*')
        parts=re.split(r'\s+[—–-]\s+|\t',value,maxsplit=2)
        issue=''
        if len(parts)>=2:
            artist,title=parts[:2] if order=='artist-title' else parts[:2][::-1]
            album=parts[2] if len(parts)>2 else ''
        elif ' by ' in value:
            title,artist=value.rsplit(' by ',1);album=''
        else:artist='';title=value;album='';issue='Separate the artist and song, or remove this line.'
        rows.append({'artist':artist.strip().strip('"“”'),'title':title.strip().strip('"“”'),'album':album.strip(),'issue':issue,'original':raw})
    return rows

class TextSource:
    def get_playlist_tracks(self,url,fetch_artwork=True):
        from .accounts import personal_repository
        key=url.removeprefix('text:')
        value=personal_repository().load('text_sources').get(key)
        if not value:raise ValueError('Saved text playlist not found for this account')
        return value['tracks'],{'name':value['name'],'description':'A playlist curated in Harmonia.','image_url':'','complete':True,'expected_track_count':len(value['tracks'])}

def execute(payload):
    from .config import Config
    from .process_lock import ProcessLock
    from .lidarr import state_put
    from .api import execute_job
    body=Source(**payload)
    with ProcessLock():
        config=Config();key=body.source_id or str(uuid.uuid4())
        old=config.repository.load('text_sources').get(key)
        if body.source_id and (not old or old.get('revision',0)!=body.revision):raise ValueError('This text source changed. Reopen the editor before saving.')
        value={'name':body.name.strip(),'tracks':[t.model_dump() for t in body.tracks],'revision':(old or {}).get('revision',0)+1}
        if not value['name'] or any(not t['title'].strip() or not t['artist'].strip() for t in value['tracks']):raise ValueError('Every track needs an artist and title.')
        state_put(config.repository,'text_sources',key,value)
        playlist=next((p for p in config.config['playlists'] if p['source']=='text' and p['source_id']==key),None)
        if not playlist:
            playlist=config.add_playlist('text:'+key,'text',value['name'],'')
            playlist['added_at']=datetime.now(timezone.utc).isoformat()
        playlist['source_name']=value['name'];playlist['ready_to_sync']=True
        config.save()
    return execute_job('sync',{'scope':'selected','playlist_keys':['text:'+key]})

def register(app):
    from .accounts import personal_repository,actor
    from .api import job_store
    def permitted():
        if actor() and not actor().get('admin') and actor().get('can_playlists',True) is False:raise HTTPException(403,'Playlist changes are disabled for this account.')
    @app.post('/api/playlists/text/parse')
    def preview(body:Paste):
        permitted();return {'rows':parse(body.text,body.format)}
    @app.get('/api/playlists/text/{key}')
    def read(key:str):
        value=personal_repository().load('text_sources').get(key)
        if not value:raise HTTPException(404,'Text source not found')
        return {'source_id':key,**value}
    @app.post('/api/playlists/text',status_code=202)
    def save(body:Source):
        permitted()
        if body.source_id and body.source_id not in personal_repository().load('text_sources'):raise HTTPException(404,'Text source not found')
        store=job_store();return store.get(store.enqueue('text_playlist',body.model_dump()))
