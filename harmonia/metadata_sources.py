"""Server-wide display metadata preferences and reviewed artist links."""
import re
import time
from typing import Literal
from urllib.parse import urlsplit
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

SOURCE_DEFAULTS = {
    'artist_description':['lastfm','musicbrainz'],'artist_portrait':['plex','fanart','apple'],
    'artist_hero':['plex','fanart'],'genres':['apple','musicbrainz','lastfm'],
    'album_artwork':['plex','coverartarchive','fanart','apple'],'release_date':['musicbrainz','apple'],
    'popular_tracks':['lastfm'],
}

class Preferences(BaseModel):
    artist_identity: Literal['musicbrainz'] = 'musicbrainz'
    album_identity: Literal['auto','lidarr','musicbrainz'] = 'auto'
    artist_description: Literal['auto','lastfm','musicbrainz','none'] = 'auto'
    artist_portrait: Literal['auto','apple','fanart','plex','none'] = 'auto'
    artist_hero: Literal['auto','plex','fanart','apple','none'] = 'auto'
    genres: Literal['auto','apple','musicbrainz','lastfm','none'] = 'auto'
    album_artwork: Literal['auto','plex','fanart','apple','coverartarchive','none'] = 'auto'
    release_date: Literal['auto','musicbrainz','apple','none'] = 'auto'
    popular_tracks: Literal['auto','lastfm','none'] = 'auto'
    source_order: dict[str,list[str]] = Field(default_factory=dict)
    @model_validator(mode='after')
    def valid_order(self):
        cleaned={}
        for field,default in SOURCE_DEFAULTS.items():
            values=['fanart' if value=='lidarr' and field in ('artist_portrait','artist_hero','album_artwork') else value for value in self.source_order.get(field,[])]
            cleaned[field]=list(dict.fromkeys(v for v in values if v in default))
        self.source_order=cleaned
        return self

def settings(repo):
    return Preferences(**repo.load('metadata_sources').get('settings',{})).model_dump()

def order(prefs,field):
    if field in ('artist_portrait','artist_hero','album_artwork'):
        configured=prefs.get('source_order',{}).get(field)
        if not configured:
            selected=prefs.get(field,'auto')
            configured=[] if selected=='none' else [selected] if selected!='auto' and selected in SOURCE_DEFAULTS[field] else SOURCE_DEFAULTS[field]
        return ['plex']+[value for value in configured if value!='plex']
    if field in prefs.get('source_order',{}) and prefs['source_order'][field]:return prefs['source_order'][field]
    selected=prefs.get(field,'auto')
    if selected=='none':return []
    return [selected] if selected!='auto' and selected in SOURCE_DEFAULTS[field] else SOURCE_DEFAULTS[field]

def effective_mbid(repo, artist):
    return repo.load('library_artist_links').get(str(artist.get('id')),{}).get('musicbrainz_id') or artist.get('mbid','')

def spotify_relations(relations):
    """Return normalized Spotify artist destinations from MusicBrainz URL relations."""
    rows=[];seen=set()
    for relation in relations or []:
        resource=str((relation.get('url') or {}).get('resource') or '')
        parsed=urlsplit(resource)
        match=re.fullmatch(r'/artist/([A-Za-z0-9]+)',parsed.path.rstrip('/')) if parsed.hostname in ('open.spotify.com','www.open.spotify.com') else None
        if not match or match.group(1) in seen:continue
        seen.add(match.group(1));rows.append((match.group(1),'https://open.spotify.com/artist/'+match.group(1)))
    return rows

def register(app):
    from .accounts import actor,root_repository
    from .lidarr import state_put,mb_quote
    from .local_library import mb_get
    from .artist_library import itunes_get,normalize,apple_url
    def admin():
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
    def artist(artist_id):
        from .library import cached_library
        row=cached_library()['_artists_by_id'].get(str(artist_id))
        if not row:raise HTTPException(404,'Artist is not in Harmonia Library.')
        return row
    def plex_inventory():
        from .library import cached_library
        model=cached_library()
        return {**model['_plex'],'rows':list(model['_plex_by_id'].values())}
    def plex_artists(plex):
        grouped={}
        for track in plex.get('rows',[]):
            artist_id=str(track.get('plex_artist_id') or '')
            if not artist_id:continue
            value=grouped.setdefault(artist_id,{'id':artist_id,'name':track.get('album_artist') or track.get('artist') or 'Unknown artist',
                'thumb':track.get('plex_artist_thumb') or '','art':track.get('plex_artist_art') or '','track_count':0,'albums':set()})
            value['track_count']+=1
            if track.get('album'):value['albums'].add(str(track['album']))
            if not value['thumb'] and track.get('plex_artist_thumb'):value['thumb']=track['plex_artist_thumb']
            if not value['art'] and track.get('plex_artist_art'):value['art']=track['plex_artist_art']
        result=[]
        for value in grouped.values():
            albums=value.pop('albums');result.append({**value,'album_count':len(albums)})
        return result
    @app.get('/api/settings/metadata-sources')
    def get_settings():
        admin();repo=root_repository();lastfm=repo.load('lastfm').get('settings',{})
        from .lidarr import config as lidarr_config
        value=settings(repo)
        for field in SOURCE_DEFAULTS:
            if not value['source_order'].get(field):value['source_order'][field]=order(value,field)
        from .fanart import configured as fanart_configured
        return value|{'availability':{'musicbrainz':True,'apple':True,'plex':True,'lastfm':bool(lastfm.get('api_key')),'fanart':fanart_configured(repo)},
            'priorities':{'artist_description':'Last.fm → MusicBrainz annotation','artist_portrait':'Plex → Fanart.tv → Apple Music','artist_hero':'Plex → Fanart.tv background → solid color','genres':'Apple Music → MusicBrainz → Last.fm','album_artwork':'Plex → Cover Art Archive → Fanart.tv → Apple Music','release_date':'MusicBrainz → Apple Music'}}
    @app.put('/api/settings/metadata-sources')
    def save_settings(value:Preferences):
        admin();state_put(root_repository(),'metadata_sources','settings',value.model_dump());return get_settings()
    @app.get('/api/library/artists/{artist_id}/links')
    def links(artist_id:str):
        admin();row=artist(artist_id);repo=root_repository();saved=repo.load('library_artist_links').get(str(artist_id),{})
        mbid=saved.get('musicbrainz_id') or row.get('mbid','');extra=repo.load('local_artist_enrichment').get(mbid,{}) if mbid else {}
        plex=plex_inventory();plex_rows=plex_artists(plex);saved_plex=str(saved.get('plex_artist_id') or '')
        automatic=next((r for r in plex_rows if normalize(r['name'])==normalize(row.get('name',''))),{})
        selected=next((r for r in plex_rows if r['id']==saved_plex),{}) if saved_plex else automatic
        from .file_tools import plex_url
        return {'artist_id':str(artist_id),'name':row.get('name',''),'lidarr_mbid':row.get('mbid',''),'musicbrainz_id':mbid,
                'musicbrainz_source':'Harmonia override' if saved.get('musicbrainz_id') else 'Lidarr',
                'itunes_id':saved.get('itunes_id') or extra.get('itunes_id'),'itunes_url':saved.get('itunes_url') or extra.get('artist_url',''),
                'spotify_url':saved.get('spotify_url',''),'plex_artist_id':saved_plex or selected.get('id',''),
                'plex_artist_name':selected.get('name') or saved.get('plex_artist_name',''),
                'plex_url':plex_url(plex,selected.get('id')) if selected else '',
                'plex_source':'Harmonia override' if saved_plex else ('Automatic name match' if selected else ''),
                'plex_available':bool(selected)}
    class Search(BaseModel):query:str=Field(default='',max_length=300)
    @app.post('/api/library/artists/{artist_id}/links/musicbrainz/search')
    def search_mb(artist_id:str,body:Search):
        admin();row=artist(artist_id);result=mb_get('artist',{'query':'artist:'+mb_quote(body.query or row.get('name','')),'limit':25})
        return [{k:r.get(k) for k in ('id','name','type','country','disambiguation','score')} for r in result.get('artists',[])]
    @app.post('/api/library/artists/{artist_id}/links/apple/search')
    def search_apple(artist_id:str,body:Search):
        admin();row=artist(artist_id);result=itunes_get('search',{'term':body.query or row.get('name',''),'media':'music','entity':'musicArtist','limit':25})
        rows=[{k:r.get(k) for k in ('artistId','artistName','primaryGenreName','artistLinkUrl','artistViewUrl')} for r in result.get('results',[]) if r.get('artistId')]
        state_put(root_repository(),'library_artist_link_candidates',str(artist_id),{'checked_at':time.time(),'apple_ids':[r['artistId'] for r in rows]})
        return rows
    @app.post('/api/library/artists/{artist_id}/links/spotify/search')
    def search_spotify(artist_id:str,body:Search):
        """Discover reviewed Spotify artist links through MusicBrainz URL relations."""
        admin();row=artist(artist_id);repo=root_repository();query=body.query or row.get('name','')
        candidates=[];seen_mbids=set()
        current=effective_mbid(repo,row)
        if current:candidates.append({'id':current,'name':row.get('name',''),'score':100})
        result=mb_get('artist',{'query':'artist:'+mb_quote(query),'limit':6})
        candidates.extend(result.get('artists',[]))
        rows=[];spotify_urls={}
        for candidate in candidates:
            mbid=str(candidate.get('id',''))
            if not mbid or mbid in seen_mbids:continue
            seen_mbids.add(mbid)
            try:hydrated=mb_get('artist/'+mbid,{'inc':'url-rels'})
            except Exception:continue
            for spotify_id,url in spotify_relations(hydrated.get('relations',[])):
                if spotify_id in spotify_urls:continue
                spotify_urls[spotify_id]=url
                rows.append({'id':spotify_id,'name':hydrated.get('name') or candidate.get('name') or query,
                    'url':url,'country':hydrated.get('country') or candidate.get('country'),
                    'disambiguation':hydrated.get('disambiguation') or candidate.get('disambiguation'),
                    'score':candidate.get('score')})
        saved_candidates=repo.load('library_artist_link_candidates').get(str(artist_id),{})
        state_put(repo,'library_artist_link_candidates',str(artist_id),{**saved_candidates,'checked_at':time.time(),'spotify_urls':spotify_urls})
        return rows
    @app.post('/api/library/artists/{artist_id}/links/plex/search')
    def search_plex(artist_id:str,body:Search):
        admin();row=artist(artist_id);query=normalize(body.query or row.get('name',''))
        from difflib import SequenceMatcher
        values=[]
        for candidate in plex_artists(plex_inventory()):
            name=normalize(candidate['name'])
            score=round(SequenceMatcher(None,query,name).ratio()*100) if query else 100
            if query and query not in name and name not in query and score<50:continue
            values.append({**candidate,'match_score':score})
        values.sort(key=lambda value:(normalize(value['name'])!=query,-value['match_score'],normalize(value['name']),-value['track_count']))
        return values[:50]
    class LinkUpdate(BaseModel):
        provider:Literal['musicbrainz','apple','spotify','plex']
        value:str=Field(min_length=1,max_length=300)
        url:str=Field(default='',max_length=1000)
    @app.put('/api/library/artists/{artist_id}/links')
    def save_link(artist_id:str,body:LinkUpdate):
        admin();artist(artist_id);repo=root_repository();saved=repo.load('library_artist_links').get(str(artist_id),{})
        if body.provider=='musicbrainz':
            if not re.fullmatch(r'[0-9a-fA-F-]{36}',body.value):raise HTTPException(422,'Choose a valid MusicBrainz artist.')
            value=mb_get('artist/'+body.value)
            if str(value.get('id','')).lower()!=body.value.lower():raise HTTPException(409,'MusicBrainz artist could not be verified.')
            saved['musicbrainz_id']=value['id']
        elif body.provider=='apple':
            try:value=int(body.value)
            except ValueError:raise HTTPException(422,'Choose a valid Apple Music artist.')
            valid=repo.load('library_artist_link_candidates').get(str(artist_id),{}).get('apple_ids',[])
            if value not in valid:raise HTTPException(409,'Search again and choose an Apple Music result.')
            saved.update(itunes_id=value,itunes_url=apple_url(body.url))
        elif body.provider=='spotify':
            from urllib.parse import urlsplit
            parsed=urlsplit(body.url or body.value)
            if parsed.scheme!='https' or parsed.hostname not in ('open.spotify.com','www.open.spotify.com') or not re.fullmatch(r'/artist/[A-Za-z0-9]+/?',parsed.path):
                raise HTTPException(422,'Choose a valid Spotify artist.')
            url='https://open.spotify.com'+parsed.path.rstrip('/')
            candidates=repo.load('library_artist_link_candidates').get(str(artist_id),{}).get('spotify_urls',{})
            # Search selections are verified against the most recent result.
            # Complete URLs remain accepted for older clients.
            if candidates and candidates.get(body.value)!=url:raise HTTPException(409,'Search again and choose a Spotify result.')
            saved['spotify_url']=url
        else:
            candidate=next((r for r in plex_artists(plex_inventory()) if r['id']==body.value),None)
            if not candidate:raise HTTPException(409,'That Plex artist is no longer in the saved inventory. Scan Plex and search again.')
            saved.update(plex_artist_id=candidate['id'],plex_artist_name=candidate['name'])
        saved['reviewed_at']=time.time();state_put(repo,'library_artist_links',str(artist_id),saved)
        if body.provider=='plex':
            from .artwork_sync import queue_pending
            queue_pending(repo)
        return links(artist_id)
    @app.delete('/api/library/artists/{artist_id}/links/{provider}')
    def remove_link(artist_id:str,provider:Literal['musicbrainz','apple','spotify','plex']):
        admin();artist(artist_id);repo=root_repository();saved=repo.load('library_artist_links').get(str(artist_id),{})
        if provider=='musicbrainz':saved.pop('musicbrainz_id',None)
        elif provider=='apple':
            saved.pop('itunes_id',None);saved.pop('itunes_url',None)
        elif provider=='spotify':saved.pop('spotify_url',None)
        else:
            saved.pop('plex_artist_id',None);saved.pop('plex_artist_name',None)
        state_put(repo,'library_artist_links',str(artist_id),saved);return links(artist_id)
