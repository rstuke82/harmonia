"""Discover artist albums containing a requested recording, not compilation appearances."""
import re
import hashlib
import json
import time
from . import jobs
import unicodedata

def normalized(value):
    return re.sub(r'[^\w]+',' ',unicodedata.normalize('NFKC',str(value or '')).casefold()).strip()

def search_identity(artist,title):
    """Strip explicit guest credits for discovery without editing source metadata."""
    artist=unicodedata.normalize('NFKC',str(artist or '')).strip()
    title=unicodedata.normalize('NFKC',str(title or '')).strip()
    feature=r'(?:feat(?:uring)?\.?|ft\.?)'
    guests=re.search(r'\s*[\[(]\s*'+feature+r'\s+[^\])]+[\])]',title,re.I)
    if guests:
        title=re.sub(r'\s*[\[(]\s*'+feature+r'\s+[^\])]+[\])]', '',title,flags=re.I).strip()
        artist=artist.split(',',1)[0].strip()
    artist=re.sub(r'\s+'+feature+r'\s+.+$','',artist,flags=re.I).strip()
    title=re.sub(r'\s+'+feature+r'\s+.+$','',title,flags=re.I).strip()
    return artist,title

def containing_albums(repo, records, request, preferences, get, publish=None):
    # Search results embed only a limited selection of releases. Browse actual
    # containing releases and retain their credits and release-group labels.
    artist,title=search_identity(request.artist,request.title)
    song=normalized(title);artist=normalized(artist)
    selected=[]
    for recording in records:
        credits=recording.get('artist-credit',[])
        if normalized(recording.get('title'))!=song:continue
        if artist and not any(artist in {normalized(c.get('name')),normalized(c.get('artist',{}).get('name'))} for c in credits if isinstance(c,dict)):continue
        selected.append(recording)
    result=[]
    for index,recording in enumerate(selected):
        jobs.progress(f"Finding containing albums · recording {index+1} of {len(selected)}",completed=index,total=len(selected))
        if not recording.get('id'):continue
        releases=[]
        for offset in range(0,1000,100):
            jobs.progress(f'Finding containing albums · recording {index+1} of {len(selected)} · releases {offset+1}–{offset+100}')
            params={'recording':recording['id'],'inc':'release-groups+artist-credits','limit':100,'offset':offset}
            cache_key='browse:'+hashlib.sha256(json.dumps([params,preferences.get('server_url'),preferences.get('capabilities')],sort_keys=True).encode()).hexdigest()
            from .lidarr import state_get,state_put
            cached=state_get(repo,'musicbrainz_cache',cache_key) if repo else None
            if not getattr(request,'force_refresh',False) and cached and time.time()-cached['at']<preferences.get('cache_days',7)*86400:
                page=cached['page']
            else:
                page=get(repo,'release',params)
                if repo:
                    state_put(repo,'musicbrainz_cache',cache_key,{'at':time.time(),'page':page})
                    with repo.connect() as db:
                        db.execute("DELETE FROM state WHERE namespace='musicbrainz_cache' AND key NOT IN (SELECT key FROM state WHERE namespace='musicbrainz_cache' ORDER BY json_extract(value,'$.at') DESC LIMIT 500)")
            batch=page.get('releases',[]);releases.extend(batch)
            if len(batch)<100 or offset+len(batch)>=page.get('release-count',float('inf')):break
        else:raise ValueError('Too many containing releases; specify an album to narrow this search.')
        accepted=[]
        for release in releases:
            group=release.get('release-group',{})
            secondary={str(t).casefold() for t in group.get('secondary-types',[])}
            credits=group.get('artist-credit') or release.get('artist-credit') or []
            own=bool(artist) and any(artist in {normalized(c.get('name')),normalized(c.get('artist',{}).get('name'))} for c in credits if isinstance(c,dict))
            other=not own or bool(secondary & {'compilation','live','remix'})
            accepted.append({**release,'release-group':{**group,'artist-credit':credits,'other-release':other}})
        result.append({**recording,'releases':accepted})
        if publish:publish(list(result))
    return result
