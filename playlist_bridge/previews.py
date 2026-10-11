"""On-demand Apple catalog discovery; audio is played by Apple's own embed."""
import threading
import time
from urllib.parse import urlsplit
import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field

_lock = threading.Lock()
_last = 0.0


class Search(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(min_length=1, max_length=300)
    album: str = Field(default='', max_length=300)
    country: str = Field(default='US', pattern=r'^[A-Z]{2}$')


def lookup(request):
    global _last
    if not _lock.acquire(False):
        raise HTTPException(429, 'Another Apple catalog search is running. Try again shortly.')
    try:
        if time.monotonic() - _last < 3.2:
            raise HTTPException(429, 'Please wait a few seconds before searching Apple again.')
        _last = time.monotonic()
        started = time.monotonic()
        from .api import _record_log
        _record_log("INFO", "Apple preview", f"Searching Apple catalog: {request.title} — {request.artist}")
        try:
            response = requests.get('https://itunes.apple.com/search', params={
                'term': request.title + ' ' + request.artist, 'entity': 'song',
                'media': 'music', 'limit': 12, 'country': request.country}, timeout=(5, 15))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            from .diagnostics import service_failure
            failure=service_failure('Apple catalog', exc, started)
            _record_log('ERROR','Apple preview',failure.detail)
            raise failure from None
        rows = []
        for row in data.get('results', []):
            track_id = row.get('trackId')
            url = row.get('trackViewUrl', '')
            parsed = urlsplit(url)
            if not isinstance(track_id, int) or parsed.hostname not in ('music.apple.com', 'itunes.apple.com'):
                continue
            rows.append({'id': track_id, 'title': row.get('trackName', ''), 'artist': row.get('artistName', ''),
                         'album': row.get('collectionName', ''), 'store_url': url,
                         'embed_url': f'https://embed.music.apple.com/{request.country.lower()}/song/{track_id}'})
        from .api import _record_log
        _record_log('INFO', 'Apple preview', f'{request.title} — {request.artist}: found {len(rows)} preview candidates')
        return {'rows': rows}
    finally:
        _lock.release()


def register(app):
    @app.post('/api/tracks/apple-preview')
    def search(request: Search):
        return lookup(request)
