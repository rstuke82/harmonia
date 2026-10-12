"""Artist-first local library identities with reviewed MusicBrainz associations.

Apple metadata is supplemental. It never replaces MBIDs or proves ownership.
"""
import threading
import time
from urllib.parse import urlsplit
from fastapi import HTTPException
from pydantic import BaseModel, Field
from . import jobs
from .local_library import put, digest, mbid, mb_get

def repo():
    from .local_library import repo as get_repo
    return get_repo()
_itunes_lock=threading.Lock()
_itunes_last=0.0

def normalize(value):
    import unicodedata
    return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())

def artist_key(name,artist_id=''):
    return 'mb:'+artist_id if mbid(artist_id) else 'name:'+digest(normalize(name))

def directory(albums):
    associations=repo().load('local_artist_links');metadata=repo().load('local_artists');enrichment=repo().load('local_artist_enrichment')
    result={}
    for album in albums:
        source=artist_key(album['artist'],album.get('artist_id',''))
        aid=associations.get(source,{}).get('artist_id') or mbid(album.get('artist_id'))
        key='mb:'+aid if aid else source
        display=metadata.get(aid,{}).get('name') or album['artist']
        row=result.setdefault(key,{'key':key,'source_keys':[],'name':display,'artist_id':aid,'album_ids':[],'local_albums':0,'catalog_albums':0,'tracks':0,'issues':0,'metadata_loaded':aid in metadata})
        row['source_keys'].append(source);row['album_ids'].append(album['album_id']);row['catalog_albums']+=1
        row['local_albums']+=int(album['local_count']>0);row['tracks']+=album['local_count'];row['issues']+=int(album['status']=='Needs Attention')
        extra=enrichment.get(aid,{}) if aid else {}
        row.update(genre=extra.get('genre',''),itunes_url=extra.get('artist_url',''),itunes_status=extra.get('status','Not loaded'),enriched_at=extra.get('checked_at'))
    # Include artist tags even if the album has no release group metadata.
    return sorted(result.values(),key=lambda r:(normalize(r['name']),r['key']))

def apple_url(value):
    parsed=urlsplit(str(value or ''))
    return str(value) if parsed.scheme=='https' and parsed.hostname in ('music.apple.com','itunes.apple.com') else ''

def image_url(value):
    parsed=urlsplit(str(value or ''))
    return str(value) if parsed.scheme=='https' and (parsed.hostname or '').endswith('.mzstatic.com') else ''

def itunes_get(endpoint,params):
    import requests
    global _itunes_last
    key=digest(endpoint+repr(sorted(params.items())))
    saved=repo().load('local_itunes_cache').get(key,{})
    if saved and time.time()-saved['at']<7*86400:
        jobs.output('iTunes metadata cache hit');return saved['data']
    with _itunes_lock:
        # Apple documents an approximate 20 calls/minute ceiling.
        time.sleep(max(0,3.1-(time.monotonic()-_itunes_last)));_itunes_last=time.monotonic()
        started=time.monotonic()
        try:
            response=requests.get('https://itunes.apple.com/'+endpoint,params=params,timeout=(5,20));response.raise_for_status();data=response.json()
        except (requests.RequestException,ValueError) as exc:
            status=getattr(getattr(exc,'response',None),'status_code',None)
            detail=f'iTunes lookup failed: {type(exc).__name__}'+(f' HTTP {status}' if status else '')
            repo().add_log('ERROR','iTunes',detail);raise ValueError(detail) from None
        jobs.output(f'iTunes {endpoint}: HTTP {response.status_code} in {time.monotonic()-started:.2f}s · {len(data.get("results",[]))} results')
        put('local_itunes_cache',key,{'at':time.time(),'data':data})
        with repo().connect() as db:
            db.execute("DELETE FROM state WHERE namespace='local_itunes_cache' AND key NOT IN (SELECT key FROM state WHERE namespace='local_itunes_cache' ORDER BY json_extract(value,'$.at') DESC LIMIT 500)")
        return data

def enrich(artist,local_albums,selected_id=None):
    from .local_library import valid_year
    name=artist['name'];aid=artist['id']
    saved=repo().load('local_artist_enrichment').get(aid,{})
    selected_id=selected_id or (saved.get('itunes_id') if saved.get('reviewed') else None)
    candidates=[]
    if selected_id:
        candidates=[{'artistId':selected_id}]
    else:
        candidates=[r for r in itunes_get('search',{'term':name,'media':'music','entity':'musicArtist','limit':25}).get('results',[]) if normalize(r.get('artistName'))==normalize(name)]
    ids={r['artistId'] for r in candidates if isinstance(r.get('artistId'),int)}
    if len(ids)!=1:
        put('local_artist_enrichment',aid,{'status':'Review iTunes artist' if ids else 'No exact iTunes artist found','candidates':[{k:r.get(k) for k in ('artistId','artistName','primaryGenreName','artistLinkUrl','artistViewUrl')} for r in candidates],'checked_at':time.time()})
        return
    itunes_id=next(iter(ids));data=itunes_get('lookup',{'id':itunes_id,'entity':'album','limit':200})
    person=next((r for r in data.get('results',[]) if r.get('wrapperType')=='artist' and r.get('artistId')==itunes_id),{})
    albums=[r for r in data.get('results',[]) if r.get('wrapperType')=='collection' and r.get('artistId')==itunes_id]
    known={normalize(a) for a in local_albums}
    if not selected_id and not any(normalize(a.get('collectionName')) in known for a in albums):
        put('local_artist_enrichment',aid,{'status':'Review iTunes artist','candidates':[person],'checked_at':time.time()});return
    put('local_artist_enrichment',aid,{'status':'Loaded','reviewed':bool(selected_id),'itunes_id':itunes_id,'genre':person.get('primaryGenreName',''),'artist_url':apple_url(person.get('artistLinkUrl') or person.get('artistViewUrl')),'checked_at':time.time(),'albums':[{'id':a.get('collectionId'),'title':a.get('collectionName',''),'year':valid_year(a.get('releaseDate')),'artwork':image_url(a.get('artworkUrl100')),'url':apple_url(a.get('collectionViewUrl'))} for a in albums]})

def refresh(payload):
    from .local_library import native_view,refresh_catalog,settings
    key=payload['artist_key'];rows=native_view()['artists']
    row=next((r for r in rows if r['key']==key or key in r['source_keys']),None)
    if not row:raise ValueError('Artist is no longer in the current library.')
    aid=mbid(payload.get('artist_id')) or row['artist_id']
    if not aid:
        from .lidarr import mb_quote
        jobs.progress('Finding MusicBrainz artist candidates for '+row['name'])
        response=mb_get('artist',{'query':'artist:'+mb_quote(row['name']),'limit':25})
        candidates=[{k:r.get(k) for k in ('id','name','sort-name','type','country','disambiguation','life-span','score')} for r in response.get('artists',[])]
        put('local_artist_candidates',key,{'rows':candidates,'checked_at':time.time()})
        return {'summary':'Artist candidates ready. Open Link MusicBrainz to review the identity.'}
    artist=mb_get('artist/'+aid)
    # Only an explicit review or existing embedded MBID establishes identity.
    if payload.get('artist_id'):
        for source in row['source_keys']:put('local_artist_links',source,{'artist_id':aid,'name':artist.get('name',''),'checked_at':time.time()})
    refresh_catalog({'artist_id':aid})
    if settings().get('itunes_enrichment',True):
        jobs.progress('Loading supplemental iTunes metadata for '+artist.get('name',row['name']))
        albums=[a['title'] for a in native_view()['rows'] if a['album_id'] in row['album_ids']]
        enrich(artist,albums,payload.get('itunes_id'))
    return {'summary':f'Metadata refreshed for {artist.get("name",row["name"])}. Refresh Library to see changes.'}

def register(app):
    class Refresh(BaseModel):
        artist_key:str=Field(min_length=1,max_length=200)
        artist_id:str=''
        itunes_id:int | None=Field(default=None,gt=0)
    @app.post('/api/library/local/artist-metadata',status_code=202)
    def start(body:Refresh):
        from .api import job_store
        if body.artist_id and not mbid(body.artist_id):raise HTTPException(422,'Enter a valid MusicBrainz artist ID.')
        # iTunes IDs must come from reviewed cached candidates, not arbitrary input.
        if body.itunes_id:
            from .local_library import native_view
            artist=next((a for a in native_view()['artists'] if a['key']==body.artist_key),{})
            extra=repo().load('local_artist_enrichment').get(artist.get('artist_id'),{})
            valid=any(c.get('artistId')==body.itunes_id for c in extra.get('candidates',[]))
            if not valid:raise HTTPException(409,'Search and review an iTunes artist candidate first.')
        store=job_store();return store.get(store.enqueue('artist_metadata',body.model_dump()))
    @app.get('/api/library/local/artist-candidates')
    def candidates(key:str):
        from .local_library import native_view
        artist=next((a for a in native_view()['artists'] if a['key']==key),None)
        if not artist:raise HTTPException(404,'Artist not found')
        rows=repo().load('local_artist_candidates').get(key,{}).get('rows',[])
        supplemental=repo().load('local_artist_enrichment').get(artist['artist_id'],{})
        return {'rows':rows,'itunes':supplemental.get('candidates',[]),'itunes_status':supplemental.get('status','Not loaded')}
