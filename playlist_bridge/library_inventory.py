"""Revision-scoped inventory indexes for Library preparation and image lookup."""
import hashlib
import json
import threading
from collections import OrderedDict

_cache=OrderedDict()
_lock=threading.RLock()


def clear():
    with _lock:_cache.clear()


def indexed(config):
    from .accounts import actor,root_repository
    from .inventory import current
    from .saved_views import revision
    from .lidarr import config as lidarr_config
    from .lidarr_requests import server_id
    root=root_repository();repo=config.repository;user=actor() or {}
    credentials=hashlib.sha256(json.dumps(config.config.get('plex',{}),sort_keys=True).encode()).hexdigest()
    cfg=lidarr_config(root)
    key=(str(repo.directory),str(root.directory),user.get('id',''),credentials,server_id(cfg))
    stamp=(revision(repo,{'inventory','library_artist_links','plex_album_links'}),revision(root,{'inventory','library_artist_links','plex_album_links'}))
    with _lock:
        entry=_cache.get(key)
        if entry and entry[0]==stamp:
            _cache.move_to_end(key);return entry[1]
        plex,lidarr=current(repo,config)
        by_album={};by_artist={};by_name={};by_identity={};ambiguous_names=set();ambiguous_albums=set()
        for row in plex.get('rows',[]):
            aid=str(row.get('plex_artist_id') or '');album=str(row.get('plex_album_id') or '')
            name=str(row.get('album_artist') or row.get('artist') or '').casefold()
            if aid:by_artist.setdefault(aid,row)
            if name:
                if name in by_name and str(by_name[name].get('plex_artist_id'))!=aid:ambiguous_names.add(name)
                by_name.setdefault(name,row)
            if album:
                by_album.setdefault(album,row)
                identity=(name,str(row.get('album') or '').casefold())
                if identity in by_identity and str(by_identity[identity].get('plex_album_id'))!=album:ambiguous_albums.add(identity)
                by_identity.setdefault(identity,row)
        for name in ambiguous_names:by_name.pop(name,None)
        for identity in ambiguous_albums:by_identity.pop(identity,None)
        p=config.config.get('plex',{})
        scope=server_id(cfg)+':'+hashlib.sha256((p.get('url','').rstrip('/')+'|'+str(p.get('music_library_key',''))).encode()).hexdigest()
        data={'plex':plex,'lidarr':lidarr,'plex_albums':by_album,'plex_artists':by_artist,'plex_artist_names':by_name,'plex_album_names':by_identity,
              'albums':{str(a.get('album_id')):a for a in lidarr.get('rows',[])},'artists':{str(a.get('id')):a for a in lidarr.get('artists',[])},
              'artist_links':root.load('library_artist_links'),'album_links':root.load_one('plex_album_links',scope,{})}
        _cache[key]=(stamp,data);_cache.move_to_end(key)
        while len(_cache)>4:_cache.popitem(last=False)
        return data
