"""Reviewed MusicBrainz release selection and Lidarr refresh; media files are read-only."""
from . import jobs


def _release(release_id):
    from .local_library import mb_get
    value=mb_get('release/'+str(release_id),{'inc':'recordings+artist-credits+release-groups'})
    if not value.get('id'):
        raise ValueError('MusicBrainz release was not found.')
    return value


def execute(payload):
    from .accounts import root_repository
    from .inventory import scan
    from .lidarr import Client,config as lidarr_config
    repo=root_repository();cfg=lidarr_config(repo);client=Client(cfg)
    lidarr_id=int(payload['lidarr_id']);group_id=str(payload['release_group_id']);release_id=str(payload['release_id'])
    jobs.progress('Validating the selected MusicBrainz release')
    release=_release(release_id)
    if str((release.get('release-group') or {}).get('id'))!=group_id:
        raise ValueError('The selected release does not belong to this album release group.')
    album=client.call('GET',f'album/{lidarr_id}')
    if str(album.get('foreignAlbumId'))!=group_id:
        raise ValueError('The Lidarr album identity changed. Refresh the library before selecting a release.')
    known=any(str(row.get('foreignReleaseId'))==release_id for row in album.get('releases',[]))
    if not known:
        raise ValueError('Lidarr does not currently expose this MusicBrainz release. Refresh the album in Lidarr and try again.')
    jobs.progress('Applying the selected release in Lidarr')
    album['anyReleaseOk']=False
    for row in album.get('releases',[]):row['monitored']=str(row.get('foreignReleaseId'))==release_id
    client.call('PUT',f'album/{lidarr_id}',json=album)
    jobs.progress('Triggering Lidarr album refresh')
    command=client.call('POST','command',json={'name':'RefreshAlbum','albumId':lidarr_id})
    jobs.progress('Refreshing the saved Lidarr inventory')
    inventory=scan('lidarr',force=True)
    summary=f'Selected {release.get("title") or release_id} in Lidarr; Lidarr refresh command {command.get("id","submitted")}.'
    return {'summary':summary,'release_id':release_id,'inventory':inventory}
