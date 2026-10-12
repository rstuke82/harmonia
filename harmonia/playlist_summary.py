"""Consistent current-track counts and actionable health annotations."""
from collections import Counter
from datetime import datetime, timezone


def _stamp(value):
    if not value:
        return 0
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result.replace(tzinfo=timezone.utc).timestamp() if result.tzinfo is None else result.timestamp()
    except (ValueError, TypeError):
        return 0


def summarize(payload, rows):
    payload=dict(payload)
    payload['match_counts']=dict(Counter(str(r['status']).lower() for r in rows if r.get('match') and r['status']!='Ignored'))
    payload['unresolved']=sum(r['status'] in ('Unresolved','LOST') for r in rows)
    payload['lost']=sum(r['status']=='LOST' for r in rows)
    payload['ignored']=sum(r['status']=='Ignored' for r in rows)
    health=payload.get('health') or {}
    current_health=_stamp(health.get('checked_at')) >= _stamp(payload.get('last_synced'))
    missing=Counter()
    if current_health:
        for entry in (health.get('drift_details') or {}).get('missing_from_plex',[]):
            missing[str(entry.get('plex_id'))]+=int(entry.get('count') or 1)
    for row in rows:
        row['health_issues']=[]
        ident=str(row.get('plex_id'))
        if row['status']!='Ignored' and row.get('match') and missing[ident]>0:
            row['health_issues'].append('Missing from Plex playlist at last health check')
            missing[ident]-=1
        if payload.get('sync_error') and row['status'] != 'Ignored':
            row['health_issues'].append(payload['sync_error'])
        row['needs_attention']=row['status'] in ('Unresolved','LOST','Error','Mismatch') or bool(row['health_issues'])
    payload['attention_tracks']=sum(r['needs_attention'] for r in rows)
    return payload
