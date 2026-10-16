"""Anchored recurring tasks, including built-in maintenance jobs."""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

INTERVALS = {0: 'Disabled', 1: 'Every hour', 3: 'Every 3 hours', 6: 'Every 6 hours', 12: 'Every 12 hours', 24: 'Daily', 168: 'Weekly'}
DEFINITIONS = [
    ('file_audit','all','Scan Local Files',24,'03:00','library'),
    ('plex_scan','all','Scan Plex Library',1,'00:15','library'),
    ('lidarr_scan','all','Scan Lidarr Library',1,'00:00','library'),
    ('lastfm_refresh','all','Refresh Last.fm Discovery',6,'00:45','metadata'),
    ('library_metadata_all','all','Refresh Library Metadata',168,'03:30','metadata'),
    ('sync','all','Sync Playlists',24,'02:00','playlist'),
    ('health','all','Health Check',24,'03:00','health'),
    ('backup','all','Backup',24,'00:00','maintenance'),
    ('database_optimize','all','Optimize Database',168,'04:00','maintenance'),
    ('check_updates','all','Check for Updates',6,'00:30','maintenance'),
]
STARTUP_MODES = {'never','missing','outdated','always'}
STARTUP_DEFAULTS = {'plex_scan':'missing','lidarr_scan':'missing','lastfm_refresh':'outdated'}

RETIRED = {'availability','local_scan','reconcile','retry_missing'}


def local_scan_ready():
    try:
        from .file_tools import settings
        config=settings()
        return bool(config.get('audit_enabled') and any(m.get('local_path') and Path(m['local_path']).is_dir() for m in config.get('mappings',[])))
    except Exception:return False


def expression(hours, start_time="00:00"):
    hour,minute=map(int,start_time.split(":"))
    if not 0<=hour<24 or not 0<=minute<60:raise ValueError("Choose a valid start time")
    if hours not in INTERVALS:raise ValueError('Choose an available interval')
    if hours == 168:return f'{minute} {hour} * * 0'
    return f'{minute} {hour} * * *' if hours in (0,24) else (f'0 */{hours} * * *' if start_time=='00:00' else f"{minute} {','.join(str(h) for h in sorted({(hour+n)%24 for n in range(0,24,hours)}))} * * *")


def hours_for(cron):
    parts=cron.split()
    if len(parts)==5 and parts[4]!='*':return 168
    for hours in (1,3,6,12,24,168):
        if cron == expression(hours):return hours
    if len(parts)==5 and parts[1]=='*':return 1
    if len(parts)==5 and parts[1].startswith('*/'):
        try:
            n=int(parts[1][2:])
            if n in INTERVALS:return n
        except ValueError:pass
    if len(parts)==5 and ',' in parts[1]:
        count=len(parts[1].split(','))
        if count and 24//count in INTERVALS:return 24//count
    return 24


def setup(store):
    from .jobs import next_run
    with store.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # Retry Missing Matches used to be dispatched after Plex inventory scans.
        # Retire any copies left queued or running by an older build so an upgrade
        # cannot make a disabled task appear to launch itself again.
        stopped=datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE jobs SET cancel_requested=1,status='cancelled',progress='Retired automatic retry task',finished_at=? WHERE action='retry_missing' AND status='queued'",(stopped,))
        db.execute("UPDATE jobs SET cancel_requested=1,status='cancelling',progress='Retired automatic retry task — stopping at the next checkpoint' WHERE action='retry_missing' AND status IN ('running','cancelling')")
        db.execute("UPDATE schedules SET enabled=0 WHERE action='local_scan'")
        db.execute("UPDATE jobs SET status='cancelled',progress='Local catalog retired; use the Lidarr Library',finished_at=datetime('now') WHERE action IN ('local_scan','local_catalog','artist_metadata') AND status='queued'")
        db.execute("UPDATE schedules SET enabled=0,name='Previous availability task (replaced)' WHERE action='availability'")
        initialized=db.execute("SELECT value FROM state WHERE namespace='tasks' AND key='initialized'").fetchone()
        if initialized:
            saved=json.loads(initialized[0]); zone=os.environ.get('TZ')
            if zone and zone!=saved.get('timezone'):
                try:ZoneInfo(zone)
                except Exception:return
                for sid,cron in db.execute('SELECT id,cron FROM schedules').fetchall():
                    db.execute('UPDATE schedules SET timezone=?,next_run=? WHERE id=?',(zone,next_run(cron,zone),sid))
                saved['timezone']=zone
                db.execute("UPDATE state SET value=? WHERE namespace='tasks' AND key='initialized'",(json.dumps(saved),))
            zone=saved.get('timezone','UTC')
            db.execute("UPDATE schedules SET enabled=0 WHERE action IN ('reconcile','retry_missing')")
            for action,scope,name,interval,start_time,worker in DEFINITIONS:
                db.execute('INSERT INTO schedules(id,name,action,scope,cron,timezone,enabled,next_run) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(action,scope) DO NOTHING',
                    (str(uuid.uuid4()),name,action,scope,expression(interval,start_time),zone,int(interval>0 and (action!='file_audit' or local_scan_ready())),next_run(expression(interval,start_time),zone)))
                db.execute('UPDATE schedules SET name=? WHERE action=? AND scope=?',(name,action,scope))
            return
        existing=db.execute('SELECT id,name,action,scope,cron,timezone,enabled,next_run FROM schedules').fetchall()
        zone=os.environ.get('TZ') or (existing[0][5] if existing else 'UTC')
        try:ZoneInfo(zone)
        except Exception:zone='UTC'
        # Preserve the original definitions for upgrade diagnostics, without deleting job history.
        db.execute("INSERT OR REPLACE INTO state VALUES('tasks','previous_schedules',?)",(json.dumps(existing),))
        for sid,name,action,scope,cron,old_zone,enabled,upcoming in existing:
            aligned=expression(hours_for(cron))
            db.execute('UPDATE schedules SET cron=?,timezone=?,next_run=? WHERE id=?',(aligned,zone,next_run(aligned,zone),sid))
        for action,scope,name,interval,start_time,worker in DEFINITIONS:
            db.execute('INSERT INTO schedules(id,name,action,scope,cron,timezone,enabled,next_run) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(action,scope) DO UPDATE SET name=excluded.name',
                (str(uuid.uuid4()),name,action,scope,expression(interval,start_time),zone,int(interval>0 and (action!='file_audit' or local_scan_ready())),next_run(expression(interval,start_time),zone)))
        db.execute("UPDATE schedules SET enabled=0 WHERE action IN ('reconcile','retry_missing')")
        db.execute("INSERT OR REPLACE INTO state VALUES('tasks','initialized',?)",(json.dumps({'timezone':zone,'migrated':bool(existing)}),))


def payload(task):
    return {'scope':task['scope']} if task['action'] in ('sync','health') else {}


def rows(store):
    result=[]
    for task in store.schedules():
        if task['action'] in RETIRED or (task['action'] in ('sync','health') and task['scope']!='all'):continue
        # Run Now is deliberately a manual job and therefore has no schedule_id.
        # Visible recurring tasks are consolidated to one row per action, so the
        # action is the stable relationship for both manual and scheduled runs.
        predicate="action=?"
        params=(task['action'],)
        def latest(extra=''):
            found=store._rows('SELECT * FROM jobs WHERE '+predicate+extra+' ORDER BY created_at DESC LIMIT 1',params)
            return found[0] if found else None
        completed=store._rows("SELECT * FROM jobs WHERE action=? AND status='completed' AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",params)
        last=completed[0] if completed else None
        active=latest(" AND status IN ('queued','running','cancelling')")
        duration=None
        if last and last['started_at']:duration=max(0,round((datetime.fromisoformat(last['finished_at'])-datetime.fromisoformat(last['started_at'])).total_seconds(),1))
        definition=next((d for d in DEFINITIONS if d[0]==task['action']),None)
        start_default=definition[4] if definition else '00:00'
        result.append({**task,'hours':hours_for(task['cron']) if task['enabled'] else 0,
            'startup':store.repository.load('task_startup').get(task['action'],STARTUP_DEFAULTS.get(task['action'],'never')),
            'start_time':store.repository.load('task_times').get(task['action'],start_default),'worker':definition[5] if definition else 'maintenance','fixed':task['action']=='check_updates','configured':task['action']!='file_audit' or local_scan_ready(),'last_job':last,'latest_event':latest(),
            'active_job':active,'active_origin':('startup' if active and active.get('payload',{}).get('trigger_label')=='At startup' else 'scheduled' if active and active.get('schedule_id') else 'manual') if active else None,
            'duration':duration,'next_run':task['next_run'] if task['enabled'] else None})
    from .api import _config
    from .sync_policy import eligible
    playlists=_config(read_only=True,namespaces=[]).config.get('playlists',[])
    for task in result:
        if task['action'] in ('sync','health'):
            count=len(eligible(store.repository,playlists)) if task['action']=='sync' else len(playlists)
            task.update(playlist_count=count,excluded_count=len(playlists)-count)
    from .updates import enabled as updates_enabled
    if not updates_enabled(store.repository):
        for task in result:
            if task['action'] == 'check_updates':
                task.update(enabled=False, next_run=None, configured=False,
                            disabled_reason='Update checks are disabled in About')
    return result


def restore_defaults(store, action=None):
    definitions=[d for d in DEFINITIONS if not action or d[0]==action]
    if action and not definitions:raise ValueError('Unknown task')
    existing={(s['action'],s['scope']):s for s in store.schedules()}
    for task,scope,name,hours,start_time,worker in definitions:
        current=existing.get((task,scope))
        selected=hours if task!='file_audit' or local_scan_ready() else 0
        store.save_schedule({'action':task,'scope':scope,'hours':selected,'start_time':start_time,'startup':STARTUP_DEFAULTS.get(task,'never')},current['id'] if current else None)
    if not action:
        with store.repository.connect() as db:db.execute("INSERT OR REPLACE INTO state VALUES('task_startup','page_views',?)",(json.dumps('outdated'),))
    return rows(store)


def startup_needed(mode, checked_at, hours, timestamp):
    if mode=='never':return False
    if mode=='always':return True
    if not checked_at:return True
    return mode=='outdated' and timestamp-float(checked_at)>=hours*3600


def health_checked_at(value):
    """Only saved completed comparisons count, never attempts or active jobs."""
    checked=value.get('checked_at') if isinstance(value,dict) else None
    if not checked:return None
    try:
        parsed=datetime.fromisoformat(str(checked))
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError,TypeError,OverflowError):return None


def health_startup_keys(repo,playlists,mode,hours,timestamp):
    from .api import _playlist_key
    saved=repo.load('health')
    return [_playlist_key(p) for p in playlists
            if startup_needed(mode,health_checked_at(saved.get(_playlist_key(p))),hours,timestamp)]


def startup_check(store, action):
    """Inspect saved evidence only; startup never contacts external services."""
    from .accounts import root_repository
    from .storage import read_json
    from .lidarr import config as lidarr_config
    repo=store.repository
    if action in ('plex_scan','lidarr_scan'):
        if action=='plex_scan':
            from .inventory import identity
            from types import SimpleNamespace
            cfg=read_json(root_repository().directory/'config.json').get('plex',{})
            if not all(cfg.get(k) for k in ('url','token','music_library_key')):return False,None
            key=identity(SimpleNamespace(base_url=cfg['url'].rstrip('/'),music_library_key=str(cfg['music_library_key']).strip(),headers={'X-Plex-Token':cfg['token'],'Accept':'application/json'}))
            service='plex';source=repo
        else:
            from .lidarr_requests import server_id
            from .availability import preferences
            cfg=lidarr_config(root_repository())
            if not cfg.get('enabled') or not cfg.get('url') or not cfg.get('api_key') or not preferences(root_repository()).refresh_lidarr:return False,None
            key=server_id(cfg);service='lidarr';source=root_repository()
        snap=source.load_one('inventory',service,{})
        return True,snap.get('checked_at') if snap.get('identity')==key else None
    if action=='lastfm_refresh':
        from .user_preferences import preferences
        username=preferences().get('lastfm_username','').strip().casefold()
        if not username:return False,None
        return True,root_repository().load_one('lastfm_feed_refresh',username,{}).get('updated_at')
    if action=='file_audit' and not local_scan_ready():return False,None
    if action in ('sync','health'):
        from .accounts import actor
        user=actor() or {}
        if action=='sync' and not user.get('admin') and user.get('can_playlists',True) is False:return False,None
        from .api import _config
        from .sync_policy import eligible
        playlists=_config(read_only=True,namespaces=[]).config.get('playlists',[])
        if action=='sync':playlists=eligible(repo,playlists)
        if not playlists:return False,None
        if action=='health':
            from .api import _playlist_key
            saved=repo.load('health')
            checked=[health_checked_at(saved.get(_playlist_key(p))) for p in playlists]
            return True,min(checked) if all(value is not None for value in checked) else None
    found=store._rows("SELECT * FROM jobs WHERE action=? AND status='completed' ORDER BY finished_at DESC LIMIT 1",(action,))
    return True,datetime.fromisoformat(found[0]['finished_at']).timestamp() if found else None


def startup(store):
    """One dispatch per scheduler owner; existing queued/running work wins."""
    import time
    from .accounts import as_user,member_stores,users,is_owner
    policies=store.repository.load('task_startup');timestamp=time.time()
    owner=next((u for u in users().values() if is_owner(u) and not u.get('disabled')),None)
    contexts=[(owner,store),*list(member_stores())]
    queued=[]
    for user,target in contexts:
        with as_user(user):
            for task in store.schedules():
                action=task['action']
                if action not in {d[0] for d in DEFINITIONS}:continue
                if action == 'check_updates':
                    from .updates import enabled
                    if not enabled(store.repository):continue
                if target is not store and action not in ('lastfm_refresh','sync','health'):continue
                mode=policies.get(action,STARTUP_DEFAULTS.get(action,'never'))
                if mode=='never':continue
                if target._rows("SELECT id FROM jobs WHERE action=? AND status IN ('queued','running','cancelling') LIMIT 1",(action,)):continue
                try:
                    configured,checked=startup_check(target,action)
                    definition=next(d for d in DEFINITIONS if d[0]==action)
                    hours=hours_for(task['cron']) if task['enabled'] else definition[3]
                    if not configured or not startup_needed(mode,checked,hours,timestamp):continue
                    data=payload(task)|{'trigger_label':'At startup'}
                    if action in ('sync','health'):
                        from .api import _config,_playlist_key
                        from .sync_policy import eligible
                        playlists=_config(read_only=True,namespaces=[]).config.get('playlists',[])
                        if action=='sync':playlists=eligible(target.repository,playlists)
                        keys=health_startup_keys(target.repository,playlists,mode,hours,timestamp) if action=='health' else [_playlist_key(p) for p in playlists]
                        if not keys:continue
                        data.update(scope='selected',playlist_keys=keys)
                    queued.append(target.enqueue(action,data))
                except Exception as exc:
                    target.repository.add_log('WARNING','Startup task',f'{action}: {type(exc).__name__}')
            if policies.get('page_views','outdated')!='never':
                from .saved_views import startup_pages
                try:startup_pages(target.repository,policies.get('page_views','outdated'))
                except Exception as exc:target.repository.add_log('WARNING','Startup pages',type(exc).__name__)
    return queued
