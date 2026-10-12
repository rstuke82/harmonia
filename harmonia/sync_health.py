"""Persist health from the work already performed by a sync."""
from collections import Counter


def persist_sync_health(config, playlist, tracks, matched, unmatched, stats, actual):
    key = f"{playlist['source']}:{playlist['source_id']}"
    expected = Counter(str(t) for t in matched)
    observed = Counter(str(t.get('plex_id')) for t in actual)
    from .verification import compare
    verification = compare(matched, actual)
    effective = observed if verification['duplicates_collapsed'] else expected
    missing, extra = effective - observed, observed - effective
    result = {
        'key': key, 'name': playlist.get('plex_playlist_name', ''), 'source': playlist['source'],
        'source_tracks': len(tracks), 'plex_playlist_tracks': len(actual),
        'matched_in_library': len(matched), 'unresolved': len(unmatched),
        'ignored': len(stats.get('ignored_tracks', [])),
        'lost': sum(1 for track in unmatched if track.get('status') == 'lost'),
        'missing_from_plex_playlist': sum(missing.values()), 'extra_in_plex_playlist': sum(extra.values()),
        'source_added_since_last_sync': 0, 'source_removed_since_last_sync': 0,
        'healthy': not unmatched and not missing and not extra and verification['ok'],
        'duplicates_collapsed': verification['duplicates_collapsed'],
        'verification': verification,
        'read_only': False, 'origin': 'sync',
        'source_preview': [{k:t.get(k, '') for k in ('title','artist','album','source_id')} for t in tracks],
        'drift_details': {'unresolved': unmatched,
            'missing_from_plex': [{'plex_id':k,'title':k,'count':v} for k,v in missing.items()],
            'extra_in_plex': [{'plex_id':k,'title':k,'count':v} for k,v in extra.items()],
            'source_added': [], 'source_removed': []},
    }
    config.repository.save_health(key, result)
    config.repository.record_health_attempt(key)
    return result
