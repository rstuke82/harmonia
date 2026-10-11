"""Direct Fanart.tv music artwork provider keyed by MusicBrainz identities."""
import time
import uuid
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field

BASE_URL='https://webservice.fanart.tv/v3.2'
CACHE_SECONDS=7*86400


def settings(repo):
    value=repo.load('fanart').get('settings',{})
    return {'api_key':str(value.get('api_key','')).strip(),'client_key':str(value.get('client_key','')).strip()}


def configured(repo):
    value=settings(repo)
    return bool(value['api_key'] or value['client_key'])


def _safe_images(rows):
    result=[]
    for row in rows or []:
        url=str(row.get('url',''))
        host=(urlsplit(url).hostname or '').casefold()
        if url.startswith('https://') and (host=='fanart.tv' or host.endswith('.fanart.tv')):
            result.append({k:row.get(k) for k in ('id','url','lang','likes','width','height','added')})
    return sorted(result,key=lambda row:(str(row.get('lang') or '') not in ('','00','en'),-int(row.get('likes') or 0),str(row.get('id') or '')))


def lookup(repo,kind,mbid,force=False):
    if kind not in ('artist','album'):raise ValueError('Unsupported Fanart.tv lookup type.')
    try:identifier=str(uuid.UUID(str(mbid)))
    except (ValueError,TypeError,AttributeError):raise HTTPException(422,'Fanart.tv requires a valid MusicBrainz ID.') from None
    keys=settings(repo)
    if not keys['api_key'] and not keys['client_key']:raise HTTPException(409,'Configure a Fanart.tv API key in Metadata Sources first.')
    cache_key=kind+':'+identifier; saved=repo.load('fanart_cache').get(cache_key,{})
    if not force and saved and time.time()-float(saved.get('checked_at',0))<CACHE_SECONDS:return saved.get('data',{})
    headers={'Accept':'application/json','User-Agent':'Harmonia/1.0'}
    if keys['api_key']:headers['api-key']=keys['api_key']
    if keys['client_key']:headers['client-key']=keys['client_key']
    path='/music/'+identifier if kind=='artist' else '/music/albums/'+identifier
    try:
        response=requests.get(BASE_URL+path,headers=headers,timeout=(5,25),allow_redirects=False)
        if response.status_code==404:data={}
        else:
            response.raise_for_status();data=response.json()
            if not isinstance(data,dict):raise ValueError('Fanart.tv returned an unexpected response.')
    except (requests.RequestException,ValueError) as exc:
        status=getattr(getattr(exc,'response',None),'status_code',None)
        detail='Fanart.tv could not complete the artwork lookup.'+(f' HTTP {status}.' if status else ' Check the API key and network connection.')
        repo.add_log('ERROR','Fanart.tv',f'{kind} lookup failed'+(f' with HTTP {status}' if status else f' ({type(exc).__name__})'))
        raise HTTPException(502,detail) from None
    from .lidarr import state_put
    state_put(repo,'fanart_cache',cache_key,{'checked_at':time.time(),'data':data})
    repo.add_log('INFO','Fanart.tv',f'{kind} artwork lookup completed; {int(data.get("image_count") or 0)} images reported')
    return data


def cached(repo,kind,mbid):
    return repo.load('fanart_cache').get(kind+':'+str(mbid).casefold(),{}).get('data',{})


def artist_images(data,kind):
    keys={'portrait':('artistthumb',),'hero':('musicbanner','artistbackground','artist4kbackground'),'logo':('hdmusiclogo','musiclogo')}.get(kind,())
    return [row|{'type':key} for key in keys for row in _safe_images(data.get(key,[]))]


def album_images(data,release_group_id):
    albums=data.get('albums',[])
    if isinstance(albums,dict):album=albums.get(str(release_group_id),{})
    else:album=next((row for row in albums if str(row.get('release_group_id'))==str(release_group_id)),{})
    return [row|{'type':'albumcover'} for row in _safe_images(album.get('albumcover',[]))]


def register(app):
    from .accounts import actor,root_repository
    from .lidarr import state_put
    def admin():
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
    class FanartSettings(BaseModel):
        api_key:str=Field(default='',max_length=200)
        client_key:str=Field(default='',max_length=200)
        clear_api_key:bool=False
        clear_client_key:bool=False
    def public():
        value=settings(root_repository())
        return {'configured':bool(value['api_key'] or value['client_key']),'api_key_configured':bool(value['api_key']),'client_key_configured':bool(value['client_key'])}
    @app.get('/api/settings/fanart')
    def get_settings():admin();return public()
    @app.put('/api/settings/fanart')
    def save_settings(body:FanartSettings):
        admin();value=settings(root_repository())
        if body.clear_api_key:value['api_key']=''
        elif body.api_key.strip():value['api_key']=body.api_key.strip()
        if body.clear_client_key:value['client_key']=''
        elif body.client_key.strip():value['client_key']=body.client_key.strip()
        state_put(root_repository(),'fanart','settings',value);return public()
    @app.post('/api/settings/fanart/test')
    def test_settings():
        admin();data=lookup(root_repository(),'artist','f4a31f0a-51dd-4fa7-986d-3095c40c5ed9',True)
        count=sum(len(data.get(key,[])) for key in ('artistthumb','artistbackground','artist4kbackground','musicbanner','hdmusiclogo','musiclogo'))
        return {'ok':True,'message':f'Connected to Fanart.tv. Found {count} artist artwork choices in the test record.'}
