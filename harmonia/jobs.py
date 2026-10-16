"""Durable account-scoped jobs with bounded concurrency and anchored interval schedules."""
from .job_labels import action_name
import contextlib
import json
import threading
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from croniter import croniter

ACTIONS = {'musicbrainz_lookup','artwork_sync','prepare_views','request_status','text_playlist','file_audit','file_audit_duplicates','library_metadata','library_metadata_all','lastfm_refresh','artist_metadata','local_scan','local_catalog','playlist_description','recreate','plex_scan','lidarr_scan','lidarr_webhook','album_finalize','release_change','reconcile','retry_missing','playlist_settings','availability','sync', 'health', 'analyze', 'add', 'fix_match', 'track_match', 'remove', 'backup', 'check_updates', 'restore_backup', 'database_optimize','database_compact','database_cleanup','database_rebuild','lidarr_add', 'lidarr_search', 'match_batch', 'ignore', 'ignore_batch'}
SCOPES = {'all', 'favorites', 'automatic', 'selected'}
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
_local = threading.local()


# Independent bounded lanes. Playlist state is still serialized per account;
# network-only/catalog jobs never acquire account or destination write locks.
# Page projections have one reserved worker, independent of long service jobs.
WORKER_ACTIONS = {
    'lookup': {'musicbrainz_lookup'},
    'views': {'prepare_views'},
    'metadata': {'library_metadata','library_metadata_all','lastfm_refresh','artist_metadata', 'local_catalog'},
    'library': {'artwork_sync','file_audit','file_audit_duplicates','local_scan', 'plex_scan', 'lidarr_scan', 'lidarr_webhook', 'album_finalize', 'release_change', 'reconcile'},
    'downloads': {'request_status','lidarr_add', 'lidarr_search'},
    'health': {'health'},
    'maintenance': {'backup', 'restore_backup', 'check_updates','database_optimize','database_compact','database_cleanup','database_rebuild'},
}
WORKER_LIMITS = {'lookup': 2,'playlist': 2, 'health': 1, 'metadata': 1, 'views': 1, 'library': 1, 'downloads': 1, 'maintenance': 1}
EXCLUSIVE = {'backup', 'restore_backup','database_compact','database_cleanup','database_rebuild'}
RESOURCE_HEAVY = {'health', 'library'}
INVENTORY_SCANS = {'plex_scan', 'lidarr_scan'}


def worker_for(action):
    return next((name for name, actions in WORKER_ACTIONS.items() if action in actions), 'playlist')


def dispatch_priority(job):
    """Keep exclusive maintenance first, then first-load pages before warming."""
    return (job['action'] not in EXCLUSIVE,worker_for(job['action'])!='views',
            (job.get('payload') or {}).get('view_priority',2) if job['action']=='prepare_views' else 0)


def history_where(worker):
    if not worker:
        return ' WHERE action NOT IN (?,?)', ('prepare_views','request_status')
    if worker not in WORKER_LIMITS:
        raise ValueError('Unknown worker')
    actions = sorted(a for a in ACTIONS if worker_for(a) == worker and a != 'request_status')
    return ' WHERE action IN (' + ','.join('?' for _ in actions) + ')', tuple(actions)


def wait_reason(action, account, running, exclusive_waiting=False):
    lane = worker_for(action)
    if any(w['action'] in EXCLUSIVE for w in running):
        return 'Queued — exclusive database maintenance has access'
    if action in EXCLUSIVE:
        return 'Queued — waiting for active workers to finish safely' if running else None
    if exclusive_waiting:
        return 'Queued — exclusive database maintenance is next'
    if (lane in ('playlist', 'health') or action == 'reconcile') and any(w['account'] == account and worker_for(w['action']) in ('playlist', 'health') for w in running):
        return 'Queued — another playlist operation for this account is running'
    if lane in ('playlist', 'health') and any(w['account'] == account and w['action'] == 'reconcile' for w in running):
        return 'Queued — library reconciliation is updating this account'
    # Only distinct full service scans may share the library lane. Targeted
    # catalog/file changes and reconciliation retain exclusive lane access.
    library_running = [w for w in running if worker_for(w['action']) == 'library']
    if action in INVENTORY_SCANS and library_running:
        if all(w['action'] in INVENTORY_SCANS and w['action'] != action for w in library_running):
            return None
        return 'Queued — another operation is updating library inventories'
    if lane in RESOURCE_HEAVY and action not in INVENTORY_SCANS and any(worker_for(w['action']) in RESOURCE_HEAVY and w['action'] not in INVENTORY_SCANS for w in running):
        active = next(worker_for(w['action']) for w in running if worker_for(w['action']) in RESOURCE_HEAVY and w['action'] not in INVENTORY_SCANS)
        return f'Queued — waiting for the active {active} job to finish'
    if lane=='lookup' and sum(worker_for(w['action'])=='lookup' for w in running)>=8:
        return 'Queued — cancelled network requests are finishing'
    if sum(worker_for(w['action']) == lane and not (lane=='lookup' and w.get('cancelled')) for w in running) >= WORKER_LIMITS[lane]:
        return f'Queued — {lane} worker capacity is busy'
    return None


def now():
    return datetime.now(timezone.utc).isoformat()


def next_run(expression, zone, after=None):
    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ValueError('Use five cron fields: minute hour day month weekday.')
    base = (after or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    return croniter(expression, base, max_years_between_matches=5).get_next(datetime).astimezone(timezone.utc).isoformat()


class Cancelled(BaseException):
    pass


def current():
    return getattr(_local, 'context', None)


def progress(message, check=True, completed=None, total=None):
    ctx = current()
    if ctx:
        if check:
            ctx.checkpoint()
        ctx.store.update(ctx.id, progress=message)
        activity(mode='working', stage=message, completed=completed, total=total,
                 since=now(), service=None)


class Store:
    def __init__(self, repository):
        self.repository = repository

    def _rows(self, sql, params=()):
        with self.repository.connect() as db:
            cursor = db.execute(sql, params)
            rows = [dict(zip([d[0] for d in cursor.description], row)) for row in cursor.fetchall()]
        for row in rows:
            if 'action' in row: row['worker'] = worker_for(row['action'])
            for key in ('payload', 'result', 'activity'):
                if key in row:
                    row[key] = json.loads(row[key]) if row[key] else None
        return rows

    def get(self, job_id):
        rows = self._rows("SELECT jobs.*, state.value AS activity FROM jobs LEFT JOIN state ON state.namespace='job_activity' AND state.key=jobs.id WHERE jobs.id=?", (job_id,))
        return rows[0] if rows else None

    def list(self):
        return self._rows("SELECT jobs.*, state.value AS activity FROM jobs LEFT JOIN state ON state.namespace='job_activity' AND state.key=jobs.id ORDER BY CASE WHEN status IN ('queued','running','cancelling') THEN 0 ELSE 1 END, created_at DESC LIMIT 200")

    def enqueue(self, action, payload, schedule_id=None, db=None):
        if action not in ACTIONS:
            raise ValueError('Unsupported job action')
        job_id = str(uuid.uuid4())
        def insert(conn):
            def lifecycle(target,message):
                timestamp=now()
                conn.execute('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',(target,timestamp,'lifecycle',message))
                conn.execute('''INSERT INTO diagnostic_events(created_at,level,operation,message,worker,job_id)
                                VALUES(?,?,?,?,?,?)''',(timestamp,'INFO',action,message,worker_for(action),target))
            # Identical active requests share a job, preventing double clicks.
            encoded = json.dumps(payload, sort_keys=True)
            existing = conn.execute("SELECT id FROM jobs WHERE action=? AND payload=? AND status IN ('queued','running','cancelling') AND NOT (action='musicbrainz_lookup' AND cancel_requested=1)", (action, encoded)).fetchone()
            if existing:
                lifecycle(existing[0],'Duplicate request joined this existing job')
                return existing[0]
            from .accounts import actor
            user=actor()
            limit=200 if not user or user.get('admin') else 25
            count=conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','cancelling') AND NOT (action='musicbrainz_lookup' AND cancel_requested=1)").fetchone()[0]
            if count>=limit:
                from fastapi import HTTPException
                raise HTTPException(429,'The pending job limit has been reached. Wait for jobs to finish or cancel queued work.',headers={'Retry-After':'30'})
            conn.execute('INSERT INTO jobs(id,action,payload,status,progress,created_at,schedule_id) VALUES(?,?,?,?,?,?,?)',
                         (job_id, action, encoded, 'queued', 'Waiting for the worker', now(), schedule_id))
            origin='scheduled' if schedule_id else str(payload.get('trigger_label') or 'manual')
            lifecycle(job_id,f'Queued · {origin} · {worker_for(action)} worker')
            return job_id
        if db is not None:
            return insert(db)
        with self.repository.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return insert(conn)

    def update(self, job_id, **values):
        allowed = {'status','progress','result','error','started_at','finished_at','cancel_requested'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid job update')
        if 'result' in values:
            values['result'] = json.dumps(values['result'], default=str)
        if 'progress' in values:
            self.event(job_id, values['progress'], 'stage')
        with self.repository.connect() as db:
            condition=" WHERE id=?"
            if values.get('status')!='cancelled':
                condition+=" AND NOT (action='musicbrainz_lookup' AND cancel_requested=1)"
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+condition, (*values.values(),job_id))


    def event(self, job_id, message, stage='output'):
        from .diagnostics import redact
        from .config import Config
        import re
        message = re.sub(r'\x1b\[[0-9;]*m', '', str(message)).strip()
        if not message:
            return
        ctx = current()
        config = ctx.redaction_config if ctx else Config(read_only=True, namespaces=[])
        message = redact(message, config)
        lane = getattr(_local, 'target', None)
        if ctx and ctx.action == 'health' and lane and stage != 'playlist':
            message = '[' + lane['name'] + '] ' + message
        with self.repository.connect() as db:
            timestamp=now();cursor=db.execute('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)', (job_id,timestamp,stage,message))
            if stage in ('stage','output','lifecycle','playlist','schedule'):
                action=ctx.action if ctx else (db.execute('SELECT action FROM jobs WHERE id=?',(job_id,)).fetchone() or ['jobs'])[0]
                try:
                    from .accounts import actor
                    user=actor();user_id=str(user.get('id')) if user else None
                except Exception:user_id=None
                db.execute('''INSERT INTO diagnostic_events(created_at,level,operation,message,worker,job_id,user_id)
                              VALUES(?,?,?,?,?,?,?)''',(timestamp,'INFO',action,message,worker_for(action),job_id,user_id))
            if cursor.lastrowid%1000==0:
                db.execute('DELETE FROM job_events WHERE job_id=? AND id < COALESCE((SELECT id FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 1 OFFSET 9999),0)',(job_id,job_id))

    def events(self, job_id, after=0):
        return self._rows('SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT 1000', (job_id,after))

    def cancel(self, job_id):
        job=self.get(job_id)
        if job and job['action']=='musicbrainz_lookup':
            with self.repository.connect() as db:
                db.execute("UPDATE jobs SET cancel_requested=1,status='cancelled',progress='Search cancelled',finished_at=? WHERE id=? AND status IN ('queued','running','cancelling')",(now(),job_id))
            return
        with self.repository.connect() as db:
            db.execute("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END, progress='Cancellation requested; current playlist update may finish',finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END WHERE id=? AND status IN ('queued','running','cancelling')", (now(),job_id))
            if db.total_changes:
                timestamp=now();action=(db.execute('SELECT action FROM jobs WHERE id=?',(job_id,)).fetchone() or ['jobs'])[0]
                db.execute('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',(job_id,timestamp,'lifecycle','Cancellation requested'))
                db.execute('''INSERT INTO diagnostic_events(created_at,level,operation,message,worker,job_id)
                              VALUES(?,?,?,?,?,?)''',(timestamp,'INFO',action,'Cancellation requested',worker_for(action),job_id))
        return self.get(job_id)

    def cancel_pending(self):
        """Cancel every queued job in this account while leaving running work alone."""
        stamp=now();message='Stopped before starting by Stop Pending Jobs'
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ids=[row[0] for row in db.execute("SELECT id FROM jobs WHERE status='queued'").fetchall()]
            if not ids:return []
            db.execute("UPDATE jobs SET cancel_requested=1,status='cancelled',progress=?,finished_at=? WHERE status='queued'",(message,stamp))
            db.executemany('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',[(job_id,stamp,'lifecycle',message) for job_id in ids])
        return ids

    def claim(self, job_id=None):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE status='queued' AND (? IS NULL OR id=?) ORDER BY created_at LIMIT 1", (job_id,job_id)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',started_at=?,progress='Starting' WHERE id=?", (now(),row[0]))
            timestamp=now();action=db.execute('SELECT action FROM jobs WHERE id=?',(row[0],)).fetchone()[0]
            db.execute('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',(row[0],timestamp,'lifecycle','Worker claimed job · starting'))
            db.execute('''INSERT INTO diagnostic_events(created_at,level,operation,message,worker,job_id)
                          VALUES(?,?,?,?,?,?)''',(timestamp,'INFO',action,'Worker claimed job · starting',worker_for(action),row[0]))
        return self.get(row[0])

    def schedules(self):
        return self._rows('SELECT * FROM schedules ORDER BY name,id')

    def save_schedule(self, data, schedule_id=None):
        from .tasks import expression, STARTUP_MODES
        if data['action'] not in {'file_audit','local_scan','sync','health','backup','check_updates','availability','plex_scan','lidarr_scan','lastfm_refresh','library_metadata_all','reconcile','retry_missing','database_optimize'}:
            raise ValueError('Unsupported recurring task')
        if data['action'] in ('sync','health') and data['scope']!='all':raise ValueError('This scoped task has been retired; use the consolidated task')
        if data.get('startup') is not None and data['startup'] not in STARTUP_MODES:raise ValueError('Choose an available startup setting')
        hours = data['hours']
        if data['action']=='check_updates' and hours!=6:
            raise ValueError('Update checks run every six hours')
        cron=expression(hours,data.get("start_time","00:00"))
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id,timezone FROM schedules WHERE action=? AND scope=?',(data['action'],data['scope'])).fetchone()
            if not existing:raise ValueError('Unknown task')
            sid,zone=existing
            db.execute("INSERT OR REPLACE INTO state VALUES('task_times',?,?)",(data['action'],json.dumps(data.get('start_time','00:00'))))
            if data.get('startup') is not None:
                db.execute("INSERT OR REPLACE INTO state VALUES('task_startup',?,?)",(data['action'],json.dumps(data['startup'])))
            db.execute('UPDATE schedules SET cron=?,enabled=?,next_run=? WHERE id=?',(cron,int(hours>0),next_run(cron,zone),sid))
            if not hours:
                finished=now()
                db.execute("UPDATE jobs SET cancel_requested=1,status='cancelled',progress='Cancelled because the schedule was disabled',finished_at=? WHERE schedule_id=? AND status='queued'",(finished,sid))
                db.execute("UPDATE jobs SET cancel_requested=1,status='cancelling',progress='Schedule disabled — stopping at the next safe checkpoint' WHERE schedule_id=? AND status IN ('running','cancelling')",(sid,))
        if data['action']=='sync':
            from .description_refresh import queue_server_schedule
            queue_server_schedule()
        if not hours:
            # Server schedules also dispatch work into each Plex user's queue.
            # Cancel those scheduled copies while leaving manual jobs alone.
            from .accounts import member_stores
            for _user, member_store in member_stores():
                with member_store.repository.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    finished=now()
                    db.execute("UPDATE jobs SET cancel_requested=1,status='cancelled',progress='Cancelled because the server schedule was disabled',finished_at=? WHERE schedule_id=? AND status='queued'",(finished,sid))
                    db.execute("UPDATE jobs SET cancel_requested=1,status='cancelling',progress='Server schedule disabled — stopping at the next safe checkpoint' WHERE schedule_id=? AND status IN ('running','cancelling')",(sid,))
        return next(s for s in self.schedules() if s['id']==sid)

    def due(self, timestamp=None):
        from .tasks import payload
        timestamp = timestamp or now()
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE action='restore_backup' AND status IN ('running','cancelling')").fetchone():return
            for sid,action,scope,cron,zone in db.execute('SELECT id,action,scope,cron,timezone FROM schedules WHERE enabled=1 AND next_run<=?', (timestamp,)).fetchall():
                if action == 'check_updates':
                    from .updates import enabled
                    if not enabled(self.repository):
                        db.execute('UPDATE schedules SET next_run=? WHERE id=?', (next_run(cron,zone,datetime.fromisoformat(timestamp)),sid))
                        continue
                from .accounts import scheduled_members
                scheduled_members(action,sid)
                active = db.execute("SELECT 1 FROM jobs WHERE action=? AND COALESCE(json_extract(payload,'$.scope'),'all')=? AND status IN ('queued','running','cancelling')", (action,scope)).fetchone()
                data=payload({'action':action,'scope':scope})
                if action=='health':
                    active=db.execute("SELECT 1 FROM jobs WHERE action='health' AND status IN ('queued','running','cancelling') LIMIT 1").fetchone()
                if action=='sync':
                    from .api import _config,_playlist_key
                    from .sync_policy import eligible
                    keys=[_playlist_key(p) for p in eligible(self.repository,_config(read_only=True,namespaces=[]).config.get('playlists',[]))]
                    busy=set()
                    for encoded, in db.execute("SELECT payload FROM jobs WHERE action='sync' AND status IN ('queued','running','cancelling')"):
                        other=json.loads(encoded)
                        busy.update(other.get('playlist_keys',[]) if other.get('scope')=='selected' else keys)
                    keys=[key for key in keys if key not in busy]
                    data={'scope':'selected','playlist_keys':keys}
                    active=active or not keys

                if not active:
                    self.enqueue(action, data, sid, db)
                else:
                    jid=str(uuid.uuid4())
                    message='Skipped — this task is already queued or running'
                    db.execute('INSERT INTO jobs(id,action,payload,status,progress,created_at,finished_at,schedule_id) VALUES(?,?,?,?,?,?,?,?)',
                        (jid,action,json.dumps(data,sort_keys=True),'skipped',message,timestamp,timestamp,sid))
                    db.execute('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',(jid,timestamp,'schedule',message))
                # Missed runs coalesce to one; manual runs never change these boundaries.
                db.execute('UPDATE schedules SET next_run=? WHERE id=?', (next_run(cron,zone,datetime.fromisoformat(timestamp)),sid))


class Context:
    def __init__(self, store, job, stop):
        self.store, self.id, self.action, self.stop = store, job['id'], job['action'], stop
        from .config import Config
        self.redaction_config = Config(read_only=True, namespaces=[])

    def checkpoint(self):
        if self.stop.is_set() or self.store.get(self.id)['cancel_requested']:
            raise Cancelled()


class Manager:
    def __init__(self, repository):
        self.store = Store(repository)
        self.stop = threading.Event()
        self.thread = None
        self.worker = None
        self.workers = []
        self.lock = None

    def start(self):
        from .process_lock import ProcessLock
        self.lock = ProcessLock(self.store.repository.directory / '.jobs.lock')
        try:
            self.lock.__enter__()
        except RuntimeError:
            self.lock = None
            return  # Another web process owns the shared queue.
        from .api_v1 import coalesce_queued_lidarr_webhooks
        from .lidarr_downloads import coalesce_queued_album_finalizers
        coalesce_queued_lidarr_webhooks(self.store.repository)
        coalesce_queued_album_finalizers(self.store.repository)
        with self.store.repository.connect() as db:
            interrupted=[r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('running','cancelling')")]
            db.execute("UPDATE jobs SET status='interrupted',error='Server restarted during execution. Review the playlist before running again.',finished_at=? WHERE status IN ('running','cancelling')", (now(),))
            db.executemany('INSERT INTO job_events(job_id,created_at,stage,message) VALUES(?,?,?,?)',
                [(jid,now(),'lifecycle','Interrupted by server restart') for jid in interrupted])
        from .accounts import member_stores
        for _user, member_store in member_stores():
            with member_store.repository.connect() as db:
                db.execute("UPDATE jobs SET status='interrupted',error='Server restarted; review before retrying',finished_at=? WHERE status IN ('running','cancelling')",(now(),))
        for recovery_store in [self.store,*[item[1] for item in member_stores()]]:
            with recovery_store.repository.connect() as db:
                db.execute("UPDATE prepared_views SET attempt_revision=NULL WHERE job_id IN (SELECT id FROM jobs WHERE status='interrupted')")
        from .metadata_refresh import recover as recover_metadata
        from .accounts import as_user
        for recovery_user,recovery_store in [(None,self.store),*list(member_stores())]:
            with as_user(recovery_user):recover_metadata(recovery_store,recovery_user)
        from .tasks import setup
        setup(self.store)
        from .sync_policy import migrate
        migrate(self.store)
        from .tasks import startup
        startup(self.store)
        self.thread = threading.Thread(target=self.loop, daemon=True, name='playlist-job-scheduler')
        self.thread.start()

    def loop(self):
        try:
            while not self.stop.is_set():
                try:
                    from . import reset_guard
                    with reset_guard.scheduler_lock:
                        if not reset_guard.restart_required:
                            from .saved_views import tick as prepare_saved_views
                            prepare_saved_views(self.store)
                            self.store.due()
                            from .playlist_schedules import due as playlist_due
                            playlist_due(self.store)
                            from .accounts import member_stores, as_user
                            member_candidates=list(member_stores())
                            for member, member_store in member_candidates:
                                with as_user(member):
                                    playlist_due(member_store)
                                    if member_store.repository.load("pending_playlist_settings"):
                                        member_store.enqueue("playlist_settings",{})
                            if self.store.repository.load('pending_playlist_settings'):
                                self.store.enqueue('playlist_settings',{})
                            self.workers=[w for w in self.workers if w['thread'].is_alive()]
                            for worker in self.workers:
                                if worker['action']=='musicbrainz_lookup':
                                    worker['cancelled']=bool(worker['store'].get(worker['id'])['cancel_requested'])
                            candidates=[(None,self.store),*member_candidates]
                            from .metadata_refresh import recover as recover_metadata
                            for recovery_user,recovery_store in candidates:
                                with as_user(recovery_user):recover_metadata(recovery_store,recovery_user)
                            offset=getattr(self,"_turn",0)%len(candidates)
                            planned=[]
                            for user,store in candidates[offset:]+candidates[:offset]:
                                # Inspect all pending lanes: a blocked playlist must not hide metadata.
                                pending=store._rows("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at")
                                planned.extend((user,store,job) for job in pending)
                            exclusive_waiting=any(job['action'] in EXCLUSIVE for _,_,job in planned)
                            planned.sort(key=lambda item: dispatch_priority(item[2]))
                            for user,store,pending in planned:
                                account=str(store.repository.directory)
                                action=pending['action']
                                if action=='lidarr_webhook':
                                    from .api_v1 import catalog_batch_wait_seconds
                                    if catalog_batch_wait_seconds(store.repository,pending.get('payload') or {})>0:
                                        message='Collecting Lidarr events — starts after 30 seconds without another event'
                                        if pending['progress']!=message:store.update(pending['id'],progress=message)
                                        continue
                                if action=='album_finalize':
                                    from .lidarr_downloads import finalize_batch_wait_seconds
                                    if finalize_batch_wait_seconds(store.repository,pending.get('payload') or {})>0:
                                        message='Collecting imported albums — starts after 20 seconds without another completion'
                                        if pending['progress']!=message:store.update(pending['id'],progress=message)
                                        continue
                                reason=wait_reason(action,account,self.workers,exclusive_waiting)
                                if reason:
                                    if pending['progress']!=reason:store.update(pending['id'],progress=reason)
                                    continue
                                job=store.claim(pending['id'])
                                if not job:continue
                                lane=worker_for(action)
                                thread=threading.Thread(target=self.execute,args=(job,store,user),daemon=True,name=f'bridge-{lane}-worker')
                                self.workers.append({'thread':thread,'account':account,'action':action,'store':store,'id':job['id']})
                                self.worker=thread
                                thread.start()
                                self._turn=(offset+1)%len(candidates)
                                if action in EXCLUSIVE:break
                except Exception as exc:
                    self.store.repository.add_log('ERROR','jobs',f'Queue error: {type(exc).__name__}')
                self.stop.wait(1)
        finally:
            for worker in self.workers:
                worker['thread'].join()  # Finish safe units before releasing ownership.
            if self.lock:
                self.lock.__exit__(None,None,None)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=20)

    def execute(self, job, store=None, user=None):
        from .accounts import as_user
        with as_user(user):
            return self._execute_account(job,store or self.store)

    def _execute_account(self, job, store):
        from . import api
        from .diagnostics import redact
        ctx = Context(store, job, self.stop)
        _local.context = ctx
        _local.target = None
        result = None
        try:
            if job.get('schedule_id'):
                with store.repository.connect() as db:
                    scheduled=db.execute('SELECT enabled FROM schedules WHERE id=?',(job['schedule_id'],)).fetchone()
                if not scheduled:
                    from .accounts import root_repository
                    with root_repository().connect() as db:
                        scheduled=db.execute('SELECT enabled FROM schedules WHERE id=?',(job['schedule_id'],)).fetchone()
                if scheduled and not scheduled[0]:
                    raise Cancelled()
            ctx.checkpoint()
            api._record_log('INFO',job['action'],f"{action_name(job['action'])} started: {job['id']} · {worker_for(job['action'])} worker")
            from .job_locks import destinations
            with destinations(store,job['payload']) if worker_for(job['action']) in ('playlist','health') else contextlib.nullcontext():
                result = api.execute_job(job['action'], job['payload'])
            ctx.checkpoint()
            store.update(job['id'],status='completed',progress=completion_message(result),result=result,finished_at=now())
            api._record_log('INFO',job['action'],f"{action_name(job['action'])} completed: {job['id']}")
        except Cancelled:
            store.update(job['id'],status='cancelled',progress='Cancelled at a safe checkpoint; completed changes are retained',finished_at=now(),**({'result':result} if result is not None else {}))
            api._record_log('INFO',job['action'],f"{action_name(job['action'])} cancelled: {job['id']}")
        except Exception as exc:
            message = redact(getattr(exc,'detail',str(exc)),api._config())
            if isinstance(exc,RuntimeError) and 'Another Harmonia process is already running' in str(exc):
                store.update(job['id'],status='queued',progress='Queued — waiting for this playlist or account to become available',error=None,started_at=None)
                self.stop.wait(2)
                return
            store.update(job['id'],status='failed',error=message,progress='Failed — ' + message,finished_at=now())
            api._record_log('ERROR',job['action'],f'{type(exc).__name__}: {message}')
            import traceback
            api._record_log('DEBUG',job['action'],traceback.format_exc())
        finally:
            _local.context = None
            _local.target = None


def output(message):
    buffer = getattr(_local, 'output', None)
    if buffer is not None:
        buffer.append(message)
    ctx = current()
    if ctx:
        ctx.store.event(ctx.id, message)


_activity_lock = threading.RLock()


def activity(**changes):
    """Persist independently labeled lanes for concurrent read-only health jobs."""
    ctx = current()
    if not ctx:
        return
    lane = getattr(_local, 'target', None) or {'key': 'job', 'name': 'Playlist operation'}
    with _activity_lock, ctx.store.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM state WHERE namespace='job_activity' AND key=?", (ctx.id,)).fetchone()
        state = json.loads(row[0]) if row else {'lanes': {}}
        lanes = state.setdefault('lanes', {})
        entry = lanes.setdefault(lane['key'], dict(lane))
        entry.update(lane)
        entry.update(changes)
        state['updated_at'] = now()
        if 'playlist_total' in lane:
            state['playlist_total'] = lane['playlist_total']
        db.execute("INSERT OR REPLACE INTO state(namespace,key,value) VALUES('job_activity',?,?)", (ctx.id,json.dumps(state)))


def target(key, name, index=None, total=None):
    _local.target = {'key': key, 'name': name, 'playlist_index': index, 'playlist_total': total}
    activity(mode='working', stage='Starting playlist', since=now(), completed=None, total=None, service=None)
    ctx = current()
    if ctx:
        ctx.store.event(ctx.id, f"{name}" + (f" · playlist {index} of {total}" if index and total else ''), 'playlist')


@contextlib.contextmanager
def waiting(service):
    ctx = current()
    if not ctx:
        yield
        return
    key = (getattr(_local, 'target', None) or {}).get('key', 'job')
    state = ctx.store.get(ctx.id).get('activity') or {}
    previous = state.get('lanes', {}).get(key, {}).copy()
    activity(mode='waiting', service=service, since=now())
    try:
        yield
    finally:
        activity(mode='working', service=None, since=now(),
                 stage=previous.get('stage', 'Processing response'))


def completion_message(result):
    if isinstance(result,dict) and isinstance(result.get('summary'),str):
        return result['summary']
    rows = result.get('playlists', []) if isinstance(result, dict) else []
    values = [row.get('result', {}).get('health') or row.get('result', {}).get('summary') or row.get('result') or row.get('summary') or {} for row in rows]
    if not values:
        values = [result.get('health') or result] if isinstance(result, dict) else []
    missing = sum(value.get('unresolved', 0) for value in values if isinstance(value, dict))
    return f'Completed with missing tracks · {missing} missing' if missing else 'Completed — output and results retained'
