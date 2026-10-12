"""Serve saved Library covers; unavailable Plex images may use saved alternatives."""
import hashlib
import json
import re
from urllib.parse import quote,urlsplit
import requests
from fastapi import HTTPException,Response

def url(album_id,candidates,selected_at=None,target='albums',kind=''):
    if not candidates:return ''
    stamp=hashlib.sha256(json.dumps([candidates,selected_at],sort_keys=True).encode()).hexdigest()[:16]
    return '/api/library/'+target+'/'+quote(str(album_id),safe='')+'/artwork?v='+stamp+('&kind='+kind if kind else '')

def serve(config,candidates,external_only=False):
    from .image_download import download
    from .library_inventory import indexed
    from .plex_images import serve as plex_image
    for source in candidates:
        if not source:continue
        if source.startswith('/api/library/plex-albums/'):
            if external_only:continue
            match=re.fullmatch(r'/api/library/plex-albums/(\d+)/image',urlsplit(source).path)
            if not match:continue
            row=indexed(config)['plex_albums'].get(match[1])
            # Never fetch a rating key outside this account's saved Library.
            if not row:raise HTTPException(404,'Album artwork unavailable')
            path=row.get('plex_album_thumb')
            if not path:continue
            try:return plex_image(config,path)
            except HTTPException as exc:
                if exc.status_code!=404:raise
            continue
        if not source.startswith(('https://','http://')):continue
        try:
            response=download(source,max_bytes=20*1024*1024,oversized_status=502,external=True)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (404,410):continue
            raise HTTPException(502,'Artwork source temporarily unavailable',headers={'Retry-After':'2'}) from None
        except requests.RequestException:
            raise HTTPException(502,'Artwork source temporarily unavailable',headers={'Retry-After':'2'}) from None
        media_type=response.headers.get('Content-Type','').split(';',1)[0].lower()
        if not media_type.startswith('image/') or media_type=='image/svg+xml':
            raise HTTPException(502,'Artwork source returned an invalid image')
        # A fallback is display-only: never overwrite the chosen cover or send it to Plex.
        return Response(response.content,media_type=media_type,headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})
    raise HTTPException(404,'Album artwork unavailable')
