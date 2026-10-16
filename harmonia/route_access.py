"""Route dependencies with explicit member access and fail-closed defaults.

New routes (including routes under a member URL prefix) require an administrator
browser session until deliberately declared here. Tokens have a smaller API.
"""
from fastapi import HTTPException, Request

PUBLIC_ROUTES = {('GET', '/api/auth/poll'), ('POST', '/api/auth/start'), ('GET', '/api/auth/me'), ('POST', '/api/auth/poll'), ('GET', '/api/health')}

MEMBER_ROUTES = {
    ('POST', '/api/account/manual-matches/remove'),
    ('GET', '/api/account/manual-matches'),
    ('PUT', '/api/account/api-key/scopes'),
    ('GET', '/api/playlists'),
    ('PATCH', '/api/playlists/{playlist_key:path}'),
    ('GET', '/api/detail-progress/{progress_id}'),
    ('GET', '/api/playlists/{playlist_key:path}/detail'),
    ('GET', '/api/missing'),
    ('POST', '/api/missing/candidates'),
    ('POST', '/api/jobs'),
    ('GET', '/api/jobs'),
    ('POST', '/api/jobs/cancel-pending'),
    ('GET', '/api/jobs/submission/{submission_id}'),
    ('GET', '/api/jobs/{job_id}'),
    ('GET', '/api/jobs/{job_id}/events'),
    ('GET', '/api/jobs/{job_id}/live'),
    ('GET', '/api/jobs/{job_id}/log'),
    ('POST', '/api/jobs/{job_id}/cancel'),
    ('POST', '/api/playlists/analyze'),
    ('POST', '/api/playlists'),
    ('POST', '/api/sync/all'),
    ('POST', '/api/sync/favorites'),
    ('POST', '/api/sync/automatic'),
    ('POST', '/api/sync/{playlist_key:path}'),
    ('POST', '/api/playlists/{playlist_key:path}/health'),
    ('POST', '/api/missing/match'),
    ('POST', '/api/missing/ignore'),
    ('GET', '/api/ignored'),
    ('POST', '/api/ignored/restore'),
    ('GET', '/api/search'),
    ('DELETE', '/api/playlists/{playlist_key:path}'),
    ('POST', '/api/missing/automatic'),
    ('POST', '/api/tracks/details'),
    ('POST', '/api/tracks/preview'),
    ('GET', '/api/job-history'),
    ('POST', '/api/playlists/auto-sync'),
    ('GET', '/api/matches/queue'),
    ('POST', '/api/matches/queue'),
    ('POST', '/api/matches/queue/preview'),
    ('DELETE', '/api/matches/queue/{key}'),
    ('POST', '/api/matches/queue/apply'),
    ('POST', '/api/tracks/apple-preview'),
    ('GET', '/api/library/artists/{artist_id}/links'),
    ('PUT', '/api/playlists/sync-mode'),
    ('GET', '/api/playlist-schedule/{key:path}'),
    ('GET', '/api/library'),
    ('GET', '/api/library/artists/{artist_id}'),
    ('GET', '/api/library/scan-status'),
    ('GET', '/api/library/issues'),
    ('GET', '/api/library/albums/{album_id}'),
    ('GET', '/api/library/albums/{album_id}/service-links'),
    ('GET', '/api/library/albums/{album_id}/releases'),
    ('GET', '/api/library/plex-tracks'),
    ('GET', '/api/library/about'),
    ('GET', '/api/library/recently-added'),
    ('GET', '/api/library/plex-albums'),
    ('GET', '/api/library/plex-albums/{plex_album_id}/image'),
    ('GET', '/api/library/albums/{album_id}/artwork'),
    ('GET', '/api/library/lidarr-albums'),
    ('GET', '/api/library/artists/{artist_id}/artwork'),
    ('GET', '/api/library/artists/{artist_id}/plex-image'),
    ('GET', '/api/library/albums/{album_id}/plex-image'),
    ('GET', '/api/library/artwork/{target}/{target_id}/{kind}'),
    ('GET', '/api/shared-playlists'),
    ('GET', '/api/shared-playlists/{key}/preview'),
    ('POST', '/api/shared-playlists/{key}/subscribe'),
    ('POST', '/api/playlists/{key:path}/recreate'),
    ('GET', '/api/discover'),
    ('GET', '/api/discover/history-gaps'),
    ('POST', '/api/discover/history-gaps/candidates'),
    ('PUT', '/api/discover/history-gaps/link'),
    ('GET', '/api/requests-user/search-settings'),
    ('POST', '/api/requests-user/search-jobs'),
    ('POST', '/api/requests-user/search'),
    ('GET', '/api/requests-user/search-jobs/{job_id}'),
    ('DELETE', '/api/requests-user/search-jobs/{job_id}'),
    ('POST', '/api/requests-user/add'),
    ('GET', '/api/requests-user'),
    ('GET', '/api/requests-user/downloads'),
    ('POST', '/api/requests-user/{album}/download-action'),
    ('POST', '/api/auth/logout'),
    ('GET', '/api/account/initial-library'),
    ('GET', '/api/account/api-key'),
    ('POST', '/api/account/api-key'),
    ('POST', '/api/account/api-key/regenerate'),
    ('DELETE', '/api/account/api-key'),
    ('GET', '/api/discover/preferences'),
    ('PUT', '/api/discover/preferences'),
    ('GET', '/api/discover/blocked-artists'),
    ('POST', '/api/discover/blocked-artists'),
    ('DELETE', '/api/discover/blocked-artists/{key}'),
    ('GET', '/api/discover/blocked-albums'),
    ('POST', '/api/discover/blocked-albums'),
    ('DELETE', '/api/discover/blocked-albums/{key}'),
    ('GET', '/api/discover/history-ignored'),
    ('POST', '/api/discover/history-ignored'),
    ('DELETE', '/api/discover/history-ignored/{key}'),
    ('GET', '/api/playlists/{key}/source-history'),
    ('GET', '/api/v1/status'),
    ('POST', '/api/v1/playlists'),
    ('POST', '/api/v1/share'),
    ('GET', '/api/v1/jobs/{job_id}'),
    ('POST', '/api/playlists/text/parse'),
    ('GET', '/api/playlists/text/{key}'),
    ('POST', '/api/playlists/text'),
    ('GET', '/api/playlists/{key}/editor'),
    ('PUT', '/api/playlists/{key}/editor'),
    ('GET', '/api/playlists/{key}/editor/search'),
}
KEY_WRITE_ROUTES = {
    ('POST', '/api/v1/share'): 'playlists:write',
    ('POST', '/api/v1/playlists'): 'playlists:write',
    ('POST', '/api/requests-user/add'): 'requests:write',
    ('POST', '/api/v1/webhooks/lidarr'): 'webhooks:lidarr',
}
KEY_SESSION_ONLY = {
    '/api/account/api-key', '/api/account/api-key/regenerate',
    '/api/account/api-key/scopes', '/api/auth/logout',
}
PLAYLIST_WRITES = {('POST', '/api/account/manual-matches/remove'), ('POST', '/api/sync/automatic'), ('POST', '/api/v1/playlists'), ('PUT', '/api/playlists/{key}/editor'), ('PATCH', '/api/playlists/{playlist_key:path}'), ('POST', '/api/ignored/restore'), ('PUT', '/api/playlists/sync-mode'), ('POST', '/api/missing/automatic'), ('POST', '/api/jobs/{job_id}/cancel'), ('POST', '/api/v1/share'), ('POST', '/api/playlists/text'), ('POST', '/api/matches/queue/preview'), ('DELETE', '/api/matches/queue/{key}'), ('POST', '/api/jobs/cancel-pending'), ('POST', '/api/missing/ignore'), ('POST', '/api/playlists'), ('POST', '/api/matches/queue'), ('POST', '/api/sync/all'), ('POST', '/api/matches/queue/apply'), ('POST', '/api/playlists/{key:path}/recreate'), ('POST', '/api/playlists/text/parse'), ('POST', '/api/sync/favorites'), ('POST', '/api/jobs'), ('POST', '/api/playlists/analyze'), ('POST', '/api/tracks/apple-preview'), ('POST', '/api/sync/{playlist_key:path}'), ('POST', '/api/missing/candidates'), ('DELETE', '/api/playlists/{playlist_key:path}'), ('POST', '/api/missing/match'), ('POST', '/api/playlists/auto-sync'), ('POST', '/api/playlists/{playlist_key:path}/health')}

async def require_route_access(request: Request):
    from .accounts import actor, as_user
    route = request.scope.get('route')
    path = getattr(route, 'path', '')
    identity = (request.method, path)
    if identity in PUBLIC_ROUTES:
        yield
        return
    user = actor()
    if not user or user.get('disabled'):
        raise HTTPException(401, 'Sign in with Plex to continue.')
    member = identity in MEMBER_ROUTES
    if not member and not user.get('admin'):
        raise HTTPException(403, 'Administrator access required.')
    if getattr(request.state, 'auth_method', '') == 'api_key':
        scope = KEY_WRITE_ROUTES.get(identity)
        if path in KEY_SESSION_ONLY or (not member and scope != 'webhooks:lidarr'):
            raise HTTPException(403, 'Use a browser session for administrator settings and account security.')
        if scope is None:
            if request.method not in ('GET', 'HEAD'):
                raise HTTPException(403, 'This operation requires a browser session.')
            scope = 'read'
        if scope not in user.get('api_key_scopes', ['read']):
            raise HTTPException(403, 'API key permission required: '+scope)
    if not user.get('admin'):
        if identity in PLAYLIST_WRITES and user.get('can_playlists', True) is False:
            raise HTTPException(403, 'Playlist changes are disabled for this account.')
        if path in ('/api/library/issues', '/api/library/scan-status') and not user.get('can_library_issues'):
            raise HTTPException(403, 'Issues access is disabled for this account.')
        if path == '/api/requests-user/downloads' and not (user.get('can_downloads') or user.get('can_manage_downloads')):
            raise HTTPException(403, 'Download visibility is disabled for this account.')
    # Administrative service operations and shared Library reads use the server
    # context; personal playlist/request routes retain their account context.
    target_id=request.query_params.get('user_id')
    if target_id is not None:
        if not user.get('admin') or getattr(request.state, 'auth_method', '') == 'api_key':
            raise HTTPException(403, 'Administrator browser access required.')
        if identity not in {('GET','/api/playlists'),('POST','/api/missing/candidates'),('POST','/api/missing/automatic')}:
            raise HTTPException(400, 'User selection is not supported for this operation.')
        from .accounts import users
        target=users().get(target_id)
        if not target or target.get('disabled') or target.get('pending_login'):
            raise HTTPException(404, 'Active user not found.')
        with as_user({k:v for k,v in target.items() if k!='server_context'}):
            yield
        return
    server = user.get('admin') and (not member or (path=='/api/library' or path.startswith('/api/library/')))
    with as_user({**user, 'server_context':True} if server else user):
        yield
