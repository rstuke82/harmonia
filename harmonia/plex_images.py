"""Account-scoped artwork cache with bounded stale-thumbnail recovery."""
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from . import network
from fastapi import HTTPException
from fastapi.responses import Response

_lock=threading.RLock()
_LIMIT=128*1024*1024
_missing={}
_generation=0


def invalidate(repo):
    """Explicit artwork choices retire cached bytes, including unchanged URLs."""
    global _generation
    folder=Path(repo.directory)/'plex-image-cache'
    with _lock:
        _generation+=1
        if folder.exists():
            for file in folder.iterdir():
                if file.suffix in ('.bin','.json'):file.unlink(missing_ok=True)
        _missing.clear()


def _recover(config,client,path):
    match=re.fullmatch(r'/library/metadata/(\d+)/(thumb|art)(?:/[^/?]+)?',path)
    if not match:return None
    rating,field=match.groups()
    snapshot=config.repository.load_one('inventory','plex',{})
    id_key='plex_album_id' if any(str(row.get('plex_album_id'))==rating for row in snapshot.get('rows',[])) else 'plex_artist_id'
    path_key='plex_album_thumb' if id_key=='plex_album_id' else 'plex_artist_art' if field=='art' else 'plex_artist_thumb'
    if id_key=='plex_album_id' and field!='thumb':return None
    if not any(str(row.get(id_key))==rating for row in snapshot.get('rows',[])):return None
    if snapshot.get('machine_identifier') and snapshot['machine_identifier']!=client.machine_identifier:return None
    from .image_download import download
    try:
        response=download(client.base_url+'/library/metadata/'+rating,headers=client.headers,max_bytes=2*1024*1024,oversized_status=502)
        items=json.loads(response.content).get('MediaContainer',{}).get('Metadata',[])
    except network.HTTPError as exc:
        if exc.response is not None and exc.response.status_code==404:return None
        raise
    except (ValueError,TypeError):return None
    item=next((item for item in items if str(item.get('ratingKey'))==rating),{})
    updated=str(item.get(field) or '')
    # A server response cannot redirect credentials or change the authorized ID.
    if not re.fullmatch(r'/library/metadata/'+rating+'/'+field+r'(?:/[^/?]+)?',updated) or updated==path:return None
    with config.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        saved=db.execute("SELECT value FROM state WHERE namespace='inventory' AND key='plex'").fetchone()
        if not saved:return None
        value=json.loads(saved[0])
        if value.get('machine_identifier')!=snapshot.get('machine_identifier'):return None
        changed=False
        for row in value.get('rows',[]):
            if str(row.get(id_key))==rating and row.get(path_key)==path:
                row[path_key]=updated;changed=True
        if changed:db.execute("UPDATE state SET value=? WHERE namespace='inventory' AND key='plex'",(json.dumps(value),))
    if changed:
        from .library import invalidate as invalidate_library
        invalidate_library()
    if not changed:
        # A newer cover may have been selected during this metadata request.
        return next((str(row.get(path_key)) for row in value.get('rows',[]) if str(row.get(id_key))==rating and re.fullmatch(r'/library/metadata/'+rating+'/'+field+r'(?:/[^/?]+)?',str(row.get(path_key) or ''))),None)
    return updated


def serve(config,path):
    if not str(path).startswith('/') or str(path).startswith('//'):
        raise HTTPException(404,'Plex image unavailable')
    from .api import _health_plex
    from .inventory import identity
    from .image_download import download
    client=_health_plex(config)
    key=hashlib.sha256((identity(client)+'\0'+path).encode()).hexdigest()
    folder=Path(config.repository.directory)/'plex-image-cache';target=folder/(key+'.json')
    with _lock:
        generation=_generation
        if _missing.get(key,0)>time.monotonic():raise HTTPException(404,'Plex image unavailable')
        folder.mkdir(parents=True,exist_ok=True)
        try:
            meta=json.loads(target.read_text())
            if time.time()-float(meta.get('saved_at',0))<21600:
                return Response((folder/(key+'.bin')).read_bytes(),media_type=meta['type'],headers={'Cache-Control':'private, max-age=21600'})
        except (OSError,ValueError,KeyError,TypeError):pass
    try:
        try:response=download(client.base_url+path,headers=client.headers,max_bytes=20*1024*1024,oversized_status=502)
        except network.HTTPError as exc:
            if exc.response is None or exc.response.status_code!=404:raise
            updated=_recover(config,client,path)
            if not updated:raise HTTPException(404,'Plex image unavailable') from None
            response=download(client.base_url+updated,headers=client.headers,max_bytes=20*1024*1024,oversized_status=502)
    except network.HTTPError as exc:
        if exc.response is not None and exc.response.status_code==404:
            with _lock:
                _missing[key]=time.monotonic()+30
                while len(_missing)>256:_missing.pop(next(iter(_missing)))
            raise HTTPException(404,'Plex image unavailable') from None
        raise HTTPException(502,'Plex artwork is temporarily unavailable.',headers={'Retry-After':'2'}) from None
    except network.RequestException:
        raise HTTPException(502,'Plex artwork is temporarily unavailable.',headers={'Retry-After':'2'}) from None
    except HTTPException as exc:
        if exc.status_code==404:
            with _lock:
                _missing[key]=time.monotonic()+30
                while len(_missing)>256:_missing.pop(next(iter(_missing)))
        raise
    content=response.content;mime=response.headers.get('Content-Type','image/jpeg').split(';')[0]
    if not mime.startswith('image/') or len(content)>20*1024*1024:raise HTTPException(502,'Plex did not return a supported image.')
    with _lock:
        if generation!=_generation:
            return Response(content,media_type=mime,headers={'Cache-Control':'private, no-store'})
        import uuid
        tmp=folder/(key+'.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(content);tmp.replace(folder/(key+'.bin'))
        tmp=folder/(key+'.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps({'type':mime,'saved_at':time.time(),'path':path}));tmp.replace(target)
        files=sorted(folder.glob('*.bin'),key=lambda f:f.stat().st_mtime)
        total=sum(f.stat().st_size for f in files)
        for file in files:
            if total<=_LIMIT:break
            total-=file.stat().st_size;file.unlink(missing_ok=True);file.with_suffix('.json').unlink(missing_ok=True)
    return Response(content,media_type=mime,headers={'Cache-Control':'private, max-age=21600'})
