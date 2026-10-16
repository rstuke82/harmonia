"""Read-only comparison of the last successful server inventories."""
import hashlib
import json
import weakref
import threading
import time
from collections import OrderedDict
from fastapi import HTTPException
from .saved_views import saved, LIBRARY
from .library_inventory import indexed
from .artwork_sync import image_url,source as plex_art_source
from .album_apple_artwork import artwork as apple_album_artwork


_view_cache=OrderedDict()
_view_loading=weakref.WeakValueDictionary()
_view_epoch=0
_view_lock=threading.Lock()
_VIEW_NAMESPACES={
    'library_views_epoch','library_album_apple_artwork','inventory','local_artist_enrichment','library_artist_catalog',
    'metadata_sources','artwork_overrides','fanart_cache','file_audit','file_issue_reviews',
    'library_artist_metadata','library_lastfm_artist_metadata','library_artist_links',
    'library_album_service_links','musicbrainz_settings','blocked_albums','blocked_artists','file_tools','track_links','plex_album_links','fanart','lidarr',
}


def _revision(repo):
    from .saved_views import revision
    return revision(repo,_VIEW_NAMESPACES)


def invalidate():
    """Drop prepared Library views after inventory or metadata mutations."""
    global _view_epoch
    with _view_lock:
        _view_cache.clear()
        _view_epoch+=1
    from .library_inventory import clear
    clear()


def compare(plex, lidarr):
    from .matching import Matcher
    norm = Matcher._normalize_match_text
    def key(artist, album):
        return (norm(artist or ''), norm(album or ''))
    groups = {}
    for track in plex.get('rows', []):
        k = key(track.get('album_artist') or track.get('artist'), track.get('album'))
        groups.setdefault(k, []).append(track)
    catalog = {}
    for album in lidarr.get('rows', []):
        catalog.setdefault(key(album.get('artist'), album.get('title')), []).append(album)
    complete = bool(plex.get('checked_at') and lidarr.get('checked_at'))
    result = []
    for k in sorted(groups.keys() | catalog.keys()):
        tracks, albums = groups.get(k, []), catalog.get(k, [])
        first = albums[0] if albums else {}
        title = first.get('title') or (tracks[0].get('album') if tracks else '') or 'Unknown album'
        artist = first.get('artist') or (tracks[0].get('album_artist') or tracks[0].get('artist') if tracks else '')
        if not complete:
            status, reason = 'pending', 'Waiting for both library scans'
        elif not all(k) or len(albums) > 1:
            status, reason = 'review', 'Album identity needs review'
        elif tracks and not albums:
            status, reason = 'attention', 'Missing from Lidarr — review the album identity'
        elif not tracks:
            status, reason = 'lidarr_only', 'Not yet available in Plex'
        else:
            status, reason = 'linked', 'Linked by artist and album name'
        expected = max((int(a.get('statistics', {}).get('trackCount') or 0) for a in albums), default=0)
        unique = len({str(t.get('plex_id')) for t in tracks})
        result.append({'id': hashlib.sha256(repr(k).encode()).hexdigest(), 'artist': artist, 'album': title,
                       'status': status, 'reason': reason, 'plex_tracks': unique, 'expected_tracks': expected or None,
                       'completeness': 'unknown' if not expected else ('incomplete' if unique < expected else 'count_met'),
                       'lidarr_albums': albums, 'tracks': tracks})
    return {'rows': result, 'scans_complete': complete, 'plex_checked_at': plex.get('checked_at'),
            'lidarr_checked_at': lidarr.get('checked_at'),
            'note': 'Links use exact normalized artist and album names. Different editions may need review; matching counts do not prove identical recordings.'}


def issues(ctx,plex,identities=None):
    """Inventory evidence only: multiple recordings/files are candidates, not proven copies."""
    from collections import defaultdict
    from .track_bridge import path
    from .file_tools import mapped,settings,plex_url
    cfg=settings();result=[];paths=defaultdict(list);recordings=defaultdict(list)
    lidarr_objects={id(t) for t in ctx['tracks']}
    for p in plex.get('rows',[]):
        for f in set(p.get('files',[])):paths[path(f)].append(p)
    def add(kind,t,reason,files=None):
        plex_id=ctx['links'].get(str(t.get('id')),{}).get('plex_id') if id(t) in lidarr_objects else t.get('plex_id')
        result.append({**t,'artist':t.get('album_artist') or t.get('artist',''),'stale':bool(ctx.get('pending') or time.time()-plex.get('checked_at',0)>86400),'issue':kind,'reason':reason,'files':files or [],'issue_id':str(len(result)),
                       'plex_url':plex_url(plex,plex_id) if plex_id else None})
    for f,ps in paths.items():
        album_groups={}
        for p in ps:
            album_key=identities.resolve(p,p.get('files',[]))['album_key'] if identities else (str(p.get('album_artist') or p.get('artist') or '').casefold(),str(p.get('album') or '').casefold())
            album_groups.setdefault(album_key,[]).append(p)
        for same in album_groups.values():
            if len({str(p['plex_id']) for p in same})>1:
                add('Duplicate Plex entries',same[0],'Multiple Plex items reference the same file path. Review the duplicate entries in Plex.',[f])
    for t in ctx['tracks']:
        file=t.get('lidarr_file',t.get('file',''))
        if not file:continue
        if cfg.get('audit_enabled') and not mapped(file,config=cfg):add('Unmapped path',t,'Lidarr has an imported file, but no Harmonia folder mapping is configured.',[file])
        if str(t['id']) not in ctx['links']:add('Link needs review',t,'Imported in Lidarr; no unique playable Plex association was found.',[file])
        if t.get('recording_id'):recordings[(t.get('album_id'),t['recording_id'])].append(t)
    for ts in recordings.values():
        files=sorted({t.get('lidarr_file',t.get('file','')) for t in ts})
        if len(files)>1:add('Possible duplicate files',ts[0],'The same recording in this album has multiple file paths. These may be different editions or intentional copies; review before changing anything.',files)
    if not ctx.get('pending') and plex.get('checked_at') and time.time()-plex['checked_at']<86400:
        linked={str(v['plex_id']) for v in ctx['links'].values()}
        for t in plex.get('rows',[]):
            if str(t['plex_id']) not in linked:add('Plex-only / unlinked',t,'No Lidarr track association was found. Review identity and path mappings before treating this as unmanaged.',t.get('files',[]))
    return result


def read():
    from .api import _config
    from .track_bridge import context
    config=_config(read_only=True,namespaces=[])
    from .library_inventory import indexed
    inventory=indexed(config);plex=inventory['plex'];lidarr=inventory['lidarr']
    from .lidarr import config as lidarr_config
    from .accounts import root_repository
    if not lidarr_config(root_repository()).get('enabled'):
        lidarr={}
        ctx={'tracks':[],'links':{},'pending':False}
    else:
        ctx=context(config,plex.get('rows',[]),inventory=lidarr) or {'tracks':[],'links':{},'pending':True}
    return config,plex,lidarr,ctx

def can_issues():
    from .accounts import actor
    user=actor() or {}
    return bool(user.get('admin') or user.get('can_library_issues'))


def public_library(value):
    """Remove server paths and file-audit evidence from member browsing."""
    hidden={'path','rootFolderPath','local_path','lidarr_file','file','files','file_audit_status','file_audit_checked_at','existing_path','expected_path','roots','relative_path','lidarr_path','plex_path'}
    if isinstance(value,dict):return {k:public_library(v) for k,v in value.items() if k not in hidden}
    if isinstance(value,list):return [public_library(v) for v in value]
    return value

def build_library():
    from .accounts import actor
    started=time.monotonic()
    config,plex,lidarr,ctx=read()
    rows=[]
    from .accounts import root_repository
    extras=root_repository().load('local_artist_enrichment')
    catalogs=root_repository().load('library_artist_catalog')
    from .album_catalog import catalog_rows
    from .metadata_sources import settings as metadata_preferences
    link_overrides=root_repository().load('library_artist_links')
    linked_artists=[{**a,'mbid':link_overrides.get(str(a.get('id')),{}).get('musicbrainz_id') or a.get('mbid','')} for a in lidarr.get('artists',[])]
    linked_by_id={str(a.get('id')):a.get('mbid','') for a in linked_artists}
    linked_rows=[{**r,'artist_mbid':linked_by_id.get(str(r.get('artist_id')),r.get('artist_mbid',''))} for r in lidarr.get('rows',[])]
    from .user_preferences import normalize
    preference_repo=config.repository
    blocked_artists=list(preference_repo.load('blocked_artists').values());blocked_albums=list(preference_repo.load('blocked_albums').values())
    def is_blocked(row):
        credits=row.get('artists') or [{'name':row.get('artist',''),'mbid':row.get('artist_mbid','')}]
        artist_hit=any((rule.get('mbid') and rule['mbid']==credit.get('mbid')) or normalize(rule.get('name'))==normalize(credit.get('name','')) for rule in blocked_artists for credit in credits)
        row_mbid=row.get('mbid') or row.get('musicbrainz_id') or row.get('album_id');row_title=row.get('album') or row.get('title')
        album_hit=any((rule.get('mbid') and rule['mbid']==row_mbid) or (not rule.get('mbid') and normalize(rule.get('artist'))==normalize(row.get('artist')) and normalize(rule.get('album'))==normalize(row_title)) for rule in blocked_albums)
        return artist_hit or album_hit
    artwork_index=indexed(config)
    from .artwork_sync import source as plex_art_source,image_url
    sources=[row for row in catalog_rows(linked_rows,linked_artists,catalogs,extras) if not is_blocked(row)]
    prefs=metadata_preferences(root_repository());artwork_overrides=root_repository().load('artwork_overrides');fanart_cache=root_repository().load('fanart_cache')
    from .fanart import album_images as fanart_album_images,artist_images as fanart_artist_images
    from .metadata_sources import order as metadata_order
    album_art_order=metadata_order(prefs,'album_artwork');date_order=metadata_order(prefs,'release_date')
    apple_by_artist={mbid:{str(a.get('title','')).casefold():a for a in value.get('albums',[])} for mbid,value in extras.items()}
    dates_by_artist={mbid:{str(r.get('id')):str(r.get('first_release_date',''))[:4] for r in value.get('rows',[])} for mbid,value in catalogs.items()}
    fanart_albums={}
    artwork_sources={}
    artist_artwork_sources={}
    from .library_artwork import url as cover_url
    # Read reviewed Apple links once, rather than opening SQLite per release.
    apple_links=root_repository().load('library_album_service_links')
    apple_artwork=root_repository().load('library_album_apple_artwork')
    for row in sources:
        original_artwork=row.get('artwork','')
        mbid=row.get('artist_mbid','');extra=extras.get(mbid,{}) if mbid else {}
        apple=apple_by_artist.get(mbid,{}).get(str(row.get('title','')).casefold(),{})
        fanart_data=fanart_cache.get('artist:'+str(mbid).casefold(),{}).get('data',{}) if mbid else {}
        fanart_key=(mbid,str(row.get('musicbrainz_id','')))
        if fanart_key not in fanart_albums:fanart_albums[fanart_key]=fanart_album_images(fanart_data,row.get('musicbrainz_id',''))
        fanart_album=fanart_albums[fanart_key]
        plex_cover=plex_art_source(artwork_index,'album',str(row.get('album_id')),row)
        arts={'plex':image_url('album',str(row.get('album_id')),'cover',plex_cover),'fanart':fanart_album[0]['url'] if fanart_album else '','apple':apple_album_artwork(root_repository(),str(row.get('album_id')),apple.get('artwork',''),saved_links=apple_links,saved_artwork=apple_artwork),'coverartarchive':('https://coverartarchive.org/release-group/'+row['musicbrainz_id']+'/front-500' if row.get('musicbrainz_id') else '')}
        dates={'musicbrainz':dates_by_artist.get(mbid,{}).get(str(row.get('musicbrainz_id')),''),'apple':apple.get('year','')}
        row['artwork']=next((arts[s] for s in album_art_order if arts.get(s)),row.get('artwork','') if album_art_order else '')
        if arts['plex']:row['artwork_source']='plex'
        selected=artwork_overrides.get('album:'+str(row.get('album_id'))+':cover',{})
        if selected.get('url') and not arts['plex']:row.update(artwork=selected['url'],artwork_source=selected.get('provider','Harmonia override'))
        candidates=[arts.get('plex'),selected.get('url')]
        candidates.extend(arts.get(s) for s in album_art_order if s!='plex')
        if album_art_order:candidates.append(original_artwork)
        candidates=list(dict.fromkeys(value for value in candidates if value))
        artwork_sources[str(row.get('album_id'))]=candidates
        row['artwork']=arts.get('plex') or cover_url(row.get('album_id'),candidates,selected.get('selected_at'))
        row['year']=next((dates[s] for s in date_order if dates.get(s)),'')
    from collections import defaultdict
    tracks=defaultdict(list)
    for t in ctx['tracks']:tracks[str(t.get('album_id'))].append(t)
    queued_albums={q.get('albumId') for q in lidarr.get('queue',[]) if q.get('albumId') is not None}
    for a in sources:
        ts=tracks[str(a.get('album_id'))];found=sum(str(t['id']) in ctx['links'] for t in ts)
        if not a.get('in_lidarr'):plex_status='Not in Lidarr'
        elif ctx.get('pending') or time.time()-plex.get('checked_at',0)>86400:plex_status='Scan pending'
        elif ts and found==len(ts):plex_status='Linked to Plex'
        elif found:plex_status=f'{found} of {len(ts)} linked to Plex'
        elif a.get('statistics',{}).get('trackFileCount',0)>0:plex_status='Awaiting Plex'
        else:plex_status='Not downloaded'
        files=int(a.get('statistics',{}).get('trackFileCount') or 0)
        total=int(a.get('statistics',{}).get('trackCount') or 0)
        state=a.get('library_state','Missing')
        if state=='In Library':status='In Library' if not total or files>=total else f'In Library · {files} of {total} tracks'
        elif state=='Wanted':status='Wanted · Downloading' if a.get('id') in queued_albums else (f'Wanted · {files} of {total} tracks' if files and total else 'Wanted')
        else:status='Missing · '+('Not in Lidarr' if not a.get('in_lidarr') else 'Not monitored')
        rows.append({**a,'status':status,'plex_status':plex_status,'track_count':len(ts),'plex_count':found,'local_count':files,'year':a.get('year',''),'type':a.get('type') or 'Other'})
    from .file_audit import report,decorate
    from .file_tools import settings as file_settings,plex_url
    cfg=file_settings()
    from .file_audit import scope
    audit_snapshot=root_repository().load_one('file_audit',scope(cfg,lidarr),{}) if cfg.get('audit_enabled') else {}
    audit=report(cfg,lidarr,root_repository(),snapshot=audit_snapshot)
    album_links=root_repository().load_one('plex_album_links',ctx.get('scope',''),{})
    from .album_identity import Albums
    identities=Albums(lidarr,plex,cfg['mappings'],album_links,ctx['links'])
    from .edition_preferences import findings as edition_findings
    errors=decorate([identities.finding(e) for e in issues(ctx,plex,identities)+audit.pop('issues')+edition_findings(lidarr,root_repository())],root_repository())
    metadata=root_repository().load('library_artist_metadata');lastfm=root_repository().load('library_lastfm_artist_metadata')
    from .lidarr import config as lidarr_config
    lidarr_base=lidarr_config(root_repository()).get('url','').rstrip('/')
    albums_by_artist=defaultdict(list)
    for row in rows:albums_by_artist[str(row.get('artist_id'))].append(row)
    plex_by_artist={}
    plex_by_artist_id={}
    for item in plex.get('rows',[]):
        name=str(item.get('album_artist') or item.get('artist','')).casefold()
        if name and item.get('plex_artist_id') and name not in plex_by_artist:plex_by_artist[name]=item
        if item.get('plex_artist_id') and str(item['plex_artist_id']) not in plex_by_artist_id:plex_by_artist_id[str(item['plex_artist_id'])]=item
    errors_by_artist=defaultdict(list)
    for error in errors:errors_by_artist[str(error.get('artist','')).casefold()].append(error)
    genre_order=metadata_order(prefs,'genres');portrait_order=metadata_order(prefs,'artist_portrait')
    hero_order=metadata_order(prefs,'artist_hero');description_order=metadata_order(prefs,'artist_description')
    popular_order=metadata_order(prefs,'popular_tracks')
    artists=[]
    for source in lidarr.get('artists',[]):
        aid=str(source.get('id'));mbid=linked_by_id.get(aid) or source.get('mbid','');albums=albums_by_artist.get(aid,[])
        extra=extras.get(mbid,{}) if mbid else {};mb=metadata.get(mbid,{}) if mbid else {};lf=lastfm.get(mbid,{}) if mbid else {};override=link_overrides.get(aid,{})
        tags=[t.get('name','') for t in mb.get('tags',[]) if t.get('name')][:3]
        genre_sources={'apple':[extra.get('genre')] if extra.get('genre') else [],'musicbrainz':tags,'lastfm':lf.get('genres',[])[:3]}
        genres=next((genre_sources[s] for s in genre_order if genre_sources.get(s)),[])
        artist_name=str(source.get('name',''));manual_plex_id=str(override.get('plex_artist_id') or '')
        plex_row=plex_art_source(artwork_index,'artist',aid)
        artist_plex=plex_row.get('plex_artist_id','')
        affected={str(r.get('album_id') or r.get('title')) for r in albums if r.get('plex_status') in ('Awaiting Plex',) or ' of ' in r.get('plex_status','')}
        affected.update(str(e.get('album_id') or e.get('album') or e.get('title')) for e in errors_by_artist.get(artist_name.casefold(),[]) if e.get('issue')!='iTunes Match review' and not e.get('intentional'))
        attention=len({value for value in affected if value})
        apple_art=next((a.get('artwork') for a in extra.get('albums',[]) if a.get('artwork')),'')
        if apple_art:apple_art=apple_art.replace('100x100bb','600x600bb')
        fanart_data=fanart_cache.get('artist:'+str(mbid).casefold(),{}).get('data',{}) if mbid else {}
        fanart_portraits=fanart_artist_images(fanart_data,'portrait');fanart_heroes=fanart_artist_images(fanart_data,'hero')
        portraits={'fanart':fanart_portraits[0]['url'] if fanart_portraits else '','apple':apple_art,'plex':image_url('artist',aid,'portrait',plex_row)}
        portrait=next((portraits[s] for s in portrait_order if portraits.get(s)),'')
        heroes={'fanart':fanart_heroes[0]['url'] if fanart_heroes else '','plex':image_url('artist',aid,'hero',plex_row)}
        hero=next((heroes[s] for s in hero_order if heroes.get(s)),'')
        descriptions={'lastfm':lf.get('description',''),'musicbrainz':mb.get('annotation','')}
        description=next((descriptions[s] for s in description_order if descriptions.get(s)),'')
        portrait_override=artwork_overrides.get('artist:'+aid+':portrait',{});hero_override=artwork_overrides.get('artist:'+aid+':hero',{})
        portrait=portraits['plex'] or portrait_override.get('url') or portrait;hero=heroes['plex'] or hero_override.get('url') or hero
        for kind,choices,order,chosen in (('portrait',portraits,portrait_order,portrait_override),('hero',heroes,hero_order,hero_override)):
            candidates=list(dict.fromkeys(value for value in [chosen.get('url'),*[choices.get(s) for s in order if s!='plex']] if value))
            artist_artwork_sources[aid+':'+kind]=candidates
            if not choices.get('plex') and candidates:
                resolved=cover_url(aid,candidates,chosen.get('selected_at'),target='artists',kind=kind)
                if kind=='portrait':portrait=resolved
                else:hero=resolved
        artists.append({**source,'mbid':mbid,
            'artwork':portrait or next((r.get('artwork') for r in albums if r.get('artwork')),''),'portrait_artwork':portrait,'hero_artwork':hero,
            'portrait_source':'plex' if portraits['plex'] else portrait_override.get('provider',''),'hero_source':'plex' if heroes['plex'] else hero_override.get('provider',''),
            'genres':list(dict.fromkeys(genres)),'itunes_url':override.get('itunes_url') or extra.get('artist_url',''),
            'spotify_url':override.get('spotify_url',''),
            'description':description,'description_source':next((s for s,v in descriptions.items() if v==description),'') if description else '', 'country':mb.get('country',''),'artist_type':mb.get('type',''),
            'popular_tracks':lf.get('popular_tracks',[]) if 'lastfm' in popular_order else [],
            'musicbrainz_source':'Harmonia override' if override.get('musicbrainz_id') else 'Lidarr',
            'plex_artist_id':artist_plex,'plex_source':'Harmonia override' if manual_plex_id and plex_row else ('Automatic name match' if plex_row else ''),
            'album_count':len(albums),'available_count':sum(r.get('library_state')=='In Library' for r in albums),
            'track_count':sum(r.get('track_count',0) for r in albums),'attention_count':attention,
            'lidarr_url':lidarr_base+'/artist/'+mbid if lidarr_base and mbid else '',
            'plex_url':plex_url(plex,artist_plex) if artist_plex else '',
        })
    rows_by_album={str(r.get('album_id')):r for r in rows}
    errors_by_album=defaultdict(list);errors_by_plex=defaultdict(list)
    for error in errors:
        if error.get('plex_album_id'):errors_by_plex[str(error['plex_album_id'])].append(error)
        destination=error.get('review_album_id') or error.get('album_id')
        if destination:errors_by_album[str(destination)].append(error)
    plex_tracks_by_album=defaultdict(list)
    for track in plex.get('rows',[]):
        pid=str(track.get('plex_album_id') or '')
        if pid:plex_tracks_by_album[pid].append(track)
    for pid,ps in plex_tracks_by_album.items():
        first=ps[0];identity=identities.resolve(first,first.get('files',[]))
        route='plex:'+pid
        rows_by_album[route]={'album_id':route,'album_key':identity['album_key'],'title':first.get('album',''),
            'artist':first.get('album_artist') or first.get('artist',''),'in_lidarr':False,'plex_only':True,
            'linked_lidarr_album_id':identity.get('album_id'),'plex_album_id':pid,'plex_linked':True,
            'library_state':'In Plex','status':'In Plex','type':'Album','statistics':{},'releases':[],
            'artwork':image_url('album','plex:'+pid,'cover',first),
            'plex_url':plex_url(plex,pid),'identity_source':'Plex','track_count':len(ps)}
        alternatives=artwork_sources.get(str(identity.get('album_id')),[])
        selected=artwork_overrides.get('album:'+route+':cover',{})
        candidates=list(dict.fromkeys(value for value in [rows_by_album[route]['artwork'],selected.get('url'),*alternatives] if value))
        artwork_sources[route]=candidates
        rows_by_album[route]['artwork']=rows_by_album[route]['artwork'] or cover_url(route,candidates,selected.get('selected_at'))
        # A directly opened Plex route shows the very same evidence as Issues.
        errors_by_album[route]=errors_by_plex.get(pid,[])

    if not lidarr:
        for pid,ps in plex_tracks_by_album.items():
            row=rows_by_album['plex:'+pid];first=ps[0]
            aid='plex:'+str(first.get('plex_artist_id') or hashlib.sha256(row['artist'].casefold().encode()).hexdigest()[:16])
            row.update(artist_id=aid,library_state='In Library',year=first.get('year',''),secondary_types=[])
            rows.append(row);albums_by_artist[aid].append(row)
        for aid,albums in albums_by_artist.items():
            first=plex_tracks_by_album[albums[0]['plex_album_id']][0]
            artists.append({'id':aid,'name':albums[0]['artist'],'genres':[],'album_count':len(albums),'available_count':len(albums),'track_count':sum(a['track_count'] for a in albums),'attention_count':0,'plex_artist_id':str(first.get('plex_artist_id') or ''),'plex_url':plex_url(plex,first['plex_artist_id']) if first.get('plex_artist_id') else '', 'artwork':image_url('artist',aid,'portrait',first) or albums[0]['artwork'],'portrait_artwork':image_url('artist',aid,'portrait',first),'hero_artwork':image_url('artist',aid,'hero',first),'popular_tracks':[]})
        # There is no missing-Lidarr link issue in Plex-only mode.
        errors=[];errors_by_album=defaultdict(list)

    album_links=root_repository().load_one('plex_album_links',ctx.get('scope',''),{})
    result={'_artist_artwork_sources':artist_artwork_sources,'_artwork_sources':artwork_sources,'_plex_tracks_by_album':plex_tracks_by_album,'_identities':identities,'_artists_by_id':{str(a.get('id')):a for a in artists},'_rows_by_artist':albums_by_artist,'_missing_files':{f for issue in audit_snapshot.get('issues',[]) if issue.get('issue')=='Missing imported file' for f in issue.get('files',[])},'_rows_by_album':rows_by_album,'_errors_by_album':errors_by_album,'_audit_snapshot':audit_snapshot,'_file_settings':cfg,'_album_links':album_links,'_lidarr_base':lidarr_base,'audit':audit,'rows':rows,'artists':artists,'errors':errors,'plex_checked_at':plex.get('checked_at'),'lidarr_checked_at':lidarr.get('checked_at'),'tracks_scanned':'tracks' in lidarr,
        '_plex':plex,'_lidarr':lidarr,'_ctx':ctx,'_plex_by_id':{str(p['plex_id']):p for p in plex.get('rows',[])},'_tracks_by_album':tracks}
    if not actor() or not actor().get('admin'):
        result.update(audit=public_library(audit) if can_issues() else {},errors=public_library(errors) if can_issues() else [],rows=public_library(rows),artists=public_library(artists))
        if not can_issues():
            for artist in result['artists']:artist['attention_count']=0
            for artist in artists:artist['attention_count']=0
    elapsed=time.monotonic()-started
    from .api import _record_log
    _record_log('INFO' if elapsed>=1 else 'DEBUG','Library',f'Prepared Library view: {len(artists)} artists, {len(rows)} releases, {len(errors)} findings in {elapsed:.2f}s')
    return result


def cached_library(refresh=False):
    from .accounts import actor
    from .api import _config
    from .accounts import root_repository
    user=actor() or {};config=_config(read_only=True,namespaces=[]);repo=config.repository;root=root_repository()
    credentials=hashlib.sha256(json.dumps(getattr(config,'config',{}).get('plex',{}),sort_keys=True).encode()).hexdigest()
    key=(str(repo.directory),str(root.directory),str(user.get('id') or 'server'),bool(user.get('admin')),bool(user.get('can_library_issues')),credentials)
    revision=(_revision(repo),_revision(root))
    with _view_lock:
        entry=_view_cache.get(key)
        if not refresh and entry and entry['revision']==revision:
            _view_cache.move_to_end(key);return entry['data']
        loading=_view_loading.setdefault(key,threading.Lock())
    with loading:
        revision=(_revision(repo),_revision(root))
        with _view_lock:
            entry=_view_cache.get(key)
            epoch=_view_epoch
            if not refresh and entry and entry['revision']==revision:
                _view_cache.move_to_end(key);return entry['data']
        data=build_library()
        with _view_lock:
            if epoch==_view_epoch:
                _view_cache[key]={'revision':revision,'at':time.monotonic(),'data':data};_view_cache.move_to_end(key)
                while len(_view_cache)>4:_view_cache.popitem(last=False)
        return data


def link_targets(model=None):
    """Library's unified saved album records, with its current artwork and tracks."""
    if model is None:model=cached_library()
    records=model['_rows_by_album']
    result={}
    for ident,row in records.items():
        # MusicBrainz catalog-only releases are not existing library targets.
        if not row.get('in_lidarr') and not row.get('plex_only'):continue
        if row.get('plex_only') and records.get(str(row.get('linked_lidarr_album_id') or ''),{}).get('in_lidarr'):
            continue
        tracks=list(model['_plex_tracks_by_album'].get(str(row.get('plex_album_id')),[])) if row.get('plex_only') else list(model['_tracks_by_album'].get(ident,[]))
        linked_ids={str(model['_ctx']['links'].get(str(track.get('id')),{}).get('plex_id') or '') for track in tracks}
        playable=[model['_plex_by_id'][key] for key in linked_ids if key in model['_plex_by_id']]
        saved_pid=str(model['_album_links'].get(ident,{}).get('plex_album_id') or '')
        if saved_pid:playable=list(model['_plex_tracks_by_album'].get(saved_pid,[]))
        if row.get('plex_only'):playable=tracks
        result[ident]={**row,'library_album_id':ident,'target_type':'plex' if row.get('plex_only') else 'lidarr',
                       'target_id':str(row.get('plex_album_id')) if row.get('plex_only') else ident,'tracks':tracks,'plex_tracks':playable}
    for ident,row in records.items():
        linked=str(row.get('linked_lidarr_album_id') or '')
        if row.get('plex_only') and linked in result:
            target=result[linked]
            target.update(plex_present=True,plex_target_id=str(row.get('plex_album_id')),plex_linked=True)
            target['plex_tracks']=list(model['_plex_tracks_by_album'].get(str(row.get('plex_album_id')),[]))
            if not target['tracks']:target['tracks']=list(model['_plex_tracks_by_album'].get(str(row.get('plex_album_id')),[]))
    return list(result.values())


def playable_tracks():
    """Playable track records from the account-scoped Harmonia Library model."""
    # The same account-scoped saved inventory index used by Library pages.
    # Track lookup needs no release enrichment, artwork or Issues calculation.
    from .api import _config
    data=indexed(_config(read_only=True,namespaces=[]))
    if not data['plex'].get('checked_at'):raise HTTPException(409,'Run Scan Plex Library before matching tracks.')
    return list(data['playable_tracks'].values())


def register(app):
    from .file_tools import register as register_tools
    register_tools(app)
    from .api import _config,job_store
    from .accounts import actor,root_repository
    from .track_bridge import identity
    from pydantic import BaseModel,Field
    @app.get('/api/library')
    @saved('library directory v17',LIBRARY)
    def library(view:str='full',q:str='',sort:str='name:asc',offset:int=0,limit:int=2000,refresh:bool=False):
        data=cached_library(refresh)
        if view=='full':return {k:v for k,v in data.items() if not k.startswith('_')}
        if view!='directory':raise HTTPException(422,'Unknown Library view.')
        q=q.strip().casefold();artists=[a for a in data['artists'] if not q or q in (str(a.get('name',''))+' '+' '.join(a.get('genres',[]))).casefold()]
        field,direction=(sort.split(':',1)+['asc'])[:2] if ':' in sort else (sort,'asc')
        allowed={'name','album_count','available_count','attention_count'};field=field if field in allowed else 'name'
        artists.sort(key=lambda a:(str(a.get(field,'' )).casefold() if field=='name' else int(a.get(field) or 0),str(a.get('name','')).casefold()),reverse=direction=='desc')
        offset=max(0,offset);limit=max(1,min(2000,limit));total=len(artists)
        return {'artists':artists[offset:offset+limit],'total_artists':total,'release_count':len(data['rows']),
            'issue_count':sum(not e.get('intentional') and e.get('issue')!='iTunes Match review' for e in data['errors']),
            'audit':{k:v for k,v in data.get('audit',{}).items() if k!='issues'},'plex_checked_at':data.get('plex_checked_at'),
            'lidarr_checked_at':data.get('lidarr_checked_at'),'tracks_scanned':data.get('tracks_scanned')}

    @app.get('/api/library/artists/{artist_id}')
    @saved('artist albums v17',LIBRARY)
    def artist_view(artist_id:str,refresh:bool=False):
        data=cached_library(refresh);artist=data['_artists_by_id'].get(artist_id)
        if not artist:raise HTTPException(404,'Artist not found in the saved Lidarr library.')
        name=str(artist.get('name','')).casefold()
        rows=data['_rows_by_artist'].get(artist_id,[])
        errors=[e for e in data['errors'] if str(e.get('artist_id') or '')==artist_id or (not e.get('artist_id') and str(e.get('artist','')).casefold()==name)]
        result={'artist':artist,'rows':rows,'errors':errors,'tracks_scanned':data['tracks_scanned'],'plex_checked_at':data.get('plex_checked_at'),'lidarr_checked_at':data.get('lidarr_checked_at')}
        return result if actor() and actor().get('admin') else public_library(result)

    @app.get('/api/library/scan-status')
    def scan_status():
        # Current configuration is cheap to read and must not be inferred from
        # a historical audit or a prepared projection.
        from .file_tools import settings
        return {'enabled':settings()['audit_enabled']}

    @app.get('/api/library/issues')
    @saved('library issues v20261008_14',LIBRARY)
    def issue_view(refresh:bool=False):
        data=cached_library(refresh)
        if not can_issues():raise HTTPException(403,'Issues access is disabled for this account.')
        result={'errors':[e for e in data['errors'] if e.get('issue')!='iTunes Match review'],'audit':data['audit'],'issue_count':sum(not e.get('intentional') and e.get('issue')!='iTunes Match review' for e in data['errors'])}
        return result if actor() and actor().get('admin') else public_library(result)
    @app.get('/api/library/albums/{album_id}')
    @saved('album details v18',LIBRARY)
    def album(album_id:str,refresh:bool=False):
        # Preparation runs on the reserved Views worker. HTTP reads only the
        # persisted album projection; the full model is shared per revision.
        model=cached_library()
        a=model['_rows_by_album'].get(album_id)
        if not a:raise HTTPException(404,'Album not found in the saved Lidarr or MusicBrainz catalog')
        from .album_service_links import apply
        a=apply(a,root_repository())
        plex=model['_plex'];ctx=model['_ctx'];by_id=model['_plex_by_id']
        if a.get('plex_only'):
            from .file_tools import plex_url
            ps=model['_plex_tracks_by_album'].get(a['plex_album_id'],[])
            result={**a,'tracks':[{**t,'id':str(t['plex_id']),'plex_url':plex_url(plex,t['plex_id'])} for t in ps],
                'issues':model['_errors_by_album'].get(album_id,[]) if can_issues() else []}
            return result if actor() and actor().get('admin') else public_library(result)
        tracks=[{**t,'link':ctx['links'].get(str(t['id'])),'plex':by_id.get(str(ctx['links'].get(str(t['id']),{}).get('plex_id')))} for t in model['_tracks_by_album'].get(album_id,[])]
        from .file_tools import mapped,plex_url
        cfg=model['_file_settings'];audit=model['_audit_snapshot']
        for t in tracks:
            t['local_path']=mapped(t.get('lidarr_file',t.get('file','')),config=cfg)
            local=t['local_path']
            root=next((r for r in audit.get('roots',[]) if local.startswith(r['path']+'/')),None)
            t['file_audit_status']='Not checked'
            if audit and time.time()-audit.get('checked_at',0)>86400:t['file_audit_status']='Stale audit — recheck'
            elif root and root['status']=='unavailable':t['file_audit_status']='Audit unavailable'
            elif local in audit.get('files',{}):t['file_audit_status']='File found'
            elif local in model['_missing_files']:t['file_audit_status']='Missing imported file'
            t['file_audit_checked_at']=audit.get('checked_at')
            t['plex_url']=plex_url(plex,t['link']['plex_id']) if t.get('link') else None
        def position(t):
            def n(value,default):
                try:return int(str(value).split('/')[0])
                except (ValueError,TypeError):return default
            return n(t.get('disc_number'),1),n(t.get('track_number'),99999),str(t['id'])
        tracks.sort(key=position)
        files=int(a.get('statistics',{}).get('trackFileCount') or 0);total=int(a.get('statistics',{}).get('trackCount') or 0)
        state=a.get('library_state','Missing')
        status=a.get('status',state)
        base=model['_lidarr_base']
        saved_album_links=model['_album_links']
        saved_plex_album_id=str(saved_album_links.get(album_id,{}).get('plex_album_id') or '')
        linked_art=plex_art_source(indexed(_config(read_only=True,namespaces=[])),'album',album_id)
        linked_plex_album_id=saved_plex_album_id or str(linked_art.get('plex_album_id') or '') or next((str((t.get('plex') or {}).get('plex_album_id') or '') for t in tracks if (t.get('plex') or {}).get('plex_album_id')),'')
        album_errors=model['_errors_by_album'].get(album_id,[]) if can_issues() else []
        result={**a,'issues':album_errors,'status':status,'tracks':tracks,'lidarr_url':base+'/album/'+album_id if base and a.get('in_lidarr') else '',
            'plex_album_id':linked_plex_album_id,'plex_linked':bool(linked_plex_album_id),'plex_url':plex_url(plex,linked_plex_album_id) if linked_plex_album_id else ''}
        return result if actor() and actor().get('admin') else public_library(result)
    def service_link_album(album_id):
        row=cached_library()['_rows_by_album'].get(album_id)
        if not row:raise HTTPException(404,'Album not found in your library catalog.')
        return row
    @app.get('/api/library/albums/{album_id}/service-links')
    def album_service_links(album_id:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        from .album_service_links import apply,FIELDS
        repo=root_repository();row=apply(service_link_album(album_id),repo)
        saved=repo.load_one('library_album_service_links',album_id) or {}
        result={provider:{'url':row.get(field,'') or '', 'manual':provider in saved} for provider,field in FIELDS.items()}
        cached=repo.load_one('library_album_apple_artwork',album_id) or {}
        result['apple']['artwork_message']=cached.get('message','') if saved.get('apple') and cached.get('url')==saved['apple'] else ''
        return result
    class AlbumServiceLink(BaseModel):
        provider:str=Field(max_length=30)
        url:str=Field(default='',max_length=2000)
    @app.put('/api/library/albums/{album_id}/service-links')
    def save_album_service_link(album_id:str,body:AlbumServiceLink):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        service_link_album(album_id)
        from .album_service_links import validate
        from .lidarr import state_put
        url=validate(body.provider,body.url);repo=root_repository()
        if body.provider=='apple':
            from .album_apple_artwork import album_reference
            album_reference(url)
        saved=repo.load_one('library_album_service_links',album_id) or {}
        saved[body.provider]=url
        state_put(repo,'library_album_service_links',album_id,saved)
        if body.provider=='apple':
            from .album_apple_artwork import refresh
            refresh(repo,album_id,url)
        invalidate()
        return album_service_links(album_id)
    @app.delete('/api/library/albums/{album_id}/service-links/{provider}')
    def reset_album_service_link(album_id:str,provider:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        service_link_album(album_id)
        from .album_service_links import FIELDS
        from .lidarr import state_put
        if provider not in FIELDS:raise HTTPException(422,'Unsupported album link service.')
        repo=root_repository();saved=repo.load_one('library_album_service_links',album_id) or {}
        saved.pop(provider,None);state_put(repo,'library_album_service_links',album_id,saved);invalidate()
        return album_service_links(album_id)
    @app.post('/api/library/albums/{album_id}/artwork/apple/refresh')
    def refresh_apple_album_artwork(album_id:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        service_link_album(album_id)
        repo=root_repository();saved=repo.load_one('library_album_service_links',album_id) or {}
        if not saved.get('apple'):return {'message':'No reviewed Apple album link to refresh.'}
        from .album_apple_artwork import refresh
        result=refresh(repo,album_id,saved['apple']);invalidate()
        return {'message':result.get('message','')}
    @app.get('/api/library/albums/{album_id}/releases')
    def album_releases(album_id:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        _,_,lidarr,_=read();album=next((row for row in lidarr.get('rows',[]) if str(row.get('album_id'))==album_id),None)
        if not album or not album.get('in_lidarr',True):raise HTTPException(404,'Album is not managed by Lidarr.')
        from .local_library import mb_get
        group=mb_get('release-group/'+album_id,{'inc':'releases'})
        exposed={str(row.get('foreignReleaseId')):row for row in album.get('releases',[])}
        rows=[]
        for release in group.get('releases',[]):
            if str(release.get('status','')).casefold()!='official':continue
            rid=str(release.get('id',''));lidarr_release=exposed.get(rid,{})
            media=release.get('media') or []
            rows.append({'id':rid,'title':release.get('title') or album.get('title'),'date':release.get('date',''),
                'country':release.get('country') or next((code for event in (release.get('release-events') or []) for code in event.get('area',{}).get('iso-3166-1-codes',[]) if code),''),
                'format':', '.join(dict.fromkeys(str(m.get('format') or '') for m in media if m.get('format'))),
                'track_count':sum(int(m.get('track-count') or 0) for m in media),
                'label':', '.join(dict.fromkeys(str(i.get('label',{}).get('name') or '') for i in release.get('label-info',[]) if i.get('label'))),
                'barcode':release.get('barcode',''),'selected':bool(lidarr_release.get('monitored')),
                'available_in_lidarr':bool(lidarr_release),'disambiguation':release.get('disambiguation','')})
        rows.sort(key=lambda row:(not row['selected'],not row['available_in_lidarr'],row['date'] or '9999',row['country'],row['id']))
        return {'release_group_id':album_id,'album':album.get('title'),'artist':album.get('artist'),'lidarr_id':album.get('id'),'rows':rows}
    class ReleaseChoice(BaseModel):
        release_id:str=Field(min_length=36,max_length=36)
    @app.post('/api/library/albums/{album_id}/release',status_code=202)
    def select_album_release(album_id:str,body:ReleaseChoice):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        options=album_releases(album_id);selected=next((row for row in options['rows'] if row['id']==body.release_id),None)
        if not selected:raise HTTPException(422,'Choose an official MusicBrainz release from this album.')
        if not selected['available_in_lidarr']:raise HTTPException(409,'Refresh this album in Lidarr before selecting that release.')
        store=job_store();job_id=store.enqueue('release_change',{'release_group_id':album_id,'release_id':body.release_id,
            'lidarr_id':options['lidarr_id'],'trigger_label':'Album release selection'})
        return store.get(job_id)
    @app.get('/api/library/plex-tracks')
    def candidates(q:str=''):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        terms=q.casefold().split()
        return [{k:t.get(k) for k in ('title','artist','album','plex_id','duration')} for t in playable_tracks() if all(v in (' '.join(str(t.get(k,'')) for k in ('title','artist','album'))).casefold() for v in terms)][:100]
    @app.get('/api/library/about')
    def about_summary():
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        from .library_inventory import indexed
        cfg=_config(read_only=True,namespaces=[]);data=indexed(cfg)
        return {'artists':len(data['plex_artists']),'albums':len({r.get('plex_album_id') for r in data['plex'].get('rows',[]) if r.get('plex_album_id')}),
            'tracks':len(data['plex'].get('rows',[])),'requests':len(root_repository().load('lidarr_requests')),'data_directory':str(cfg.repository.directory)}

    @app.get('/api/library/recently-added')
    @saved('Plex recently added v20261008_10',LIBRARY)
    def recently_added():
        from .library_inventory import indexed
        data=indexed(_config(read_only=True,namespaces=[]));grouped={}
        for track in data['plex'].get('rows',[]):
            pid=str(track.get('plex_album_id') or '')
            if not pid or not track.get('plex_album_added_at'):continue
            grouped.setdefault(pid,{'album_id':'plex:'+pid,'title':track.get('album',''),
                'artist':track.get('album_artist') or track.get('artist',''),
                'artwork':image_url('album','plex:'+pid,'cover',track),
                'added_at':int(track['plex_album_added_at'])})
        rows=sorted(grouped.values(),key=lambda r:(-r['added_at'],r['album_id']))[:40]
        return {'rows':rows,'checked_at':data['plex'].get('checked_at'),'dates_available':bool(grouped)}

    @app.get('/api/library/plex-albums')
    def plex_album_candidates(q:str=''):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        terms=q.casefold().split();rows=cached_library()['_rows_by_album']
        return [{'plex_album_id':r['plex_album_id'],'album':r.get('title',''),'artist':r.get('artist',''),'artwork':r.get('artwork',''),'track_count':r.get('track_count',0)} for r in rows.values() if r.get('plex_only') and all(term in (str(r.get('title',''))+' '+str(r.get('artist',''))).casefold() for term in terms)][:100]
    @app.get('/api/library/plex-albums/{plex_album_id}/image')
    def plex_album_candidate_image(plex_album_id:str):
        config=_config(read_only=True,namespaces=[])
        from .library_inventory import indexed
        row=indexed(config)['plex_albums'].get(plex_album_id)
        if not row:raise HTTPException(404,'Plex album artwork unavailable')
        from .plex_images import serve
        path=row.get('plex_album_thumb')
        try:
            if not path:raise HTTPException(404,'Plex image unavailable')
            return serve(config,path)
        except HTTPException as exc:
            if exc.status_code!=404:raise
        from .library_artwork import serve as alternate_image
        model=cached_library()
        return alternate_image(config,model['_artwork_sources'].get('plex:'+plex_album_id,[]),external_only=True)

    @app.get('/api/library/albums/{album_id}/artwork')
    def saved_album_artwork(album_id:str):
        model=cached_library()
        if album_id not in model['_rows_by_album']:raise HTTPException(404,'Album artwork unavailable')
        from .library_artwork import serve
        return serve(_config(read_only=True,namespaces=[]),model['_artwork_sources'].get(album_id,[]))

    class AlbumLink(BaseModel):
        plex_album_id:str | None=Field(default=None,max_length=100)
    @app.put('/api/library/albums/{album_id}/plex-link')
    def save_album_link(album_id:str,body:AlbumLink):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        _,plex,lidarr,ctx=read()
        tracks=[t for t in ctx.get('tracks',[]) if str(t.get('album_id'))==album_id and t.get('file')]
        if not any(str(a.get('album_id'))==album_id for a in lidarr.get('rows',[])) or not ctx.get('scope'):raise HTTPException(409,'Run a current Lidarr library scan before linking this album.')
        candidates=[p for p in plex.get('rows',[]) if str(p.get('plex_album_id') or '')==str(body.plex_album_id or '')]
        if body.plex_album_id and not candidates:raise HTTPException(409,'That Plex album is no longer in the inventory. Refresh Plex and try again.')
        from .track_bridge import identity
        from .matching import Matcher
        from .accounts import root_repository
        import json
        linked=0;used=set();repo=root_repository()
        with repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM state WHERE namespace='track_links' AND key=?",(ctx['scope'],)).fetchone()
            values=json.loads(row[0]) if row else {}
            album_row=db.execute("SELECT value FROM state WHERE namespace='plex_album_links' AND key=?",(ctx['scope'],)).fetchone()
            album_values=json.loads(album_row[0]) if album_row else {}
            for track_id,saved_link in list(values.items()):
                if saved_link.get('album_link')==album_id:values.pop(track_id,None)
            if body.plex_album_id is None:
                album_values.pop(album_id,None)
            else:
                if any(gid!=album_id and str(link.get('plex_album_id'))==str(body.plex_album_id) for gid,link in album_values.items()):
                    raise HTTPException(409,'This Plex album is already linked to another Lidarr album. Remove that association before linking it here.')
                album_values[album_id]={'plex_album_id':body.plex_album_id,'linked_at':time.time()}
                for track in tracks:
                    choices=[]
                    for candidate in candidates:
                        if str(candidate.get('plex_id')) in used:continue
                        comparable={**candidate,'album':track.get('album','')}
                        score=Matcher.score_candidate(track,comparable).get('adjusted_score',0)
                        choices.append((score,candidate))
                    if not choices:continue
                    score,candidate=max(choices,key=lambda item:item[0])
                    if score < 55:continue
                    plex_id=str(candidate['plex_id']);used.add(plex_id);linked+=1
                    values[str(track['id'])]={'plex_id':plex_id,'identity':list(identity(track)),'plex_identity':list(identity(candidate)),'album_link':album_id}
            db.execute("INSERT OR REPLACE INTO state VALUES('track_links',?,?)",(ctx['scope'],json.dumps(values)))
            db.execute("INSERT OR REPLACE INTO state VALUES('plex_album_links',?,?)",(ctx['scope'],json.dumps(album_values)))
        from .artwork_sync import queue_pending
        from .accounts import root_repository
        queue_pending(root_repository())
        invalidate()
        return {'saved':True,'linked_tracks':linked,'total_tracks':len(tracks)}
    @app.get('/api/library/lidarr-albums')
    def lidarr_album_candidates(q:str=''):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        terms=q.casefold().split()
        return [{k:r.get(k) for k in ('album_id','title','artist','artwork','statistics')} for r in link_targets() if r['target_type']=='lidarr' and all(v in (str(r.get('title',''))+' '+str(r.get('artist',''))).casefold() for v in terms)][:100]

    class LidarrAlbumLink(BaseModel):
        album_id:str=Field(min_length=1,max_length=100)
    @app.put('/api/library/plex-albums/{plex_album_id}/lidarr-link')
    def save_lidarr_album_link(plex_album_id:str,body:LidarrAlbumLink):
        return save_album_link(body.album_id,AlbumLink(plex_album_id=plex_album_id))

    @app.get('/api/library/artists/{artist_id}/artwork')
    def saved_artist_artwork(artist_id:str,kind:str='portrait'):
        if kind not in ('portrait','hero'):raise HTTPException(422,'Choose portrait or background artwork.')
        model=cached_library()
        if artist_id not in model['_artists_by_id']:raise HTTPException(404,'Artist not found')
        from .library_artwork import serve
        return serve(_config(read_only=True,namespaces=[]),model['_artist_artwork_sources'].get(artist_id+':'+kind,[]),external_only=True)

    @app.get('/api/library/artists/{artist_id}/plex-image')
    def artist_image(artist_id:str,kind:str='portrait'):
        if kind not in ('portrait','hero'):raise HTTPException(422,'Choose portrait or background artwork.')
        config=_config(read_only=True,namespaces=[])
        from .library_inventory import indexed
        data=indexed(config);source=data['artists'].get(artist_id) or ({'name':''} if artist_id.startswith('plex:') else None)
        if not source:raise HTTPException(404,'Artist not found')
        manual=str(data['artist_links'].get(artist_id,{}).get('plex_artist_id') or '')
        from .artwork_sync import source as artwork_source
        row=artwork_source(data,'artist',artist_id)
        path=row.get('plex_artist_art' if kind=='hero' else 'plex_artist_thumb')
        from .plex_images import serve
        try:
            if not path or not str(path).startswith('/'):raise HTTPException(404,'Plex image unavailable')
            return serve(config,path)
        except HTTPException as exc:
            if exc.status_code!=404:raise
        return saved_artist_artwork(artist_id,kind)
    @app.get('/api/library/albums/{album_id}/plex-image')
    def album_image(album_id:str):
        config=_config(read_only=True,namespaces=[])
        from .library_inventory import indexed
        data=indexed(config);source=data['albums'].get(album_id)
        if not source:raise HTTPException(404,'Album not found')
        saved=str(data['album_links'].get(album_id,{}).get('plex_album_id') or '')
        row=data['plex_albums'].get(saved,{}) if saved else data['plex_album_names'].get((str(source.get('artist','')).casefold(),str(source.get('title','')).casefold()),{})
        path=row.get('plex_album_thumb')
        from .plex_images import serve
        try:
            if not path or not str(path).startswith('/'):raise HTTPException(404,'Plex image unavailable')
            return serve(config,path)
        except HTTPException as exc:
            if exc.status_code!=404:raise
        from .library_artwork import serve as alternate_image
        return alternate_image(config,cached_library()['_artwork_sources'].get(album_id,[]),external_only=True)

    def artwork_candidates(target,target_id,kind):
        if target not in ('album','artist') or kind not in ('cover','portrait','hero'):
            raise HTTPException(422,'Choose album cover, artist portrait, or artist background.')
        if target=='album' and kind!='cover':raise HTTPException(422,'Albums use cover artwork.')
        if target=='artist' and kind=='cover':raise HTTPException(422,'Artists use portrait or background artwork.')
        config,plex,lidarr,_=read();fanart_message=''
        from .accounts import root_repository
        repo=root_repository()
        result=[]
        def add(provider,label,url):
            if url and not any(r['url']==url for r in result):result.append({'provider':provider,'label':label,'url':url})
        if target=='album':
            view=library.__wrapped__();row=next((r for r in view.get('rows',[]) if str(r.get('album_id'))==target_id),None)
            if not row:raise HTTPException(404,'Album not found')
            mbid=row.get('artist_mbid','');extra=repo.load('local_artist_enrichment').get(mbid,{}) if mbid else {}
            apple=next((a for a in extra.get('albums',[]) if str(a.get('title','')).casefold()==str(row.get('title','')).casefold()),{})
            apple_art=apple_album_artwork(repo,target_id,apple.get('artwork','')).replace('100x100bb','1200x1200bb')
            add('apple','Apple Music / iTunes',apple_art)
            if row.get('musicbrainz_id'):
                add('coverartarchive','Cover Art Archive','https://coverartarchive.org/release-group/'+row['musicbrainz_id']+'/front')
                try:
                    from .fanart import album_images,lookup
                    for index,image in enumerate(album_images(lookup(repo,'album',row['musicbrainz_id']),row['musicbrainz_id']),1):add('fanart',f'Fanart.tv Album Cover {index}',image['url'])
                except Exception as exc:fanart_message='Fanart.tv artwork could not be loaded: '+str(getattr(exc,'detail',exc))
        else:
            view=library.__wrapped__();row=next((r for r in view.get('artists',[]) if str(r.get('id'))==target_id),None)
            if not row:raise HTTPException(404,'Artist not found')
            mbid=row.get('mbid','');extra=repo.load('local_artist_enrichment').get(mbid,{}) if mbid else {}
            if mbid:
                try:
                    from .fanart import artist_images,lookup
                    direct=artist_images(lookup(repo,'artist',mbid),'portrait' if kind=='portrait' else 'hero')
                    for index,image in enumerate(direct,1):add('fanart',f"Fanart.tv {str(image.get('type','artwork')).replace('artist','Artist ').replace('music','Music ').title()} {index}",image['url'])
                except Exception as exc:fanart_message='Fanart.tv artwork could not be loaded: '+str(getattr(exc,'detail',exc))
            if kind=='portrait':
                apple=next((a.get('artwork') for a in extra.get('albums',[]) if a.get('artwork')),'')
                add('apple','Apple Music catalog artwork',apple.replace('100x100bb','1200x1200bb'))
        key=target+':'+target_id+':'+kind;selected=repo.load('artwork_overrides').get(key,{})
        for candidate in result:candidate['selected']=candidate['url']==selected.get('url')
        if selected.get('url') and selected.get('provider')!='plex' and not any(r['selected'] for r in result):result.insert(0,{'provider':selected.get('provider','unknown'),'label':'Saved image · source unavailable','url':selected['url'],'selected':True,'stale':True})
        apple_saved=repo.load_one('library_album_service_links',target_id) or {}
        apple_cached=repo.load_one('library_album_apple_artwork',target_id) or {}
        apple_message=apple_cached.get('message','') if target=='album' and apple_saved.get('apple') and apple_cached.get('url')==apple_saved['apple'] else ''
        apple_refresh_required=bool(target=='album' and apple_saved.get('apple') and
            (apple_cached.get('url')!=apple_saved['apple'] or not apple_cached.get('artwork')))
        if apple_refresh_required and not apple_message:
            apple_message='Loading artwork for your saved Apple Music album link…'
        return {'key':key,'selected':selected,'candidates':result,'fanart_message':fanart_message,'apple_message':apple_message,'apple_refresh_required':apple_refresh_required}
    @app.get('/api/library/artwork/{target}/{target_id}/{kind}')
    def artwork_choices(target:str,target_id:str,kind:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        return artwork_candidates(target,target_id,kind)
    class ArtworkChoice(BaseModel):
        provider:str=Field(min_length=1,max_length=40)
        url:str=Field(min_length=1,max_length=2000)
    @app.put('/api/library/artwork/{target}/{target_id}/{kind}')
    def choose_artwork(target:str,target_id:str,kind:str,body:ArtworkChoice):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        choices=artwork_candidates(target,target_id,kind)
        selected=next((r for r in choices['candidates'] if r['url']==body.url and r['provider']==body.provider and not r.get('stale')),None)
        if not selected:raise HTTPException(409,'That image is no longer available. Refresh the artwork choices.')
        from .artwork_sync import choose
        sync=choose(_config(read_only=True,namespaces=[]),target,target_id,kind,body.url,body.provider)
        return {**artwork_candidates(target,target_id,kind),'sync':sync}
    @app.delete('/api/library/artwork/{target}/{target_id}/{kind}')
    def reset_artwork(target:str,target_id:str,kind:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        choices=artwork_candidates(target,target_id,kind)
        from .accounts import root_repository
        from .lidarr import state_put
        state_put(root_repository(),'artwork_overrides',choices['key'],{})
        return artwork_candidates(target,target_id,kind)
    @app.post('/api/library/albums/{album_id}/artwork/plex')
    def apply_album_artwork_to_plex(album_id:str):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        from .accounts import root_repository
        from .artwork_sync import apply
        selected=root_repository().load_one('artwork_overrides','album:'+album_id+':cover',{})
        if not selected.get('url'):raise HTTPException(409,'Choose album artwork first.')
        return apply(_config(read_only=True,namespaces=[]),'album',album_id,'cover',selected['url'],selected.get('provider',''))
    class Link(BaseModel):
        plex_id:str | None=Field(default=None,max_length=100)
    @app.put('/api/library/tracks/{track_id}/link')
    def save_link(track_id:str,body:Link):
        if not actor() or not actor().get('admin'):raise HTTPException(403,'Administrator access required.')
        config,plex,lidarr,ctx=read()
        t=next((t for t in ctx['tracks'] if str(t['id'])==track_id),None)
        if not t or not t.get('file') or not ctx.get('scope'):raise HTTPException(409,'Import this track into Lidarr and run a current library scan before linking it.')
        import json
        from .accounts import root_repository
        with root_repository().connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM state WHERE namespace='track_links' AND key=?",(ctx['scope'],)).fetchone()
            values=json.loads(row[0]) if row else {}
            if body.plex_id is None:values.pop(track_id,None)
            else:
                p=next((p for p in plex.get('rows',[]) if str(p['plex_id'])==body.plex_id),None)
                if not p:raise HTTPException(409,'Plex track is no longer in the inventory.')
                values[track_id]={'plex_id':body.plex_id,'identity':list(identity(t)),'plex_identity':list(identity(p))}
            db.execute("INSERT OR REPLACE INTO state VALUES('track_links',?,?)",(ctx['scope'],json.dumps(values)))
        return {'saved':True}
