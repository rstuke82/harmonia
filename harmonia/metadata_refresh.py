"""Durable artist checkpoints and bounded publication of bulk metadata changes."""
import time
from .lidarr import state_put

NAMESPACE='library_metadata_refresh'
SOFT={'library_artist_metadata','library_artist_catalog','library_lastfm_artist_metadata','local_artist_enrichment','fanart_cache'}
PUBLISH_SECONDS=60

def generations(repo):
    with repo.connect() as db:
        return dict(db.execute('SELECT namespace,generation FROM state_revisions WHERE namespace IN ('+','.join('?' for _ in SOFT)+')',tuple(SOFT)))

def save(repo,state):
    state_put(repo,NAMESPACE,'run',state)

def active(repo,state):
    if state.get('status')!='running' or not state.get('job_id'):return False
    from .jobs import Store
    from .storage import get_repository
    owner=state.get('account_id')
    store_repo=repo if owner is None else get_repository(repo.directory/'users'/str(int(owner)))
    try:return Store(store_repo).get(state['job_id'])['status']=='running'
    except (KeyError,TypeError):return False

def stable_generations(repo,rows):
    state=repo.load_one(NAMESPACE,'run',{})
    if not active(repo,state):return rows
    baseline=state.get('published',{})
    # Newly created namespaces stay hidden until the next publication.
    values=dict(rows)
    for name in SOFT:
        if name in values:
            if name in baseline:values[name]=baseline[name]
            else:values.pop(name)
    return sorted(values.items())

def publish(repo,state,force=False):
    if force or time.time()-state.get('published_at',0)>=PUBLISH_SECONDS:
        state.update(published=generations(repo),published_at=time.time())
    save(repo,state)

def recover(store,user=None):
    """Resume the latest interrupted bulk run, never an explicitly cancelled one."""
    if user and not user.get('admin'):return
    rows=store._rows("SELECT * FROM jobs WHERE action='library_metadata_all' ORDER BY created_at DESC LIMIT 1")
    if not rows:return
    job=rows[0]
    from .accounts import root_repository
    state=root_repository().load_one(NAMESPACE,'run',{})
    stopped=job['status']=='cancelled' and state.get('job_id')==job['id'] and state.get('status')=='interrupted'
    if not job.get('cancel_requested') and (job['status']=='interrupted' or stopped):
        from fastapi import HTTPException
        try:store.enqueue('library_metadata_all',job.get('payload') or {})
        except HTTPException as exc:
            if exc.status_code!=429:raise
            # Scheduler retries after capacity becomes available; startup must succeed.
