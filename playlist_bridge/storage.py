"""Transactional SQLite repository; legacy JSON is imported exactly once."""
from contextlib import contextmanager
import json
import threading
from functools import lru_cache
import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone

STARTUP_KEYS = {'plex', 'server', 'data_directory', 'data_dir'}
FILES = {'mapping': 'mapping.json', 'missing': 'missing_tracks.json',
         'match_metadata': 'match_metadata.json', 'source_snapshots': 'source_snapshots.json',
         'ignored_tracks': 'ignored_tracks.json', 'artist_aliases': 'artist_aliases.json'}


def read_json(path):
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text() or '{}') if path.read_text().strip() else {}
        if '_schema_version' in raw:
            if raw['_schema_version'] not in (0, 1, 2):
                raise ValueError('Unsupported legacy schema')
            raw = raw['data']
        if not isinstance(raw, dict):
            raise ValueError('Expected an object')
        return raw
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f'Could not read {path.name}: {exc}') from exc


_repository_lock = threading.Lock()

@lru_cache(maxsize=16)
def _repository(directory):
    return Repository(directory)

def get_repository(directory):
    # Cache schema initialization only; connections and state remain request-local.
    with _repository_lock:
        return _repository(str(Path(directory).resolve()))


class Repository:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'playlist-bridge.db'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name=='posix':
            os.chmod(self.directory,0o700)
            fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
            try:os.fchmod(fd,0o600)
            finally:os.close(fd)
            self.secure_files()
        # WAL keeps web reads responsive while workers persist progress.
        self.secure_files()
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA busy_timeout=30000')
            mode = str(db.execute('PRAGMA journal_mode=WAL').fetchone()[0]).lower()
            if mode != 'wal':
                raise RuntimeError(f'Database could not enable concurrent read mode (journal={mode})')
            db.execute('PRAGMA synchronous=NORMAL')
        finally:
            db.close()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 5:
                raise RuntimeError('Database requires a newer Harmonia build')
            if version == 0:
                db.execute('CREATE TABLE state (namespace TEXT, key TEXT, value TEXT NOT NULL, PRIMARY KEY(namespace,key))')
                db.execute('CREATE TABLE health_history (id INTEGER PRIMARY KEY, playlist_key TEXT, checked_at TEXT, result TEXT)')
                db.execute('CREATE TABLE migration_backups (name TEXT PRIMARY KEY, content BLOB NOT NULL)')
                config = read_json(self.directory / 'config.json')
                buckets = {name: read_json(self.directory / file) for name, file in FILES.items()}
                buckets['runtime'] = {k: v for k, v in config.items() if k not in STARTUP_KEYS}
                for name, values in buckets.items():
                    for key, value in values.items():
                        encoded = json.dumps(value, ensure_ascii=False)
                        db.execute('INSERT INTO state VALUES (?,?,?)', (name, key, encoded))
                    actual = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM state WHERE namespace=?', (name,))}
                    if actual != values:
                        raise RuntimeError(f'Migration validation failed: {name}')
                for name in ['config.json', *FILES.values()]:
                    path = self.directory / name
                    if path.exists():
                        db.execute('INSERT INTO migration_backups VALUES (?,?)', (name, path.read_bytes()))
                db.execute('PRAGMA user_version=1')
                version = 1
            if version == 1:
                db.execute('CREATE TABLE application_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT NOT NULL, operation TEXT NOT NULL, message TEXT NOT NULL)')
                db.execute('PRAGMA user_version=2')
                version = 2
            if version == 2:
                db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,action TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,progress TEXT,created_at TEXT,started_at TEXT,finished_at TEXT,result TEXT,error TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,schedule_id TEXT)")
                db.execute("CREATE INDEX jobs_status ON jobs(status,created_at)")
                db.execute("CREATE TABLE schedules(id TEXT PRIMARY KEY,name TEXT NOT NULL,action TEXT NOT NULL,scope TEXT NOT NULL,cron TEXT NOT NULL,timezone TEXT NOT NULL,enabled INTEGER NOT NULL,next_run TEXT NOT NULL)")
                db.execute('PRAGMA user_version=3')
                version = 3
            if version == 3:
                db.execute("CREATE TABLE job_events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,created_at TEXT NOT NULL,stage TEXT NOT NULL,message TEXT NOT NULL)")
                db.execute("CREATE INDEX job_events_job_id ON job_events(job_id,id)")
                # Retain historical jobs while merging duplicate schedule definitions.
                for action, scope in db.execute("SELECT action,scope FROM schedules GROUP BY action,scope HAVING COUNT(*)>1").fetchall():
                    ids = [r[0] for r in db.execute("SELECT id FROM schedules WHERE action=? AND scope=? ORDER BY rowid DESC", (action,scope))]
                    for old in ids[1:]:
                        db.execute("UPDATE jobs SET schedule_id=? WHERE schedule_id=?", (ids[0],old))
                        db.execute("DELETE FROM schedules WHERE id=?", (old,))
                db.execute("CREATE UNIQUE INDEX schedules_type ON schedules(action,scope)")
                db.execute("UPDATE schedules SET name=CASE WHEN action='health' THEN 'Health Check' ELSE 'Sync' END || CASE scope WHEN 'all' THEN CASE WHEN action='health' THEN '' ELSE ' All' END WHEN 'favorites' THEN ' Favorites' ELSE ' Automatics' END")
                db.execute('PRAGMA user_version=4')
                version = 4
            if version == 4:
                db.execute('''CREATE TABLE diagnostic_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    message TEXT NOT NULL,
                    worker TEXT,
                    job_id TEXT,
                    request_id TEXT,
                    user_id TEXT,
                    service TEXT,
                    entity_type TEXT,
                    entity_id TEXT,
                    details TEXT
                )''')
                db.execute('''INSERT INTO diagnostic_events(created_at,level,operation,message)
                              SELECT created_at,level,operation,message FROM application_logs''')
                db.execute('PRAGMA user_version=5')
            for statement in (
                'CREATE INDEX IF NOT EXISTS logs_level_id ON application_logs(level,id DESC)',
                'CREATE INDEX IF NOT EXISTS logs_operation_id ON application_logs(operation,id DESC)',
                'CREATE INDEX IF NOT EXISTS logs_level_operation_id ON application_logs(level,operation,id DESC)',
                'CREATE INDEX IF NOT EXISTS diagnostic_time ON diagnostic_events(id DESC)',
                'CREATE INDEX IF NOT EXISTS diagnostic_level_time ON diagnostic_events(level,id DESC)',
                'CREATE INDEX IF NOT EXISTS diagnostic_operation_time ON diagnostic_events(operation,id DESC)',
                'CREATE INDEX IF NOT EXISTS diagnostic_worker_time ON diagnostic_events(worker,id DESC)',
                'CREATE INDEX IF NOT EXISTS diagnostic_job_time ON diagnostic_events(job_id,id)',
                'CREATE INDEX IF NOT EXISTS health_playlist_time ON health_history(playlist_key,checked_at DESC)',
                "CREATE INDEX IF NOT EXISTS jobs_display ON jobs(CASE WHEN status IN ('queued','running','cancelling') THEN 0 ELSE 1 END,created_at DESC)",
                'CREATE INDEX IF NOT EXISTS jobs_schedule_status ON jobs(schedule_id,status)',
                'CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled,next_run)',
            ):
                db.execute(statement)
            # Monotonic generations detect equal-sized edits and deletions cheaply.
            db.execute('CREATE TABLE IF NOT EXISTS state_revisions(namespace TEXT PRIMARY KEY, generation INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS prepared_views(key TEXT PRIMARY KEY,name TEXT NOT NULL,params TEXT NOT NULL,user_id TEXT,permissions TEXT NOT NULL,revision TEXT,data TEXT,updated_at REAL,error TEXT,attempt_revision TEXT,job_id TEXT)')
            for operation in ('INSERT','UPDATE','DELETE'):
                ref='OLD' if operation=='DELETE' else 'NEW'
                if operation=='UPDATE':db.execute('DROP TRIGGER IF EXISTS view_revision_update')
                condition="WHEN OLD.value IS NOT NEW.value AND (NEW.namespace!='lidarr_requests' OR json_remove(OLD.value,'$.download_checked_at','$.updated_at') IS NOT json_remove(NEW.value,'$.download_checked_at','$.updated_at'))" if operation=='UPDATE' else ''
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS view_revision_{operation.lower()} AFTER {operation} ON state
                    {condition}
                    BEGIN INSERT INTO state_revisions VALUES({ref}.namespace,1)
                    ON CONFLICT(namespace) DO UPDATE SET generation=generation+1; END""")
            for table in ('health_history',):
                for operation in ('INSERT','UPDATE','DELETE'):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS view_revision_{table}_{operation.lower()} AFTER {operation} ON {table}
                        BEGIN INSERT INTO state_revisions VALUES('{table}',1)
                        ON CONFLICT(namespace) DO UPDATE SET generation=generation+1; END""")
            for operation in ('INSERT','UPDATE','DELETE'):
                ref='OLD' if operation=='DELETE' else 'NEW'
                if operation=='UPDATE':db.execute('DROP TRIGGER IF EXISTS request_job_revision_update')
                condition=" AND (OLD.status IS NOT NEW.status OR OLD.result IS NOT NEW.result OR OLD.error IS NOT NEW.error)" if operation=='UPDATE' else ''
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS request_job_revision_{operation.lower()} AFTER {operation} ON jobs
                    WHEN {ref}.action IN ('lidarr_add','lidarr_search','album_finalize','lidarr_webhook','release_change'){condition}
                    BEGIN INSERT INTO state_revisions VALUES('request_jobs',1)
                    ON CONFLICT(namespace) DO UPDATE SET generation=generation+1; END""")
        # Durable originals in the transaction make backup completion restart-safe.
        with self.connect() as db:
            backups = db.execute('SELECT name,content FROM migration_backups').fetchall()
        for name, content in backups:
            backup = self.directory / (name + '.pre-sqlite.bak')
            if not backup.exists():
                with os.fdopen(os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as stream:
                    stream.write(content)
        self.secure_files()
        startup = read_json(self.directory / 'config.json')
        clean = {k: v for k, v in startup.items() if k in STARTUP_KEYS}
        if startup != clean:
            self.save_startup(clean)

    def secure_files(self):
        if os.name!='posix':return
        for name in ['playlist-bridge.db','playlist-bridge.db-wal','playlist-bridge.db-shm','config.json',*FILES.values()]:
            path=self.directory/name
            if path.exists():
                if path.is_symlink():raise RuntimeError('Credential-store files must not be symlinks.')
                try:os.chmod(path,0o600)
                except FileNotFoundError:pass
        for path in self.directory.glob('*.pre-sqlite.bak'):
            if path.is_symlink():raise RuntimeError('Migration backup must not be a symlink.')
            os.chmod(path,0o600)

    @contextmanager
    def connect(self):
        self.secure_files()
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA busy_timeout=30000')
            db.execute('PRAGMA synchronous=NORMAL')
            with db:
                yield db
        finally:
            db.close()

    def load(self, namespace):
        with self.connect() as db:
            return {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM state WHERE namespace=?', (namespace,))}

    def load_one(self, namespace, key, default=None):
        """Load one state value without decoding every sibling in its namespace."""
        with self.connect() as db:
            row = db.execute(
                'SELECT value FROM state WHERE namespace=? AND key=?',
                (namespace, key),
            ).fetchone()
        return json.loads(row[0]) if row else default

    def save(self, buckets, baseline=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for name, values in buckets.items():
                old = baseline.get(name, {}) if baseline is not None else self.load(name)
                for key in old.keys() - values.keys():
                    db.execute('DELETE FROM state WHERE namespace=? AND key=?', (name, key))
                for key, value in values.items():
                    if key not in old or old[key] != value:
                        db.execute('INSERT OR REPLACE INTO state VALUES (?,?,?)', (name, key, json.dumps(value, ensure_ascii=False)))

    def save_startup(self, config):
        from .atomic_files import _atomic_write_json
        _atomic_write_json(self.directory / 'config.json', config)

    def save_health(self, key, result):
        result['checked_at'] = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('INSERT INTO health_history(playlist_key,checked_at,result) VALUES (?,?,?)', (key, result['checked_at'], json.dumps(result)))
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?,?)', ('health', key, json.dumps(result)))

    def record_health_attempt(self, key, error=None):
        attempt = {"attempted_at": datetime.now(timezone.utc).isoformat(), "error": error}
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?,?)', ('health_attempts', key, json.dumps(attempt)))

    def add_log(self, level, operation, message, **context):
        """Persist one structured diagnostic event.

        Callers may remain simple; active job/account context is attached here so
        direct service logs and job logs can be correlated in one timeline.
        """
        level=str(level).upper()
        # DEBUG is opt-in for both the console and the diagnostic timeline.
        # Job progress is stored separately in job_events and remains available.
        if level=='DEBUG' and not bool(self.load('console_settings').get('debug',False)):
            return
        console=context.pop('_console',True)
        try:
            from . import jobs
            current = jobs.current()
        except Exception:
            current = None
        if current:
            context.setdefault('job_id', current.id)
            context.setdefault('worker', jobs.worker_for(current.action))
        try:
            from .accounts import actor
            account = actor()
            if account: context.setdefault('user_id', str(account.get('id','')))
        except Exception:
            pass
        details = context.pop('details', None)
        fields = ('worker','job_id','request_id','user_id','service','entity_type','entity_id')
        values = [context.pop(name, None) for name in fields]
        if context:
            details = {**(details if isinstance(details,dict) else {}), **context}
        if console:
            try:
                from .console_logging import web_mode
            except Exception:
                web_mode = False
            if web_mode:
                import logging
                logging.getLogger('uvicorn.error').log(getattr(logging,str(level).upper(),logging.INFO),'%s: %s',operation,message)
        with self.connect() as db:
            cursor = db.execute('''INSERT INTO diagnostic_events(
                created_at,level,operation,message,worker,job_id,request_id,user_id,service,entity_type,entity_id,details)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (datetime.now(timezone.utc).isoformat(), level, str(operation), str(message)[:16000],
                 *values, json.dumps(details,ensure_ascii=False,default=str) if details else None))
            # Keep enough history for diagnosis without allowing an unbounded DB.
            if cursor.lastrowid % 500 == 0:
                db.execute('DELETE FROM diagnostic_events WHERE id < (SELECT id FROM diagnostic_events ORDER BY id DESC LIMIT 1 OFFSET 99999)')

    def logs(self, limit=100, level=None, action=None, worker=None, query=None, job_id=None, since=None):
        clauses, params = [], []
        if level:
            clauses.append('level=?'); params.append(level)
        if action:
            clauses.append('operation=?'); params.append(action)
        if worker:
            clauses.append('worker=?'); params.append(worker)
        if job_id:
            clauses.append('job_id=?'); params.append(job_id)
        if since:
            clauses.append('created_at>=?'); params.append(since)
        if query:
            clauses.append('(message LIKE ? OR operation LIKE ? OR COALESCE(service,\'\') LIKE ?)')
            needle=f'%{query}%';params.extend((needle,needle,needle))
        with self.connect() as db:
            columns=('id','created_at','level','operation','message','worker','job_id','request_id','user_id','service','entity_type','entity_id','details')
            rows = db.execute('SELECT '+','.join(columns)+' FROM diagnostic_events '
                              + ('WHERE '+' AND '.join(clauses)+' ' if clauses else '') + 'ORDER BY id DESC LIMIT ?',
                              (*params, limit)).fetchall()
        result=[]
        for row in rows:
            item=dict(zip(columns,row))
            item['details']=json.loads(item['details']) if item.get('details') else None
            result.append(item)
        return result

    def log_facets(self):
        with self.connect() as db:
            operations=[r[0] for r in db.execute("SELECT DISTINCT operation FROM diagnostic_events WHERE operation<>'' ORDER BY operation")]
            services=[r[0] for r in db.execute("SELECT DISTINCT service FROM diagnostic_events WHERE service IS NOT NULL AND service<>'' ORDER BY service")]
            workers=[r[0] for r in db.execute("SELECT DISTINCT worker FROM diagnostic_events WHERE worker IS NOT NULL AND worker<>'' ORDER BY worker")]
        return {'operations':operations,'services':services,'workers':workers}

    def clear_logs(self):
        with self.connect() as db:
            db.execute('DELETE FROM diagnostic_events')

    def clear_debug_logs(self):
        with self.connect() as db:
            db.execute("DELETE FROM diagnostic_events WHERE level='DEBUG'")
