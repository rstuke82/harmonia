"""One automatic sync policy per playlist; legacy flags retained only for upgrades."""
import json
from .jobs import next_run


def migrate(store):
    repo=store.repository
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM state WHERE namespace='tasks' AND key='sync_modes_v3'").fetchone():return
        old=[dict(zip(('id','action','scope','cron','timezone','enabled'),r)) for r in db.execute("SELECT id,action,scope,cron,timezone,enabled FROM schedules WHERE action IN ('sync','health')")]
        row=db.execute("SELECT value FROM state WHERE namespace='runtime' AND key='playlists'").fetchone()
        for p in json.loads(row[0]) if row else []:
            key=f"{p.get('source','')}:{p.get('source_id','')}"
            saved=db.execute("SELECT value FROM state WHERE namespace='playlist_schedules' AND key=?",(key,)).fetchone()
            value=json.loads(saved[0]) if saved else {}
            pending=db.execute("SELECT value FROM state WHERE namespace='pending_playlist_settings' AND key=?",(key,)).fetchone()
            enabled=(json.loads(pending[0]) if pending else {}).get('auto_sync',p.get('auto_sync',True))
            if value.get('mode') not in ('custom','disabled'):
                value.update(mode='inherit' if enabled else 'disabled',days=[0],hour=2,minute=0)
                db.execute("INSERT OR REPLACE INTO state VALUES('playlist_schedules',?,?)",(key,json.dumps(value)))
        notes=[]
        for action in ('sync','health'):
            target=next((s for s in old if s['action']==action and s['scope']=='all'),None)
            active=[s for s in old if s['action']==action and s['enabled']]
            chosen=target if target and target['enabled'] else next((s for s in active if action=='sync' and s['scope']=='automatic'),None)
            if target and chosen:
                db.execute('UPDATE schedules SET cron=?,timezone=?,enabled=1,next_run=? WHERE id=?',(chosen['cron'],chosen['timezone'],next_run(chosen['cron'],chosen['timezone']),target['id']))
            if active and not chosen:notes.append(f'{action.title()} had scoped schedules; the consolidated task is disabled. Choose a frequency in Tasks.')
            if len(active)>1:notes.append(f'{action.title()} schedules consolidated; retained the general schedule, or Auto Sync frequency when no general schedule was enabled.')
        db.execute("UPDATE schedules SET enabled=0 WHERE action IN ('sync','health') AND scope!='all'")
        db.execute("UPDATE schedules SET name='Sync Playlists' WHERE action='sync' AND scope='all'")
        db.execute("INSERT OR REPLACE INTO state VALUES('tasks','sync_modes_v3',?)",(json.dumps({'notes':notes,'previous_schedules':old}),))


def eligible(repo,playlists):
    modes=repo.load('playlist_schedules')
    return [p for p in playlists if modes.get(f"{p.get('source','')}:{p.get('source_id','')}",{}).get('mode','inherit')=='inherit']
