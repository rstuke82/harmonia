"""Successful source observations, independent of Plex mutation success."""
import json
import uuid
from collections import Counter
from datetime import datetime,timezone

def record(repo,key,tracks,complete=False):
    # Never turn an unverified empty/partial provider response into removals.
    if not tracks and not complete:return
    identity=lambda t:json.dumps({k:t.get(k,'') for k in ('title','artist','album')},sort_keys=True)
    current=Counter(identity(t) for t in tracks)
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute("SELECT value FROM state WHERE namespace='source_audit_baselines' AND key=?",(key,)).fetchone()
        previous=Counter(json.loads(old[0])) if old else current
        added=current-previous;removed=previous-current
        if added or removed:
            from . import jobs
            context=jobs.current()
            event={'playlist_key':key,'detected_at':datetime.now(timezone.utc).isoformat(),'job_id':context.id if context else None,
                   'added':[{**json.loads(k),'count':v} for k,v in added.items()], 'removed':[{**json.loads(k),'count':v} for k,v in removed.items()]}
            db.execute("INSERT INTO state VALUES('source_audit',?,?)",(str(uuid.uuid4()),json.dumps(event)))
        if complete or tracks:
            db.execute("INSERT OR REPLACE INTO state VALUES('source_audit_baselines',?,?)",(key,json.dumps(current)))

def register(app):
    from .accounts import personal_repository
    @app.get('/api/playlists/{key}/source-history')
    def history(key:str):
        return sorted([v for v in personal_repository().load('source_audit').values() if v['playlist_key']==key],key=lambda e:e['detected_at'],reverse=True)
