"""Shared Lidarr edition preferences for request selection and library findings."""
import time


def ranker(preferences):
    # Lidarr country names and media formats share request-time ranking.
    names={'US':'United States','XW':'[Worldwide]','XE':'Europe','GB':'United Kingdom',
           'CA':'Canada','AU':'Australia','NZ':'New Zealand','JP':'Japan','DE':'Germany',
           'FR':'France','BR':'Brazil','MX':'Mexico','RU':'Russia','ZA':'South Africa',
           'IT':'Italy','ES':'Spain','NL':'Netherlands','SE':'Sweden','NO':'Norway',
           'FI':'Finland','DK':'Denmark','IE':'Ireland','AT':'Austria','CH':'Switzerland',
           'BE':'Belgium','PL':'Poland','PT':'Portugal','CZ':'Czech Republic','GR':'Greece',
           'CN':'China','TW':'Taiwan','HK':'Hong Kong','KR':'South Korea','IN':'India',
           'AR':'Argentina','CL':'Chile','IL':'Israel','TR':'Turkey','UA':'Ukraine',
           'HU':'Hungary','RO':'Romania','IS':'Iceland','SG':'Singapore','MY':'Malaysia',
           'ID':'Indonesia','TH':'Thailand','PH':'Philippines','CO':'Colombia','PE':'Peru'}
    countries=[str(c).casefold() for c in preferences.get('preferred_countries',[])]
    formats=[str(f).casefold() for f in preferences.get('preferred_formats',[])]
    def position(values,wanted):
        return min((wanted.index(v) for v in values if v in wanted),default=len(wanted))
    def rank(release):
        raw=release.get('country') or []
        if isinstance(raw,str):raw=[raw]
        cs={str(c).casefold() for c in raw}
        for code,name in names.items():
            if name.casefold() in cs:cs.add(code.casefold())
        if 'worldwide' in cs:cs.add('xw')
        fs={str(m.get('mediumFormat') or '').casefold() for m in (release.get('media') or []) if isinstance(m,dict)}
        if not fs:fs={str(release.get('format') or '').casefold()}
        return (int(preferences.get('prefer_official',True) and release.get('status')!='Official'),
                position(cs,countries),position(fs,formats),str(release['foreignReleaseId']))
    return rank


def describe(release):
    country=release.get('country') or []
    if isinstance(country,str):country=[country]
    formats=[str(m.get('mediumFormat') or '') for m in (release.get('media') or []) if isinstance(m,dict)]
    return ' · '.join(filter(None,[str(release.get('status') or ''),', '.join(country),', '.join(dict.fromkeys(formats)) or str(release.get('format') or ''),str(release.get('disambiguation') or 'standard')]))


def findings(inventory,repo):
    from .musicbrainz_settings import settings
    from .lidarr import config
    prefs=settings(repo);rank=ranker(prefs);result=[]
    base=config(repo).get('url','').rstrip('/')
    for album in inventory.get('rows',[]):
        releases=[r for r in album.get('releases',[]) if isinstance(r,dict) and r.get('foreignReleaseId')]
        # Older snapshots did not retain release status/media. Do not manufacture
        # a preference finding until a refreshed inventory supplies that evidence.
        if not releases or any(not r.get('status') for r in releases):continue
        selected=[r for r in releases if r.get('monitored')]
        if len(selected)!=1:continue
        current=selected[0]
        # Library review treats each configured list as an acceptable set.
        # Its ordering still ranks new requests, but does not make a downloaded
        # CD/Digital edition an issue when both formats are allowed.
        def acceptable(release):
            official,country,format_position,_=rank(release)
            countries=prefs.get('preferred_countries',[])
            formats=prefs.get('preferred_formats',[])
            return (not official and (not countries or country<len(countries))
                    and (not formats or format_position<len(formats)))
        if acceptable(current):continue
        alternatives=[release for release in releases if acceptable(release)]
        if not alternatives:continue
        best=min(alternatives,key=rank)
        group=str(album.get('album_id') or '')
        result.append({'issue':'Release preference mismatch','album_id':group,'artist_id':album.get('artist_id'),'album':album.get('title',''),'artist':album.get('artist',''),
            'title':album.get('title',''),'files':[],'stale':time.time()-inventory.get('checked_at',0)>86400,
            'reason':'Selected release is outside your configured preferences: '+describe(current)+'. A release within your preferences is available: '+describe(best)+'. Review in Lidarr; existing files will not be changed automatically.',
            'lidarr_url':base+'/album/'+group if base and group else '',
            'evidence':{'source':'lidarr_release_preferences','selected_release':current['foreignReleaseId'],'preferred_release':best['foreignReleaseId'],'preferences':{key:prefs.get(key) for key in ('preferred_countries','preferred_formats','prefer_official')}}})
    return result


def release_summaries(releases):
    """Keep ranking evidence without embedding release track catalogs."""
    fields=('id','foreignReleaseId','title','releaseDate','country','format','label','catalogNumber','trackCount','monitored','status','disambiguation')
    return [{**{key:release.get(key) for key in fields},'media':[{'mediumFormat':medium.get('mediumFormat','')} for medium in (release.get('media') or []) if isinstance(medium,dict)]} for release in (releases or []) if isinstance(release,dict)]
