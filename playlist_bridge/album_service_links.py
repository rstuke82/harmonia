"""Reviewed external album URLs, separate from library and recording identities."""
from urllib.parse import urlsplit
from fastapi import HTTPException

FIELDS={'apple':'itunes_url','spotify':'spotify_url','musicbrainz':'musicbrainz_url'}

def validate(provider,url):
    if provider not in FIELDS:raise HTTPException(422,'Unsupported album link service.')
    url=url.strip()
    if not url:return ''
    try:parsed=urlsplit(url);host=(parsed.hostname or '').lower();port=parsed.port
    except ValueError:raise HTTPException(422,'Enter a valid album URL.')
    if parsed.scheme!='https' or parsed.username or parsed.password or port not in (None,443):
        raise HTTPException(422,'Enter a secure album URL for this service.')
    parts=[p for p in parsed.path.split('/') if p]
    valid=False
    if provider=='apple':valid=host in ('music.apple.com','itunes.apple.com') and 'album' in parts
    elif provider=='spotify':valid=host=='open.spotify.com' and len(parts)>=2 and (parts[0]=='album' or (parts[0].startswith('intl-') and len(parts)>=3 and parts[1]=='album'))
    elif provider=='musicbrainz':
        from uuid import UUID
        if host=='musicbrainz.org' and len(parts)==2 and parts[0] in ('release','release-group'):
            try:UUID(parts[1]);valid=True
            except ValueError:pass
    if not valid:raise HTTPException(422,'Use an album URL from the selected service, rather than an artist or track URL.')
    return url

def apply(row,repo):
    saved=repo.load_one('library_album_service_links',str(row['album_id'])) or {}
    result=dict(row)
    result['musicbrainz_url']='https://musicbrainz.org/release-group/'+row['musicbrainz_id'] if row.get('musicbrainz_id') else ''
    for provider,field in FIELDS.items():
        if provider in saved:result[field]=saved[provider]
    return result
