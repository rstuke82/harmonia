"""Consistent SQLite snapshots and transactional restore of app-owned state."""
import json
import re
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from contextlib import closing
from . import __version__

TABLES=('state','health_history','migration_backups','application_logs','diagnostic_events','jobs','schedules','job_events')


def _upgrade_database(path):
    """Bring a Beta 12 database snapshot forward before compatibility checks."""
    with closing(sqlite3.connect(path)) as db:
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version==4:
            db.execute('''CREATE TABLE diagnostic_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                level TEXT NOT NULL, operation TEXT NOT NULL, message TEXT NOT NULL,
                worker TEXT, job_id TEXT, request_id TEXT, user_id TEXT, service TEXT,
                entity_type TEXT, entity_id TEXT, details TEXT)''')
            db.execute('''INSERT INTO diagnostic_events(created_at,level,operation,message)
                          SELECT created_at,level,operation,message FROM application_logs''')
            db.execute('PRAGMA user_version=5')
            version=5
        if version!=5:
            raise ValueError('Invalid user database in backup')


def directory(repo):
    path=repo.directory/'backups';path.mkdir(exist_ok=True,mode=0o700)
    return path


def path_for(repo,name):
    if Path(name).name!=name or not name.startswith('bridge-') or not name.endswith('.zip'):
        raise ValueError('Invalid backup name')
    path=directory(repo)/name
    if not path.is_file() or path.is_symlink():raise ValueError('Backup not found')
    return path


def listing(repo):
    result=[]
    for p in sorted(directory(repo).glob('bridge-*.zip'),reverse=True):
        if p.is_symlink():continue
        try:
            with ZipFile(p) as z:meta=json.loads(z.read('manifest.json'))
            result.append({**meta,'name':p.name,'bytes':p.stat().st_size})
        except Exception:continue
    return result


def create(repo,kind='manual'):
    from . import jobs
    jobs.progress('Creating a consistent database backup')
    stamp=datetime.now(timezone.utc)
    name=f"bridge-{stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
    dest=directory(repo)/name
    with tempfile.TemporaryDirectory(dir=directory(repo),prefix='.snapshot-') as tmp:
        dbpath=Path(tmp)/'playlist-bridge.db'
        with repo.connect() as source, closing(sqlite3.connect(dbpath)) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Database snapshot verification failed')
        config=repo.directory/'config.json'
        startup=config.read_bytes() if config.exists() else b'{}'
        manifest={'created_at':stamp.isoformat(),'version':__version__,'schema':4,'kind':kind}
        pending=Path(tmp)/'backup.zip'
        with ZipFile(pending,'w',ZIP_DEFLATED) as z:
            z.write(dbpath,'playlist-bridge.db');z.writestr('config.json',startup);z.writestr('manifest.json',json.dumps(manifest))
            for member in sorted((repo.directory/'users').glob('*/playlist-bridge.db')):
                if not member.parent.name.isdigit() or member.is_symlink() or member.parent.is_symlink():continue
                snapshot=Path(tmp)/('user-'+member.parent.name+'.db')
                with closing(sqlite3.connect(member)) as source,closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('User database snapshot verification failed')
                z.write(snapshot,'users/'+member.parent.name+'/playlist-bridge.db')
        pending.chmod(0o600);os.replace(pending,dest)
    # Daily retention is separate from manually requested and pre-restore copies.
    keep=min(7,int(repo.load('backup_settings').get('retention',7)))
    buckets={}
    for item in listing(repo):buckets.setdefault(item.get('kind','manual'),[]).append(item)
    for kind,items in buckets.items():
        for item in items[(keep if kind=='daily' else 5):]:path_for(repo,item['name']).unlink()
    jobs.progress('Backup saved')
    return {'backup':name,'bytes':dest.stat().st_size,'created_at':stamp.isoformat()}


def restore(repo,name):
    from . import jobs
    from .atomic_files import _atomic_write_json
    archive=path_for(repo,name)
    ctx=jobs.current()
    with tempfile.TemporaryDirectory(dir=directory(repo),prefix='.restore-') as tmp:
        restored=Path(tmp)/'restored.db'
        with ZipFile(archive) as z:
            meta=json.loads(z.read('manifest.json'))
            if meta.get('schema')!=4:raise ValueError('This backup uses an unsupported database schema')
            startup=json.loads(z.read('config.json'))
            if not isinstance(startup,dict):raise ValueError('Invalid backup configuration')
            restored.write_bytes(z.read('playlist-bridge.db'))
            members={}
            for member in z.namelist():
                match=re.fullmatch(r'users/([0-9]+)/playlist-bridge\.db',member)
                if not match:continue
                path=Path(tmp)/('user-'+match[1]+'.db');path.write_bytes(z.read(member))
                _upgrade_database(path)
                with closing(sqlite3.connect(path)) as check:
                    if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid user database in backup')
                members[match[1]]=path
        _upgrade_database(restored)
        with closing(sqlite3.connect(restored)) as check:
            if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Backup database is damaged')
            for table in TABLES:
                columns=[row[1] for row in check.execute(f'PRAGMA table_info({table})')]
                with repo.connect() as live:expected=[row[1] for row in live.execute(f'PRAGMA table_info({table})')]
                if columns!=expected:raise ValueError('Backup database is not compatible with this release')
        safety=create(repo,'before-restore')
        jobs.progress('Restoring app data; Plex playlists remain untouched')
        config_path=repo.directory/'config.json'
        original=config_path.read_bytes() if config_path.exists() else None
        try:
            with repo.connect() as db:
                db.execute('ATTACH DATABASE ? AS saved',(str(restored),))
                db.execute('BEGIN IMMEDIATE')
                # Keep the current restore job and any work queued since the snapshot.
                db.execute("CREATE TEMP TABLE keep_jobs AS SELECT * FROM jobs WHERE status IN ('queued','running','cancelling')")
                db.execute('CREATE TEMP TABLE keep_events AS SELECT * FROM job_events WHERE job_id IN (SELECT id FROM keep_jobs)')
                for table in TABLES:
                    db.execute(f'DELETE FROM {table}');db.execute(f'INSERT INTO {table} SELECT * FROM saved.{table}')
                db.execute("UPDATE jobs SET status='interrupted',finished_at=?,progress='Not replayed after backup restore',error='Backup contained unfinished work; review before running again.' WHERE status IN ('queued','running','cancelling')",(jobs.now(),))
                db.execute('DELETE FROM job_events WHERE job_id IN (SELECT id FROM keep_jobs)')
                db.execute('INSERT OR REPLACE INTO jobs SELECT * FROM keep_jobs')
                db.execute('INSERT INTO job_events(job_id,created_at,stage,message) SELECT job_id,created_at,stage,message FROM keep_events')
                # Newly queued work is cancelled because it targeted the state before restore.
                db.execute("UPDATE jobs SET status='cancelled',finished_at=?,progress='Cancelled because app data was restored' WHERE status='queued'",(jobs.now(),))
                for sid,cron,zone in db.execute('SELECT id,cron,timezone FROM schedules').fetchall():
                    db.execute('UPDATE schedules SET next_run=? WHERE id=?',(jobs.next_run(cron,zone),sid))
                db.execute("DELETE FROM state WHERE namespace IN ('sessions','login_pins')")
                _atomic_write_json(config_path,startup)
        except Exception:
            if original is not None:config_path.write_bytes(original)
            elif config_path.exists():config_path.unlink()
            raise
        # Personal databases are retained in the safety backup and restored by SQLite,
        # never by extracting arbitrary ZIP paths.
        for user_id,path in members.items():
            from .storage import get_repository
            personal=get_repository(repo.directory/'users'/user_id)
            with closing(sqlite3.connect(path)) as source,personal.connect() as target:
                source.backup(target)
                target.execute("UPDATE jobs SET status='interrupted',finished_at=?,error='Restored from backup; review before retrying' WHERE status IN ('queued','running','cancelling')",(jobs.now(),))
    from .tasks import setup
    setup(jobs.Store(repo))
    from . import api, track_routes
    with api._analysis_lock:api._analysis_cache.clear()
    with track_routes._lock:track_routes._cache.clear()
    jobs.progress('Restore completed; browser refresh will load restored app data')
    return {'restored':name,'safety_backup':safety['backup'],'plex_untouched':True}
