"""Account-owned associations between a history album and a requested release group."""
import time
from pydantic import BaseModel, Field

class HistorySource(BaseModel):
    history_key: str = Field(pattern=r'^[0-9a-f]{64}$')
    artist: str = Field(min_length=1,max_length=500)
    album: str = Field(min_length=1,max_length=500)

def requested(source, album_id, job_id):
    if source is None:return
    from .accounts import personal_repository
    from .discover import album_association_key
    from .lidarr import state_put
    repo=personal_repository()
    value={**source.model_dump(),'target_type':'lidarr','target_id':str(album_id),
           'origin':'request','job_id':job_id,'linked_at':time.time()}
    # A queued request is an association, not proof of imported files. The
    # history reader checks the current inventory before suppressing it.
    state_put(repo,'lastfm_history_links',source.history_key,value)
    state_put(repo,'lastfm_album_links',album_association_key(source.artist,source.album),value)
