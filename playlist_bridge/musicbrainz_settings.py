"""Independent MusicBrainz preferences and local release ordering."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class Preferences(BaseModel):
    server_url: str = ""
    enabled: bool = True
    cache_days: Literal[7, 30] = 7
    release_priority: list[Literal['Album', 'EP', 'Single', 'Other']] = Field(default_factory=lambda: ['Album','EP','Single','Other'])
    preferred_countries: list[str] = Field(default_factory=lambda: ["US", "XE"], max_length=30)
    preferred_formats: list[str] = Field(default_factory=lambda: ["Digital Media", "CD"], max_length=20)
    prefer_official: bool = True
    prefer_studio: bool = True
    retries: int = Field(default=2, ge=0, le=3)
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_types(self):
        from urllib.parse import urlsplit
        if not self.server_url.strip():
            self.server_url = ''
        else:
            parsed = urlsplit(self.server_url.strip())
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Enter an HTTP(S) MusicBrainz mirror URL without credentials, query, or fragment.')
            self.server_url = self.server_url.strip().rstrip('/')
        if len(self.release_priority) != 4 or len(set(self.release_priority)) != 4:
            raise ValueError('Include Album, EP, Single and Other exactly once.')
        self.preferred_countries = list(dict.fromkeys(v.strip().upper() for v in self.preferred_countries if v.strip()))
        if any(len(v) != 2 or not v.isalpha() for v in self.preferred_countries):
            raise ValueError("Use two-letter release countries, such as US, XE or XW.")
        self.preferred_formats = list(dict.fromkeys(v.strip() for v in self.preferred_formats if v.strip()))
        if any(len(v)>60 for v in self.preferred_formats):
            raise ValueError("Release formats must be at most 60 characters.")
        return self

def settings(repo):
    old = repo.load('lidarr').get('settings', {})
    return Preferences(**({'enabled':old.get('musicbrainz_enabled',True),'cache_days':old.get('cache_days',7)} | repo.load('musicbrainz_settings').get('preferences',{}))).model_dump()

def ordered(rows, preferences):
    priority = preferences['release_priority']
    def rank(row):
        secondary = {str(s).strip().casefold() for s in (row.get('secondary_types') or [])}
        special = bool(secondary & {'live','compilation','remix'})
        kind = next((p for p in priority if p.casefold() == str(row.get('type','')).strip().casefold()), 'Other')
        return (int(row.get('other_release',False)), int(preferences['prefer_studio'] and special), priority.index(kind if kind in priority else 'Other'))
    return sorted(rows, key=rank)

def register(app):
    from .lidarr import repository, musicbrainz_search, Lookup, config, _mb_lock
    @app.get('/api/settings/musicbrainz')
    def get():
        repo = repository()
        return settings(repo) | {'cache_entries':len(repo.load('musicbrainz_cache'))}
    @app.put('/api/settings/musicbrainz')
    def put(request: Preferences):
        repository().save({'musicbrainz_settings':{'preferences':request.model_dump()}})
        return get()
    @app.delete('/api/settings/musicbrainz/cache')
    def clear():
        with _mb_lock:
            repository().save({'musicbrainz_cache':{}})
        return get()
    @app.post('/api/settings/musicbrainz/test')
    def test():
        import time
        import requests
        from .musicbrainz_client import get as mb_get
        repo = repository()
        start = time.monotonic()
        try:
            result = musicbrainz_search(repo,config(repo),Lookup(title='The Distance',artist='CAKE',album='Fashion Nugget',provider='musicbrainz',force_refresh=True), test=True)
            candidate=next((row for row in result['rows'] if row.get('album_id')),None)
            if not candidate:
                raise HTTPException(502,'Public MusicBrainz search responded but did not return a release-group ID to test.')
            direct = mb_get(repo, 'release-group/'+candidate['album_id'], {'inc':'artist-credits+releases'})
            capabilities=[]
            mirror=settings(repo).get('server_url','').rstrip('/')
            if mirror:
                from .musicbrainz_client import probe_capabilities
                capabilities=probe_capabilities(mirror,candidate['album_id'])
                current=settings(repo);current['capabilities']=capabilities
                repo.save({'musicbrainz_settings':{'preferences':current}})
        except HTTPException:
            raise
        except requests.HTTPError as exc:
            code=getattr(exc.response,'status_code',None)
            target='configured mirror' if settings(repo).get('server_url') else 'public MusicBrainz'
            if code==404:
                raise HTTPException(502,f'Public search passed, but {target} returned 404 for the release group it supplied. Check mirror replication and MusicBrainz API support.') from None
            raise HTTPException(502,f'MusicBrainz connection test failed during the direct metadata lookup ({target}{f" · HTTP {code}" if code else ""}).') from None
        except requests.RequestException as exc:
            raise HTTPException(502,f'MusicBrainz connection test could not reach the metadata service: {exc}.') from None
        route='configured mirror' if settings(repo).get('server_url') else 'public service'
        summary=', '.join(capabilities) if settings(repo).get('server_url') else 'public service provides all supported operations'
        return {'message':f'Connection test succeeded in {time.monotonic()-start:.2f}s. {len(result["rows"])} album candidates returned; {direct.get("title","release group")} loaded. Available capabilities: {summary}.','capabilities':capabilities}


def search_scores(rows,request):
    """Query similarity, not proof of recording identity; preserve edition text."""
    import re,unicodedata
    from difflib import SequenceMatcher
    def normalize(s):
        return re.sub(r'[^\w]+',' ',unicodedata.normalize('NFKC',s or '').casefold()).strip()
    fields=[('artist',request.artist),('title',request.album)]
    supplied=[(key,normalize(value)) for key,value in fields if value and value.lower() not in ('n/a','unknown')]
    result=[]
    for row in rows:
        score=round(sum(SequenceMatcher(None,q,normalize(row.get(k,''))).ratio() for k,q in supplied)/len(supplied)*100) if supplied else None
        exact=len(supplied)==2 and all(q==normalize(row.get(k,'')) for k,q in supplied)
        album_id=str(row.get('album_id',''))
        artwork=row.get('artwork') or ('https://coverartarchive.org/release-group/'+album_id+'/front-500' if len(album_id)==36 else '')
        result.append({**row,'artwork':artwork,'query_score':100 if exact else min(99,score) if score is not None else None,'query_exact':exact})
    return sorted(result,key=lambda r:(not r['query_exact'],-(r['query_score'] or 0)))


def ordered_releases(rows, preferences):
    """Soft preferences for editions, never an identity or ownership decision."""
    countries=preferences.get('preferred_countries',[])
    formats=[f.casefold() for f in preferences.get('preferred_formats',[])]
    def position(values, wanted):
        return min((wanted.index(v) for v in values if v in wanted),default=len(wanted))
    def rank(r):
        cs={r.get('country','')} | {e.get('area',{}).get('iso-3166-1-codes',[''])[0] for e in r.get('release-events',[]) if e.get('area',{}).get('iso-3166-1-codes')}
        fs={m.get('format','').casefold() for m in r.get('media',[])}
        return (int(preferences.get('prefer_official',True) and r.get('status')!='Official'),position(cs,countries),position(fs,formats),r.get('date') or '9999',r.get('id',''))
    return sorted(rows,key=rank)
