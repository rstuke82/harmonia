"""Compare destination order and occurrence counts, allowing Plex duplicate collapse."""
from collections import Counter


def compare(expected, items):
    expected = [str(value) for value in expected]
    actual = [str(item.get('plex_id')) for item in items]
    # A server may collapse repeats. Accept only a subsequence with all distinct IDs,
    # no extra occurrences, and at least one retained occurrence of every expected ID.
    remaining = iter(expected)
    ordered = all(any(candidate == value for candidate in remaining) for value in actual)
    collapsed = actual != expected and set(actual) == set(expected) and ordered and not (Counter(actual)-Counter(expected))
    missing, extra = Counter(expected)-Counter(actual), Counter(actual)-Counter(expected)
    return {'ok': actual == expected or collapsed, 'duplicates_collapsed': sum(missing.values()) if collapsed else 0,
            'missing': dict(missing), 'extra': dict(extra), 'order_matches': ordered,
            'expected_count': len(expected), 'actual_count': len(actual)}


def describe(result):
    return (f"Plex verification: expected {result['expected_count']} occurrences, retained {result['actual_count']}; "
            f"missing IDs/counts: {result['missing']}; extra IDs/counts: {result['extra']}; source order preserved: {result['order_matches']}")


def track_details(expected, items, library):
    """Explain absent IDs separately from missing repeated occurrences."""
    wanted = Counter(str(v) for v in expected)
    actual = Counter(str(t.get('plex_id')) for t in items)
    lookup = {str(t.get('plex_id')): t for t in items}
    for track in library:
        key = str(track.get('plex_id'))
        lookup[key] = {**lookup.get(key, {}), **track}
    lines = []
    for kind, counts in [('Missing', wanted-actual), ('Unexpected', actual-wanted)]:
        for key in counts:
            track = lookup.get(key, {})
            title = track.get('title') or 'Unknown title'
            artist = track.get('artist') or 'Unknown artist'
            album = track.get('album') or 'Unknown album'
            reason = 'entire track absent' if kind == 'Missing' and not actual[key] else 'occurrence difference'
            lines.append(f'{kind}: {title} — {artist} ({album}) · Plex ID {key} · expected {wanted[key]}, retained {actual[key]} · {reason}')
    return lines
