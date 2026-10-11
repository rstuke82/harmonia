"""MusicBrainz identities layered over the Lidarr-owned album library."""
from uuid import UUID


def mbid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ''


def normalize(value):
    import unicodedata
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def library_state(album):
    """Return the mutually exclusive Library filter state owned by Lidarr."""
    if not album.get('in_lidarr', True):
        return 'Missing'
    files = int(album.get('statistics', {}).get('trackFileCount') or 0)
    total = int(album.get('statistics', {}).get('trackCount') or 0)
    if files > 0 and total and files < total:
        return 'Wanted'
    if files > 0:
        return 'In Library'
    return 'Wanted' if album.get('monitored') else 'Missing'


def secondary_types(source):
    """Normalize the saved MusicBrainz/Lidarr representations without guessing."""
    values=source.get('secondary_types') or source.get('secondaryTypes') or source.get('secondary-types') or []
    if isinstance(values,str):values=[values]
    if not isinstance(values,list):values=[]
    known={v.casefold():v for v in ('Live','Compilation','Remix','Soundtrack','Spokenword','Interview','Audiobook','DJ-mix','Mixtape/Street')}
    result=[]
    for value in values:
        name=value.get('name') if isinstance(value,dict) else value
        if isinstance(name,str) and name.strip():result.append(known.get(name.strip().casefold(),name.strip()))
    primary=str(source.get('albumType') or source.get('type') or '')
    if primary.casefold() in known:result.append(known[primary.casefold()])
    return list(dict.fromkeys(result))


def catalog_rows(lidarr_rows, artists, catalogs, enrichment):
    """Merge the Lidarr collection with cached MusicBrainz artist catalogs.

    Lidarr remains the authority for monitored/downloaded state. MusicBrainz only
    supplies stable identities and albums that are absent from Lidarr.
    """
    artist_by_id = {str(a.get('id')): a for a in artists}
    result = []
    seen = set()
    for source in lidarr_rows:
        artist = artist_by_id.get(str(source.get('artist_id')), {})
        artist_mbid = mbid(source.get('artist_mbid') or artist.get('mbid'))
        group_id = mbid(source.get('album_id'))
        catalog = catalogs.get(artist_mbid, {}) if artist_mbid else {}
        candidates = [r for r in catalog.get('rows', []) if r.get('official') is True and normalize(r.get('title')) == normalize(source.get('title'))]
        if not group_id and len(candidates) == 1:
            group_id = mbid(candidates[0].get('id'))
        catalog_match=next((r for r in catalog.get('rows',[]) if mbid(r.get('id'))==group_id),None) if group_id else None
        if not catalog_match and len(candidates)==1:catalog_match=candidates[0]
        secondary=list(dict.fromkeys(secondary_types(source)+secondary_types(catalog_match or {})))
        apple = enrichment.get(artist_mbid, {}) if artist_mbid else {}
        apple_rows = [r for r in apple.get('albums', []) if normalize(r.get('title')) == normalize(source.get('title'))]
        supplemental = apple_rows[0] if len(apple_rows) == 1 else {}
        row = {**source,
               'in_lidarr': True,
               'secondary_types':secondary,
               'musicbrainz_id': group_id,
               'identity_source': 'Lidarr' if mbid(source.get('album_id')) else ('MusicBrainz' if group_id else ''),
               'itunes_id': supplemental.get('id'),
               'itunes_url': supplemental.get('url', ''),
               'artwork': source.get('artwork') or supplemental.get('artwork', ''),
               'year': source.get('year') or supplemental.get('year', '')}
        row['library_state'] = library_state(row)
        result.append(row)
        if group_id:
            seen.add(group_id)
    for artist in artists:
        artist_mbid = mbid(artist.get('mbid'))
        if not artist_mbid:
            continue
        apple = enrichment.get(artist_mbid, {})
        for release in catalogs.get(artist_mbid, {}).get('rows', []):
            # Catalog rows written before Beta 8 did not prove release status.
            # Hide them until the official-only metadata task refreshes them.
            if release.get('official') is not True:
                continue
            group_id = mbid(release.get('id'))
            if not group_id or group_id in seen:
                continue
            apple_rows = [r for r in apple.get('albums', []) if normalize(r.get('title')) == normalize(release.get('title'))]
            supplemental = apple_rows[0] if len(apple_rows) == 1 else {}
            secondary = secondary_types(release)
            result.append({
                'id': None, 'album_id': group_id, 'musicbrainz_id': group_id,
                'identity_source': 'MusicBrainz', 'title': release.get('title', ''),
                'artist': artist.get('name', ''), 'artist_id': artist.get('id'),
                'artist_mbid': artist_mbid, 'monitored': False, 'statistics': {},
                'releases': [], 'anyReleaseOk': None, 'in_lidarr': False,
                'library_state': 'Missing', 'missing_reason': 'Not in Lidarr',
                'year': str(release.get('first_release_date', ''))[:4],
                'type': release.get('primary_type') or (secondary[0] if secondary else 'Other'),
                'secondary_types': secondary, 'artwork': supplemental.get('artwork', ''),
                'itunes_id': supplemental.get('id'), 'itunes_url': supplemental.get('url', ''),
            })
            seen.add(group_id)
    return result
