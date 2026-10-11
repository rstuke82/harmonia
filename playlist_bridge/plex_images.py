"""Account-scoped disk cache of image bytes referenced by saved inventory."""
import hashlib
import json
import threading
import time
from pathlib import Path
import requests
from fastapi import HTTPException
from fastapi.responses import Response

_lock=threading.RLock()
_LIMIT=128*1024*1024

def serve(config,path):
    if not str(path).startswith('/') or str(path).startswith('//'):
        raise HTTPException(404,'Plex image unavailable')
    from .api import _health_plex
    from .inventory import identity
    client=_health_plex(config)
    key=hashlib.sha256((identity(client)+'\0'+path).encode()).hexdigest()
    folder=Path(config.repository.directory)/'plex-image-cache'
    target=folder/(key+'.json')
    with _lock:
        folder.mkdir(parents=True,exist_ok=True)
        try:
            meta=json.loads(target.read_text());content=(folder/(key+'.bin')).read_bytes()
            return Response(content,media_type=meta['type'],headers={'Cache-Control':'private, max-age=21600'})
        except (OSError,ValueError,KeyError):pass
    # No global lock held while contacting an external server.
    from .image_download import download
    response=download(client.base_url+path,headers=client.headers,max_bytes=20*1024*1024,oversized_status=502)
    content=response.content;mime=response.headers.get('Content-Type','image/jpeg').split(';')[0]
    if not mime.startswith('image/') or len(content)>20*1024*1024:
        raise HTTPException(502,'Plex did not return a supported image.')
    with _lock:
        # Atomic publication prevents partially written images on concurrent reads.
        import uuid
        tmp=folder/(key+'.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(content);tmp.replace(folder/(key+'.bin'))
        tmp=folder/(key+'.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps({'type':mime,'saved_at':time.time()}));tmp.replace(target)
        files=sorted(folder.glob('*.bin'),key=lambda f:f.stat().st_mtime)
        total=sum(f.stat().st_size for f in files)
        for file in files:
            if total<=_LIMIT:break
            total-=file.stat().st_size;file.unlink(missing_ok=True);file.with_suffix('.json').unlink(missing_ok=True)
    return Response(content,media_type=mime,headers={'Cache-Control':'private, max-age=21600'})
